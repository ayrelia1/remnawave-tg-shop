"""Rich message emoji must never leak into Telegram's plain button labels."""

from pathlib import Path

import pytest

from bot.handlers.admin import lte_traffic
from bot.middlewares.i18n import JsonI18n
from db.dal import lte_dal
from db.models import User
from test_lte_admin import admin as lte_admin_fixture, prepare
from test_lte_service import GIB, flow as lte_flow_fixture

lte_admin = lte_admin_fixture
flow = lte_flow_fixture


@pytest.fixture(params=[("ru", True), ("en", True), ("ru", False), ("en", False)])
def ui(request):
    lang, premium = request.param
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=premium)
    return lang, premium, i18n


def check_message(text, premium):
    assert ("<tg-emoji " in text) == premium
    assert "::" not in text


def check_buttons(markup):
    for row in markup.inline_keyboard:
        for button in row:
            assert button.icon_custom_emoji_id
            assert "<" not in button.text
            assert "::" not in button.text
            assert button.callback_data and len(button.callback_data.encode()) <= 64


async def test_lte_balance_and_adjustment_buttons_use_premium_icons(lte_admin, ui):
    admin = lte_admin
    lang, premium, i18n = ui
    admin.i18n_data = {"i18n_instance": i18n, "current_language": lang}
    async with admin.flow.factory() as session:
        await lte_traffic.show_balance(admin.callback, admin.state, admin.flow.settings,
                                        admin.i18n_data, session, admin.flow.panel)
        check_message(admin.edit.await_args.args[1], premium)
        check_buttons(admin.edit.await_args.kwargs["reply_markup"])
        for operation in ("add", "remove"):
            await prepare(admin, session, operation, "25")
            check_message(admin.edit.await_args.args[1], premium)
            check_buttons(admin.edit.await_args.kwargs["reply_markup"])
            check_message(admin.message.answer.await_args.args[0], premium)
            check_buttons(admin.message.answer.await_args.kwargs["reply_markup"])
    # An expired callback is a plain Telegram alert, not an HTML message.
    assert "<" not in i18n.gettext(lang, "admin_lte_expired")


async def test_low_and_depleted_notifications_have_rich_text_and_plain_topup_buttons(flow, ui):
    lang, premium, i18n = ui
    flow.lte.i18n = i18n
    async with flow.factory() as session:
        (await session.get(User, 1)).language_code = lang
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    for used in (6 * GIB, 4 * GIB):
        flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += used
        await flow.lte.run_checks(flow.factory)
        check_message(flow.bot.send_message.await_args.args[1], premium)
        check_buttons(flow.bot.send_message.await_args.kwargs["reply_markup"])
    assert flow.bot.send_message.await_count == 2
