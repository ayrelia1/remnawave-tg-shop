"""Top-up entry requires a current subscription; displayed prices keep precision."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, User as TelegramUser

from bot.handlers.user.subscription import core
from bot.keyboards.inline.user_keyboards import get_lte_packages_keyboard
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
async def test_my_subscription_shows_topup_only_for_current_subscription(
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
    if can_topup:
        assert next(button for button in buttons if button.callback_data == "lte:packages").icon_custom_emoji_id
        assert i18n.gettext("ru", "subscription_not_active") not in edit.await_args.kwargs["caption"]
    else:
        assert "main_action:subscribe" in actions


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
