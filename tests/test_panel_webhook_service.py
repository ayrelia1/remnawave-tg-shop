"""Panel webhook handling against the Remnawave 2.8+/3.x event schema.

2.8.0 collapsed user.expires_in_{72,48,24}_hours and user.expired_24_hours_ago
into a single `user.expiration` event whose `meta.expiration` is a signed hour
offset: negative before expiry, positive after it.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from bot.services import panel_webhook_service as pws
from bot.services.panel_webhook_service import (
    PanelWebhookService,
    expiration_bucket,
    expiration_hours_from_event,
)

SECRET = "s" * 32


# --------------------------------------------------------------------------
# Pure mapping helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("meta", "expected"),
    [({"expiration": -72}, -72), ({"expiration": -24}, -24), ({"expiration": 24}, 24)],
)
def test_expiration_hours_read_from_meta(meta, expected):
    assert expiration_hours_from_event("user.expiration", meta) == expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("user.expires_in_72_hours", -72),
        ("user.expires_in_48_hours", -48),
        ("user.expires_in_24_hours", -24),
        ("user.expired_24_hours_ago", 24),
    ],
)
def test_legacy_event_names_still_map(event, expected):
    """A panel that has not been upgraded past 2.7 keeps working."""
    assert expiration_hours_from_event(event, None) == expected


@pytest.mark.parametrize("meta", [None, {}, {"expiration": None}, {"expiration": "soon"}])
def test_expiration_event_without_usable_meta_yields_none(meta):
    assert expiration_hours_from_event("user.expiration", meta) is None


@pytest.mark.parametrize("event", ["user.expired", "user.created", "node.connection_lost"])
def test_non_expiration_events_yield_none(event):
    assert expiration_hours_from_event(event, {"expiration": -72}) is None


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (-72, ("before", 3)),
        (-48, ("before", 2)),
        (-24, ("before", 1)),
        (-1, ("before", 1)),
        (-36, ("before", 2)),
        (24, ("after", 1)),
        (12, ("after", 1)),
        (48, ("after", 2)),
        (0, ("before", 1)),
    ],
)
def test_expiration_bucket(hours, expected):
    assert expiration_bucket(hours) == expected


# --------------------------------------------------------------------------
# End-to-end event handling
# --------------------------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeI18n:
    def gettext(self, lang, key, **kwargs):
        return key


class FakeSessionFactory:
    """Async context manager standing in for a sessionmaker."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


