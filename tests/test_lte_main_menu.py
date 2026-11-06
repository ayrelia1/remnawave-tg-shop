"""The home screen keeps subscription status and devices without an LTE balance."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, User as TelegramUser

from bot.handlers.user import start
from bot.middlewares.i18n import JsonI18n
from bot.services.panel_api_service import PanelUnavailableError
from config.settings import Settings
from db.models import LteAccount
from test_lte_service import flow as lte_flow_fixture

flow = lte_flow_fixture


@pytest.mark.parametrize("lang,premium", [("ru", True), ("en", True), ("ru", False), ("en", False)])
@pytest.mark.parametrize("remaining_gb", [0, 0.5, 47.5, 10])
async def test_main_menu_keeps_subscription_info_without_lte_balance(
        flow, monkeypatch, lang, premium, remaining_gb):
    caption = await render_menu(flow, monkeypatch, lang=lang, premium=premium,
                                remaining_gb=remaining_gb)
    assert "LTE" not in caption
    assert ("<tg-emoji " in caption.splitlines()[-1]) == premium
    assert "::" not in caption
    assert "0/5" in caption


@pytest.mark.parametrize("scenario", ["legacy", "missing_account", "paused", "no_subscription", "panel_error"])
async def test_main_menu_does_not_show_a_misleading_lte_balance(flow, monkeypatch, scenario):
    caption = await render_menu(flow, monkeypatch, scenario=scenario)
    assert "LTE-трафик:" not in caption
    assert "Привет" in caption
    if scenario == "panel_error":
        assert "работы" in caption.lower()


async def render_menu(flow, monkeypatch, *, lang="ru", premium=False, remaining_gb=10, scenario="managed"):
    now = datetime.now(timezone.utc)
    settings = Settings(_env_file=None, BOT_TOKEN="test", LTE_ENFORCEMENT_ENABLED=scenario != "paused",
                        LTE_SQUAD_UUIDS="11111111-1111-1111-1111-111111111111")
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=premium)
    details = None if scenario == "no_subscription" else {
        "end_date": now + timedelta(days=7), "status_from_panel": "ACTIVE", "max_devices": 5,
        "user_id": "42", "config_link": "https://example.test/sub/short",
    }
    subscription = SimpleNamespace(get_active_subscription_details=AsyncMock(return_value=details),
                                   has_had_any_subscription=AsyncMock(return_value=True))
    if scenario == "panel_error":
        subscription.get_active_subscription_details.side_effect = PanelUnavailableError("unavailable")
    panel = SimpleNamespace(get_user_devices=AsyncMock(return_value=[]))
    bot = AsyncMock(spec=Bot)
    event = Message(message_id=1, date=now, chat=Chat(id=1, type="private"),
                    from_user=TelegramUser(id=1, is_bot=False, first_name="Test"), text="/start").as_(bot)
    edit = AsyncMock()
    monkeypatch.setattr(start, "edit_or_send_with_photo", edit)
    async with flow.factory() as session:
        if scenario != "missing_account":
            session.add(LteAccount(user_id=1, managed=scenario != "legacy",
                                   credited_bytes=int((remaining_gb + 3) * 2**30), used_bytes=3 * 2**30))
            await session.commit()
        await start.send_main_menu(event, settings, {"i18n_instance": i18n, "current_language": lang},
                                   subscription, session, panel_service=panel)
    return edit.await_args.kwargs["caption"]
