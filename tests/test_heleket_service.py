"""Heleket gateway: signing, invoice creation and webhook handling.

The signature is the whole security boundary here, so it is pinned against the
algorithm the docs specify in PHP:

    md5(base64_encode(json_encode($data, JSON_UNESCAPED_UNICODE)) . $apiKey)

PHP renders JSON compactly and escapes forward slashes; Python does neither by
default, so `_canonical_json` is asserted literally rather than just
round-tripped through our own code.
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import heleket_service as heleket_module
from bot.services.heleket_service import HeleketService
from db.dal import payment_dal, user_dal


MERCHANT_ID = "8b03432e-385b-4670-8d06-064591096795"
API_KEY = "payment-api-key"
ORDER_AMOUNT = 129.0
PAYMENT_ID = 7


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _RecordingHttpSession:
    def __init__(self, response=None, status=200):
        self.posts = []
        self._response = response
        self._status = status

    def post(self, url, data=None, headers=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        body = self._response or {
            "state": 0,
            "result": {
                "uuid": "invoice-uuid-1",
                "order_id": str(PAYMENT_ID),
                "amount": "129.00",
                "currency": "RUB",
                "payment_status": "check",
                "url": "https://pay.heleket.com/invoice-uuid-1",
            },
        }
        return _FakeResponse(body, self._status)


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _settings(enabled=True, merchant=MERCHANT_ID, key=API_KEY, webhook_base="https://bot.example"):
    return SimpleNamespace(
        HELEKET_ENABLED=enabled,
        HELEKET_MERCHANT_ID=merchant,
        HELEKET_API_KEY=key,
        HELEKET_BASE_URL="https://api.heleket.com",
        HELEKET_TO_CURRENCY=None,
        HELEKET_LIFETIME_SECONDS=3600,
        HELEKET_RETURN_URL=None,
        HELEKET_SUCCESS_URL=None,
        heleket_full_webhook_url=(f"{webhook_base}/webhook/heleket" if webhook_base else None),
        DEFAULT_LANGUAGE="ru",
        traffic_sale_mode=False,
    )


def _service(settings=None, subscription_service=None):
    class _Bot:
        async def send_message(self, *args, **kwargs):
            return None

    class _I18n:
        def gettext(self, lang, key, **kwargs):
            return key

    class _SubscriptionService:
        async def activate_subscription(self, *args, **kwargs):
            return {
                "end_date": datetime.now(timezone.utc) + timedelta(days=30),
                "subscription_url": "https://sub.example/link",
                "applied_promo_bonus_days": 0,
            }

    class _ReferralService:
        async def apply_referral_bonuses_for_payment(self, *args, **kwargs):
            return None

    return HeleketService(
        bot=_Bot(),
        settings=settings or _settings(),
        i18n=_I18n(),
        async_session_factory=_FakeSession,
        subscription_service=subscription_service or _SubscriptionService(),
        referral_service=_ReferralService(),
        default_return_url="test_bot",
    )


def _php_sign(payload, key=API_KEY):
    """Independent reimplementation of the documented PHP snippet."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/")
    return hashlib.md5(
        (base64.b64encode(encoded.encode("utf-8")).decode("ascii") + key).encode("utf-8")
    ).hexdigest()


def _signed_webhook(**overrides):
    body = {
        "type": "payment",
        "uuid": "invoice-uuid-1",
        "order_id": str(PAYMENT_ID),
        "amount": "129.00",
        "payment_amount": "1.35",
        "payment_amount_usd": "1.42",
        "merchant_amount": "1.34",
        "commission": "0.01",
        "is_final": True,
        "status": "paid",
        "from": "TQ1...",
        "wallet_address_uuid": None,
        "network": "tron",
        "currency": "RUB",
        "payer_currency": "USDT",
        "additional_data": None,
        "txid": "0xabc",
    }
    body.update(overrides)
    body["sign"] = _php_sign(body)
    return body


# --------------------------------------------------------------------- signing


def test_canonical_json_matches_php_json_encode():
    """PHP: compact separators, escaped slashes, unescaped unicode."""
    service = _service()

    rendered = service._canonical_json(
        {"url": "https://t.me/bot", "note": "Подписка", "n": 1, "flag": True, "empty": None}
    )

    assert rendered == (
        '{"url":"https:\\/\\/t.me\\/bot","note":"Подписка","n":1,"flag":true,"empty":null}'
    )


def test_sign_matches_the_documented_php_algorithm():
    service = _service()
    payload = {"amount": "129.00", "currency": "RUB", "order_id": "7"}

    body = service._canonical_json(payload)

    assert service._sign(body) == _php_sign(payload)


