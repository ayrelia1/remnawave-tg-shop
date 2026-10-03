"""Only a new user's first trial-eligible welcome has a shortened menu."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.handlers.user import start as start_handlers
from bot.keyboards.inline.user_keyboards import (
    get_channel_subscription_keyboard,
    get_main_menu_inline_keyboard,
)
from bot.middlewares.channel_subscription import ChannelSubscriptionMiddleware
from bot.middlewares.i18n import JsonI18n
from bot.services.notification_service import NotificationService
from db.dal import user_dal


WELCOME_CALLBACK = "channel_subscription:verify:welcome:-100123"
WELCOME_ACTIONS = ["main_action:request_trial", "main_action:subscribe"]


def _actions(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _db_user():
    return SimpleNamespace(
        user_id=111,
        username="alice",
        first_name="Alice",
        last_name=None,
        language_code="ru",
        referred_by_id=None,
        channel_subscription_verified=False,
        channel_subscription_verified_for=None,
    )


@pytest.fixture
def welcome_env(monkeypatch):
    settings = SimpleNamespace(
        DEFAULT_LANGUAGE="ru",
        TRIAL_ENABLED=True,
        TRIAL_DURATION_DAYS=3,
        SUBSCRIPTION_MINI_APP_URL=None,
        NAME_BONUS_ENABLED=True,
        REFERRAL_ENABLED=True,
        SERVER_STATUS_URL="https://status.example.com",
        SUPPORT_LINK="https://t.me/support",
        REQUIRED_CHANNEL_SUBSCRIBE_TO_USE=True,
        REQUIRED_CHANNEL_ID=-100123,
        REQUIRED_CHANNEL_LINK="https://t.me/channel",
        ADMIN_IDS=[],
    )
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), default="ru")
    bot = AsyncMock(spec=Bot)
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    sender = User(id=111, is_bot=False, first_name="Alice", username="alice")
    message = Message(
        message_id=1, date=0, chat=Chat(id=111, type="private"), from_user=sender, text="/start"
    ).as_(bot)
    env = {
        "user": None,
        "bot": bot,
        "message": message,
        "sender": sender,
        "state": SimpleNamespace(clear=AsyncMock()),
        "menu": AsyncMock(),
        "edit_prompt": AsyncMock(),
        "dependencies": {
            "settings": settings,
            "i18n_data": {"current_language": "ru", "i18n_instance": i18n},
            "subscription_service": SimpleNamespace(
                has_had_any_subscription=AsyncMock(return_value=False),
                get_active_subscription_details=AsyncMock(return_value=None),
            ),
            "session": AsyncMock(),
        },
    }

    async def get_user(session, user_id):
        return env["user"]

    async def create_user(session, user_data):
        env["user"] = _db_user()
        return env["user"], True

    async def update_user(session, user_id, updates):
        for key, value in updates.items():
            setattr(env["user"], key, value)
        return env["user"]

    monkeypatch.setattr(user_dal, "get_user_by_id", get_user)
    monkeypatch.setattr(user_dal, "create_user", create_user)
    monkeypatch.setattr(user_dal, "update_user", update_user)
    monkeypatch.setattr(NotificationService, "notify_new_user_registration", AsyncMock())
    monkeypatch.setattr(start_handlers, "edit_or_send_with_photo", env["menu"])
    monkeypatch.setattr(start_handlers, "safe_edit_text", env["edit_prompt"])
    return env


def _callback(env, data):
    return CallbackQuery(
        id="check-1", from_user=env["sender"], chat_instance="chat-1", message=env["message"], data=data
    ).as_(env["bot"])


def _last_menu(env):
    return env["menu"].await_args.kwargs["reply_markup"]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_welcome_keyboard_only_has_trial_and_purchase(welcome_env, lang):
    settings = welcome_env["dependencies"]["settings"]
    i18n = welcome_env["dependencies"]["i18n_data"]["i18n_instance"]
    full = get_main_menu_inline_keyboard(lang, i18n, settings, show_trial_button=True)

    welcome = get_main_menu_inline_keyboard(
        lang, i18n, settings, show_trial_button=True, welcome_menu=True
    )

    assert _actions(welcome) == WELCOME_ACTIONS
    assert [len(row) for row in welcome.inline_keyboard] == [1, 1]
    assert welcome.inline_keyboard == full.inline_keyboard[:2]


@pytest.mark.parametrize("trial_enabled, show_trial", [(False, True), (True, False)])
def test_welcome_keyboard_falls_back_to_full_menu_without_trial(
    welcome_env, trial_enabled, show_trial
):
    settings = welcome_env["dependencies"]["settings"]
    settings.TRIAL_ENABLED = trial_enabled
    i18n = welcome_env["dependencies"]["i18n_data"]["i18n_instance"]

    full = get_main_menu_inline_keyboard("ru", i18n, settings, show_trial_button=show_trial)
    welcome = get_main_menu_inline_keyboard(
        "ru", i18n, settings, show_trial_button=show_trial, welcome_menu=True
    )

    assert welcome == full
    assert "main_action:my_subscription" in _actions(welcome)


def test_channel_keyboard_preserves_the_legacy_callback(welcome_env):
    i18n = welcome_env["dependencies"]["i18n_data"]["i18n_instance"]
    normal = get_channel_subscription_keyboard("ru", i18n, "https://t.me/channel")
    welcome = get_channel_subscription_keyboard(
        "ru", i18n, "https://t.me/channel", welcome_channel_id=-100123
    )

    assert normal.inline_keyboard[-1][0].callback_data == "channel_subscription:verify"
    assert welcome.inline_keyboard[-1][0].callback_data == WELCOME_CALLBACK
    assert len(WELCOME_CALLBACK.encode()) <= 64


@pytest.mark.parametrize("gate", ["disabled", "already_member", "verify"])
async def test_new_user_first_menu_is_short_and_next_start_is_full(welcome_env, gate):
    env = welcome_env
    settings = env["dependencies"]["settings"]
    if gate == "disabled":
        settings.REQUIRED_CHANNEL_SUBSCRIBE_TO_USE = False
    elif gate == "verify":
        env["bot"].get_chat_member.return_value = SimpleNamespace(status="left")

    await start_handlers.start_command_handler(
        env["message"], env["state"], **env["dependencies"]
    )

    if gate == "verify":
        env["menu"].assert_not_awaited()
        prompt = env["bot"].await_args.args[0]
        assert prompt.reply_markup.inline_keyboard[-1][0].callback_data == WELCOME_CALLBACK
        env["bot"].get_chat_member.return_value = SimpleNamespace(status="member")
        await start_handlers.verify_channel_subscription_callback(
            _callback(env, WELCOME_CALLBACK), **env["dependencies"]
        )

    assert _actions(_last_menu(env)) == WELCOME_ACTIONS
    first_caption = env["menu"].await_args.kwargs["caption"]

    await start_handlers.start_command_handler(
        env["message"], env["state"], **env["dependencies"]
    )

    assert "main_action:request_trial" in _actions(_last_menu(env))
    assert "main_action:my_subscription" in _actions(_last_menu(env))
    assert "tasks:menu" in _actions(_last_menu(env))
    assert env["menu"].await_args.kwargs["caption"] == first_caption


@pytest.mark.parametrize("event", ["start", "legacy_verify"])
async def test_existing_user_with_unused_trial_always_gets_full_menu(welcome_env, event):
    env = welcome_env
    env["user"] = _db_user()

    if event == "start":
        await start_handlers.start_command_handler(
            env["message"], env["state"], **env["dependencies"]
        )
    else:
        await start_handlers.verify_channel_subscription_callback(
            _callback(env, "channel_subscription:verify"), **env["dependencies"]
        )

    assert "main_action:request_trial" in _actions(_last_menu(env))
    assert "main_action:my_subscription" in _actions(_last_menu(env))


@pytest.mark.parametrize("action", ["back_to_main", "back_to_main_keep"])
async def test_returning_from_the_first_welcome_opens_the_full_menu(welcome_env, action):
    env = welcome_env
    await start_handlers.start_command_handler(
        env["message"], env["state"], **env["dependencies"]
    )
    assert _actions(_last_menu(env)) == WELCOME_ACTIONS

    await start_handlers.main_action_callback_handler(
        _callback(env, f"main_action:{action}"),
        state=env["state"],
        bot=env["bot"],
        referral_service=None,
        panel_service=None,
        promo_code_service=None,
        **env["dependencies"],
    )

    assert "main_action:request_trial" in _actions(_last_menu(env))
    assert "main_action:my_subscription" in _actions(_last_menu(env))


async def test_concurrent_registration_does_not_repeat_the_first_welcome(welcome_env, monkeypatch):
    env = welcome_env

    async def existing_user_created_by_another_start(session, user_data):
        env["user"] = _db_user()
        return env["user"], False

    monkeypatch.setattr(user_dal, "create_user", existing_user_created_by_another_start)

    await start_handlers.start_command_handler(
        env["message"], env["state"], **env["dependencies"]
    )

    assert "main_action:request_trial" in _actions(_last_menu(env))
    assert "main_action:my_subscription" in _actions(_last_menu(env))


@pytest.mark.parametrize("unavailable", ["disabled", "subscription_history"])
async def test_new_user_without_trial_gets_full_menu(welcome_env, unavailable):
    env = welcome_env
    if unavailable == "disabled":
        env["dependencies"]["settings"].TRIAL_ENABLED = False
    else:
        env["dependencies"]["subscription_service"].has_had_any_subscription.return_value = True

    await start_handlers.start_command_handler(
        env["message"], env["state"], **env["dependencies"]
    )

    assert "main_action:request_trial" not in _actions(_last_menu(env))
    assert "main_action:my_subscription" in _actions(_last_menu(env))


async def test_channel_check_retries_keep_welcome_but_verified_repeats_show_full_menu(welcome_env):
    env = welcome_env
    env["bot"].get_chat_member.return_value = SimpleNamespace(status="left")
    await start_handlers.start_command_handler(
        env["message"], env["state"], **env["dependencies"]
    )
    callback = _callback(env, WELCOME_CALLBACK)

    await start_handlers.verify_channel_subscription_callback(callback, **env["dependencies"])

    env["menu"].assert_not_awaited()
    prompt_keyboard = env["edit_prompt"].await_args.kwargs["reply_markup"]
    assert prompt_keyboard.inline_keyboard[-1][0].callback_data == WELCOME_CALLBACK

    env["bot"].get_chat_member.return_value = SimpleNamespace(status="member")
    await start_handlers.verify_channel_subscription_callback(callback, **env["dependencies"])
    assert _actions(_last_menu(env)) == WELCOME_ACTIONS
    assert env["user"].channel_subscription_verified is True

    await start_handlers.verify_channel_subscription_callback(callback, **env["dependencies"])
    assert "main_action:my_subscription" in _actions(_last_menu(env))


async def test_old_welcome_callback_does_not_restart_welcome_for_a_new_channel(welcome_env):
    env = welcome_env
    env["user"] = _db_user()
    env["dependencies"]["settings"].REQUIRED_CHANNEL_ID = -100456

    await start_handlers.verify_channel_subscription_callback(
        _callback(env, WELCOME_CALLBACK), **env["dependencies"]
    )

    assert "main_action:my_subscription" in _actions(_last_menu(env))


@pytest.mark.parametrize("data", ["channel_subscription:verify", WELCOME_CALLBACK])
async def test_channel_verification_callbacks_reach_the_registered_handler(welcome_env, data):
    callback = _callback(welcome_env, data)
    handler = next(
        handler for handler in start_handlers.router.callback_query.handlers
        if handler.callback is start_handlers.verify_channel_subscription_callback
    )

    matched, _ = await handler.check(callback)

    assert matched is True


@pytest.mark.parametrize("data, allowed", [(WELCOME_CALLBACK, True), ("main_action:subscribe", False)])
async def test_channel_gate_allows_welcome_verification_and_still_blocks_other_actions(
    welcome_env, data, allowed
):
    env = welcome_env
    env["user"] = _db_user()
    middleware = ChannelSubscriptionMiddleware(
        env["dependencies"]["settings"], env["dependencies"]["i18n_data"]["i18n_instance"]
    )
    handler = AsyncMock()
    update = Update(update_id=1, callback_query=_callback(env, data))

    await middleware(handler, update, {
        "event_from_user": env["sender"],
        "session": env["dependencies"]["session"],
        "i18n_data": env["dependencies"]["i18n_data"],
        "bot": env["bot"],
    })

    assert handler.await_count == int(allowed)
