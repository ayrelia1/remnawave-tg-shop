"""Bulk polling must preserve per-user transaction and counter semantics."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.services.lte_service import LteService
from bot.utils.async_utils import run_bounded
from db.dal import lte_dal
from db.models import Base, LteAccount, Subscription, User
from tests.conftest import RecordingPanelApiService, ok, panel_user
from test_lte_service import GIB, flow as lte_flow_fixture

flow = lte_flow_fixture


@pytest.fixture
async def bulk(tmp_path, panel_settings):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bulk.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = SimpleNamespace(LTE_ENFORCEMENT_ENABLED=True, parsed_lte_squad_uuids=["lte"])
    panel = RecordingPanelApiService(panel_settings)
    service = LteService(settings, panel)
    yield SimpleNamespace(engine=engine, factory=factory, panel=panel, service=service)
    await engine.dispose()


async def seed(bulk, count):
    async with bulk.factory() as session:
        await session.execute(insert(User), [dict(user_id=i, panel_user_uuid=str(i))
                                             for i in range(1, count + 1)])
        await session.execute(insert(LteAccount), [dict(
            user_id=i, managed=True, credited_bytes=10 * GIB, used_bytes=0,
            panel_user_ref=str(i), last_panel_counter=10000,
            access_sync_pending=False, access_enabled=True) for i in range(1, count + 1)])
        await session.commit()
    return [panel_user(user_id=i, description="10 ГБ", activeInternalSquads=["foreign", "lte"],
                       userTraffic={"lifetimeUsedTrafficBytes": 10000},
                       expireAt=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat())
            for i in range(1, count + 1)]


async def test_ten_thousand_unchanged_users_need_ten_gets_and_no_per_user_transactions(bulk):
    users = await seed(bulk, 10000)
    bulk.panel.responses = [ok(dict(users=users[start:start + 1000],
                                   nextCursor=str(start + 1000), hasMore=start < 9000))
                            for start in range(0, 10000, 1000)]
    statements = []
    event.listen(bulk.engine.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *args: statements.append(statement))
    bulk.service._notify = AsyncMock()
    await bulk.service.run_checks(bulk.factory)
    assert len(bulk.panel.calls) == 10
    assert all(call[:2] == ("GET", "/users/stream") for call in bulk.panel.calls)
    assert all(call[2]["params"]["size"] == 1000 for call in bulk.panel.calls)
    assert len(statements) <= 4
    assert not any(query.lstrip().upper().startswith("UPDATE") for query in statements)
    bulk.service._notify.assert_not_awaited()


async def test_changed_users_are_patched_with_at_most_five_workers(bulk):
    users = await seed(bulk, 14)
    for user in users[:12]:
        user["description"] = "stale"
    bulk.panel.get_all_panel_users = AsyncMock(return_value=users)
    bulk.panel.get_user_by_ref = AsyncMock(side_effect=AssertionError("Unexpected per-user read"))
    active = maximum = 0
    updated_ids = []

    async def patch(ref, payload):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        updated_ids.append(ref)
        try:
            await asyncio.sleep(0.02)
            return dict(id=int(ref))
        finally:
            active -= 1

    bulk.panel.update_user_details_on_panel = patch
    await bulk.service.run_checks(bulk.factory)
    assert 1 < maximum <= 5
    assert set(updated_ids) == {str(i) for i in range(1, 13)}
    bulk.panel.get_user_by_ref.assert_not_awaited()


async def test_pending_notifications_only_are_dispatched(bulk):
    users = await seed(bulk, 12)
    async with bulk.factory() as session:
        account = await session.get(LteAccount, 3)
        account.credited_bytes = 4 * GIB
        account.low_notification_pending = True
        users[2]["description"] = "4 ГБ"
        await session.commit()
    bulk.panel.get_all_panel_users = AsyncMock(return_value=users)
    bulk.service._notify = AsyncMock()
    await bulk.service.run_checks(bulk.factory)
    bulk.service._notify.assert_awaited_once_with(bulk.factory, 3)


@pytest.mark.parametrize("counter", [820 * GIB, 2 * GIB])
async def test_older_snapshot_after_a_concurrent_refresh_is_not_a_counter_reset(flow, counter):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 100 * GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 820 * GIB
        account.used_bytes = 20 * GIB
        await session.commit()
    old = deepcopy(flow.panel.user)
    old["userTraffic"]["lifetimeUsedTrafficBytes"] = counter
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] = 830 * GIB

    async def snapshot():
        async with flow.factory() as session:
            await flow.lte.refresh_balance(session, 1)
            await session.commit()
        return [old]

    flow.panel.get_all_panel_users = snapshot
    get_user = flow.panel.get_user_by_ref
    flow.panel.get_user_by_ref = AsyncMock(wraps=get_user)
    await flow.lte.run_checks(flow.factory)
    flow.panel.get_user_by_ref.assert_awaited_once()  # The concurrent refresh itself.
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 30 * GIB
        assert account.remaining_bytes == 70 * GIB
        assert account.last_panel_counter == 830 * GIB


async def test_bonus_extension_with_unchanged_wallet_does_not_apply_expired_snapshot(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        session.add(Subscription(user_id=1, panel_user_uuid="42", is_active=True,
                                 end_date=datetime.now(timezone.utc) - timedelta(days=1)))
        await session.commit()
    old = deepcopy(flow.panel.user)
    old["expireAt"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old["status"] = "EXPIRED"

    async def snapshot():
        async with flow.factory() as session:
            sub = (await session.execute(select(Subscription))).scalar_one()
            sub.end_date = datetime.now(timezone.utc) + timedelta(days=1)
            await session.commit()
        return [old]

    flow.panel.get_all_panel_users = snapshot
    await flow.lte.run_checks(flow.factory)
    assert "lte" in flow.panel.user["activeInternalSquads"][1]["uuid"]
    assert not flow.panel.updates
    flow.panel.get_all_panel_users = AsyncMock(return_value=[deepcopy(flow.panel.user)])
    flow.panel.get_user_by_ref = AsyncMock(side_effect=AssertionError("Unexpected per-user read"))
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).access_enabled


async def test_incomplete_panel_snapshot_cannot_charge_or_disable_users(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
    flow.panel.get_all_panel_users = AsyncMock(return_value=None)
    await flow.lte.run_checks(flow.factory)
    assert not flow.panel.updates
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.last_panel_counter is None
        assert account.used_bytes == 0


@pytest.mark.parametrize("missing", [True, False])
async def test_unverified_panel_user_does_not_receive_an_old_pending_notice(flow, missing):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 4 * GIB, "initial")
        account.low_notification_pending = True
        await session.commit()
    if missing:
        flow.panel.get_all_panel_users = AsyncMock(return_value=[])
    else:
        # The pending low warning must not be sent while an attempt to remove
        # exhausted LTE access fails and rolls its observation back.
        async with flow.factory() as session:
            account = await session.get(LteAccount, 1)
            account.panel_user_ref, account.last_panel_counter = "42", 0
            await session.commit()
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] = 4 * GIB
        flow.panel.reject_patch = True
    await flow.lte.run_checks(flow.factory)
    flow.bot.send_message.assert_not_awaited()
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).low_notification_pending


async def test_worker_cancellation_leaves_no_background_mutations():
    started = asyncio.Event()
    blocked = asyncio.Event()
    active = finished = 0

    async def operation(value):
        nonlocal active, finished
        active += 1
        if active == 2:
            started.set()
        try:
            await blocked.wait()
        finally:
            active -= 1
            finished += 1

    task = asyncio.create_task(run_bounded(list(range(10000)), operation, limit=2))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0
    assert finished == 2


@pytest.mark.parametrize("exhausted", [True, False])
async def test_only_membership_changes_refresh_and_preserve_recent_panel_edits(flow, exhausted):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 10 * GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 10000
        await session.commit()
    if exhausted:
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 10 * GIB
    else:
        flow.panel.user["activeInternalSquads"] = ["foreign"]

    async def snapshot():
        old = deepcopy(flow.panel.user)
        flow.panel.user["activeInternalSquads"].append("new-foreign")
        return [old]

    flow.panel.get_all_panel_users = snapshot
    flow.panel.get_user_by_ref = AsyncMock(wraps=flow.panel.get_user_by_ref)
    await flow.lte.run_checks(flow.factory)
    squads = flow.panel.user["activeInternalSquads"]
    assert "foreign" in squads and "new-foreign" in squads
    assert ("lte" in squads) == (not exhausted)
    flow.panel.get_user_by_ref.assert_awaited_once_with("42")


async def test_access_refresh_respects_a_panel_account_disabled_after_bulk_read(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
    flow.panel.user["activeInternalSquads"] = ["foreign"]

    async def snapshot():
        old = deepcopy(flow.panel.user)
        flow.panel.user["status"] = "DISABLED"
        return [old]

    flow.panel.get_all_panel_users = snapshot
    flow.panel.get_user_by_ref = AsyncMock(wraps=flow.panel.get_user_by_ref)
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["status"] == "DISABLED"
    flow.panel.get_user_by_ref.assert_awaited_once_with("42")
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    async with flow.factory() as session:
        assert not (await session.get(LteAccount, 1)).access_enabled


async def test_one_failed_patch_rolls_back_only_that_user_and_retries_without_double_charging(bulk):
    users = await seed(bulk, 12)
    for user in users:
        user["userTraffic"]["lifetimeUsedTrafficBytes"] += GIB
    bulk.panel.get_all_panel_users = AsyncMock(return_value=users)
    reject = True

    async def patch(ref, payload):
        if reject and ref == "3":
            return {"error": True}
        users[int(ref) - 1].update(payload)
        return {"id": int(ref)}

    bulk.panel.update_user_details_on_panel = patch
    await bulk.service.run_checks(bulk.factory)
    async with bulk.factory() as session:
        for account in (await session.execute(select(LteAccount))).scalars():
            assert account.used_bytes == (0 if account.user_id == 3 else GIB)
    reject = False
    await bulk.service.run_checks(bulk.factory)
    async with bulk.factory() as session:
        assert all(account.used_bytes == GIB for account in
                   (await session.execute(select(LteAccount))).scalars())


async def test_concurrent_panel_relink_baselines_the_new_identity_without_charging_history(flow):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 100 * GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 800 * GIB
        account.used_bytes = 40 * GIB
        await session.commit()

    async def snapshot():
        old = deepcopy(flow.panel.user)
        old["userTraffic"]["lifetimeUsedTrafficBytes"] = 810 * GIB
        async with flow.factory() as session:
            user = await session.get(User, 1)
            user.panel_user_uuid = "99"
            await session.commit()
        flow.panel.user["id"] = 99
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] = 2 * GIB
        return [old]

    flow.panel.get_all_panel_users = snapshot
    await flow.lte.run_checks(flow.factory)
    assert not flow.panel.updates
    flow.panel.get_all_panel_users = AsyncMock(return_value=[deepcopy(flow.panel.user)])
    flow.panel.get_user_by_ref = AsyncMock(side_effect=AssertionError("Unexpected per-user read"))
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.panel_user_ref == "99"
        assert account.last_panel_counter == 2 * GIB
        assert account.used_bytes == 40 * GIB


async def test_real_counter_reset_still_charges_new_epoch_traffic(flow):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 100 * GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 800 * GIB
        account.used_bytes = 40 * GIB
        await session.commit()
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] = 2 * GIB
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 42 * GIB
        assert account.remaining_bytes == 58 * GIB


async def test_access_refresh_failure_does_not_charge_or_patch_until_retry(flow):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 10 * GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 10000
        await session.commit()
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 10 * GIB
    get_user = flow.panel.get_user_by_ref
    flow.panel.get_user_by_ref = AsyncMock(return_value=None)
    await flow.lte.run_checks(flow.factory)
    assert not flow.panel.updates
    flow.bot.send_message.assert_not_awaited()
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 0 and account.last_panel_counter == 10000
    flow.panel.get_user_by_ref = AsyncMock(wraps=get_user)
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).used_bytes == 10 * GIB
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    flow.bot.send_message.assert_awaited_once()


async def test_access_refresh_can_cancel_a_restore_after_more_traffic_is_used(flow):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 10000
        await session.commit()
    flow.panel.user["activeInternalSquads"] = ["foreign"]

    async def snapshot():
        old = deepcopy(flow.panel.user)
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += GIB
        return [old]

    flow.panel.get_all_panel_users = snapshot
    flow.panel.get_user_by_ref = AsyncMock(wraps=flow.panel.get_user_by_ref)
    await flow.lte.run_checks(flow.factory)
    flow.panel.get_user_by_ref.assert_awaited_once_with("42")
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    assert not any("activeInternalSquads" in patch for patch in flow.panel.updates)
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == GIB and not account.access_enabled


async def test_counter_reset_between_bulk_and_access_reads_preserves_both_epochs_once(flow):
    async with flow.factory() as session:
        account = await lte_dal.credit(session, 1, 100 * GIB, "initial")
        account.panel_user_ref, account.last_panel_counter = "42", 800 * GIB
        account.used_bytes = 40 * GIB
        await session.commit()

    async def snapshot():
        old = deepcopy(flow.panel.user)
        old["userTraffic"]["lifetimeUsedTrafficBytes"] = 860 * GIB
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] = 2 * GIB
        return [old]

    flow.panel.get_all_panel_users = snapshot
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 102 * GIB
        assert account.last_panel_counter == 2 * GIB
        assert account.remaining_bytes == 0
    flow.panel.get_all_panel_users = AsyncMock(return_value=[deepcopy(flow.panel.user)])
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).used_bytes == 102 * GIB
    flow.bot.send_message.assert_awaited_once()
