"""Offline arithmetic checks, not a PostgreSQL integration test. No dependencies."""

from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AS_OF = "2026-10-20 09:00:00"
FILES = sorted(ROOT.glob("0[1-6]_*.sql"))


def split_sql_list(source: str) -> list[str]:
    """Split an expression list, respecting quoted literals and parentheses."""
    result, current, depth, quote = [], [], 0, None
    i = 0
    while i < len(source):
        char = source[i]
        if quote:
            current.append(char)
            if char == quote:
                if i + 1 < len(source) and source[i + 1] == quote:
                    current.append(source[i + 1])
                    i += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    result.append("".join(current).strip())
    return result


def adapt(source: str) -> str:
    """Explicitly adapt PG-only syntax while retaining report calculations."""
    sql = re.sub(r"--[^\n]*", "", source).strip()
    if "CROSS JOIN LATERAL (VALUES" in sql:
        marker = "\nSELECT v."
        prefix, final = sql.split(marker, 1)
        rows = final.split("CROSS JOIN LATERAL (VALUES", 1)[1].split(") AS v(", 1)[0]
        overview = 'metric AS "' in final
        base = "totals t" if overview else "cfg c"
        value_expr = "ROUND({value}, 2)" if overview else "{value}"
        selects = []
        for row in split_sql_list(rows):
            order, label, value = split_sql_list(row.strip()[1:-1])
            selects.append(
                f"SELECT {order} AS sort_order, {label} AS metric, "
                + value_expr.format(value=value)
                + f" AS value FROM {base}"
            )
        sql = prefix + "\n" + "\nUNION ALL\n".join(selects) + "\nORDER BY sort_order;"
    sql = sql.replace("CURRENT_TIMESTAMP", "'" + AS_OF + "'")
    sql = re.sub(
        r"(\b(?:\w+\.)?\w+|'[^']*') AT TIME ZONE 'Europe/Moscow'",
        r"moscow_time(\1)", sql,
    )
    sql = re.sub(r"(\b(?:\w+\.)?\w+) \+ h\.observation_window",
                 r"plus_hours(\1, h.observation_window)", sql)
    sql = re.sub(r"INTERVAL '(\d+) hours'", r"\1", sql)
    sql = re.sub(
        r"horizons AS \(.*?\n\), paid AS",
        "horizons AS (SELECT 30 AS days, 720 AS observation_window "
        "UNION ALL SELECT 90, 2160 UNION ALL SELECT 180, 4320 "
        "UNION ALL SELECT 400, 9600\n), paid AS", sql, flags=re.S,
    )
    sql = re.sub(
        r"months AS \(.*?\n\), monthly_money AS",
        "months AS (SELECT cal.month FROM calendar cal CROSS JOIN bounds b "
        "CROSS JOIN cfg c WHERE cal.month >= b.first_month "
        "AND cal.month <= c.current_month\n), monthly_money AS", sql, flags=re.S,
    )
    sql = re.sub(
        r"\(PERCENTILE_CONT\(0\.5\) WITHIN GROUP \(ORDER BY revenue\)\s*"
        r"FILTER \(WHERE purchases > 0\)\)::numeric",
        "median(revenue) FILTER (WHERE purchases > 0)", sql,
    )
    sql = re.sub(r"\b((?:\w+\.)?\w+)::numeric", r"CAST(\1 AS REAL)", sql)
    sql = sql.replace("::numeric", "")
    sql = re.sub(r"\b((?:\w+\.)?\w+)::date", r"substr(\1, 1, 10)", sql)
    sql = re.sub(r"\b((?:\w+\.)?\w+)::text", r"CAST(\1 AS TEXT)", sql)
    sql = sql.replace("NULL::timestamp", "NULL")
    sql = sql.replace("TO_CHAR(period_month, 'YYYY-MM')", "substr(period_month, 1, 7)")
    return sql


class Median:
    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None:
            self.values.append(value)

    def finalize(self):
        return statistics.median(self.values) if self.values else None


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.create_function("moscow_time", 1, lambda x: None if x is None else (
        datetime.fromisoformat(x) + timedelta(hours=3)).isoformat(" "))
    db.create_function("date_trunc", 2, lambda unit, x: None if x is None else
        datetime.fromisoformat(x).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(" "))
    db.create_function("plus_hours", 2, lambda x, hours: None if x is None else (
        datetime.fromisoformat(x) + timedelta(hours=hours)).isoformat(" "))
    db.create_aggregate("median", 1, Median)
    db.executescript("""
        CREATE TABLE users(user_id INTEGER PRIMARY KEY, registration_date TEXT, is_banned BOOLEAN);
        CREATE TABLE payments(payment_id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL,
            base_amount REAL, currency TEXT, fx_rate REAL, status TEXT, created_at TEXT,
            subscription_duration_months INTEGER, hwid_device_limit INTEGER);
        CREATE TABLE subscriptions(subscription_id INTEGER PRIMARY KEY, user_id INTEGER,
            is_active BOOLEAN, end_date TEXT, provider TEXT, duration_months INTEGER,
            hwid_device_limit INTEGER);
        CREATE TABLE calendar(month TEXT);
    """)
    for year in (2025, 2026):
        db.executemany("INSERT INTO calendar VALUES (?)", [
            (f"{year}-{month:02d}-01 00:00:00",) for month in range(1, 13)
        ])
    return db