def test_webhook_signature_accepts_a_genuine_callback():
    service = _service()

    assert service.verify_webhook_sign(_signed_webhook()) is True


def test_webhook_signature_accepts_unescaped_slashes():
    """Guards against a gateway switching to JSON_UNESCAPED_SLASHES."""
    service = _service()
    body = {"uuid": "u", "order_id": "7", "status": "paid", "txid": "https://x/y"}
    plain = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    body["sign"] = hashlib.md5(
        (base64.b64encode(plain.encode()).decode("ascii") + API_KEY).encode()
    ).hexdigest()

    assert service.verify_webhook_sign(body) is True


def test_webhook_signature_rejects_a_tampered_amount():
    service = _service()
    body = _signed_webhook()

    body["amount"] = "1.00"

    assert service.verify_webhook_sign(body) is False


def test_webhook_signature_rejects_a_foreign_key():
    service = _service()
    body = _signed_webhook()
    body["sign"] = _php_sign({k: v for k, v in body.items() if k != "sign"}, key="not-the-key")

    assert service.verify_webhook_sign(body) is False


def test_webhook_signature_rejects_a_missing_sign():
    service = _service()
    body = _signed_webhook()
    body.pop("sign")

    assert service.verify_webhook_sign(body) is False


def test_service_is_unconfigured_without_credentials():
    assert _service(_settings(merchant=None)).configured is False
    assert _service(_settings(key=None)).configured is False
    assert _service(_settings(enabled=False)).configured is False


# ------------------------------------------------------------------- invoicing


@pytest.fixture
def service_with_http(monkeypatch):
    service = _service()
    http = _RecordingHttpSession()

    async def fake_get_session():
        return http

    monkeypatch.setattr(service, "_get_session", fake_get_session)
    return service, http


async def _create(service, **overrides):
    kwargs = dict(
        payment_db_id=PAYMENT_ID,
        user_id=111,
        months=1,
        amount=ORDER_AMOUNT,
        currency="RUB",
        description="Subscription for 1 month",
    )
    kwargs.update(overrides)
    return await service.create_invoice(**kwargs)


async def test_create_invoice_posts_the_documented_request(service_with_http):
    service, http = service_with_http

    ok, result = await _create(service)

    assert ok
    assert result["uuid"] == "invoice-uuid-1"
    post = http.posts[0]
    assert post["url"] == "https://api.heleket.com/v1/payment"
    assert post["headers"]["merchant"] == MERCHANT_ID
    assert post["headers"]["Content-Type"] == "application/json"

    body = json.loads(post["data"].decode("utf-8"))
    assert body["amount"] == "129.00"
    assert body["currency"] == "RUB"
    assert body["order_id"] == str(PAYMENT_ID)
    assert body["url_callback"] == "https://bot.example/webhook/heleket"
    assert body["lifetime"] == 3600


async def test_create_invoice_signs_exactly_the_bytes_it_sends(service_with_http):
    """Re-serialising the body before sending would invalidate the signature."""
    service, http = service_with_http

    await _create(service)

    post = http.posts[0]
    sent_bytes = post["data"].decode("utf-8")
    assert post["headers"]["sign"] == service._sign(sent_bytes)


async def test_create_invoice_reports_a_gateway_error(monkeypatch):
    service = _service()
    http = _RecordingHttpSession(response={"state": 1, "message": "amount is invalid"}, status=422)

    async def fake_get_session():
        return http

    monkeypatch.setattr(service, "_get_session", fake_get_session)

    ok, result = await _create(service)

    assert ok is False
    assert result["status"] == 422


async def test_create_invoice_omits_callback_without_webhook_base(monkeypatch):
    service = _service(_settings(webhook_base=None))
    http = _RecordingHttpSession()

    async def fake_get_session():
        return http

    monkeypatch.setattr(service, "_get_session", fake_get_session)

    await _create(service)

    assert "url_callback" not in json.loads(http.posts[0]["data"].decode("utf-8"))


# --------------------------------------------------------------------- webhook


