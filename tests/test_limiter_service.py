"""Signed limiter inbox, subscription-scoped warnings and resumable actions."""

import hashlib
import hmac
import json
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import delete, event as sqlalchemy_event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.middlewares.i18n import JsonI18n
from bot.services.limiter_service import LimiterService, limiter_webhook_route
from bot.services.panel_api_service import PanelUnavailableError
from bot.services.subscription_service import SubscriptionService, subscription_reset_marker
from db.models import Base, LimiterViolation, LteAccount, Subscription, User

# The production web app uses string dependency keys; exercise that existing convention.
pytestmark = pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")


class FakePanel:
    def __init__(self):
        self.users = {}
        self.revokes = []
        self.clears = []
        self.disables = []
        self.unavailable = False
        self.fail_clear_once = False
        self.lose_revoke_response = False
        self.lose_disable_response = False

    async def get_user_by_ref(self, ref):
        if self.unavailable:
            raise PanelUnavailableError("test outage")
        return deepcopy(self.users.get(ref))

    async def revoke_user_subscription(self, ref):
        self.revokes.append(ref)
        short = f"rotated-{len(self.revokes)}-{ref}"
        self.users[ref].update(shortUuid=short, subscriptionUrl=f"https://panel.example/sub/{short}")
        if self.lose_revoke_response:
            self.lose_revoke_response = False
            raise PanelUnavailableError("lost response after successful rotation")
        return deepcopy(self.users[ref])

    async def delete_all_user_hwid_devices(self, ref):
        self.clears.append(ref)
        if self.fail_clear_once:
            self.fail_clear_once = False
            raise PanelUnavailableError("test cleanup outage")
        return True

    async def update_user_status_on_panel(self, ref, enable):
        self.disables.append((ref, enable))
        self.users[ref]["status"] = "ACTIVE" if enable else "DISABLED"
        if self.lose_disable_response:
            self.lose_disable_response = False
            raise PanelUnavailableError("lost disable response")
        return True


class FakeBot:
    def __init__(self):
        self.messages = []
        self.fail_once = False
        self.unavailable = False

    async def send_message(self, chat_id, text, **kwargs):
        if self.unavailable:
            raise RuntimeError("test Telegram outage")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("test Telegram outage")
        self.messages.append((chat_id, text, kwargs))


@pytest.fixture
async def setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(
        LIMITER_WEBHOOK_SECRET="test-limiter-secret",
        SUPPORT_LINK="https://t.me/example_support",
        DEFAULT_LANGUAGE="ru",
        SUBSCRIPTION_DOMAIN="https://sub.example",
        CRYPT4_ENABLED=False,
        CRYPT4_REDIRECT_URL=None,
    )
    panel, bot = FakePanel(), FakeBot()
    subscriptions = SubscriptionService(settings, panel, bot)
    service = LimiterService(settings, panel, subscriptions, bot, JsonI18n("locales", default="ru"))
    result = SimpleNamespace(factory=factory, settings=settings, panel=panel, bot=bot, service=service)
    try:
        yield result
    finally:
        await engine.dispose()