def make_settings(**overrides):
    settings = SimpleNamespace(
        PANEL_WEBHOOK_SECRET=SECRET,
        SUBSCRIPTION_NOTIFICATIONS_ENABLED=True,
        SUBSCRIPTION_NOTIFY_DAYS_BEFORE=3,
        SUBSCRIPTION_NOTIFY_ON_EXPIRE=True,
        SUBSCRIPTION_NOTIFY_AFTER_EXPIRE=True,
        DEFAULT_LANGUAGE="ru",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture
def service(monkeypatch):
    bot = FakeBot()

    async def fake_get_user_by_id(session, user_id):
        return SimpleNamespace(language_code="ru", first_name="Тест")

    monkeypatch.setattr(pws.user_dal, "get_user_by_id", fake_get_user_by_id)

    def _build(**settings_overrides):
        svc = PanelWebhookService(
            bot=bot,
            settings=make_settings(**settings_overrides),
            i18n=FakeI18n(),
            async_session_factory=FakeSessionFactory(),
            panel_service=object(),
            notification_service=SimpleNamespace(),
        )
        return svc, bot

    return _build


@pytest.fixture
def no_active_subscription(monkeypatch):
    """Keep the auto-renew branches inert: they only fire on a saved sub."""
    from db.dal import subscription_dal

    async def fake_get_active(session, user_id, panel_user_uuid=None):
        return None

    monkeypatch.setattr(subscription_dal, "get_active_subscription_by_user_id", fake_get_active)


USER = {"telegramId": 111, "expireAt": "2026-08-10T00:00:00.000Z"}


@pytest.mark.parametrize(
    ("hours", "expected_key"),
    [
        (-72, "subscription_72h_notification"),
        (-48, "subscription_48h_notification"),
        (-24, "subscription_24h_notification"),
    ],
)
async def test_expiration_event_sends_matching_reminder(
    service, no_active_subscription, hours, expected_key
):
    svc, bot = service()

    await svc.handle_event("user.expiration", USER, {"expiration": hours})

    assert bot.sent == [(111, expected_key)]


async def test_expired_after_event_sends_yesterday_reminder(service, no_active_subscription):
    svc, bot = service()

    await svc.handle_event("user.expiration", USER, {"expiration": 24})

    assert bot.sent == [(111, "subscription_expired_yesterday_notification")]


async def test_legacy_event_still_sends_reminder(service, no_active_subscription):
    svc, bot = service()

    await svc.handle_event("user.expires_in_72_hours", USER, None)

    assert bot.sent == [(111, "subscription_72h_notification")]


async def test_unmapped_threshold_is_skipped_not_mislabelled(service, no_active_subscription):
    """-120h would render as "expires in 3 days", which is false. Stay silent."""
    svc, bot = service()

    await svc.handle_event("user.expiration", USER, {"expiration": -120})

    assert bot.sent == []


async def test_after_expiry_beyond_one_day_is_skipped(service, no_active_subscription):
    svc, bot = service()

    await svc.handle_event("user.expiration", USER, {"expiration": 72})

    assert bot.sent == []


async def test_notify_days_before_setting_is_respected(service, no_active_subscription):
    svc, bot = service(SUBSCRIPTION_NOTIFY_DAYS_BEFORE=1)

    await svc.handle_event("user.expiration", USER, {"expiration": -72})

    assert bot.sent == []


async def test_after_expire_notification_can_be_disabled(service, no_active_subscription):
    svc, bot = service(SUBSCRIPTION_NOTIFY_AFTER_EXPIRE=False)

    await svc.handle_event("user.expiration", USER, {"expiration": 24})

    assert bot.sent == []


async def test_user_expired_event_still_handled(service, no_active_subscription):
    svc, bot = service()

    await svc.handle_event("user.expired", USER, None)

    assert bot.sent == [(111, "subscription_expired_notification")]


async def test_event_without_telegram_id_is_ignored(service, no_active_subscription):
    svc, bot = service()

    await svc.handle_event("user.expiration", {"expireAt": "2026-08-10"}, {"expiration": -72})

    assert bot.sent == []


async def test_notifications_disabled_globally(service, no_active_subscription):
    svc, bot = service(SUBSCRIPTION_NOTIFICATIONS_ENABLED=False)

    await svc.handle_event("user.expiration", USER, {"expiration": -72})

    assert bot.sent == []


# --------------------------------------------------------------------------
# Signature verification and payload routing
# --------------------------------------------------------------------------

def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_routes_3x_payload_with_meta(service, no_active_subscription):
    svc, bot = service()
    body = json.dumps(
        {
            "scope": "user",
            "event": "user.expiration",
            "timestamp": "2026-08-07T00:00:00.000Z",
            "data": USER,
            "meta": {"expiration": -48, "notConnectedAfterHours": None},
        }
    ).encode()

    response = await svc.handle_webhook(body, sign(body))

    assert response.status == 200
    assert bot.sent == [(111, "subscription_48h_notification")]


async def test_webhook_rejects_bad_signature(service):
    svc, bot = service()
    body = json.dumps({"event": "user.expired", "data": USER}).encode()

    response = await svc.handle_webhook(body, "deadbeef")

    assert response.status == 403
    assert bot.sent == []


async def test_webhook_rejects_missing_signature(service):
    svc, _ = service()
    body = b"{}"

    assert (await svc.handle_webhook(body, None)).status == 403


async def test_webhook_requires_configured_secret(service):
    svc, _ = service(PANEL_WEBHOOK_SECRET=None)

    assert (await svc.handle_webhook(b"{}", "sig")).status == 503


async def test_webhook_rejects_malformed_json(service):
    svc, _ = service()
    body = b"not json"

    assert (await svc.handle_webhook(body, sign(body))).status == 400


async def test_node_event_is_routed_to_notification_service(service):
    svc, _ = service()
    notified = []

    async def notify_node_down(name, address):
        notified.append((name, address))

    svc.notification_service.notify_node_down = notify_node_down
    body = json.dumps(
        {
            "scope": "node",
            "event": "node.connection_lost",
            "data": {"name": "de-1", "address": "1.2.3.4", "port": 2222},
        }
    ).encode()

    response = await svc.handle_webhook(body, sign(body))

    assert response.status == 200
    assert notified == [("de-1", "1.2.3.4:2222")]
