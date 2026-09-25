import asyncio
from types import SimpleNamespace

from bot.handlers.admin import common
from bot.keyboards.inline.admin_keyboards import get_stats_monitoring_keyboard


class FakeI18n:
    def gettext(self, lang, key, **kwargs):
        return key


class FakeCallback:
    def __init__(self):
        self.data = "admin_action:check_name_bonus"
        self.from_user = SimpleNamespace(id=777)
        self.message = SimpleNamespace()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text):
        self.messages.append((user_id, text))


async def _press_check_button(callback, bot, session_factory):
    await common.admin_panel_actions_callback_handler(
        callback=callback,
        state=None,
        settings=SimpleNamespace(DEFAULT_LANGUAGE="ru"),
        i18n_data={"current_language": "ru", "i18n_instance": FakeI18n()},
        bot=bot,
        panel_service=None,
        subscription_service=None,
        session=None,
        async_session_factory=session_factory,
    )


async def test_admin_can_run_bonus_check_and_duplicate_click_is_rejected(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    session_factory = object()
    checked_factories = []

    class FakeService:
        def __init__(self, *args):
            pass

        async def run_checks(self, factory):
            checked_factories.append(factory)
            started.set()
            await release.wait()

    monkeypatch.setattr(common, "NameBonusService", FakeService)
    monkeypatch.setattr(common, "_name_bonus_check_task", None)
    keyboard = get_stats_monitoring_keyboard(FakeI18n(), "ru")
    assert any(
        button.callback_data == "admin_action:check_name_bonus"
        for row in keyboard.inline_keyboard for button in row
    )

    bot = FakeBot()
    first = FakeCallback()
    second = FakeCallback()
    await _press_check_button(first, bot, session_factory)
    await started.wait()
    await _press_check_button(second, bot, session_factory)
    assert first.answers == [("admin_name_bonus_check_started", {"show_alert": True})]
    assert second.answers == [("admin_name_bonus_check_running", {"show_alert": True})]
    assert checked_factories == [session_factory]

    release.set()
    await common._name_bonus_check_task
    assert bot.messages == [(777, "admin_name_bonus_check_finished")]


async def test_admin_receives_failure_when_manual_bonus_check_crashes(monkeypatch):
    class FailingService:
        def __init__(self, *args):
            pass

        async def run_checks(self, factory):
            raise RuntimeError("check failed")

    monkeypatch.setattr(common, "NameBonusService", FailingService)
    monkeypatch.setattr(common, "_name_bonus_check_task", None)
    bot = FakeBot()
    await _press_check_button(FakeCallback(), bot, object())
    await common._name_bonus_check_task
    assert bot.messages == [(777, "admin_name_bonus_check_failed")]
