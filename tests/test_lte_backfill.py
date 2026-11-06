"""Legacy conversion is a baseline, never a reset or a second paid-plan grant."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backfill_lte import allowance_for_expiry, backfill_users, transfer_user
from bot.services.lte_service import LteService
from bot.services.panel_api_service import PanelUnavailableError
from db.dal.lte_dal import GIB
from db.models import Base, LteAccount, LteGrant, Payment, Subscription, User


@pytest.fixture
async def legacy():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(User(user_id=1, panel_user_uuid="42"))
        session.add(Subscription(user_id=1, panel_user_uuid="42", panel_subscription_uuid="short",
                                 end_date=now + timedelta(days=45), is_active=True))
        session.add(LteAccount(user_id=1, managed=False))
        await session.commit()
    panel_user = dict(userTraffic={"lifetimeUsedTrafficBytes": 800 * GIB},
                      expireAt=(now + timedelta(days=45)).isoformat(), status="ACTIVE",
                      activeInternalSquads=["foreign", "lte"])
    panel = SimpleNamespace(get_user_by_ref=AsyncMock(return_value=panel_user))
    yield SimpleNamespace(factory=factory, panel=panel, now=now)
    await engine.dispose()


def test_proration_is_50_divided_by_30_with_a_10_gib_minimum_for_active_subscriptions():
    now = datetime.now(timezone.utc)
    assert allowance_for_expiry(now + timedelta(days=30), now) == 50 * GIB
    assert allowance_for_expiry(now + timedelta(days=45), now) == 75 * GIB
    assert allowance_for_expiry(now + timedelta(hours=12), now) == 10 * GIB
    assert allowance_for_expiry(now + timedelta(microseconds=1), now) == 10 * GIB
    assert allowance_for_expiry(now + timedelta(days=6), now) == 10 * GIB
    assert allowance_for_expiry(now + timedelta(days=7), now) == 50 * GIB * 7 // 30
    assert allowance_for_expiry(now + timedelta(days=6, hours=12), now) == 50 * GIB * 13 // 60
    assert allowance_for_expiry(now, now) == 0
    assert allowance_for_expiry(now - timedelta(seconds=1), now) == 0
    assert allowance_for_expiry(None, now) == 0


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("existing_gb", [0, 25])
async def test_short_subscription_receives_a_minimum_grant_once_in_addition_to_existing_credit(
        legacy, apply, existing_gb):
    async with legacy.factory() as session:
        subscription = (await session.execute(select(Subscription))).scalar_one()
        subscription.end_date = legacy.now + timedelta(days=1)
        account = await session.get(LteAccount, 1)
        account.credited_bytes = existing_gb * GIB
        if existing_gb:
            session.add(LteGrant(user_id=1, source="admin:before", traffic_bytes=existing_gb * GIB))
        await session.commit()

        result = await transfer_user(session, legacy.panel, 1, legacy.now, apply=apply)
        await session.commit()
        assert result.status == ("applied" if apply else "preview")
        assert result.allowance_bytes == 10 * GIB
        account = await session.get(LteAccount, 1)
        assert account.credited_bytes == (existing_gb + (10 if apply else 0)) * GIB
        assert account.used_bytes == 0
        assert account.managed == apply
        assert account.last_panel_counter == (800 * GIB if apply else None)
        if apply:
            again = await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
            assert again.status == "already_managed"
            assert account.credited_bytes == (existing_gb + 10) * GIB
            grants = list((await session.execute(select(LteGrant).where(
                LteGrant.source == "legacy_lte_v1:1"))).scalars())
            assert len(grants) == 1 and grants[0].traffic_bytes == 10 * GIB


async def test_preview_does_not_grant_or_enable_or_set_a_baseline(legacy):
    async with legacy.factory() as session:
        result = await transfer_user(session, legacy.panel, 1, legacy.now, apply=False)
        assert result.status == "preview"
        assert result.allowance_bytes == 75 * GIB
        assert result.panel_counter == 800 * GIB
        await session.commit()
    async with legacy.factory() as session:
        account = await session.get(LteAccount, 1)
        assert not account.managed
        assert account.credited_bytes == account.used_bytes == 0
        assert account.last_panel_counter is None
        assert not list((await session.execute(select(LteGrant))).scalars())


async def test_apply_baselines_old_usage_and_rerun_cannot_grant_twice(legacy):
    async with legacy.factory() as session:
        result = await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        await session.commit()
        assert result.status == "applied"
        again = await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        assert again.status == "already_managed"
        account = await session.get(LteAccount, 1)
        assert account.managed
        assert account.credited_bytes == 75 * GIB
        assert account.used_bytes == 0
        assert account.last_panel_counter == 800 * GIB
        assert account.panel_user_ref == "42"
        assert len(list((await session.execute(select(LteGrant))).scalars())) == 1
    legacy.panel.get_user_by_ref.assert_awaited_once()


async def test_new_managed_default_on_empty_import_still_receives_the_one_time_grant(legacy):
    async with legacy.factory() as session:
        account = await session.get(LteAccount, 1)
        account.managed = True
        await session.commit()
        result = await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        await session.commit()
        assert result.status == "applied"
        assert (await session.get(LteAccount, 1)).remaining_bytes == 75 * GIB


async def test_controller_charges_only_new_traffic_after_transfer(legacy):
    async with legacy.factory() as session:
        await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        await session.commit()
    legacy.panel.get_user_by_ref.return_value["userTraffic"]["lifetimeUsedTrafficBytes"] += 2 * GIB
    legacy.panel.update_user_details_on_panel = AsyncMock(return_value={"id": 42})
    settings = SimpleNamespace(LTE_ENFORCEMENT_ENABLED=True, parsed_lte_squad_uuids=["lte"])
    async with legacy.factory() as session:
        await LteService(settings, legacy.panel).reconcile(session, 1)
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 2 * GIB
        assert account.remaining_bytes == 73 * GIB
    legacy.panel.update_user_details_on_panel.assert_awaited_once_with("42", {"description": "73 ГБ"})


async def test_existing_topups_are_preserved_in_addition_to_prorated_days(legacy):
    async with legacy.factory() as session:
        account = await session.get(LteAccount, 1)
        account.credited_bytes = 25 * GIB
        payment = Payment(user_id=1, amount=100, currency="RUB", status="succeeded",
                          product_type="lte_topup", lte_traffic_bytes=25 * GIB)
        session.add(payment)
        await session.flush()
        session.add(LteGrant(user_id=1, payment_id=payment.payment_id, source="payment:topup", traffic_bytes=25 * GIB))
        await session.commit()
        await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        await session.commit()
        assert (await session.get(LteAccount, 1)).remaining_bytes == 100 * GIB


@pytest.mark.parametrize("source", ["payment:plan", "trial:subscription"])
async def test_overlapping_plan_or_trial_credits_require_review(legacy, source):
    async with legacy.factory() as session:
        payment = Payment(user_id=1, amount=100, currency="RUB", status="succeeded", product_type="subscription")
        session.add(payment)
        await session.flush()
        session.add(LteGrant(user_id=1, source=source, traffic_bytes=10 * GIB,
                             payment_id=payment.payment_id if source.startswith("payment:") else None))
        await session.commit()
        result = await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        assert result.status == "needs_review"
        assert not (await session.get(LteAccount, 1)).managed
    legacy.panel.get_user_by_ref.assert_not_awaited()


@pytest.mark.parametrize("counter", [None, True, -1, "800", 2**63])
async def test_bad_counter_cannot_enable_legacy_account(legacy, counter):
    legacy.panel.get_user_by_ref.return_value["userTraffic"]["lifetimeUsedTrafficBytes"] = counter
    async with legacy.factory() as session:
        result = await transfer_user(session, legacy.panel, 1, legacy.now, apply=True)
        await session.commit()
        assert result.status == "error"
        assert not (await session.get(LteAccount, 1)).managed
        assert not list((await session.execute(select(LteGrant))).scalars())


async def test_users_without_a_subscription_are_also_managed_with_zero_credit(legacy):
    async with legacy.factory() as session:
        session.add(User(user_id=2))
        sub = (await session.execute(select(Subscription))).scalar_one()
        sub.end_date = legacy.now - timedelta(days=1)
        await session.commit()
    rows = []
    counts = await backfill_users(legacy.factory, legacy.panel, apply=True, emit=rows.append)
    assert counts["applied"] == 2
    async with legacy.factory() as session:
        for user_id in (1, 2):
            account = await session.get(LteAccount, user_id)
            assert account.managed and account.remaining_bytes == 0
        assert (await session.get(LteAccount, 2)).last_panel_counter is None


async def test_panel_outage_leaves_user_untouched_and_stops_the_batch(legacy):
    legacy.panel.get_user_by_ref.side_effect = PanelUnavailableError("unavailable")
    rows = []
    counts = await backfill_users(legacy.factory, legacy.panel, apply=True, emit=rows.append)
    assert counts["error"] == 1
    async with legacy.factory() as session:
        assert not (await session.get(LteAccount, 1)).managed
        assert not list((await session.execute(select(LteGrant))).scalars())
