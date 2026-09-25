"""The name bonus must respect paid eligibility, cooldown and partial revocation."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.services.name_bonus_service import ClaimResult, NameBonusService, as_utc, matches_name
from bot.services.notification_service import NotificationService
from bot.handlers.user.name_bonus import claim_name_bonus, format_moscow_time
from bot.handlers.user import tasks as tasks_handler
from bot.handlers.admin import statistics as admin_statistics
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.keyboards.inline.user_keyboards import (
    get_main_menu_inline_keyboard,
    get_name_bonus_task_keyboard,
    get_tasks_keyboard,
)
from db.dal.name_bonus_dal import get_bonus_statistics
from db.models import Base, NameBonusClaim, Payment, Subscription, User


class FakeBot:
    def __init__(self, first_name="Alice @MansurVpn_bot"):
        self.first_name = first_name
        self.messages = []

    async def get_chat(self, user_id):
        return SimpleNamespace(first_name=self.first_name)

    async def send_message(self, user_id, text):
        self.messages.append((user_id, text))


class FakePanel:
    def __init__(self):
        self.updates = []
        self.fail_once = False

    async def update_user_details_on_panel(self, panel_user_uuid, payload):
        self.updates.append((panel_user_uuid, payload))
        if self.fail_once:
            self.fail_once = False
            return None
        return {"id": panel_user_uuid}


class FakeI18n:
    def gettext(self, lang, key, **kwargs):
        return key


@pytest.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[User.__table__, Payment.__table__, Subscription.__table__, NameBonusClaim.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def make_service(bot=None, panel=None):
    bot = bot or FakeBot()
    panel = panel or FakePanel()
    settings = SimpleNamespace(
        NAME_BONUS_ENABLED=True,
        user_traffic_limit_bytes=0,
        base_device_limit=1,
        USER_TRAFFIC_STRATEGY="NO_RESET",
        DEFAULT_LANGUAGE="ru",
    )
    return NameBonusService(settings, bot, panel, FakeI18n())


async def seed_user(session, *, payment=True, active=True):
    session.add(User(user_id=123, panel_user_uuid="42", language_code="ru"))
    if payment:
        session.add(Payment(user_id=123, provider="test", amount=100, currency="RUB", status="succeeded"))
    if active:
        session.add(Subscription(
            user_id=123,
            panel_user_uuid="42",
            panel_subscription_uuid="short-42",
            start_date=datetime.now(timezone.utc) - timedelta(days=10),
            end_date=datetime.now(timezone.utc) + timedelta(days=20),
            duration_months=1,
            is_active=True,
        ))
    await session.commit()


@pytest.mark.parametrize("first_name", [
    "@mansurvpn_bot Alice",
    "Alice @mansurvpn_bot",
    "Alice @MansurVpn_bot",
    "Alice @MANSURVPN_BOT Bob",
])
def test_name_accepts_tag_anywhere_and_in_any_case(first_name):
    assert matches_name(first_name)


@pytest.mark.parametrize("first_name", [
    None, "", "Alice", "Mansur VPN", "MansurVPN", "mansurvpn_bot", "@mansurvpn",
])
def test_name_rejects_missing_tag(first_name):
    assert not matches_name(first_name)


def test_main_menu_button_respects_switch():
    options = SimpleNamespace(
        NAME_BONUS_ENABLED=True,
        REFERRAL_ENABLED=False,
        SERVER_STATUS_URL=None,
        SUPPORT_LINK=None,
        REQUIRED_CHANNEL_LINK="https://t.me/example",
    )
    i18n = FakeI18n()
    enabled = get_main_menu_inline_keyboard("ru", i18n, options)
    callbacks = [button.callback_data for row in enabled.inline_keyboard for button in row]
    subscription_index = callbacks.index("main_action:my_subscription")
    assert callbacks[subscription_index + 1] == "tasks:menu"
    assert "name_bonus:claim" not in callbacks
    assert enabled.inline_keyboard[-1][0].text == "menu_channel_subscribe_button"
    options.NAME_BONUS_ENABLED = False
    disabled = get_main_menu_inline_keyboard("ru", i18n, options)
    assert not any(button.callback_data == "tasks:menu" for row in disabled.inline_keyboard for button in row)


def test_task_keyboards_and_labels():
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), default="ru")
    assert i18n.gettext("ru", "tasks_button") == "Задания"
    assert i18n.gettext("en", "tasks_button") == "Tasks"
    task_list = get_tasks_keyboard("ru", i18n)
    task_detail = get_name_bonus_task_keyboard("ru", i18n)
    assert task_list.inline_keyboard[0][0].text == "5 дней за имя Telegram"
    assert task_list.inline_keyboard[0][0].callback_data == "tasks:name_bonus"
    assert task_list.inline_keyboard[1][0].callback_data == "main_action:back_to_main"
    assert task_detail.inline_keyboard[0][0].callback_data == "name_bonus:claim"
    assert task_detail.inline_keyboard[1][0].callback_data == "tasks:menu"
    assert "@MansurVPN_bot" in i18n.gettext("ru", "name_bonus_task_description")


@pytest.mark.asyncio
async def test_task_screens_show_premium_emoji_and_respect_switch(monkeypatch):
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), default="ru")
    edits = []
    answers = []

    async def capture_edit(message, text, reply_markup):
        edits.append((text, reply_markup))

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    monkeypatch.setattr(tasks_handler, "safe_edit_text", capture_edit)
    callback = SimpleNamespace(message=object(), answer=answer)
    settings = SimpleNamespace(DEFAULT_LANGUAGE="ru", NAME_BONUS_ENABLED=True)
    i18n_data = {"i18n_instance": i18n, "current_language": "ru"}

    await tasks_handler.show_tasks(callback, settings, i18n_data)
    await tasks_handler.show_name_bonus_task(callback, settings, i18n_data)
    assert len(edits) == 2
    assert all("<tg-emoji" in text for text, _ in edits)
    assert edits[0][1].inline_keyboard[0][0].callback_data == "tasks:name_bonus"
    assert edits[1][1].inline_keyboard[0][0].callback_data == "name_bonus:claim"
    assert "каждые 30 минут" not in edits[1][0]

    settings.NAME_BONUS_ENABLED = False
    await tasks_handler.show_tasks(callback, settings, i18n_data)
    assert len(edits) == 2
    assert answers[-1][1] is True
    assert answers[-1][0].startswith("⛔")


def test_bonus_timestamps_use_moscow_time():
    assert format_moscow_time(datetime(2026, 10, 1, tzinfo=timezone.utc)) == "01.10.2026 03:00 МСК"


def test_bonus_callback_alerts_use_plain_text():
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), default="ru")
    alert_keys = (
        "name_bonus_claimed_alert", "name_bonus_disabled", "name_bonus_purchase_required",
        "name_bonus_name_required", "name_bonus_telegram_error", "name_bonus_panel_error",
        "name_bonus_cooldown",
    )
    for language in ("ru", "en"):
        for key in alert_keys:
            alert = i18n.gettext(language, key)
            assert "<" not in alert
            assert not alert[0].isalpha()


@pytest.mark.asyncio
async def test_paid_user_can_claim_once_then_wait_30_days(db_session_factory):
    service = make_service()
    async with db_session_factory() as session:
        await seed_user(session)
        before = (await session.execute(select(Subscription))).scalar_one().end_date
        first = await service.claim(session, 123)
        second = await service.claim(session, 123)
        claims = (await session.execute(select(NameBonusClaim))).scalars().all()
        after = (await session.execute(select(Subscription))).scalar_one().end_date

    assert first.status == "granted" and first.panel_synced
    assert second.status == "cooldown" and second.next_at > datetime.now(timezone.utc)
    assert len(claims) == 1
    assert abs(((after - before) - timedelta(days=5)).total_seconds()) < 1
    assert service.panel_service.updates[-1][1]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_changing_tag_case_and_position_does_not_revoke_bonus(db_session_factory):
    bot = FakeBot("Alice @mansurvpn_bot")
    service = make_service(bot=bot)
    async with db_session_factory() as session:
        await seed_user(session)
        result = await service.claim(session, 123)
        assert result.status == "granted"

    bot.first_name = "@MANSURVPN_BOT Alice"
    await service.run_checks(db_session_factory)

    async with db_session_factory() as session:
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
    assert claim.status == "active"
    assert not bot.messages


@pytest.mark.asyncio
async def test_prior_purchase_can_open_bonus_access_after_expiry(db_session_factory, monkeypatch):
    async def panel_link(self, session, user_id, user):
        return "42", "short-42", None, False

    monkeypatch.setattr(SubscriptionService, "_get_or_create_panel_user_link_details", panel_link)
    service = make_service()
    async with db_session_factory() as session:
        await seed_user(session, active=False)
        result = await service.claim(session, 123)
        subscription = (await session.execute(select(Subscription))).scalar_one()

    assert result.status == "granted"
    assert subscription.duration_months == 0
    assert subscription.is_active
    assert as_utc(subscription.end_date) > datetime.now(timezone.utc) + timedelta(days=4)


@pytest.mark.asyncio
async def test_relinked_active_subscription_keeps_paid_time(db_session_factory, monkeypatch):
    async def panel_link(self, session, user_id, user):
        user.panel_user_uuid = "42"
        return "42", "short-42", None, False

    monkeypatch.setattr(SubscriptionService, "_get_or_create_panel_user_link_details", panel_link)
    service = make_service()
    async with db_session_factory() as session:
        await seed_user(session)
        user = await session.get(User, 123)
        user.panel_user_uuid = "old-ref"
        subscription = (await session.execute(select(Subscription))).scalar_one()
        before = as_utc(subscription.end_date)
        await session.commit()
        result = await service.claim(session, 123)
        subscription = (await session.execute(select(Subscription))).scalar_one()

    assert result.status == "granted"
    assert subscription.duration_months == 1
    assert abs(((as_utc(subscription.end_date) - before) - timedelta(days=5)).total_seconds()) < 1


@pytest.mark.asyncio
async def test_no_purchase_does_not_grant(db_session_factory):
    service = make_service()
    async with db_session_factory() as session:
        await seed_user(session, payment=False)
        result = await service.claim(session, 123)
        claims = (await session.execute(select(NameBonusClaim))).scalars().all()
    assert result.status == "not_paid"
    assert not claims


@pytest.mark.asyncio
async def test_removed_name_reclaims_only_unelapsed_bonus(db_session_factory):
    bot = FakeBot()
    service = make_service(bot=bot)
    async with db_session_factory() as session:
        await seed_user(session)
        result = await service.claim(session, 123)
        assert result.status == "granted"
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
        before = (await session.execute(select(Subscription))).scalar_one().end_date
        claim.granted_at = datetime.now(timezone.utc) - timedelta(days=3)
        claim.monitor_until = datetime.now(timezone.utc) + timedelta(days=2)
        await session.commit()

    bot.first_name = "Alice"
    await service.run_checks(db_session_factory)

    async with db_session_factory() as session:
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
        after = (await session.execute(select(Subscription))).scalar_one().end_date
    assert claim.status == "revoked"
    assert abs(((before - after) - timedelta(days=2)).total_seconds()) < 2
    assert abs(claim.reclaimed_seconds - 2 * 86400) < 2
    assert not claim.notification_pending
    assert len(bot.messages) == 1
    assert len(service.panel_service.updates) == 2
    panel_expiry = datetime.fromisoformat(
        service.panel_service.updates[-1][1]["expireAt"].replace("Z", "+00:00")
    )
    assert abs((panel_expiry - as_utc(after)).total_seconds()) < 0.01

    bot.first_name = "@mansurvpn_bot Alice"
    async with db_session_factory() as session:
        again = await service.claim(session, 123)
    assert again.status == "cooldown"

    await service.run_checks(db_session_factory)
    assert len(bot.messages) == 1


@pytest.mark.asyncio
async def test_failed_panel_update_is_retried(db_session_factory):
    panel = FakePanel()
    panel.fail_once = True
    service = make_service(panel=panel)
    async with db_session_factory() as session:
        await seed_user(session)
        result = await service.claim(session, 123)
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
        assert claim.panel_sync_pending
    assert result.status == "granted" and not result.panel_synced

    await service.run_checks(db_session_factory)
    async with db_session_factory() as session:
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
    assert not claim.panel_sync_pending
    assert len(panel.updates) == 2


@pytest.mark.asyncio
async def test_direct_revocation_retries_panel_before_notification(db_session_factory):
    bot = FakeBot()
    panel = FakePanel()
    service = make_service(bot=bot, panel=panel)
    async with db_session_factory() as session:
        await seed_user(session)
        await service.claim(session, 123)
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
        claim_id = claim.id
        panel.fail_once = True
        await service.revoke_remaining(session, claim_id)
        claim = await session.get(NameBonusClaim, claim_id)
        assert claim.panel_sync_pending
        assert claim.notification_pending
    assert len(panel.updates) == 2
    assert not bot.messages

    await service.run_checks(db_session_factory)
    async with db_session_factory() as session:
        claim = await session.get(NameBonusClaim, claim_id)
        subscription = (await session.execute(select(Subscription))).scalar_one()
    assert not claim.panel_sync_pending
    assert not claim.notification_pending
    assert len(bot.messages) == 1
    panel_expiry = datetime.fromisoformat(panel.updates[-1][1]["expireAt"].replace("Z", "+00:00"))
    assert abs((panel_expiry - as_utc(subscription.end_date)).total_seconds()) < 0.01


@pytest.mark.asyncio
async def test_name_change_after_monitoring_window_keeps_bonus(db_session_factory):
    bot = FakeBot()
    service = make_service(bot=bot)
    async with db_session_factory() as session:
        await seed_user(session)
        await service.claim(session, 123)
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
        claim.monitor_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    bot.first_name = "Alice"
    await service.run_checks(db_session_factory)
    async with db_session_factory() as session:
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
    assert claim.status == "completed"
    assert not bot.messages


@pytest.mark.asyncio
async def test_missing_name_in_telegram_response_does_not_revoke(db_session_factory):
    bot = FakeBot()
    service = make_service(bot=bot)
    async with db_session_factory() as session:
        await seed_user(session)
        await service.claim(session, 123)
    bot.first_name = None
    await service.run_checks(db_session_factory)
    async with db_session_factory() as session:
        claim = (await session.execute(select(NameBonusClaim))).scalar_one()
    assert claim.status == "active"
    assert not bot.messages


@pytest.mark.asyncio
async def test_bonus_statistics_count_claims_and_reclaimed_time(db_session_factory):
    now = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        empty = await get_bonus_statistics(session, now)
        session.add_all([
            NameBonusClaim(
                user_id=123, subscription_id=1, granted_at=now,
                monitor_until=now + timedelta(days=5), bonus_days=5,
                status="active", reclaimed_seconds=0,
            ),
            NameBonusClaim(
                user_id=123, subscription_id=1, granted_at=now - timedelta(days=40),
                monitor_until=now - timedelta(days=35), bonus_days=5,
                status="revoked", reclaimed_seconds=2 * 86400,
            ),
            NameBonusClaim(
                user_id=456, subscription_id=2, granted_at=now - timedelta(days=50),
                monitor_until=now - timedelta(days=45), bonus_days=5,
                status="completed", reclaimed_seconds=0,
            ),
        ])
        await session.commit()
        stats = await get_bonus_statistics(session, now)

    assert empty == {
        "total_claims": 0, "users": 0, "monitoring": 0,
        "revoked": 0, "granted_days": 0, "reclaimed_seconds": 0,
    }
    assert stats == {
        "total_claims": 3, "users": 2, "monitoring": 1,
        "revoked": 1, "granted_days": 15, "reclaimed_seconds": 2 * 86400,
    }


@pytest.mark.asyncio
async def test_claim_sends_admin_notice_in_users_topic(monkeypatch):
    async def fake_claim(self, session, user_id):
        return ClaimResult("granted", end_date=datetime(2026, 10, 1, tzinfo=timezone.utc), panel_synced=True)

    notices = []
    answers = []

    async def capture_notice(self, message, thread_id=None, reply_markup=None):
        notices.append((message, thread_id, reply_markup))

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    monkeypatch.setattr(NameBonusService, "claim", fake_claim)
    monkeypatch.setattr(NotificationService, "_send_to_log_channel", capture_notice)
    settings = SimpleNamespace(
        DEFAULT_LANGUAGE="ru", LOG_THREAD_ID_USERS=88, LOG_THREAD_ID=None,
        LOG_THREAD_ID_PURCHASES=None, LOG_THREAD_ID_STATUSES=None,
        LOG_THREAD_ID_BACKUPS=None,
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123, username="alice"), answer=answer,
    )
    bot = FakeBot()
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), default="ru")
    await claim_name_bonus(
        callback, settings, {"i18n_instance": i18n, "current_language": "ru"},
        bot, FakePanel(), None,
    )

    assert len(notices) == 1
    assert "Получен бонус за имя" in notices[0][0]
    assert "alice" in notices[0][0]
    assert "5 дн." in notices[0][0]
    assert notices[0][1] == 88
    assert notices[0][2].inline_keyboard[0][0].url == "tg://user?id=123"
    assert "2026-10-01 00:00 UTC" in notices[0][0]
    assert "01.10.2026 03:00 МСК" in bot.messages[0][1]
    assert "<tg-emoji" in bot.messages[0][1]
    assert "каждые 30 минут" not in bot.messages[0][1]
    assert answers[0][0].startswith("✅")

    async def cooldown_claim(self, session, user_id):
        return ClaimResult("cooldown", next_at=datetime(2026, 10, 1, tzinfo=timezone.utc))

    monkeypatch.setattr(NameBonusService, "claim", cooldown_claim)
    await claim_name_bonus(
        callback, settings, {"i18n_instance": i18n, "current_language": "ru"},
        bot, FakePanel(), None,
    )
    assert "01.10.2026 03:00 МСК" in answers[-1][0]
    assert answers[-1][1] is True
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_admin_statistics_displays_bonus_totals(db_session_factory, monkeypatch):
    async def user_stats(session):
        return {
            "total_users": 2, "paid_subscriptions": 1, "trial_users": 0,
            "inactive_users": 1, "banned_users": 0, "referral_users": 0,
        }

    async def financial_stats(session):
        return {
            "today_revenue": 0, "today_payments_count": 0,
            "week_revenue": 0, "month_revenue": 0, "all_time_revenue": 0,
        }

    async def empty_result(session, *args, **kwargs):
        return None

    async def empty_payments(session, *args, **kwargs):
        return []

    class EmptyPanel:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_system_stats(self):
            return None

        async def get_bandwidth_stats(self):
            return None

        async def get_nodes_statistics(self):
            return None

    monkeypatch.setattr(admin_statistics.user_dal, "get_enhanced_user_statistics", user_stats)
    monkeypatch.setattr(admin_statistics.payment_dal, "get_financial_statistics", financial_stats)
    monkeypatch.setattr(admin_statistics.payment_dal, "get_recent_payment_logs_with_user", empty_payments)
    monkeypatch.setattr(admin_statistics.panel_sync_dal, "get_panel_sync_status", empty_result)
    monkeypatch.setattr(admin_statistics, "PanelApiService", EmptyPanel)
    monkeypatch.setattr(admin_statistics, "get_back_to_admin_panel_keyboard", lambda *args: None)

    sent = []

    async def edit_text(text, **kwargs):
        sent.append(text)

    async def answer(text=None, show_alert=False):
        pass

    callback = SimpleNamespace(message=SimpleNamespace(edit_text=edit_text), answer=answer)
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), default="ru")
    now = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        session.add(NameBonusClaim(
            user_id=123, subscription_id=1, granted_at=now,
            monitor_until=now + timedelta(days=5), bonus_days=5,
            status="active", reclaimed_seconds=0,
        ))
        await session.commit()
        await admin_statistics.show_statistics_handler(
            callback,
            {"i18n_instance": i18n, "current_language": "ru"},
            SimpleNamespace(DEFAULT_LANGUAGE="ru", device_plans_active=False),
            session,
        )

    assert len(sent) == 1
    assert "Бонус за имя" in sent[0]
    assert "Выдач: <b>1</b>" in sent[0]
    assert "Сейчас на проверке: <b>1</b>" in sent[0]
