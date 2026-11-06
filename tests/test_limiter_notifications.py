"""Limiter admin alerts reuse the existing log chat, topics and profile buttons."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.middlewares.i18n import JsonI18n
from bot.services.notification_service import NotificationService
from config.settings import Settings


@pytest.fixture
def notifications(monkeypatch):
    settings = SimpleNamespace(
        DEFAULT_LANGUAGE="ru", LOG_CHAT_ID=-1001234567890, LOG_THREAD_ID=77,
        LOG_THREAD_ID_USERS=None, LOG_THREAD_ID_PURCHASES=None,
        LOG_THREAD_ID_STATUSES=None, LOG_THREAD_ID_BACKUPS=None,
        LIMITER_CHAT_THREAD_ID=15296,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr("bot.services.notification_service.get_queue_manager", lambda: None)
    return NotificationService(bot, settings, JsonI18n("locales", default="ru"))


@pytest.mark.parametrize("number", [1, 2, 3])
async def test_admin_alert_has_action_count_ids_and_profile_button(notifications, number):
    await notifications.notify_limiter_action(
        user_id=123, panel_user_ref="42", warning_number=number,
        device_limit=2, device_count=4, ip_count=7, grouping_mode="asn",
        username="example_user", first_name="<Example>",
    )
    sent = notifications.bot.send_message.await_args.kwargs
    assert sent["chat_id"] == notifications.settings.LOG_CHAT_ID
    assert sent["message_thread_id"] == 15296
    assert f"{number}/3" in sent["text"]
    assert "<code>123</code>" in sent["text"] and "<code>42</code>" in sent["text"]
    assert "example_user" in sent["text"] and "&lt;Example&gt;" in sent["text"]
    assert "<tg-emoji" in sent["text"] and "::" not in sent["text"]
    action = "Пользователь отключён в панели" if number == 3 else "Подписка сброшена"
    assert action in sent["text"]
    button = sent["reply_markup"].inline_keyboard[0][0]
    assert button.url == "tg://user?id=123" and button.icon_custom_emoji_id


async def test_admin_alert_uses_existing_queue_and_thread_fallback(notifications, monkeypatch):
    queue = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr("bot.services.notification_service.get_queue_manager", lambda: queue)
    notifications.settings.LIMITER_CHAT_THREAD_ID = None
    await notifications.notify_limiter_action(
        user_id=123, panel_user_ref="42", warning_number=1,
        device_limit=2, device_count=4, ip_count=4, grouping_mode="ip",
    )
    assert queue.send_message.await_args.args == (notifications.settings.LOG_CHAT_ID,)
    assert queue.send_message.await_args.kwargs["message_thread_id"] == 77
    notifications.bot.send_message.assert_not_awaited()


async def test_admin_alert_without_log_chat_does_not_send(notifications):
    notifications.settings.LOG_CHAT_ID = None
    await notifications.notify_limiter_action(
        user_id=123, panel_user_ref="42", warning_number=3,
        device_limit=2, device_count=4, ip_count=4, grouping_mode="ip",
    )
    notifications.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("value,expected", [("15296", 15296), ("", None)])
def test_limiter_topic_environment_value(value, expected):
    settings = Settings(_env_file=None, BOT_TOKEN="test", LIMITER_CHAT_THREAD_ID=value)
    assert settings.LIMITER_CHAT_THREAD_ID == expected
