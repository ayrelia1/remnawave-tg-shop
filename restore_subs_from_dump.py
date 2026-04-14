"""Restore subscription end dates on the Remnawave panel from a SQL dump.

Reads PANEL_API_URL / PANEL_API_KEY from .env, parses the
`COPY public.subscriptions ...` block in the dump, and for each row calls
PATCH /users with {uuid, expireAt} so the panel matches the dump's end_date.

Users listed in SKIP_TELEGRAM_IDS are left untouched (they received
promo/payments AFTER the backup was taken and must keep their newer state).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import dotenv_values


DUMP_PATH = Path(r"E:\python\remnawave-tg-shop\mansurvpn_20260414_090000.sql")
ENV_PATH = Path(r"E:\python\remnawave-tg-shop\.env")

# Telegram user IDs that must NOT be touched — they got promo/payments
# AFTER the 09:00 backup, so the panel already holds the correct (newer) data.
SKIP_TELEGRAM_IDS: set[int] = {
    357563882,  # yuriyovich1998 — promo 6TEHJ7FZ (+9d) at 09:59
    8404071302,  # alunka33 — platega 1mo at 10:34
    1114474069,  # Qushke — platega 1mo at 10:35
    7716518922,  # platega 1mo at 10:39
    6371647682,  # vinty3 — platega 1mo at 10:58
}

CONCURRENCY = 5
REQUEST_TIMEOUT = 30


@dataclass
class SubRow:
    user_id: int
    panel_user_uuid: str
    end_date: datetime


def parse_pg_timestamp(raw: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS[.ffffff]+00' into an aware UTC datetime."""
    s = raw.strip()
    # Normalize '+00' → '+00:00' so fromisoformat accepts it.
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
    s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_for_panel(dt: datetime) -> str:
    """Panel expects ISO-8601 with millisecond precision and 'Z' suffix."""
    ms = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"
    return ms


def parse_subscriptions(dump_path: Path) -> list[SubRow]:
    rows: list[SubRow] = []
    inside = False
    header_re = re.compile(
        r"^COPY public\.subscriptions \(([^)]+)\) FROM stdin;"
    )
    columns: list[str] = []

    with dump_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not inside:
                m = header_re.match(line)
                if m:
                    columns = [c.strip() for c in m.group(1).split(",")]
                    inside = True
                continue

            if line.startswith("\\."):
                break

            fields = line.rstrip("\n").split("\t")
            if len(fields) != len(columns):
                logging.warning("Skipping malformed row: %r", line[:120])
                continue

            record = dict(zip(columns, fields))
            uuid = record.get("panel_user_uuid") or ""
            user_id_raw = record.get("user_id") or ""
            end_date_raw = record.get("end_date") or ""
            if not uuid or uuid == r"\N" or not end_date_raw or end_date_raw == r"\N":
                continue
            try:
                rows.append(
                    SubRow(
                        user_id=int(user_id_raw),
                        panel_user_uuid=uuid,
                        end_date=parse_pg_timestamp(end_date_raw),
                    )
                )
            except Exception as exc:
                logging.warning("Bad row (%s): %s", exc, line[:120])

    return rows


def effective_expire_at(end_date: datetime) -> datetime:
    """Panel rejects past dates — clamp to now+12h when the dump is stale."""
    floor = datetime.now(timezone.utc) + timedelta(hours=1)
    return end_date if end_date > floor else floor


async def patch_user(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    sub: SubRow,
    sem: asyncio.Semaphore,
) -> tuple[SubRow, bool, str]:
    payload = {
        "uuid": sub.panel_user_uuid,
        "expireAt": iso_for_panel(effective_expire_at(sub.end_date)),
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
    }
    url = f"{base_url.rstrip('/')}/users"
    async with sem:
        try:
            async with session.patch(url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if 200 <= resp.status < 300:
                    return sub, True, f"{resp.status}"
                return sub, False, f"{resp.status}: {text[:200]}"
        except Exception as exc:
            return sub, False, f"exception: {exc}"


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    env = dotenv_values(ENV_PATH)
    base_url = (env.get("PANEL_API_URL") or "").strip().strip('"').strip("'")
    api_key = (env.get("PANEL_API_KEY") or "").strip().strip('"').strip("'")
    if not base_url or not api_key:
        logging.error("PANEL_API_URL / PANEL_API_KEY missing in %s", ENV_PATH)
        return 2

    subs = parse_subscriptions(DUMP_PATH)
    logging.info("Parsed %d subscription rows from dump", len(subs))

    to_apply = [s for s in subs if s.user_id not in SKIP_TELEGRAM_IDS]
    skipped = [s for s in subs if s.user_id in SKIP_TELEGRAM_IDS]
    logging.info(
        "Will patch %d users; skipping %d protected users",
        len(to_apply),
        len(skipped),
    )
    for s in skipped:
        logging.info(
            "  SKIP tg=%s uuid=%s dump_end=%s",
            s.user_id,
            s.panel_user_uuid,
            s.end_date.isoformat(),
        )

    if not to_apply:
        logging.info("Nothing to do.")
        return 0

    # Dry-run gate
    if "--apply" not in sys.argv:
        logging.warning(
            "Dry run (no --apply flag). Full list of %d intended updates:",
            len(to_apply),
        )
        for s in sorted(to_apply, key=lambda x: x.user_id):
            eff = effective_expire_at(s.end_date)
            clamped = " (CLAMPED to now+12h)" if eff != s.end_date else ""
            logging.warning(
                "  tg=%s uuid=%s expireAt=%s%s",
                s.user_id,
                s.panel_user_uuid,
                iso_for_panel(eff),
                clamped,
            )
        logging.warning("Re-run with --apply to actually push changes to the panel.")
        return 0

    sem = asyncio.Semaphore(CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    ok = 0
    failed: list[tuple[SubRow, str]] = []

    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            patch_user(session, base_url, api_key, s, sem) for s in to_apply
        ]
        for coro in asyncio.as_completed(tasks):
            sub, success, info = await coro
            if success:
                ok += 1
                logging.info(
                    "OK tg=%s uuid=%s -> %s",
                    sub.user_id,
                    sub.panel_user_uuid,
                    iso_for_panel(effective_expire_at(sub.end_date)),
                )
            else:
                failed.append((sub, info))
                logging.error(
                    "FAIL tg=%s uuid=%s: %s",
                    sub.user_id,
                    sub.panel_user_uuid,
                    info,
                )

    logging.info("Done. ok=%d failed=%d skipped=%d", ok, len(failed), len(skipped))
    if failed:
        logging.error("Failed users:")
        for sub, info in failed:
            logging.error("  tg=%s uuid=%s -> %s", sub.user_id, sub.panel_user_uuid, info)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
