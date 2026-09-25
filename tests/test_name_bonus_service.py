"""The name bonus must respect paid eligibility, cooldown and partial revocation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.services.name_bonus_service import NameBonusService, as_utc, matches_name
from bot.services.subscription_service import SubscriptionService
from bot.keyboards.inline.user_keyboards import get_main_menu_inline_keyboard
from db.models import Base, NameBonusClaim, Payment, Subscription, User


class FakeBot:
    def __init__(self, first_name="@mansurvpn_bot Alice"):
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


def test_name_must_start_with_prefix():
    assert matches_name("@mansurvpn_bot Alice")
    assert matches_name("@MANSURVPN_BOT Alice")
    assert not matches_name("Alice @mansurvpn_bot")
    assert not matches_name(None)


def test_main_menu_button_respects_switch():
    options = SimpleNamespace(
        NAME_BONUS_ENABLED=True,
        REFERRAL_ENABLED=False,
        SERVER_STATUS_URL=None,
        SUPPORT_LINK=None,
        REQUIRED_CHANNEL_LINK=None,
    )
    i18n = FakeI18n()
    enabled = get_main_menu_inline_keyboard("ru", i18n, options)
    assert any(button.callback_data == "name_bonus:claim" for row in enabled.inline_keyboard for button in row)
    options.NAME_BONUS_ENABLED = False
    disabled = get_main_menu_inline_keyboard("ru", i18n, options)
    assert not any(button.callback_data == "name_bonus:claim" for row in disabled.inline_keyboard for button in row)


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
