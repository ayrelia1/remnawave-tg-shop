"""cisPay creation and webhook contract tests."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import cispay_service as cispay_module
from bot.services.cispay_service import CisPayService
from bot.keyboards.inline.user_keyboards import get_payment_method_keyboard
from config.settings import Settings
from db.dal import payment_dal, user_dal


SHOP_ID = "0198f26a-f8a1-7000-8000-000000000001"
API_KEY = "cis_sec_test"
ORDER_AMOUNT = 129.0


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
    def __init__(self, raw_body: bytes, signature: str):
        self._raw_body = raw_body
        self.headers = {"X-Signature": signature}

    async def read(self):
        return self._raw_body


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return self._body


class _RecordingHttpSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    async def close(self):
        self.closed = True


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


def _settings():
    return SimpleNamespace(
        CISPAY_ENABLED=True,
        CISPAY_BASE_URL="https://api.cispay.app",
        CISPAY_SHOP_ID=SHOP_ID,
        CISPAY_API_KEY=API_KEY,
        CISPAY_RETURN_URL="https://t.me/test_bot?start=paid",
        CISPAY_FAILED_URL="https://t.me/test_bot?start=failed",
        DEFAULT_LANGUAGE="ru",
        traffic_sale_mode=False,
    )


def _service():
    return CisPayService(
        bot=_Bot(),
        settings=_settings(),
        i18n=_I18n(),
        async_session_factory=_FakeSession,
        subscription_service=_SubscriptionService(),
        referral_service=_ReferralService(),
        default_return_url="test_bot",
    )


def test_default_settings_place_cispay_after_platega_and_fix_webhook_path():
    settings = Settings(BOT_TOKEN="123:test", _env_file=None)

    assert settings.payment_methods_order[:2] == ["platega", "cispay"]
    assert settings.cispay_webhook_path == "/webhook/cispay"


def test_payment_keyboard_shows_cispay_as_second_method():
    settings = SimpleNamespace(
        payment_methods_order=["platega", "cispay"],
        PLATEGA_ENABLED=True,
        CISPAY_ENABLED=True,
    )

    markup = get_payment_method_keyboard(
        months=1,
        price=129,
        stars_price=None,
        currency_symbol_val="RUB",
        lang="ru",
        i18n_instance=_I18n(),
        settings=settings,
    )
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert buttons[0].text == "pay_with_platega_button"
    assert buttons[0].callback_data.startswith("pay_platega:")
    assert buttons[1].text == "pay_with_cispay_button"
    assert buttons[1].callback_data.startswith("pay_cispay:")


def test_empty_redirect_settings_fall_back_to_the_bot():
    settings = _settings()
    settings.CISPAY_RETURN_URL = None
    settings.CISPAY_FAILED_URL = None

    service = CisPayService(
        bot=_Bot(),
        settings=settings,
        i18n=_I18n(),
        async_session_factory=_FakeSession,
        subscription_service=_SubscriptionService(),
        referral_service=_ReferralService(),
        default_return_url="test_bot",
    )

    assert service.return_url == "https://t.me/test_bot"
    assert service.failed_url == "https://t.me/test_bot"


def _payment(**overrides):
    values = {
        "payment_id": 7,
        "user_id": 111,
        "amount": ORDER_AMOUNT,
        "currency": "RUB",
        "status": "pending_cispay",
        "subscription_duration_months": 1,
        "promo_code_id": None,
        "hwid_device_limit": None,
        "provider": "cispay",
        "provider_payment_id": "tx-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _webhook_payload(**overrides):
    payload = {
        "id": "tx-1",
        "store_id": SHOP_ID,
        "order_id": "cispay-7",
        "payment_method": "SBP",
        "status": "PAID",
        "amount": 12900,
        "currency": "RUB",
        "charged_amount": 13158,
        "merchant_revenue": 12900,
        "paid_at": "2026-09-08T10:00:00+00:00",
        "timestamp": "2026-09-08T10:00:01+00:00",
    }
    payload.update(overrides)
    return payload


def _signed_request(payload, *, key=API_KEY, separators=(",", ":")):
    raw_body = json.dumps(
        payload, ensure_ascii=False, separators=separators
    ).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return _FakeRequest(raw_body, signature)


@pytest.fixture
def webhook_env(monkeypatch):
    payment = _payment()
    marked_calls = []

    async def fake_get_by_provider(session, provider_payment_id):
        return payment if provider_payment_id == payment.provider_payment_id else None

    async def fake_get_by_db_id(session, payment_db_id):
        return payment if payment_db_id == payment.payment_id else None

    async def fake_mark_once(session, payment_db_id, provider_payment_id):
        marked_calls.append((payment_db_id, provider_payment_id))
        return True

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
            return None

    monkeypatch.setattr(
        payment_dal, "get_payment_by_provider_payment_id", fake_get_by_provider
    )
    monkeypatch.setattr(payment_dal, "get_payment_by_db_id", fake_get_by_db_id)
    monkeypatch.setattr(
        payment_dal, "mark_provider_payment_succeeded_once", fake_mark_once
    )
    monkeypatch.setattr(user_dal, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(cispay_module, "prepare_config_links", fake_prepare_config_links)
    monkeypatch.setattr(
        cispay_module, "get_connect_and_main_keyboard", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        cispay_module, "NotificationService", _FakeNotificationService
    )
    return _service(), payment, marked_calls


async def test_create_payment_uses_sbp_kopecks_customer_and_redirects():
    service = _service()
    response = {
        "id": "tx-1",
        "order_id": "cispay-7",
        "status": "PENDING",
        "amount": 12999,
        "charged_amount": 13259,
        "payment_url": "https://cispay.app/pay/tx-1",
        "created_at": "2026-09-08T10:00:00Z",
    }
    http_session = _RecordingHttpSession(_FakeResponse(201, response))
    service._session = http_session

    success, result = await service.create_payment(
        payment_db_id=7,
        user_id=111,
        months=1,
        amount=129.99,
        currency="RUB",
        description="Subscription for 1 month",
    )

    assert success is True
    assert result == response
    assert len(http_session.calls) == 1
    url, kwargs = http_session.calls[0]
    assert url == "https://api.cispay.app/payments"
    assert kwargs["headers"]["X-Shop-ID"] == SHOP_ID
    assert kwargs["headers"]["X-Api-Key"] == API_KEY
    assert kwargs["json"] == {
        "amount": 12999,
        "currency": "RUB",
        "order_id": "cispay-7",
        "payment_method": "SBP",
        "customer_id": "111",
        "redirect_success_url": "https://t.me/test_bot?start=paid",
        "redirect_fail_url": "https://t.me/test_bot?start=failed",
        "description": "Subscription for 1 month",
    }


@pytest.mark.parametrize(
    "amount, charged_amount",
    [
        (12900, 12900),
        (12900, 13158),
        (13158, 13421),
    ],
)
async def test_exact_amount_customer_commission_and_overpayment_are_accepted(
    webhook_env, amount, charged_amount
):
    service, _payment_record, marked_calls = webhook_env

    response = await service.webhook_route(
        _signed_request(
            _webhook_payload(amount=amount, charged_amount=charged_amount)
        )
    )

    assert response.status == 200
    assert marked_calls == [(7, "tx-1")]


async def test_one_kopeck_underpayment_is_rejected(webhook_env):
    service, _payment_record, marked_calls = webhook_env

    response = await service.webhook_route(
        _signed_request(_webhook_payload(amount=12899, charged_amount=13157))
    )

    assert response.status == 400
    assert response.text == "amount_mismatch"
    assert marked_calls == []


@pytest.mark.parametrize("amount", [None, "not-a-number", 12900.5])
async def test_invalid_amount_is_rejected(webhook_env, amount):
    service, _payment_record, marked_calls = webhook_env

    response = await service.webhook_route(
        _signed_request(_webhook_payload(amount=amount))
    )

    assert response.status == 400
    assert response.text == "amount_validation_error"
    assert marked_calls == []


async def test_signature_covers_the_original_request_bytes(webhook_env):
    service, _payment_record, marked_calls = webhook_env
    request = _signed_request(_webhook_payload(), separators=(", ", ": "))

    response = await service.webhook_route(request)

    assert response.status == 200
    assert marked_calls == [(7, "tx-1")]


async def test_invalid_signature_is_rejected(webhook_env):
    service, _payment_record, marked_calls = webhook_env

    response = await service.webhook_route(
        _signed_request(_webhook_payload(), key="foreign-key")
    )

    assert response.status == 403
    assert marked_calls == []


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("store_id", "other-shop", "store_mismatch"),
        ("currency", "USD", "currency_mismatch"),
        ("payment_method", "CARD", "payment_method_mismatch"),
    ],
)
async def test_webhook_contract_mismatches_are_rejected(
    webhook_env, field, value, error
):
    service, _payment_record, marked_calls = webhook_env

    response = await service.webhook_route(
        _signed_request(_webhook_payload(**{field: value}))
    )

    assert response.status in {400, 403}
    assert response.text == error
    assert marked_calls == []


async def test_order_id_recovers_payment_before_provider_id_is_stored(
    webhook_env, monkeypatch
):
    service, payment, marked_calls = webhook_env
    payment.provider_payment_id = None

    async def provider_lookup_misses(session, provider_payment_id):
        return None

    monkeypatch.setattr(
        payment_dal,
        "get_payment_by_provider_payment_id",
        provider_lookup_misses,
    )

    response = await service.webhook_route(_signed_request(_webhook_payload()))

    assert response.status == 200
    assert marked_calls == [(7, "tx-1")]


async def test_already_succeeded_payment_is_idempotent(webhook_env):
    service, payment, marked_calls = webhook_env
    payment.status = "succeeded"

    response = await service.webhook_route(_signed_request(_webhook_payload()))

    assert response.status == 200
    assert marked_calls == []


async def test_order_cannot_resolve_another_provider(webhook_env):
    service, payment, marked_calls = webhook_env
    payment.provider = "platega"

    response = await service.webhook_route(_signed_request(_webhook_payload()))

    assert response.status == 404
    assert marked_calls == []
