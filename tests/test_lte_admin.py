"""Manual adjustments use live usage, preserve audit history and never overdraw."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from bot.handlers.admin import lte_traffic
from bot.middlewares.i18n import JsonI18n
from bot.states.admin_states import AdminStates
from db.dal import lte_dal
from db.dal.lte_dal import GIB
from db.models import LteAccount, LteGrant
from test_lte_service import flow as lte_flow_fixture

flow = lte_flow_fixture


@pytest.fixture
async def admin(flow, monkeypatch):
    # Production does not autoflush; observed usage must survive the next lock.
    flow.factory.configure(autoflush=False)
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 50 * GIB, "seed")
        await flow.lte.reconcile(session, 1)
        await session.commit()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=777, user_id=777))
    callback = SimpleNamespace(data="admin_lte:show:1", from_user=SimpleNamespace(id=777),
                                message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    message = SimpleNamespace(text="25", answer=AsyncMock())
    edit = AsyncMock()
    monkeypatch.setattr(lte_traffic, "safe_edit_text", edit)
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=False)
    yield SimpleNamespace(flow=flow, state=state, callback=callback, message=message, edit=edit,
                          i18n_data={"i18n_instance": i18n, "current_language": "ru"})
    await storage.close()


async def prepare(admin, session, operation, amount):
    admin.callback.data = f"admin_lte:edit:{operation}:1"
    await lte_traffic.prompt_adjustment(admin.callback, admin.state, admin.flow.settings, admin.i18n_data, session)
    admin.message.text = amount
    await lte_traffic.enter_adjustment(admin.message, admin.state, admin.flow.settings,
                                       admin.i18n_data, session, admin.flow.panel)
    data = await admin.state.get_data()
    admin.callback.data = f"admin_lte:confirm:{data['lte_token']}"
    return data


async def confirm(admin, session):
    await lte_traffic.confirm_adjustment(admin.callback, admin.state, admin.flow.settings,
                                         admin.i18n_data, session, admin.flow.panel)


async def test_balance_screen_refreshes_consumed_traffic_before_display(admin):
    admin.flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 7 * GIB
    async with admin.flow.factory() as session:
        await lte_traffic.show_balance(admin.callback, admin.state, admin.flow.settings,
                                        admin.i18n_data, session, admin.flow.panel)
        assert (await session.get(LteAccount, 1)).remaining_bytes == 43 * GIB
    assert "43.00 ГБ" in admin.edit.await_args.args[1]


async def test_manual_credit_is_confirmed_and_cannot_be_replayed(admin):
    async with admin.flow.factory() as session:
        data = await prepare(admin, session, "add", "25")
        assert await admin.state.get_state() == AdminStates.confirming_lte_adjustment.state
        assert (await session.get(LteAccount, 1)).credited_bytes == 50 * GIB
        await confirm(admin, session)
        await confirm(admin, session)
        assert (await session.get(LteAccount, 1)).remaining_bytes == 75 * GIB
        assert admin.flow.panel.user["description"] == "75 ГБ"
        entry = (await session.execute(select(LteGrant).where(
            LteGrant.source == f"admin:777:{data['lte_token']}"))).scalar_one()
        assert entry.traffic_bytes == 25 * GIB
        assert entry.payment_id is None
        assert await admin.state.get_state() is None


async def test_usage_between_prompt_and_confirmation_prevents_overdraw(admin):
    async with admin.flow.factory() as session:
        await prepare(admin, session, "remove", "45")
        admin.flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 10 * GIB
        await confirm(admin, session)
        account = await session.get(LteAccount, 1)
        assert account.credited_bytes == 50 * GIB
        assert account.used_bytes == 10 * GIB
        assert account.remaining_bytes == 40 * GIB
        assert len(list((await session.execute(select(LteGrant))).scalars())) == 1
        assert "Списание не выполнено" in admin.callback.message.answer.await_args.args[0]


async def test_debit_to_zero_removes_only_lte_and_logs_a_negative_entry(admin):
    async with admin.flow.factory() as session:
        await prepare(admin, session, "remove", "50")
        await confirm(admin, session)
        account = await session.get(LteAccount, 1)
        assert account.remaining_bytes == 0
        assert account.used_bytes == 0
        entry = (await session.execute(select(LteGrant).where(LteGrant.source.startswith("admin:")))).scalar_one()
        assert entry.traffic_bytes == -50 * GIB
    assert admin.flow.panel.user["activeInternalSquads"] == ["foreign"]
    assert admin.flow.panel.user["description"] == "0 ГБ"
    await admin.flow.lte.run_checks(admin.flow.factory)
    admin.flow.bot.send_message.assert_awaited_once()
    assert admin.flow.bot.send_message.await_args.args[1] == "lte_depleted_message"


async def test_saved_credit_survives_panel_projection_failure_and_scheduler_retries(admin):
    admin.flow.panel.user["activeInternalSquads"] = ["foreign"]
    admin.flow.panel.reject_patch = True
    async with admin.flow.factory() as session:
        await prepare(admin, session, "add", "25")
        await confirm(admin, session)
        account = await session.get(LteAccount, 1)
        assert account.remaining_bytes == 75 * GIB
        assert account.access_sync_pending
        assert "Баланс сохранён" in admin.edit.await_args.args[1]
    admin.flow.panel.reject_patch = False
    await admin.flow.lte.run_checks(admin.flow.factory)
    assert "lte" in admin.flow.panel.user["activeInternalSquads"]
    async with admin.flow.factory() as session:
        assert not (await session.get(LteAccount, 1)).access_sync_pending


async def test_panel_outage_before_adjustment_does_not_change_credit(admin):
    async with admin.flow.factory() as session:
        await prepare(admin, session, "add", "25")
        admin.flow.panel.get_user_by_ref = AsyncMock(return_value={"error": True})
        await confirm(admin, session)
        account = await session.get(LteAccount, 1)
        assert account.remaining_bytes == 50 * GIB
        assert len(list((await session.execute(select(LteGrant))).scalars())) == 1
        assert "Операция не выполнена" in admin.callback.message.answer.await_args.args[0]


async def test_live_balance_failure_is_labelled_as_saved_not_current(admin):
    admin.flow.panel.get_user_by_ref = AsyncMock(return_value={"error": True})
    async with admin.flow.factory() as session:
        await lte_traffic.show_balance(admin.callback, admin.state, admin.flow.settings,
                                        admin.i18n_data, session, admin.flow.panel)
    assert "сохранённый остаток" in admin.edit.await_args.args[1]


@pytest.mark.parametrize("raw", ["0", "-1", "+1", "NaN", "Infinity", "1e999999999999999999",
                                  "0.1", "0.5", "0,5", "1.6"])
async def test_invalid_amount_keeps_the_form_and_does_not_modify_balance(admin, raw):
    async with admin.flow.factory() as session:
        await prepare(admin, session, "add", raw)
        assert await admin.state.get_state() == AdminStates.waiting_for_lte_amount.state
        assert (await session.get(LteAccount, 1)).remaining_bytes == 50 * GIB


async def test_confirmation_rejects_a_fractional_amount_from_an_old_form(admin):
    async with admin.flow.factory() as session:
        await prepare(admin, session, "add", "25")
        await admin.state.update_data(lte_amount=GIB // 2)
        await confirm(admin, session)
        assert (await session.get(LteAccount, 1)).remaining_bytes == 50 * GIB
        entries = list((await session.execute(select(LteGrant))).scalars())
        assert len(entries) == 1
    assert admin.callback.answer.await_args.kwargs.get("show_alert")


async def test_same_operation_token_is_idempotent_even_after_other_usage(admin):
    async with admin.flow.factory() as session:
        _, changed = await lte_dal.adjust_balance(session, 1, -10 * GIB, "admin:777:stable")
        assert changed
        await session.commit()
        account, changed = await lte_dal.adjust_balance(session, 1, -10 * GIB, "admin:777:stable")
        assert not changed and account.remaining_bytes == 40 * GIB
        with pytest.raises(ValueError):
            await lte_dal.adjust_balance(session, 1, -5 * GIB, "admin:777:stable")


async def test_legacy_manual_adjustment_does_not_silently_enable_enforcement(admin):
    async with admin.flow.factory() as session:
        account = await session.get(LteAccount, 1)
        account.managed = False
        await session.commit()
        await prepare(admin, session, "add", "25")
        await confirm(admin, session)
        account = await session.get(LteAccount, 1)
        assert account.remaining_bytes == 75 * GIB and not account.managed


async def test_back_to_card_clears_an_unsubmitted_adjustment(admin, monkeypatch):
    from bot.handlers.admin import user_management
    refresh = AsyncMock()
    monkeypatch.setattr(user_management, "handle_refresh_user_card", refresh)
    async with admin.flow.factory() as session:
        await prepare(admin, session, "add", "25")
        admin.callback.data = "admin_lte:back:1"
        await lte_traffic.back_to_user(admin.callback, admin.state, admin.flow.settings,
                                        admin.i18n_data, session, admin.flow.subscription)
    assert await admin.state.get_state() is None
    refresh.assert_awaited_once()


@pytest.mark.parametrize("lang,device_plans", [("ru", False), ("en", True)])
def test_user_card_exposes_the_localized_lte_entry_point(lang, device_plans):
    from bot.handlers.admin.user_management import get_user_card_keyboard
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=False)
    keyboard = get_user_card_keyboard(1, i18n, lang, referrer_id=2,
                                      settings=SimpleNamespace(device_plans_active=device_plans)).as_markup()
    row = next(row for row in keyboard.inline_keyboard
               if any(button.callback_data == "admin_lte:show:1" for button in row))
    assert len(row) == 1
    assert "LTE" in row[0].text


async def test_paused_enforcement_marks_balance_as_saved_and_does_not_read_panel(admin):
    admin.flow.settings.LTE_ENFORCEMENT_ENABLED = False
    get_user = AsyncMock()
    admin.flow.panel.get_user_by_ref = get_user
    async with admin.flow.factory() as session:
        await lte_traffic.show_balance(admin.callback, admin.state, admin.flow.settings,
                                        admin.i18n_data, session, admin.flow.panel)
    get_user.assert_not_awaited()
    assert "Контроль LTE выключен" in admin.edit.await_args.args[1]
