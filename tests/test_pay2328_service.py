"""2328 signing, invoice creation, webhook validation and fulfillment tests."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import pay2328_service as pay2328_module
from bot.services.pay2328_service import Pay2328Service
from db.dal import payment_dal, user_dal


PROJECT_ID = "8b03432e-385b-4670-8d06-064591096795"
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

    def post(self, url, data=None, headers=None, **kwargs):
        self.posts.append({"url": url, "data": data, "headers": headers, **kwargs})
        body = self._response or {
            "state": 0,
            "result": {
                "uuid": "invoice-uuid-1",
                "order_id": str(PAYMENT_ID),
                "amount": "129.00",
                "currency": "RUB",
                "payment_status": "check",
                "url": "https://2328.io/pay/invoice-uuid-1",
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

    async def read(self):
        return self._body


def _settings(enabled=True, project=PROJECT_ID, key=API_KEY, webhook_base="https://bot.example"):
    return SimpleNamespace(
        PAY2328_ENABLED=enabled,
        PAY2328_PROJECT_ID=project,
        PAY2328_API_KEY=key,
        PAY2328_BASE_URL="https://api.2328.io/api",
        PAY2328_TO_CURRENCY=None,
        PAY2328_NETWORK=None,
        PAY2328_TTL_SECONDS=3600,
        PAY2328_RETURN_URL=None,
        PAY2328_SUCCESS_URL=None,
        pay2328_full_webhook_url=(f"{webhook_base}/webhook/2328" if webhook_base else None),
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

    return Pay2328Service(
        bot=_Bot(),
        settings=settings or _settings(),
        i18n=_I18n(),
        async_session_factory=_FakeSession,
        subscription_service=subscription_service or _SubscriptionService(),
        referral_service=_ReferralService(),
        default_return_url="test_bot",
    )


def _sign_text(body, key=API_KEY):
    return hmac.new(key.encode(), base64.b64encode(body.encode()), hashlib.sha256).hexdigest()


def _php_sign(payload, key=API_KEY):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _sign_text(encoded, key)


def _signed_webhook(
    *,
    _key=API_KEY,
    _escape_slashes=True,
    _separators=(",", ":"),
    **overrides,
):
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
        "payment_status": "paid",
        "from": "TQ1...",
        "wallet_address_uuid": None,
        "network": "tron",
        "currency": "RUB",
        "payer_currency": "USDT",
        "additional_data": None,
        "txid": "0xabc",
    }
    body.update(overrides)
    unsigned = json.dumps(body, ensure_ascii=False, separators=_separators)
    if _escape_slashes:
        unsigned = unsigned.replace("/", "\\/")
    signature = _php_sign(body, _key)
    signed = (
        f'{unsigned[:-1]}{_separators[0]}"sign"'
        f'{_separators[1]}"{signature}"}}'
    )
    return signed.encode("utf-8")


# --------------------------------------------------------------------- signing


def test_canonical_json_uses_compact_unicode_without_escaping_slashes():
    """The signing representation keeps slashes and Unicode unescaped."""
    service = _service()

    rendered = service._canonical_json(
        {"url": "https://t.me/bot", "note": "Подписка", "n": 1, "flag": True, "empty": None}
    )

    assert rendered == (
        '{"url":"https://t.me/bot","note":"Подписка","n":1,"flag":true,"empty":null}'
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

    raw_body = _signed_webhook(txid="https://x/y", _escape_slashes=False)

    assert service.verify_webhook_sign(raw_body) is True


def test_webhook_signature_preserves_the_original_json_bytes():
    """Whitespace and wire escaping do not change the documented canonical signature."""
    service = _service()
    raw_body = _signed_webhook(
        additional_data="https://example.com/Оплата",
        _separators=(", ", ": "),
    )

    assert service.verify_webhook_sign(raw_body) is True


def test_webhook_signature_rejects_a_tampered_amount():
    service = _service()
    body = _signed_webhook().replace(b'"amount":"129.00"', b'"amount":"1.00"', 1)

    assert service.verify_webhook_sign(body) is False


def test_webhook_signature_rejects_a_foreign_key():
    service = _service()
    body = _signed_webhook(_key="not-the-key")

    assert service.verify_webhook_sign(body) is False


def test_webhook_signature_rejects_a_missing_sign():
    service = _service()

    assert service.verify_webhook_sign(b'{"uuid":"u","status":"paid"}') is False


def test_service_is_unconfigured_without_credentials():
    assert _service(_settings(project=None)).configured is False
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
    assert post["url"] == "https://api.2328.io/api/v1/payment"
    assert post["headers"]["project"] == PROJECT_ID
    assert post["headers"]["User-Agent"]
    assert post["headers"]["Content-Type"] == "application/json"

    body = json.loads(post["data"].decode("utf-8"))
    assert body["amount"] == "129.00"
    assert body["currency"] == "RUB"
    assert body["order_id"] == str(PAYMENT_ID)
    assert body["url_callback"] == "https://bot.example/webhook/2328"
    assert body["ttl_seconds"] == 3600


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


async def test_create_invoice_refuses_to_create_without_callback(monkeypatch):
    service = _service(_settings(webhook_base=None))
    get_session = AsyncMock()
    monkeypatch.setattr(service, "_get_session", get_session)
    ok, result = await _create(service)
    assert not ok
    get_session.assert_not_awaited()


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
        status="pending_2328",
        provider="2328",
        provider_payment_id="invoice-uuid-1",
        subscription_duration_months=1,
        promo_code_id=None,
        hwid_device_limit=None,
    )

    async def fake_get_by_provider_id(session, provider_payment_id):
        return payment if provider_payment_id == "invoice-uuid-1" else None

    async def fake_get_by_db_id(session, payment_db_id, **kwargs):
        return payment if payment_db_id == PAYMENT_ID else None

    async def fake_mark_once(session, payment_db_id, provider_payment_id):
        if payment.status == "succeeded":
            return False
        payment.status = "succeeded"
        record["activated"].append((payment_db_id, provider_payment_id))
        return True

    async def fake_update_status(session, payment_db_id, provider_payment_id, status):
        record["status_writes"].append((payment_db_id, provider_payment_id, status))
        payment.status = status
        return payment

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
    monkeypatch.setattr(pay2328_module, "prepare_config_links", fake_prepare_config_links)
    monkeypatch.setattr(
        pay2328_module, "get_connect_and_main_keyboard", lambda *a, **kw: None
    )
    monkeypatch.setattr(pay2328_module, "NotificationService", _FakeNotificationService)
    record["payment"] = payment
    return record


async def test_paid_callback_activates_the_subscription(webhook_env):
    service = _service()

    response = await service.webhook_route(
        _FakeRequest(
            _signed_webhook(
                additional_data="https://example.com/Оплата",
                _separators=(", ", ": "),
            )
        )
    )

    assert response.status == 200
    assert webhook_env["activated"] == [(PAYMENT_ID, "invoice-uuid-1")]
    assert webhook_env["notified"] == ["2328"]


async def test_malformed_callback_is_rejected_before_processing(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(b'{"type":"payment"'))

    assert response.status == 400
    assert response.text == "bad_request"
    assert webhook_env["activated"] == []


async def test_overpaid_also_activates(webhook_env):
    service = _service()

    response = await service.webhook_route(
        _FakeRequest(_signed_webhook(payment_status="overpaid", payment_amount="2.00"))
    )

    assert response.status == 200
    assert webhook_env["activated"] == [(PAYMENT_ID, "invoice-uuid-1")]


async def test_repeated_callback_does_not_activate_twice(webhook_env):
    """2328 retries until it gets a 200."""
    service = _service()

    first = await service.webhook_route(_FakeRequest(_signed_webhook()))
    second = await service.webhook_route(_FakeRequest(_signed_webhook()))

    assert (first.status, second.status) == (200, 200)
    assert len(webhook_env["activated"]) == 1


async def test_intermediate_status_waits_without_touching_the_payment(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status="awaiting_confirmation")))

    assert response.status == 200
    assert webhook_env["activated"] == []
    assert webhook_env["status_writes"] == []


async def test_final_underpayment_does_not_activate(webhook_env):
    service = _service()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status="underpaid")))

    assert response.status == 200
    assert webhook_env["activated"] == []
    assert webhook_env["status_writes"] == [(PAYMENT_ID, "invoice-uuid-1", "underpaid")]


async def test_cancelled_invoice_is_marked_canceled_without_notifying_user(webhook_env, monkeypatch):
    service = _service()
    service.bot.send_message = AsyncMock()
    commit = AsyncMock()
    monkeypatch.setattr(_FakeSession, "commit", commit)

    for _ in range(2):
        response = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status="cancel")))
        assert response.status == 200
        assert response.text == "ok_canceled"

    assert webhook_env["status_writes"] == [(PAYMENT_ID, "invoice-uuid-1", "canceled")]
    assert webhook_env["activated"] == []
    assert commit.await_count == 1
    service.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    ["underpaid", "aml_lock"],
)
async def test_failed_invoice_still_notifies_user(webhook_env, status):
    service = _service()
    service.bot.send_message = AsyncMock()

    response = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))

    assert response.status == 200
    assert response.text == "ok_canceled"
    assert webhook_env["status_writes"] == [(PAYMENT_ID, "invoice-uuid-1", status)]
    assert webhook_env["activated"] == []
    service.bot.send_message.assert_awaited_once_with(111, "payment_failed")


async def test_forged_callback_is_rejected(webhook_env):
    service = _service()
    body = _signed_webhook().replace(b'"amount":"129.00"', b'"amount":"1.00"', 1)

    response = await service.webhook_route(_FakeRequest(body))

    assert response.status == 401
    assert webhook_env["activated"] == []


async def test_callback_resolves_the_payment_by_order_id(webhook_env):
    """A callback for an invoice whose uuid was never stored still lands."""
    service = _service()

    webhook_env["payment"].provider_payment_id = None
    response = await service.webhook_route(_FakeRequest(_signed_webhook()))

    assert response.status == 200
    assert webhook_env["activated"] == [(PAYMENT_ID, "invoice-uuid-1")]


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


@pytest.mark.parametrize("status", ["pending", "check", "awaiting_confirmation", "underpaid_check"])
async def test_all_pending_statuses_wait(webhook_env, status):
    response = await _service().webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))
    assert response.status == 200
    assert webhook_env["activated"] == []


@pytest.mark.parametrize("status", ["cancel", "underpaid", "aml_lock", "refund_process", "refund_fail", "refund_paid", "check"])
async def test_late_events_cannot_regress_fulfillment(webhook_env, status):
    service = _service()
    await service.webhook_route(_FakeRequest(_signed_webhook()))
    late = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))
    repeat = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status="overpaid")))
    assert late.status == repeat.status == 200
    assert webhook_env["payment"].status == "succeeded"
    assert webhook_env["status_writes"] == []
    assert len(webhook_env["activated"]) == 1


@pytest.mark.parametrize("amount", [None, "not-a-number", "NaN", "Infinity", "-1", "0", {}, True, "128.99", "130.00"])
async def test_invalid_or_mismatched_invoice_amount_cannot_fulfill(webhook_env, amount):
    response = await _service().webhook_route(_FakeRequest(_signed_webhook(amount=amount)))
    assert response.status == 400
    assert webhook_env["activated"] == []


@pytest.mark.parametrize("overrides", [{"uuid": ""}, {"order_id": ""}, {"currency": ""}, {"payment_status": ""}, {"order_id": 7}])
async def test_required_webhook_fields_are_not_optional(webhook_env, overrides):
    response = await _service().webhook_route(_FakeRequest(_signed_webhook(**overrides)))
    assert response.status == 400
    assert webhook_env["activated"] == []


async def test_foreign_provider_order_is_rejected(webhook_env):
    webhook_env["payment"].provider = "other-provider"
    response = await _service().webhook_route(_FakeRequest(_signed_webhook()))
    assert response.status == 404
    assert webhook_env["activated"] == []


async def test_conflicting_uuid_and_order_are_rejected(webhook_env):
    response = await _service().webhook_route(_FakeRequest(_signed_webhook(uuid="other-invoice")))
    assert response.status == 404
    assert webhook_env["activated"] == []


async def test_unknown_status_is_rejected(webhook_env):
    response = await _service().webhook_route(_FakeRequest(_signed_webhook(payment_status="unexpected")))
    assert response.status == 400
    assert webhook_env["status_writes"] == []


@pytest.mark.parametrize("raw", [b'[]', b'{"uuid":"x","uuid":"y"}', b'{"value":NaN}', b'\xff'])
async def test_ambiguous_or_malformed_json_is_rejected(webhook_env, raw):
    response = await _service().webhook_route(_FakeRequest(raw))
    assert response.status == 400
    assert webhook_env["activated"] == []


async def test_oversized_webhook_is_rejected(webhook_env):
    response = await _service().webhook_route(_FakeRequest(b" " * 65537))
    assert response.status == 413


async def test_non_json_webhook_is_rejected(webhook_env):
    request = _FakeRequest(_signed_webhook())
    request.content_type = "text/plain"
    response = await _service().webhook_route(request)
    assert response.status == 415


async def test_partial_payment_then_topup_only_fulfills_once(webhook_env):
    service = _service()
    for status in ["underpaid_check", "awaiting_confirmation", "paid", "overpaid"]:
        response = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))
        assert response.status == 200
    assert len(webhook_env["activated"]) == 1


async def test_refund_paid_cannot_regress_to_processing(webhook_env):
    service = _service()
    for status in ["refund_process", "refund_paid", "refund_process", "refund_fail"]:
        assert (await service.webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))).status == 200
    assert webhook_env["payment"].status == "refund_paid"
    assert webhook_env["activated"] == []


@pytest.mark.parametrize("current_status", ["paid", "cancel"])
async def test_late_success_after_cancellation_requires_current_api_evidence(webhook_env, monkeypatch, current_status):
    service = _service()
    webhook_env["payment"].status = "canceled"
    get_info = AsyncMock(return_value=(True, {
        "uuid": "invoice-uuid-1", "order_id": str(PAYMENT_ID), "amount": "129.00",
        "currency": "RUB", "url": "https://2328.io/pay/invoice-uuid-1", "payment_status": current_status,
    }))
    monkeypatch.setattr(service, "get_payment_info", get_info)
    response = await service.webhook_route(_FakeRequest(_signed_webhook()))
    assert response.status == (200 if current_status == "paid" else 409)
    assert len(webhook_env["activated"]) == (1 if current_status == "paid" else 0)
    get_info.assert_awaited_once_with(invoice_uuid="invoice-uuid-1")


async def test_concurrent_success_and_cancel_are_serialized(webhook_env, monkeypatch):
    lock = asyncio.Lock()

    class LockedSession(_FakeSession):
        locked = False

        async def __aexit__(self, *exc_info):
            if self.locked:
                lock.release()
            return False

    async def get_locked(session, payment_id, *, for_update=False):
        assert for_update
        await lock.acquire()
        session.locked = True
        return webhook_env["payment"]

    service = _service()
    service.async_session_factory = LockedSession
    monkeypatch.setattr(payment_dal, "get_payment_by_db_id", get_locked)
    original_activation = service.subscription_service.activate_subscription

    async def slow_activation(*args, **kwargs):
        await asyncio.sleep(0)
        return await original_activation(*args, **kwargs)

    service.subscription_service.activate_subscription = AsyncMock(side_effect=slow_activation)
    responses = await asyncio.gather(*[
        service.webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))
        for status in ["paid", "cancel", "paid", "underpaid", "overpaid"]
    ])
    assert all(r.status == 200 for r in responses)
    assert webhook_env["payment"].status == "succeeded"
    service.subscription_service.activate_subscription.assert_awaited_once()


class _ScriptedHttp:
    def __init__(self, *events):
        self.events = list(events)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        result = self.events.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _invoice_result(**changes):
    result = {
        "uuid": "invoice-uuid-1", "order_id": str(PAYMENT_ID),
        "amount": "129.00", "currency": "RUB", "payment_status": "check",
        "url": "https://2328.io/pay/invoice-uuid-1",
    }
    result.update(changes)
    return {"state": 0, "result": result}


@pytest.mark.parametrize("first", [asyncio.TimeoutError(), _FakeResponse({}, status=502), _FakeResponse({}, status=429)])
async def test_uncertain_creation_is_recovered_by_the_same_order_id(monkeypatch, first):
    service = _service()
    http = _ScriptedHttp(first, _FakeResponse(_invoice_result()))
    monkeypatch.setattr(service, "_get_session", AsyncMock(return_value=http))
    ok, result = await _create(service)
    assert ok and result["uuid"] == "invoice-uuid-1"
    assert [url.rsplit("/api", 1)[-1] for url, _ in http.posts] == ["/v1/payment", "/v1/payment/info"]
    assert json.loads(http.posts[1][1]["data"]) == {"order_id": str(PAYMENT_ID)}
    for _, request in http.posts:
        assert request["headers"]["sign"] == _sign_text(request["data"].decode())
        assert request["allow_redirects"] is False


async def test_unknown_creation_is_not_reported_as_definitive_failure(monkeypatch):
    service = _service()
    http = _ScriptedHttp(asyncio.TimeoutError(), asyncio.TimeoutError())
    monkeypatch.setattr(service, "_get_session", AsyncMock(return_value=http))
    ok, result = await _create(service)
    assert not ok and result["uncertain"] is True
    assert len(http.posts) == 2


async def test_malformed_api_result_cannot_create_a_payment(monkeypatch):
    service = _service()
    http = _ScriptedHttp(_FakeResponse(["unexpected"]), _FakeResponse({"state": 1}, status=404))
    monkeypatch.setattr(service, "_get_session", AsyncMock(return_value=http))
    ok, result = await _create(service)
    assert not ok and result["uncertain"]


@pytest.mark.parametrize("change", [{"order_id": "99"}, {"currency": "USD"}, {"amount": "128.00"}, {"url": "http://2328.io/pay/x"}, {"uuid": None}])
async def test_creation_response_must_match_our_order(monkeypatch, change):
    service = _service()
    http = _ScriptedHttp(_FakeResponse(_invoice_result(**change)), _FakeResponse(_invoice_result(**change)))
    monkeypatch.setattr(service, "_get_session", AsyncMock(return_value=http))
    ok, _ = await _create(service)
    assert not ok


async def test_preselected_currency_always_includes_network(service_with_http):
    service, http = service_with_http
    service.to_currency = "USDT"
    service.network = "TRX-TRC20"
    ok, _ = await _create(service)
    body = json.loads(http.posts[0]["data"])
    assert ok and body["to_currency"] == "USDT" and body["network"] == "TRX-TRC20"
    assert body["currency"] == "RUB"


async def test_default_hosted_checkout_does_not_force_direction(service_with_http):
    service, http = service_with_http
    await _create(service)
    body = json.loads(http.posts[0]["data"])
    assert "to_currency" not in body and "network" not in body
    assert body["url_return"] == body["url_success"] == "https://t.me/test_bot"
    assert body["description"] == "Subscription for 1 month"
    assert "additional_data" not in body and "lifetime" not in body


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), "invalid"])
async def test_invalid_creation_amount_never_reaches_network(monkeypatch, amount):
    service = _service()
    get_session = AsyncMock()
    monkeypatch.setattr(service, "_get_session", get_session)
    ok, _ = await _create(service, amount=amount)
    assert not ok
    get_session.assert_not_awaited()


def test_direction_and_ttl_settings_are_validated():
    from pydantic import ValidationError
    from config.settings import Settings

    settings = Settings(BOT_TOKEN="test:token", _env_file=None, PAY2328_TTL_SECONDS=86400)
    assert settings.pay2328_webhook_path == "/webhook/2328"
    assert "2328" in settings.payment_methods_order
    for fields in [dict(PAY2328_TO_CURRENCY="USDT"), dict(PAY2328_NETWORK="TON"), dict(PAY2328_TTL_SECONDS=299), dict(PAY2328_TTL_SECONDS=86401)]:
        with pytest.raises(ValidationError):
            Settings(BOT_TOKEN="test:token", _env_file=None, **fields)
    settings = Settings(BOT_TOKEN="test:token", _env_file=None, PAY2328_TO_CURRENCY=" usdt ", PAY2328_NETWORK=" trx-trc20 ")
    assert settings.PAY2328_TO_CURRENCY == "USDT" and settings.PAY2328_NETWORK == "TRX-TRC20"


def test_button_is_available_only_with_payment_credentials():
    from config.settings import Settings
    from bot.keyboards.inline.user_keyboards import get_payment_method_keyboard
    settings = Settings(BOT_TOKEN="test:token", _env_file=None, PAY2328_ENABLED=True,
                        WEBHOOK_BASE_URL="https://bot.example", PAYMENT_METHODS_ORDER="2328")
    i18n = SimpleNamespace(gettext=lambda lang, key, **kwargs: key)
    def callbacks():
        markup = get_payment_method_keyboard(1, 129.0, None, "RUB", "ru", i18n, settings)
        return [button.callback_data for row in markup.inline_keyboard for button in row]
    assert not any((value or "").startswith("pay_2328:") for value in callbacks())
    settings.PAY2328_PROJECT_ID = PROJECT_ID
    settings.PAY2328_API_KEY = API_KEY
    assert "pay_2328:1:129.0:subscription" in callbacks()


async def test_real_database_rolls_back_failure_then_accepts_retry(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import Base, User, Payment

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(User(user_id=111, language_code="ru"))
            session.add(Payment(payment_id=PAYMENT_ID, user_id=111, amount=129.0,
                                currency="RUB", provider="2328", status="pending_2328",
                                base_amount=129.0, fx_rate=1.0,
                                subscription_duration_months=1, provider_payment_id="invoice-uuid-1"))
            await session.commit()
        service = _service()
        service.async_session_factory = sessions
        service.subscription_service.activate_subscription = AsyncMock(side_effect=RuntimeError("panel unavailable"))
        response = await service.webhook_route(_FakeRequest(_signed_webhook()))
        assert response.status == 500
        async with sessions() as session:
            assert (await session.get(Payment, PAYMENT_ID)).status == "pending_2328"
        service.subscription_service.activate_subscription = AsyncMock(return_value={
            "end_date": datetime.now(timezone.utc) + timedelta(days=30), "subscription_url": "https://sub.example/link",
        })
        monkeypatch.setattr(pay2328_module, "prepare_config_links", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(pay2328_module, "get_connect_and_main_keyboard", lambda *args, **kwargs: None)
        monkeypatch.setattr(pay2328_module, "NotificationService", lambda *args: SimpleNamespace(notify_payment_received=AsyncMock()))
        response = await service.webhook_route(_FakeRequest(_signed_webhook()))
        assert response.status == 200
        async with sessions() as session:
            assert (await session.get(Payment, PAYMENT_ID)).status == "succeeded"
        response = await service.webhook_route(_FakeRequest(_signed_webhook(payment_status="cancel")))
        assert response.status == 200
        async with sessions() as session:
            assert (await session.get(Payment, PAYMENT_ID)).status == "succeeded"
        service.subscription_service.activate_subscription.assert_awaited_once()
    finally:
        await engine.dispose()


async def test_successful_refund_can_follow_a_failed_refund_attempt(webhook_env):
    service = _service()
    for status in ["refund_fail", "refund_paid", "refund_process", "refund_fail"]:
        assert (await service.webhook_route(_FakeRequest(_signed_webhook(payment_status=status)))).status == 200
    assert webhook_env["payment"].status == "refund_paid"
    assert webhook_env["activated"] == []


async def test_unknown_attempt_reuse_is_scoped_to_the_same_user_and_offer():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import Base, User, Payment

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(User(user_id=111))
            session.add(Payment(payment_id=7, user_id=111, amount=129.0, currency="RUB",
                                provider="2328", status="pending_2328_unknown",
                                subscription_duration_months=1, hwid_device_limit=3,
                                provider_payment_id="uuid-from-early-webhook"))
            await session.commit()
            query = dict(user_id=111, provider="2328", amount=129.0, currency="RUB", months=1, device_limit=3)
            assert (await payment_dal.get_unconfirmed_provider_payment(session, **query)).payment_id == 7
            for changed in [dict(user_id=222), dict(provider="other"), dict(amount=130.0), dict(currency="USD"), dict(months=3), dict(device_limit=5)]:
                assert await payment_dal.get_unconfirmed_provider_payment(session, **(query | changed)) is None
    finally:
        await engine.dispose()
