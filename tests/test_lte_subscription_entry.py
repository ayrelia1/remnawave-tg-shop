"""Check subscription entry points and the displayed terms of LTE offers."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageMedia, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser

from bot.constants.premium_emoji import PREMIUM_EMOJI_DOCUMENT
from bot.handlers.user import start
from bot.handlers.user.subscription import core
from bot.keyboards.inline.user_keyboards import get_info_keyboard, get_lte_packages_keyboard
from bot.middlewares.i18n import JsonI18n
from db.models import Subscription
from config.settings import Settings
from test_lte_service import flow as lte_flow_fixture

flow = lte_flow_fixture


@pytest.fixture
def i18n():
    return JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=False)


@pytest.mark.parametrize("scenario,can_topup", [
    ("new", False), ("expired", False), ("stale_active", False),
    ("disabled", False), ("orphan", True), ("missing_panel", False), ("paid", True), ("trial", True),
])
async def test_my_subscription_shows_topup_and_rules_only_for_current_subscription(
        flow, i18n, monkeypatch, scenario, can_topup):
    now = datetime.now(timezone.utc)
    end_date = now + timedelta(days=-1 if scenario in {"expired", "stale_active"} else 1)
    panel_status = "EXPIRED" if scenario == "expired" else "DISABLED" if scenario == "disabled" else "ACTIVE"
    details = None if scenario in {"new", "missing_panel"} else {
        "user_id": "42", "end_date": end_date, "status_from_panel": panel_status,
        "config_link": "https://example.test/sub/short", "max_devices": 2,
    }
    subscription_service = SimpleNamespace(get_active_subscription_details=AsyncMock(return_value=details))
    panel_service = SimpleNamespace(get_user_devices=AsyncMock(return_value=[]))
    bot = AsyncMock(spec=Bot)
    event = Message(message_id=1, date=now, chat=Chat(id=1, type="private"),
                    from_user=TelegramUser(id=1, is_bot=False, first_name="Test"),
                    text="/my_subscription").as_(bot)
    flow.settings.SUBSCRIPTION_MINI_APP_URL = None
    edit = AsyncMock()
    monkeypatch.setattr(core, "edit_or_send_with_photo", edit)
    async with flow.factory() as session:
        if scenario not in {"new", "orphan"}:
            session.add(Subscription(user_id=1, panel_user_uuid="42", panel_subscription_uuid="short",
                                     end_date=end_date, is_active=True,
                                     duration_months=0 if scenario == "trial" else 1))
            await session.commit()
        await core.my_subscription_command_handler(event, {"i18n_instance": i18n}, flow.settings,
                                                   panel_service, subscription_service, session, bot)
    buttons = [button for row in edit.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
    actions = [button.callback_data for button in buttons]
    assert ("lte:packages" in actions) == can_topup
    assert ("rules:my_subscription" in actions) == can_topup
    if can_topup:
        assert actions[-2:] == ["rules:my_subscription", "main_action:back_to_main"]
        assert buttons[-2].icon_custom_emoji_id == PREMIUM_EMOJI_DOCUMENT
        assert next(button for button in buttons if button.callback_data == "lte:packages").icon_custom_emoji_id
        assert i18n.gettext("ru", "subscription_not_active") not in edit.await_args.kwargs["caption"]
    else:
        assert "main_action:subscribe" in actions


@pytest.mark.parametrize("lang,source,prices,allowance,example_key", [
    ("ru", "info", "100,270,0,850", "60,180,360,720", "subscription_rules_lte_monthly_example"),
    ("en", "my_subscription", "100,270,0,850", "50,150,300,500", "subscription_rules_lte_example"),
    ("ru", "info", "100,0,0,0", "60,180,360,720", None),
])
async def test_rules_open_as_text_and_return_to_the_origin(flow, lang, source, prices, allowance, example_key):
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=True)
    settings = Settings(_env_file=None, BOT_TOKEN="test", DEVICE_PLANS_RUB=f"2:{prices}",
                        DEVICE_PLANS_LTE_GB=allowance, TERMS_OF_SERVICE_URL="https://example.test/terms")
    bot = AsyncMock(spec=Bot)
    bot.return_value = None
    sender = TelegramUser(id=1, is_bot=False, first_name="Test")
    info_markup = get_info_keyboard(lang, i18n, settings)
    info_button = info_markup.inline_keyboard[0][0]
    assert info_button.callback_data == "rules:info"
    assert info_button.icon_custom_emoji_id == PREMIUM_EMOJI_DOCUMENT
    assert info_markup.inline_keyboard[1][0].url == settings.TERMS_OF_SERVICE_URL
    message = Message(message_id=1, date=0, chat=Chat(id=1, type="private"),
                      caption="Menu", reply_markup=info_markup).as_(bot)
    callback = CallbackQuery(id="rules-1", from_user=sender, chat_instance="chat-1",
                             message=message, data=f"rules:{source}").as_(bot)

    await start.router.propagate_event("callback_query", callback, settings=settings,
                                       i18n_data={"i18n_instance": i18n, "current_language": lang})

    send, clear, ack = [call.args[0] for call in bot.await_args_list]
    assert isinstance(send, SendMessage)
    assert send.parse_mode == "HTML"
    assert "✅" in send.text and "<tg-emoji" not in send.text
    lte_example = ""
    if example_key:
        one_month, three_months, *_ = allowance.split(",")
        lte_example = " " + i18n.gettext(lang, example_key, one_month_gb=one_month, three_month_gb=three_months)
    assert send.text == i18n.gettext(lang, "subscription_rules_text", lte_example=lte_example)
    [[back_button]] = send.reply_markup.inline_keyboard
    assert back_button.text == i18n.gettext(lang, "back_to_main_menu_button")
    assert back_button.callback_data == f"main_action:{source}"
    assert isinstance(clear, EditMessageReplyMarkup) and clear.reply_markup is None
    assert isinstance(ack, AnswerCallbackQuery)

    rules_message = Message(message_id=2, date=0, chat=message.chat, text=send.text).as_(bot)
    back = CallbackQuery(id="rules-back", from_user=sender, chat_instance="chat-1",
                         message=rules_message, data=back_button.callback_data).as_(bot)
    subscription_service = SimpleNamespace(get_active_subscription_details=AsyncMock(return_value=None))
    bot.reset_mock()
    async with flow.factory() as session:
        await start.router.propagate_event(
            "callback_query", back, settings=settings,
            i18n_data={"i18n_instance": i18n, "current_language": lang},
            bot=bot, session=session, subscription_service=subscription_service, panel_service=object(),
            state=None, referral_service=None, promo_code_service=None,
        )
    edit = next(call.args[0] for call in bot.await_args_list if isinstance(call.args[0], EditMessageMedia))
    expected_key = "info_section_title" if source == "info" else "subscription_not_active"
    assert edit.message_id == rules_message.message_id
    assert edit.media.caption == i18n.gettext(lang, expected_key)


@pytest.mark.parametrize("lang,unit", [("ru", "ГБ"), ("en", "GB")])
async def test_period_buttons_show_configured_lte_for_each_available_duration(i18n, monkeypatch, lang, unit):
    settings = Settings(_env_file=None, BOT_TOKEN="test", DEVICE_PLANS_RUB="2:100,0,0,850",
                        STARS_ENABLED=True, DEVICE_PLANS_STARS="2:0,400,0,0",
                        DEVICE_PLANS_LTE_GB="60,125,360,0")
    callback = SimpleNamespace(data="subscribe_tier:2", message=SimpleNamespace(bot=None), answer=AsyncMock())
    edit = AsyncMock()
    monkeypatch.setattr(core, "edit_or_send_with_photo", edit)

    await core.select_device_tier_callback(
        callback, {"i18n_instance": i18n, "current_language": lang}, settings, AsyncMock()
    )

    rows = edit.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in rows[:-1]] == [
        "subscribe_period:2:1", "subscribe_period:2:3", "subscribe_period:2:12"
    ]
    assert [row[0].text.split(" · ")[-1] for row in rows[:-1]] == [
        f"60 {unit} LTE", f"125 {unit} LTE", f"0 {unit} LTE"
    ]


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("price,display", [(100.0, "100"), (100.5, "100.5"), (100.25, "100.25")])
def test_topup_prices_match_tariff_number_format(i18n, lang, price, display):
    markup = get_lte_packages_keyboard({25.0: price}, "RUB", lang, i18n)
    button = markup.inline_keyboard[0][0]
    assert button.text == i18n.gettext(lang, "buy_traffic_package_button", traffic_gb="25",
                                      price=display, currency_symbol="RUB")
    assert button.callback_data == "lte_package:25"
    assert button.icon_custom_emoji_id


@pytest.mark.parametrize("rub,stars,tiers", [
    (None, None, []), ("2:100,0,0,0", None, [2]),
    (None, "5:200,0,0,0", [5]), ("2:100,0,0,0", "5:200,0,0,0", [2, 5]),
])
async def test_subscription_selection_uses_only_configured_device_tiers(
        flow, i18n, monkeypatch, rub, stars, tiers):
    settings = Settings(_env_file=None, BOT_TOKEN="test", DEVICE_PLANS_RUB=rub,
                        DEVICE_PLANS_STARS=stars, STARS_ENABLED=True)
    bot = AsyncMock(spec=Bot)
    now = datetime.now(timezone.utc)
    event = Message(message_id=1, date=now, chat=Chat(id=1, type="private"),
                    from_user=TelegramUser(id=1, is_bot=False, first_name="Test"), text="/subscribe").as_(bot)
    edit = AsyncMock()
    monkeypatch.setattr(core, "edit_or_send_with_photo", edit)
    async with flow.factory() as session:
        await core.display_subscription_options(event, {"i18n_instance": i18n}, settings, session)
    actions = [button.callback_data for row in edit.await_args.kwargs["reply_markup"].inline_keyboard
               for button in row]
    assert [action for action in actions if action.startswith("subscribe_tier:")] == [
        f"subscribe_tier:{tier}" for tier in tiers]
    assert not any(action.startswith("subscribe_period:") for action in actions)
    if not tiers:
        assert i18n.gettext("ru", "no_subscription_options_available") in edit.await_args.kwargs["caption"]
