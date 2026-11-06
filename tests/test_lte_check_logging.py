"""One aggregate log covers the full LTE cycle, including early exits."""

import asyncio
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from db.dal import lte_dal
from test_lte_service import GIB, flow as lte_flow_fixture

flow = lte_flow_fixture


def summary(caplog):
    messages = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("LTE checks:")]
    assert len(messages) == 1
    return messages[0]


async def credit(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()


async def test_full_cycle_duration_and_actual_update_attempts(flow, caplog, monkeypatch):
    await credit(flow)
    ticks = iter((100.0, 102.25))
    monkeypatch.setattr("bot.services.lte_service.monotonic", lambda: next(ticks))
    caplog.set_level(logging.INFO)
    await flow.lte.run_checks(flow.factory)
    message = summary(caplog)
    for expected in ("status=ok", "duration=2.25s", "users=1", "changed=1", "synced=1",
                     "patch_attempts=1", "access_read_attempts=0", "errors=0"):
        assert expected in message
    caplog.clear()
    monkeypatch.setattr("bot.services.lte_service.monotonic", lambda: 200.0)
    await flow.lte.run_checks(flow.factory)
    message = summary(caplog)
    assert "changed=0" in message and "patch_attempts=0" in message


async def test_empty_cycle_is_timed_without_reading_panel(flow, caplog):
    caplog.set_level(logging.INFO)
    flow.panel.get_all_panel_users = AsyncMock()
    await flow.lte.run_checks(flow.factory)
    message = summary(caplog)
    assert "status=empty" in message and "duration=" in message
    assert "users=0" in message
    flow.panel.get_all_panel_users.assert_not_awaited()


async def test_failed_snapshot_is_timed_without_applying_updates(flow, caplog):
    await credit(flow)
    caplog.set_level(logging.INFO)
    flow.panel.get_all_panel_users = AsyncMock(return_value=None)
    await flow.lte.run_checks(flow.factory)
    message = summary(caplog)
    assert "status=panel_error" in message and "duration=" in message
    assert "patch_attempts=0" in message and "errors=1" in message


async def test_failed_patch_counts_the_attempt_and_failed_user(flow, caplog):
    await credit(flow)
    caplog.set_level(logging.INFO)
    flow.panel.reject_patch = True
    await flow.lte.run_checks(flow.factory)
    message = summary(caplog)
    assert "status=partial" in message and "synced=0" in message
    assert "patch_attempts=1" in message and "errors=1" in message


async def test_access_change_counts_its_extra_read(flow, caplog):
    await credit(flow)
    await flow.lte.run_checks(flow.factory)
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 10 * GIB
    caplog.clear()
    caplog.set_level(logging.INFO)
    await flow.lte.run_checks(flow.factory)
    message = summary(caplog)
    assert "access_read_attempts=1" in message and "patch_attempts=1" in message
    assert "pending_notices=1" in message


async def test_fatal_database_error_is_logged_and_still_propagates(flow, caplog):
    @asynccontextmanager
    async def unavailable_factory():
        raise RuntimeError("test database outage")
        yield

    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError, match="test database outage"):
        await flow.lte.run_checks(unavailable_factory)
    message = summary(caplog)
    assert "status=failed" in message and "duration=" in message
    assert "errors=1" in message


async def test_cancelled_cycle_is_timed_and_cancellation_propagates(flow, caplog):
    await credit(flow)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def read():
        entered.set()
        await blocked.wait()

    flow.panel.get_all_panel_users = read
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(flow.lte.run_checks(flow.factory))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "status=cancelled" in summary(caplog)