def fixture(db: sqlite3.Connection) -> None:
    registrations = {
        1: "2026-01-05", 2: "2026-01-10", 3: "2026-01-15", 4: "2026-09-15",
        5: "2026-09-20", 6: "2026-10-10", 7: "2026-10-05", 8: "2026-10-06",
        9: "2025-01-01", 10: "2026-10-09",
    }
    db.executemany("INSERT INTO users VALUES (?, ?, ?)", [
        (uid, day + " 00:00:00", uid == 2) for uid, day in registrations.items()
    ])
    purchases = [
        (1, 1, 1200, 1200, "2026-01-06 00:00:00", 12, 5),
        (2, 1, 300, 300, "2026-10-02 00:00:00", 3, 5),
        (3, 2, 200, 200, "2026-01-11 00:00:00", 1, 5),
        (4, 2, 200, 200, "2026-02-11 00:00:00", 1, 5),
        (5, 3, 600, 600, "2026-01-16 00:00:00", 6, 5),
        (6, 4, 120, 120, "2026-09-30 22:30:00", 1, 10),
        (7, 7, 10, None, "2026-10-06 00:00:00", 3, 5),
        (8, 8, 300, 300, "2026-10-07 00:00:00", 3, 10),
        (9, 9, 100, 100, "2025-01-02 00:00:00", 1, None),
        (10, 9, 100, 100, "2025-02-10 00:00:00", 1, None),
        (11, 10, 0, 0, "2026-10-10 00:00:00", 0, None),
        (12, 1, 9999, 9999, "2026-10-21 00:00:00", 12, 5),
    ]
    db.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (pid, uid, amount, base, "XTR" if uid in (7, 8) else "RUB",
         None if base is None else 1, "succeeded", date, months, devices)
        for pid, uid, amount, base, date, months, devices in purchases
    ])
    db.execute("INSERT INTO payments VALUES (13, 6, 555, 555, 'RUB', 1, 'pending', '2026-10-15 00:00:00', 1, 5)")
    db.executemany("INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (1, 1, True, "2027-04-01 00:00:00", "card", 3, 5),
        (2, 1, True, "2026-11-01 00:00:00", None, 0, 5),
        (3, 2, True, "2026-11-01 00:00:00", "card", 1, 5),
        (4, 3, False, "2026-07-16 00:00:00", "card", 6, 5),
        (5, 4, True, "2026-11-01 00:00:00", "card", 1, 10),
        (6, 5, True, "2026-11-01 00:00:00", None, 0, 5),
        (7, 7, True, "2026-11-01 00:00:00", "stars", 3, 5),
        (8, 8, False, "2026-10-08 00:00:00", "stars", 3, 10),
        (9, 9, False, "2025-03-10 00:00:00", "card", 1, None),
    ])