@pytest.fixture
def webhook_env(monkeypatch):
    """Stub the success path; record activations, status writes and notifications."""
    record = {"activated": [], "status_writes": [], "notified": [], "messages": []}
    payment = SimpleNamespace(
        payment_id=PAYMENT_ID,
        user_id=111,
        amount=ORDER_AMOUNT,
        currency="RUB",
        status="pending_heleket",
        provider="heleket",
        subscription_duration_months=1,
        promo_code_id=None,
        hwid_device_limit=None,
    )

    async def fake_get_by_provider_id(session, provider_payment_id):
        return payment if provider_payment_id == "invoice-uuid-1" else None

    async def fake_get_by_db_id(session, payment_db_id):
        return payment if payment_db_id == PAYMENT_ID else None

    async def fake_mark_once(session, payment_db_id, provider_payment_id):
        if payment.status == "succeeded":
            return False
        payment.status = "succeeded"
        record["activated"].append((payment_db_id, provider_payment_id))
        return True

    async def fake_update_status(session, payment_db_id, provider_payment_id, status):
        record["status_writes"].append((payment_db_id, provider_payment_id, status))
        return None

    async def fake_get_user(session, user_id):
        return SimpleNamespace(
            user_id=user_id,
            language_code="ru",
            referred_by_id=None,
            first_name="Test",
            username="test",
        )

    async def fake_prepare_config_links(settings, raw_link):
        return "https://sub.example/link", "https://sub.example/link"

    class _FakeNotificationService:
        def __init__(self, *args, **kwargs):
            pass

        async def notify_payment_received(self, **kwargs):
            record["notified"].append(kwargs.get("payment_provider"))

    monkeypatch.setattr(
        payment_dal, "get_payment_by_provider_payment_id", fake_get_by_provider_id
    )
    monkeypatch.setattr(payment_dal, "get_payment_by_db_id", fake_get_by_db_id)
    monkeypatch.setattr(payment_dal, "mark_provider_payment_succeeded_once", fake_mark_once)
    monkeypatch.setattr(payment_dal, "update_provider_payment_and_status", fake_update_status)
    monkeypatch.setattr(user_dal, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(heleket_module, "prepare_config_links", fake_prepare_config_links)
    monkeypatch.setattr(
        heleket_module, "get_connect_and_main_keyboard", lambda *a, **kw: None
    )
    monkeypatch.setattr(heleket_module, "NotificationService", _FakeNotificationService)
    record["payment"] = payment
    return record


async def test_paid_callback_activates_the_subscription(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook()))

    assert response.status == 200
    assert webhook_env["activated"] == [(PAYMENT_ID, "invoice-uuid-1")]
    assert webhook_env["notified"] == ["heleket"]


async def test_paid_over_also_activates(webhook_env):
    service = _service()

    response = await service.webhook_route(
        _FakeRequest(_signed_webhook(status="paid_over", payment_amount="2.00"))
    )

    assert response.status == 200
    assert webhook_env["activated"] == [(PAYMENT_ID, "invoice-uuid-1")]


async def test_repeated_callback_does_not_activate_twice(webhook_env):
    """Heleket retries until it gets a 200."""
    service = _service()

    first = await service.webhook_route(_FakeRequest(_signed_webhook()))
    second = await service.webhook_route(_FakeRequest(_signed_webhook()))

    assert (first.status, second.status) == (200, 200)
    assert len(webhook_env["activated"]) == 1


async def test_intermediate_status_waits_without_touching_the_payment(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(status="confirm_check")))

    assert response.status == 200
    assert webhook_env["activated"] == []
    assert webhook_env["status_writes"] == []


async def test_underpayment_cancels_instead_of_activating(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(status="wrong_amount")))

    assert response.status == 200
    assert webhook_env["activated"] == []
    assert webhook_env["status_writes"] == [(PAYMENT_ID, "invoice-uuid-1", "canceled")]


async def test_cancelled_invoice_is_marked_canceled(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(status="cancel")))

    assert response.status == 200
    assert webhook_env["status_writes"] == [(PAYMENT_ID, "invoice-uuid-1", "canceled")]


async def test_forged_callback_is_rejected(webhook_env):
    service = _service()
    body = _signed_webhook()
    body["amount"] = "1.00"

    response = await service.webhook_route(_FakeRequest(body))

    assert response.status == 403
    assert webhook_env["activated"] == []


async def test_callback_resolves_the_payment_by_order_id(webhook_env):
    """A callback for an invoice whose uuid was never stored still lands."""
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(uuid="")))

    assert response.status == 200
    assert webhook_env["activated"] == [(PAYMENT_ID, str(PAYMENT_ID))]


async def test_currency_mismatch_is_rejected(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(currency="USD")))

    assert response.status == 400
    assert response.text == "currency_mismatch"
    assert webhook_env["activated"] == []


async def test_unknown_order_is_not_found(webhook_env):
    service = _service()

    response = await service.webhook_route(
        _FakeRequest(_signed_webhook(uuid="other-uuid", order_id="999"))
    )

    assert response.status == 404
    assert webhook_env["activated"] == []


async def test_disabled_service_refuses_callbacks():
    service = _service(_settings(enabled=False))

    response = await service.webhook_route(_FakeRequest(_signed_webhook()))

    assert response.status == 503