async def seed(setup, ref="42", user_id=123):
    now = datetime.now(timezone.utc)
    async with setup.factory() as session:
        if await session.get(User, user_id) is None:
            session.add(User(user_id=user_id, panel_user_uuid=ref, language_code="ru"))
            session.add(LteAccount(user_id=user_id, credited_bytes=100 * 2**30, used_bytes=20 * 2**30))
            await session.flush()
        sub = Subscription(
            user_id=user_id, panel_user_uuid=ref, panel_subscription_uuid=f"short-{ref}",
            end_date=now + timedelta(days=30), is_active=True, hwid_device_limit=2,
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.subscription_id
    setup.panel.users[ref] = {
        "id": int(ref), "telegramId": user_id, "shortUuid": f"short-{ref}",
        "subscriptionUrl": f"https://panel.example/sub/short-{ref}", "status": "ACTIVE",
        "expireAt": (now + timedelta(days=30)).isoformat(), "hwidDeviceLimit": 2,
    }
    return sub_id


def body(ref="42", user_id=123, when=None, **changes):
    value = {
        "event": "violation_detected", "action_mode": "manual",
        "user": {"user_id": int(ref), "telegram_id": user_id},
        "violation": {"ip_count": 4, "device_limit": 2, "effective_limit": 2,
                      "device_group_count": 4, "grouping_mode": "ip"},
        "timestamp": (when or datetime.now(timezone.utc)).isoformat(),
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def signed(setup, raw, timestamp=None):
    timestamp = str(timestamp if timestamp is not None else int(datetime.now(timezone.utc).timestamp()))
    signature = hmac.new(setup.settings.LIMITER_WEBHOOK_SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Timestamp": timestamp, "X-Signature-V2": "sha256=" + signature}


@pytest.fixture
async def client(setup):
    app = web.Application()
    app["limiter_service"] = setup.service
    app["async_session_factory"] = setup.factory
    app.router.add_post("/webhook/limiter", limiter_webhook_route)
    async with TestClient(TestServer(app)) as client:
        yield client


async def enqueue(setup, when, ref="42", user_id=123):
    return await setup.service.enqueue(setup.factory, body(ref, user_id, when), received_at=when)


async def test_valid_signed_hook_processes_immediately_and_scheduler_does_not_repeat(setup, client):
    await seed(setup)
    raw = body()
    response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
    assert response.status == 200
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1
    async with setup.factory() as session:
        row = await session.scalar(select(LimiterViolation))
        assert row.panel_user_ref == "42" and row.stage == "done"
        assert row.subscription_id is not None
    response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
    assert response.status == 200 and (await response.json())["status"] == "duplicate"
    await setup.service.run_checks(setup.factory)
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1


@pytest.mark.parametrize("kind", ["missing", "bad", "stale", "future", "tampered"])
async def test_invalid_auth_never_records_strikes(setup, client, kind):
    await seed(setup)
    raw = body()
    headers = signed(setup, raw)
    if kind == "missing":
        headers.pop("X-Signature-V2")
    elif kind == "bad":
        headers["X-Signature-V2"] = "sha256=" + "0" * 64
    elif kind in {"stale", "future"}:
        offset = -3600 if kind == "stale" else 3600
        headers = signed(setup, raw, int(datetime.now(timezone.utc).timestamp()) + offset)
    else:
        raw += b" "
    response = await client.post("/webhook/limiter", data=raw, headers=headers)
    assert response.status == 403
    async with setup.factory() as session:
        assert await session.scalar(select(LimiterViolation)) is None


async def test_disabled_secret_and_malformed_json(setup, client):
    raw = b"{invalid"
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 400
    setup.settings.LIMITER_WEBHOOK_SECRET = None
    assert (await client.post("/webhook/limiter", data=raw)).status == 503


async def test_duplicate_delivery_only_resets_once(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    assert await enqueue(setup, now) == "accepted"
    assert await enqueue(setup, now) == "duplicate"
    await setup.service.run_checks(setup.factory, now=now)
    await setup.service.run_checks(setup.factory, now=now)
    assert setup.panel.revokes == ["42"]
    assert len(setup.bot.messages) == 1
    assert "1/3" in setup.bot.messages[0][1]
    buttons = setup.bot.messages[0][2]["reply_markup"].inline_keyboard
    assert buttons[0][0].url.startswith("https://sub.example/sub/rotated-")
    assert buttons[1][0].url == setup.settings.SUPPORT_LINK
    assert all(button.icon_custom_emoji_id for row in buttons for button in row)


async def test_third_hook_disables_only_its_subscription_without_user_ban(setup):
    sub_id = await seed(setup)
    other_id = await seed(setup, "43")
    original_expiry = setup.panel.users["42"]["expireAt"]
    now = datetime.now(timezone.utc)
    for n in range(3):
        when = now + timedelta(minutes=5 * n)
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    assert setup.panel.revokes == ["42", "42"]
    assert setup.panel.disables == [("42", False)]
    assert setup.panel.users["43"]["status"] == "ACTIVE"
    assert setup.panel.users["42"]["expireAt"] == original_expiry
    assert "3/3" in setup.bot.messages[-1][1]
    assert len(setup.bot.messages[-1][2]["reply_markup"].inline_keyboard) == 1
    async with setup.factory() as session:
        assert (await session.get(User, 123)).is_banned is False
        sub = await session.get(Subscription, sub_id)
        assert sub.status_from_panel == "DISABLED" and not sub.is_active
        assert (await session.get(Subscription, other_id)).is_active
        wallet = await session.get(LteAccount, 123)
        assert wallet.credited_bytes == 100 * 2**30 and wallet.used_bytes == 20 * 2**30


async def test_fixed_window_does_not_slide_with_second_warning(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    # Keep panel subscription live for all three observations.
    for when in (now, now + timedelta(hours=47), now + timedelta(hours=48)):
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    assert ["1/3" in message[1] for message in setup.bot.messages] == [True, False, True]
    assert not setup.panel.disables


async def test_distinct_panel_subscriptions_of_one_owner_have_separate_windows(setup):
    await seed(setup)
    await seed(setup, "43")
    now = datetime.now(timezone.utc)
    for ref in ("42", "43"):
        await enqueue(setup, now, ref)
    await setup.service.run_checks(setup.factory, now=now)
    assert len(setup.bot.messages) == 2
    assert all("1/3" in message[1] for message in setup.bot.messages)


@pytest.mark.parametrize("failure", ["clear", "lost_revoke", "telegram"])
async def test_partial_failure_retries_without_rotating_again(setup, failure):
    await seed(setup)
    setup.panel.fail_clear_once = failure == "clear"
    setup.panel.lose_revoke_response = failure == "lost_revoke"
    setup.bot.fail_once = failure == "telegram"
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    await setup.service.run_checks(setup.factory, now=now)
    assert not setup.bot.messages
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=2))
    assert setup.panel.revokes == ["42"]
    assert len(setup.bot.messages) == 1


async def test_outage_does_not_consume_warning_and_does_not_block_other_accounts(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    setup.panel.unavailable = True
    await setup.service.run_checks(setup.factory, now=now)
    async with setup.factory() as session:
        event = await session.scalar(select(LimiterViolation))
        assert event.warning_number is None and event.stage == "pending"
    setup.panel.unavailable = False
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=2))
    assert "1/3" in setup.bot.messages[0][1]


@pytest.mark.parametrize("kind", ["telegram_mismatch", "limit_changed", "disabled", "not_exceeded"])
async def test_stale_or_nonviolating_event_never_resets(setup, kind):
    await seed(setup)
    now = datetime.now(timezone.utc)
    if kind == "telegram_mismatch":
        assert await enqueue(setup, now, user_id=999) == "ignored"
    else:
        raw = json.loads(body(when=now))
        if kind == "limit_changed":
            setup.panel.users["42"]["hwidDeviceLimit"] = 5
        elif kind == "disabled":
            setup.panel.users["42"]["status"] = "DISABLED"
        else:
            raw["violation"]["device_group_count"] = 2
        await setup.service.enqueue(setup.factory, json.dumps(raw).encode(), received_at=now)
    await setup.service.run_checks(setup.factory, now=now)
    assert not setup.panel.revokes and not setup.bot.messages


async def test_expired_pending_event_does_not_punish_after_restart(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    await setup.service.run_checks(setup.factory, now=now + timedelta(days=3))
    assert not setup.panel.revokes and not setup.bot.messages


async def test_local_subscription_replacement_keeps_panel_warning_window(setup):
    sub_id = await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    await setup.service.run_checks(setup.factory, now=now)
    async with setup.factory() as session:
        # Simulate FK SET NULL when a local subscription is replaced.
        for event in (await session.scalars(select(LimiterViolation))).all():
            event.subscription_id = None
        await session.execute(delete(Subscription).where(Subscription.subscription_id == sub_id))
        session.add(Subscription(user_id=123, panel_user_uuid="42", panel_subscription_uuid="replacement",
                                 end_date=now + timedelta(days=30), is_active=True))
        await session.commit()
    await enqueue(setup, now + timedelta(minutes=5))
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=5))
    assert "2/3" in setup.bot.messages[-1][1]


@pytest.mark.parametrize("kind", ["duplicate_key", "nonfinite", "boolean_id", "fractional_count", "oversized"])
async def test_bad_payload_is_rejected_without_side_effects(setup, client, kind):
    await seed(setup)
    value = json.loads(body())
    if kind == "duplicate_key":
        raw = body().replace(b'"event":', b'"event":"ignored","event":', 1)
    elif kind == "nonfinite":
        value["unused"] = float("nan")
        raw = json.dumps(value).encode()
    elif kind == "oversized":
        value["unused"] = "x" * (257 * 1024)
        raw = json.dumps(value).encode()
    else:
        if kind == "boolean_id":
            value["user"]["user_id"] = True
        else:
            value["violation"]["device_group_count"] = 2.5
        raw = json.dumps(value).encode()
    response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
    assert response.status == (413 if kind == "oversized" else 400)
    assert not setup.panel.revokes and not setup.bot.messages


async def test_plaintext_secret_and_v1_signature_are_not_authentication(setup, client):
    await seed(setup)
    raw = body()
    headers = signed(setup, raw)
    headers.pop("X-Signature-V2")
    headers["X-Webhook-Secret"] = setup.settings.LIMITER_WEBHOOK_SECRET
    headers["X-Signature"] = "sha256=" + hmac.new(setup.settings.LIMITER_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert (await client.post("/webhook/limiter", data=raw, headers=headers)).status == 403


@pytest.mark.parametrize("changes", [{"event": "soft_warning"}, {"action_mode": "auto"}])
async def test_other_event_modes_are_ignored(setup, client, changes):
    await seed(setup)
    raw = body(**changes)
    response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
    assert response.status == 200 and (await response.json())["status"] == "ignored"


async def test_current_owner_mismatch_does_not_act_or_block_the_inbox(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    setup.panel.users["42"]["telegramId"] = 999
    await setup.service.run_checks(setup.factory, now=now)
    async with setup.factory() as session:
        event = await session.scalar(select(LimiterViolation))
        assert event.stage == "ignored"
    assert not setup.panel.revokes


async def test_queued_events_do_not_overtake_a_failed_reset(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    await enqueue(setup, now + timedelta(minutes=5))
    setup.panel.fail_clear_once = True
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=5))
    async with setup.factory() as session:
        events = list((await session.scalars(select(LimiterViolation).order_by(LimiterViolation.id))).all())
        assert events[0].stage == "resetting" and events[1].warning_number is None
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=7))
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=8))
    assert len(setup.panel.revokes) == 2
    assert "1/3" in setup.bot.messages[0][1] and "2/3" in setup.bot.messages[1][1]