def run(db: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    return {path.name: list(db.execute(adapt(path.read_text(encoding="utf-8")))) for path in FILES}


def main() -> None:
    assert len(FILES) == 6
    for path in FILES:
        source = path.read_text(encoding="utf-8")
        assert not any(line.rstrip() != line for line in source.splitlines()), path
        body = re.sub(r"--[^\n]*", "", source).strip()
        assert len([x for x in body.split(";") if x.strip()]) == 1, path
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|COPY|CALL)\b", body, re.I), path
        assert not re.search(r"\n\s*\n", source), path
        aliases = re.findall(r'AS "([^"]+)"', source)
        assert all(len(alias.encode("utf-8")) <= 63 for alias in aliases), (path, aliases)
        assert len(aliases) == len(set(aliases)), path
    bundle = (ROOT / 'all_reports.sql').read_text(encoding='utf-8')
    assert all(path.read_text(encoding='utf-8').rstrip() in bundle for path in FILES)
    bundle_body = re.sub(r'--[^\n]*', '', bundle)
    assert len([part for part in bundle_body.split(';') if part.strip()]) == 6
    assert not re.search(r'\n\s*\n', bundle)
    with connect() as db:
        fixture(db)
        reports = run(db)
        overview = {row["metric"]: row["value"] for row in reports[FILES[0].name]}
        assert overview['Средний расход активного платящего в месяц, ₽'] == 140
        assert overview['Месячный эквивалент всех активных, оценка, ₽'] == 420
        assert overview['Активных платящих пользователей'] == 4
        assert overview['Активных в расчёте месячного расхода'] == 3
        assert overview['Выручка за всё время по учтённым оплатам, ₽'] == 3120
        assert overview['Выручка за всё время на зарегистрированного, ₽'] == 312
        assert overview['Накопленный LTV всех плативших, ₽'] == 445.71
        assert overview['Медианный LTV всех плативших, ₽'] == 300
        assert overview['Накопленный LTV нынешних активных платящих, ₽'] == 505
        assert overview['Платежей без валютной оценки'] == 1
        monthly = {row['Месяц']: row for row in reports[FILES[1].name]}
        assert len(monthly) == 22
        assert monthly['2026-03-01']['Выручка, ₽'] == 0
        assert monthly['2026-09-01']['Выручка, ₽'] == 0
        october = monthly['2026-10-01']
        assert october['Выручка, ₽'] == 720
        assert october['Впервые плативших'] == 3
        assert october['Вернувшихся платящих'] == 1
        assert october['Плативших в месяце'] == 4
        assert october['Период'] == 'Неполный' and october['Рост выручки, %'] is None
        assert october['На платившего, ₽'] == 180
        assert october['Мес. цена покупок, ₽'] == 106.67
        assert october['Зарегистрировано к концу'] == 10
        assert sum(row['Выручка, ₽'] for row in monthly.values()) == 3120
        registrations = reports[FILES[2].name]
        jan30 = next(r for r in registrations if r['Месяц регистрации'] == '2026-01-01' and r['Окно, дней'] == 30)
        assert jan30['Пользователей в группе'] == 3 and jan30['С полным окном'] == 3
        assert jan30['Конверсия за окно, %'] == 100 and jan30['На юзера за окно, ₽'] == 666.67
        jan400 = next(r for r in registrations if r['Месяц регистрации'] == '2026-01-01' and r['Окно, дней'] == 400)
        assert jan400['С полным окном'] == 0 and jan400['На юзера за окно, ₽'] is None
        sep30 = next(r for r in registrations if r['Месяц регистрации'] == '2026-09-01' and r['Окно, дней'] == 30)
        assert sep30['Конверсия за окно, %'] == 50 and sep30['На юзера за окно, ₽'] == 60
        payers = reports[FILES[3].name]
        jan1_90 = next(r for r in payers if r['Месяц первой оплаты'] == '2026-01-01' and r['Первый срок, мес.'] == 1 and r['Окно, дней'] == 90)
        assert jan1_90['LTV за окно, ₽'] == 400 and jan1_90['Повтор за окно, %'] == 100
        jan12_30 = next(r for r in payers if r['Месяц первой оплаты'] == '2026-01-01' and r['Первый срок, мес.'] == 12 and r['Окно, дней'] == 30)
        assert jan12_30['LTV за окно, ₽'] == 1200 and jan12_30['Повтор за окно, %'] == 0
        old400 = next(r for r in payers if r['Месяц первой оплаты'] == '2025-01-01' and r['Окно, дней'] == 400)
        assert old400['С полным окном'] == 1 and old400['LTV за окно, ₽'] == 200
        new30 = next(r for r in payers if r['Месяц первой оплаты'] == '2026-10-01' and r['Первый срок, мес.'] == 1 and r['Окно, дней'] == 30)
        assert new30['С полным окном'] == 0 and new30['LTV за окно, ₽'] is None
        tariffs = reports[FILES[4].name]
        assert sum(r['Выручка, ₽'] for r in tariffs if r['Период'] == 'За всё время') == 3120
        annual = next(r for r in tariffs if r['Период'] == 'За всё время' and r['Срок, мес.'] == 12)
        assert annual['Цена месяца, ₽'] == 100
        quality = {r['metric']: r['value'] for r in reports[FILES[5].name]}
        assert quality['Успешных платежей без base_amount'] == 1
        assert quality['Успешных платежей с будущей датой'] == 1
        assert quality['Не-RUB платежей с курсом 1:1: проверить миграцию'] == 1
        assert quality['Пользователей с несколькими активными подписками'] == 1
        assert quality['Заблокированных с действующей подпиской'] == 1
    with connect() as db:
        empty = run(db)
        assert len(empty[FILES[0].name]) == 17
        assert len(empty[FILES[1].name]) == 1
        assert empty[FILES[1].name][0]['Выручка, ₽'] == 0
        assert not empty[FILES[2].name] and not empty[FILES[3].name] and not empty[FILES[4].name]
    print('PASS: six reports; read-only/format/alias checks; synthetic arithmetic and empty database.')
    print('Covered annual prepayment, repeats, inactive payer, trial, duplicate subscription,')
    print('missing FX, legacy FX flag, future date, Moscow month boundary, zero-sale months,')
    print('mature/immature cohorts and monthly reconciliation. PostgreSQL was not executed.')


if __name__ == '__main__':
    main()
