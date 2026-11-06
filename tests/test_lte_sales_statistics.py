"""LTE package sales are a breakdown of revenue, not another revenue source."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.handlers.admin import statistics
from bot.middlewares.i18n import JsonI18n
from config.settings import Settings
from db.dal import payment_dal
from db.models import Base, LteGrant, Payment, User

GIB = 2**30
NOW = datetime(2026, 11, 6, 12)
TODAY = NOW.replace(hour=0)


class FixedDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return NOW


@pytest.fixture
async def session(monkeypatch):
    monkeypatch.setattr(payment_dal, "datetime", FixedDatetime, raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(User(user_id=1))
        await session.flush()
        yield session
    await engine.dispose()


async def add_sales(session):
    rows = [
        (NOW, 25, 100, "RUB", 100),
        (TODAY, 10, 2, "USDT", 200),
        (TODAY - timedelta(seconds=1), 0.5, 5, "XTR", 10),
        (TODAY - timedelta(days=7), 5, 100, "RUB", 100),
        (TODAY - timedelta(days=7, seconds=1), 7, 70, "RUB", 70),
        (TODAY - timedelta(days=30), 8, 80, "RUB", 80),
        (TODAY - timedelta(days=30, seconds=1), 9, 90, "RUB", 90),
        (NOW, 3, 3, "UNKNOWN", None),
    ]
    for created_at, gb, amount, currency, base_amount in rows:
        session.add(Payment(user_id=1, amount=amount, currency=currency,
                            base_amount=base_amount, status="succeeded", created_at=created_at,
                            product_type="lte_topup", subscription_duration_months=0,
                            lte_traffic_bytes=int(gb * GIB)))
    session.add(Payment(user_id=1, amount=1000, currency="RUB", base_amount=1000,
                        status="succeeded", created_at=NOW, product_type="subscription",
                        subscription_duration_months=3, lte_traffic_bytes=150 * GIB))
    for status in ("pending", "pending_yookassa", "failed", "cancelled"):
        session.add(Payment(user_id=1, amount=900, currency="RUB", base_amount=900,
                            status=status, created_at=NOW, product_type="lte_topup",
                            lte_traffic_bytes=500 * GIB))
    # Free trial traffic and manual credits do not represent package sales.
    for source in ("trial:test", "admin:test"):
        session.add(LteGrant(user_id=1, source=source, traffic_bytes=100 * GIB))
    await session.flush()


async def test_package_sales_preserve_periods_and_do_not_double_count_revenue(session):
    await add_sales(session)
    result = await payment_dal.get_financial_statistics(session)
    assert result["all_time_revenue"] == 1650
    assert result["today_revenue"] == 1300
    assert result["today_payments_count"] == 4
    for period, count, gb, revenue in [
        ("today", 3, 38, 300), ("week", 5, 43.5, 410),
        ("month", 7, 58.5, 560), ("all_time", 8, 67.5, 650),
    ]:
        assert result["lte_topups"][period] == {
            "count": count, "traffic_bytes": int(gb * GIB),
            "revenue": revenue, "unvalued_count": 1,
        }


async def test_no_package_sales_returns_zero_totals(session):
    result = await payment_dal.get_financial_statistics(session)
    assert result["all_time_revenue"] == 0
    assert result["lte_topups"] == {period: {
        "count": 0, "traffic_bytes": 0, "revenue": 0, "unvalued_count": 0,
    } for period in ("today", "week", "month", "all_time")}


@pytest.mark.parametrize("lang,premium", [("ru", True), ("en", True), ("ru", False), ("en", False)])
async def test_admin_statistics_displays_separate_lte_sales(session, monkeypatch, lang, premium):
    await add_sales(session)
    monkeypatch.setattr(statistics.user_dal, "get_enhanced_user_statistics", AsyncMock(return_value={
        "total_users": 1, "paid_subscriptions": 1, "trial_users": 0, "inactive_users": 0,
        "banned_users": 0, "referral_users": 0,
    }))
    monkeypatch.setattr(statistics.name_bonus_dal, "get_bonus_statistics", AsyncMock(return_value={
        "total_claims": 0, "users": 0, "monitoring": 0, "revoked": 0,
        "granted_days": 0, "reclaimed_seconds": 0,
    }))
    monkeypatch.setattr(statistics.panel_sync_dal, "get_panel_sync_status", AsyncMock(return_value=None))

    class EmptyPanel:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        get_system_stats = AsyncMock(return_value=None)
        get_bandwidth_stats = AsyncMock(return_value=None)
        get_nodes_statistics = AsyncMock(return_value=None)

    monkeypatch.setattr(statistics, "PanelApiService", EmptyPanel)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
                               answer=AsyncMock())
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=premium)
    settings = Settings(_env_file=None, BOT_TOKEN="test")
    await statistics.show_statistics_handler(callback, {"i18n_instance": i18n, "current_language": lang},
                                             settings, session)
    callback.message.edit_text.assert_awaited_once()
    callback.message.answer.assert_not_awaited()
    text = callback.message.edit_text.await_args.args[0]
    expected_header = "Продажи LTE-пакетов" if lang == "ru" else "LTE package sales"
    assert expected_header in text
    section = text.split(expected_header, 1)[1].split(i18n.gettext(lang, "admin_stats_recent_payments_header"), 1)[0]
    assert "<b>3</b>" in section and "<b>38" in section and "<b>300.00 RUB</b>" in section
    assert "<b>8</b>" in section and "<b>67.5" in section and "<b>650.00 RUB</b>" in section
    assert ("<tg-emoji" in section) == premium
    assert "::" not in section
    assert "1300.00 RUB" in text and "1650.00 RUB" in text
    assert ("без курса" if lang == "ru" else "without an exchange rate") in section