async def test_disabled_account_is_not_notified_repeatedly_and_can_later_renew(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    for n in range(4):
        when = now + timedelta(minutes=5 * n)
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    assert len(setup.bot.messages) == 3
    # The approved policy allows a later purchase/bonus/operator action to reactivate.
    setup.panel.users["42"]["status"] = "ACTIVE"
    when = now + timedelta(minutes=20)
    await enqueue(setup, when)
    await setup.service.run_checks(setup.factory, now=when)
    assert setup.panel.users["42"]["status"] == "DISABLED"
    assert "3/3" in setup.bot.messages[-1][1]


@pytest.mark.parametrize("has_local_subscription", [True, False])
async def test_shared_manual_reset_preserves_best_effort_cleanup(setup, has_local_subscription):
    sub_id = await seed(setup)
    setup.panel.fail_clear_once = True
    async with setup.factory() as session:
        if not has_local_subscription:
            await session.execute(delete(Subscription).where(Subscription.subscription_id == sub_id))
            await session.commit()
        result = await setup.service.subscriptions.reset_subscription_keys(session, 123, "42")
        await session.commit()
    assert result["subscriptionUrl"].endswith("rotated-1-42")
    assert setup.panel.revokes == ["42"]


async def test_cancellation_after_remote_reset_is_resumable(setup, monkeypatch):
    await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    original = setup.panel.revoke_user_subscription

    async def cancel_after_revoke(ref):
        await original(ref)
        raise asyncio.CancelledError()

    monkeypatch.setattr(setup.panel, "revoke_user_subscription", cancel_after_revoke)
    with pytest.raises(asyncio.CancelledError):
        await setup.service.run_checks(setup.factory, now=now)
    monkeypatch.setattr(setup.panel, "revoke_user_subscription", original)
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=1))
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1


