"""Platega webhook amount validation.

Platega adds the client-side commission on top of the order price and rounds it
on its own side, so the same 129 RUB tariff arrives as 136.10 or as 137. The
webhook must activate the subscription in every one of those cases — rejecting
it means the customer paid and got nothing. Only an underpayment is refused.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import platega_service as platega_module
from bot.services.platega_service import PlategaService
from db.dal import payment_dal, user_dal


MERCHANT_ID = "merchant-1"
SECRET = "secret-1"
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
    """Only `.json()` and `.headers.get()` are touched by the webhook route."""

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers if headers is not None else {
            "X-MerchantId": MERCHANT_ID,
            "X-Secret": SECRET,
        }

    async def json(self):
        return self._body


def _payment():
    return SimpleNamespace(
        payment_id=7,
        user_id=111,
        amount=ORDER_AMOUNT,
        currency="RUB",
        status="pending_platega",
        subscription_duration_months=1,
        promo_code_id=None,
        hwid_device_limit=None,
    )


@pytest.fixture
def marked_calls(monkeypatch):
    """Stub everything the success path touches; record the activation call."""
    calls = []

    async def fake_get_payment(session, provider_payment_id):
        return _payment()

    async def fake_mark_once(session, payment_db_id, provider_payment_id):
        calls.append((payment_db_id, provider_payment_id))
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

    def fake_keyboard(*args, **kwargs):
        return None

    class _FakeNotificationService:
        def __init__(self, *args, **kwargs):
            pass

        async def notify_payment_received(self, **kwargs):
            return None

    monkeypatch.setattr(payment_dal, "get_payment_by_provider_payment_id", fake_get_payment)
    monkeypatch.setattr(payment_dal, "mark_provider_payment_succeeded_once", fake_mark_once)
    monkeypatch.setattr(user_dal, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(platega_module, "prepare_config_links", fake_prepare_config_links)
    monkeypatch.setattr(platega_module, "get_connect_and_main_keyboard", fake_keyboard)
    monkeypatch.setattr(platega_module, "NotificationService", _FakeNotificationService)
    return calls


@pytest.fixture
def service():
    settings = SimpleNamespace(
        PLATEGA_ENABLED=True,
        PLATEGA_BASE_URL="https://app.platega.io",
        PLATEGA_MERCHANT_ID=MERCHANT_ID,
        PLATEGA_SECRET=SECRET,
        PLATEGA_PAYMENT_METHOD=2,
        PLATEGA_RETURN_URL=None,
        PLATEGA_FAILED_URL=None,
        DEFAULT_LANGUAGE="ru",
        traffic_sale_mode=False,
    )

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

    return PlategaService(
        bot=_Bot(),
        settings=settings,
        i18n=_I18n(),
        async_session_factory=_FakeSession,
        subscription_service=_SubscriptionService(),
        referral_service=_ReferralService(),
        default_return_url="test_bot",
    )


def _confirmed(amount):
    return {"id": "tx-1", "status": "CONFIRMED", "amount": amount, "currency": "RUB"}


@pytest.mark.parametrize(
    "amount, label",
    [
        (129.0, "exact order price (merchant pays the fee)"),
        (136.10, "5.5% client commission, rounded to kopecks"),
        (137.0, "client commission rounded up to a whole ruble"),
        (128.99, "a kopeck short — provider-side rounding"),
        (500.0, "large overpayment"),
    ],
)
async def test_overpayment_and_commission_are_accepted(service, marked_calls, amount, label):
    response = await service.webhook_route(_FakeRequest(_confirmed(amount)))

    assert response.status == 200, f"{label}: webhook rejected a real payment"
    assert marked_calls == [(7, "tx-1")], f"{label}: subscription was not activated"


async def test_underpayment_is_rejected(service, marked_calls):
    response = await service.webhook_route(_FakeRequest(_confirmed(100.0)))

    assert response.status == 400
    assert response.text == "amount_mismatch"
    assert marked_calls == []


async def test_unparsable_amount_still_activates(service, marked_calls):
    """The transaction id and header auth already identify the payment."""
    response = await service.webhook_route(_FakeRequest(_confirmed("not-a-number")))

    assert response.status == 200
    assert marked_calls == [(7, "tx-1")]


async def test_missing_amount_still_activates(service, marked_calls):
    body = _confirmed(None)
    body.pop("amount")

    response = await service.webhook_route(_FakeRequest(body))

    assert response.status == 200
    assert marked_calls == [(7, "tx-1")]


async def test_currency_mismatch_is_rejected(service, marked_calls):
    body = _confirmed(136.10)
    body["currency"] = "USD"

    response = await service.webhook_route(_FakeRequest(body))

    assert response.status == 400
    assert response.text == "currency_mismatch"
    assert marked_calls == []


async def test_bad_auth_headers_are_rejected(service, marked_calls):
    request = _FakeRequest(
        _confirmed(136.10),
        headers={"X-MerchantId": MERCHANT_ID, "X-Secret": "wrong"},
    )

    response = await service.webhook_route(request)

    assert response.status == 403
    assert marked_calls == []
