"""Panel descriptions expose only the managed LTE balance, in GiB."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.admin.sync_admin import perform_sync
from db.dal import lte_dal
from db.models import LteAccount, User
from test_lte_service import GIB, activate, flow as lte_flow_fixture, invoice

flow = lte_flow_fixture


@pytest.mark.parametrize("amount,description", [
    (0, "0 ГБ"),
    (GIB // 2, "0.5 ГБ"),
    (GIB, "1 ГБ"),
    (5 * GIB, "5 ГБ"),
    (13 * GIB // 16, "0.81 ГБ"),
])
async def test_check_projects_only_remaining_gb(flow, amount, description):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 10 * GIB - amount
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["description"] == description


async def test_check_does_not_patch_until_formatted_balance_changes(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, GIB, "initial")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["description"] == "1 ГБ"
    flow.panel.updates.clear()
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 1
    await flow.lte.run_checks(flow.factory)
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.updates == []
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).used_bytes == 1


async def test_description_only_failure_retries_without_losing_usage(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += GIB
    flow.panel.reject_patch = True
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["description"] == "10 ГБ"
    flow.panel.reject_patch = False
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["description"] == "9 ГБ"
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).used_bytes == GIB


async def test_purchase_trial_and_topup_publish_the_new_balance(flow):
    async with flow.factory() as session:
        user = await session.get(User, 1)
        user.username, user.first_name = "telegram_name", "Telegram Name"
        trial = await flow.subscription.activate_trial_subscription(session, 1)
        assert trial["activated"]
        assert flow.panel.user["description"] == "10 ГБ"
        payment = await invoice(session)
        await activate(flow, session, payment)
        await session.commit()
        assert flow.panel.user["description"] == "160 ГБ"
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 2 * GIB
        payment = await invoice(session, "lte_topup", 25, None)
        await activate(flow, session, payment)
        await session.commit()
        assert flow.panel.user["description"] == "183 ГБ"


async def test_renewal_does_not_patch_an_unchanged_description(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, GIB, "initial")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    async with flow.factory() as session:
        payload = await flow.lte.project_payload(session, 1, "42", {"status": "ACTIVE"})
        assert "description" not in payload


@pytest.mark.parametrize("managed", [True, False])
async def test_panel_sync_preserves_balance_and_legacy_descriptions(flow, managed):
    async with flow.factory() as session:
        user = await session.get(User, 1)
        user.username, user.first_name = "telegram_name", "Telegram Name"
        session.add(LteAccount(user_id=1, managed=managed, credited_bytes=GIB // 2, used_bytes=0))
        await session.commit()
    description = "0.5 ГБ" if managed else "Legacy note"
    flow.panel.user["description"] = description
    flow.panel.get_all_panel_users = AsyncMock(return_value=[deepcopy(flow.panel.user)])
    async with flow.factory() as session:
        result = await perform_sync(flow.panel, session, flow.settings,
                                    SimpleNamespace(gettext=lambda lang, key, **kw: key))
        assert result["status"] == "completed"
    assert flow.panel.user["description"] == description
    assert flow.panel.updates == []


async def test_legacy_purchase_preserves_its_panel_description(flow):
    flow.panel.user["description"] = "Legacy note"
    async with flow.factory() as session:
        session.add(LteAccount(user_id=1, managed=False, credited_bytes=0, used_bytes=0))
        user = await session.get(User, 1)
        user.username = "telegram_name"
        await session.commit()
        payment = await invoice(session)
        await activate(flow, session, payment)
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["description"] == "Legacy note"