def test_reset_fingerprint_ignores_panel_origin_changes():
    first = {"shortUuid": "same-token", "subscriptionUrl": "https://old.example/sub/same-token"}
    second = first | {"subscriptionUrl": "https://new.example/sub/same-token"}
    assert subscription_reset_marker(first) == subscription_reset_marker(second)
    assert subscription_reset_marker({"subscriptionUrl": first["subscriptionUrl"]}) == subscription_reset_marker(
        {"subscriptionUrl": second["subscriptionUrl"]})


def test_secret_requires_support_link_and_empty_secret_is_disabled():
    from config.settings import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="SUPPORT_LINK"):
        Settings(_env_file=None, BOT_TOKEN="test", LIMITER_WEBHOOK_SECRET="test", SUPPORT_LINK=None)
    settings = Settings(_env_file=None, BOT_TOKEN="test", LIMITER_WEBHOOK_SECRET="", SUPPORT_LINK=None)
    assert settings.LIMITER_WEBHOOK_SECRET is None


async def test_third_received_before_deadline_counts_when_worker_runs_after_deadline(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    for when in (now, now + timedelta(hours=24)):
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    await enqueue(setup, now + timedelta(hours=48, seconds=-10))
    await setup.service.run_checks(setup.factory, now=now + timedelta(hours=48, seconds=30))
    assert setup.panel.disables == [("42", False)]
    assert "3/3" in setup.bot.messages[-1][1]


async def test_cleanup_keeps_window_history_required_by_an_unfinished_event(setup):
    from db.dal import limiter_dal

    await seed(setup)
    now = datetime.now(timezone.utc)
    for when in (now, now + timedelta(hours=24)):
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    await enqueue(setup, now + timedelta(hours=48, seconds=-10))
    async with setup.factory() as session:
        await limiter_dal.prune(session, now + timedelta(days=2))
        await session.commit()
    await setup.service.run_checks(setup.factory, now=now + timedelta(hours=50))
    assert "3/3" in setup.bot.messages[-1][1]


async def test_lost_disable_response_finishes_without_another_disable(setup):
    sub_id = await seed(setup)
    now = datetime.now(timezone.utc)
    for n in range(2):
        when = now + timedelta(minutes=n * 5)
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    setup.panel.lose_disable_response = True
    when = now + timedelta(minutes=10)
    await enqueue(setup, when)
    await setup.service.run_checks(setup.factory, now=when)
    assert len(setup.bot.messages) == 2
    await setup.service.run_checks(setup.factory, now=when + timedelta(minutes=2))
    assert setup.panel.disables == [("42", False)]
    assert "3/3" in setup.bot.messages[-1][1]
    async with setup.factory() as session:
        assert (await session.get(Subscription, sub_id)).status_from_panel == "DISABLED"


async def test_sql_commit_failure_after_reset_does_not_rotate_again(setup):
    await seed(setup)
    admin = AsyncMock()
    setup.service.notification_service = admin
    now = datetime.now(timezone.utc)
    await enqueue(setup, now)
    async with setup.factory() as session:
        sync_class = type(session.sync_session)
    failed = False

    def fail_applied_commit(session):
        nonlocal failed
        if not failed and any(isinstance(row, LimiterViolation) and row.stage == "applied"
                              for row in session.identity_map.values()):
            failed = True
            raise RuntimeError("test failed SQL commit")

    sqlalchemy_event.listen(sync_class, "before_commit", fail_applied_commit)
    try:
        await setup.service.run_checks(setup.factory, now=now)
    finally:
        sqlalchemy_event.remove(sync_class, "before_commit", fail_applied_commit)
    assert failed and setup.panel.revokes == ["42"] and not setup.bot.messages
    admin.notify_limiter_action.assert_not_awaited()
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=2))
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1
    admin.notify_limiter_action.assert_awaited_once()


@pytest.mark.parametrize("mode", ["ip", "subnet", "asn"])
async def test_notification_localization_and_grouping(setup, mode):
    await seed(setup)
    now = datetime.now(timezone.utc)
    setup.service.i18n = JsonI18n("locales", default="en", premium_emoji=False)
    async with setup.factory() as session:
        user = await session.get(User, 123)
        user.language_code = "en"
        await session.commit()
    payload = json.loads(body(when=now))
    payload["violation"].update(grouping_mode=mode, ip_count=10)
    await setup.service.enqueue(setup.factory, json.dumps(payload).encode(), received_at=now)
    await setup.service.run_checks(setup.factory, now=now)
    text = setup.bot.messages[0][1]
    assert "1/3" in text and "<b>10</b>" in text
    assert "\n\n" in text and "::" not in text and "<tg-emoji" not in text
    assert {"ip": "active IP addresses", "subnet": "subnet groups", "asn": "ASN provider groups"}[mode] in text


async def test_telegram_outage_does_not_delay_third_violation_disable(setup):
    await seed(setup)
    now = datetime.now(timezone.utc)
    setup.bot.unavailable = True
    for n in range(3):
        when = now + timedelta(minutes=5 * n)
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    assert setup.panel.revokes == ["42", "42"]
    assert setup.panel.disables == [("42", False)]
    setup.bot.unavailable = False
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=30))
    # Old 1/3 and 2/3 notices must not arrive after the subscription is disabled.
    assert len(setup.bot.messages) == 1 and "3/3" in setup.bot.messages[0][1]


async def test_panel_token_selects_current_local_row_among_subscription_history(setup):
    current_id = await seed(setup)
    now = datetime.now(timezone.utc)
    async with setup.factory() as session:
        session.add(Subscription(user_id=123, panel_user_uuid="42", panel_subscription_uuid="historical",
                                 end_date=now + timedelta(days=60), is_active=True))
        await session.commit()
    await enqueue(setup, now)
    await setup.service.run_checks(setup.factory, now=now)
    async with setup.factory() as session:
        row = await session.scalar(select(LimiterViolation))
        assert row.subscription_id == current_id
        assert (await session.get(Subscription, current_id)).panel_subscription_uuid == "rotated-1-42"


@pytest.mark.parametrize("failure", ["panel", "lost_revoke", "telegram"])
async def test_webhook_retry_resumes_saved_event_without_waiting_for_scheduler(setup, client, failure):
    await seed(setup)
    setup.panel.unavailable = failure == "panel"
    setup.panel.lose_revoke_response = failure == "lost_revoke"
    setup.bot.fail_once = failure == "telegram"
    raw = body()
    response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
    assert response.status == 503
    async with setup.factory() as session:
        rows = list((await session.scalars(select(LimiterViolation))).all())
        assert len(rows) == 1 and rows[0].stage not in ("done", "ignored")
    setup.panel.unavailable = False
    response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
    assert response.status == 200
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1
    assert "1/3" in setup.bot.messages[0][1]


async def test_scheduler_recovers_when_webhook_sender_has_stopped_retrying(setup, client):
    await seed(setup)
    setup.panel.unavailable = True
    raw = body()
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 503
    setup.panel.unavailable = False
    await setup.service.run_checks(setup.factory, now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1


async def test_three_webhooks_disable_subscription_without_scheduler(setup, client):
    await seed(setup)
    for _ in range(3):
        raw = body()
        response = await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))
        assert response.status == 200
    assert setup.panel.revokes == ["42", "42"]
    assert setup.panel.disables == [("42", False)]
    assert "3/3" in setup.bot.messages[-1][1]


async def test_immediate_webhook_does_not_overtake_an_older_pending_action(setup, client):
    await seed(setup)
    now = datetime.now(timezone.utc)
    await enqueue(setup, now - timedelta(minutes=5))
    raw = body()
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 503
    assert not setup.panel.revokes
    await setup.service.run_checks(setup.factory, now=now + timedelta(seconds=1))
    await setup.service.run_checks(setup.factory, now=now + timedelta(seconds=2))
    assert len(setup.panel.revokes) == 2
    assert "1/3" in setup.bot.messages[0][1] and "2/3" in setup.bot.messages[1][1]


async def test_delayed_disable_notice_is_suppressed_after_subscription_reactivation(setup):
    sub_id = await seed(setup)
    now = datetime.now(timezone.utc)
    for number in range(3):
        when = now + timedelta(minutes=number * 5)
        if number == 2:
            setup.bot.fail_once = True
        await enqueue(setup, when)
        await setup.service.run_checks(setup.factory, now=when)
    assert len(setup.bot.messages) == 2
    setup.panel.users["42"]["status"] = "ACTIVE"
    async with setup.factory() as session:
        sub = await session.get(Subscription, sub_id)
        sub.is_active = True
        sub.status_from_panel = "ACTIVE"
        await session.commit()
    await setup.service.run_checks(setup.factory, now=now + timedelta(minutes=12))
    assert len(setup.bot.messages) == 2
    async with setup.factory() as session:
        event = await session.scalar(select(LimiterViolation).order_by(LimiterViolation.id.desc()))
        assert event.stage == "done" and event.warning_number == 3
        assert (await session.get(Subscription, sub_id)).is_active


async def test_admin_alerts_are_once_per_applied_action_despite_user_notification_retry(setup, client):
    await seed(setup)
    admin = AsyncMock()
    setup.service.notification_service = admin
    setup.bot.fail_once = True
    first = body()
    assert (await client.post("/webhook/limiter", data=first, headers=signed(setup, first))).status == 503
    assert admin.notify_limiter_action.await_count == 1
    assert (await client.post("/webhook/limiter", data=first, headers=signed(setup, first))).status == 200
    for _ in range(2):
        raw = body()
        assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 200
    await setup.service.run_checks(setup.factory)
    calls = admin.notify_limiter_action.await_args_list
    assert [call.kwargs["warning_number"] for call in calls] == [1, 2, 3]
    assert all(call.kwargs["user_id"] == 123 and call.kwargs["panel_user_ref"] == "42" for call in calls)


async def test_failed_panel_action_does_not_send_admin_alert_until_recovered(setup, client):
    await seed(setup)
    admin = AsyncMock()
    setup.service.notification_service = admin
    setup.panel.lose_revoke_response = True
    raw = body()
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 503
    admin.notify_limiter_action.assert_not_awaited()
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 200
    admin.notify_limiter_action.assert_awaited_once()


async def test_admin_alert_failure_does_not_repeat_action_or_block_user_notification(setup, client):
    await seed(setup)
    admin = AsyncMock()
    admin.notify_limiter_action.side_effect = RuntimeError("test log chat failure")
    setup.service.notification_service = admin
    raw = body()
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 200
    assert (await client.post("/webhook/limiter", data=raw, headers=signed(setup, raw))).status == 200
    assert setup.panel.revokes == ["42"] and len(setup.bot.messages) == 1
    admin.notify_limiter_action.assert_awaited_once()
