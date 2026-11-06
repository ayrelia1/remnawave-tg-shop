"""Top-ups must never be interpreted as subscription months by a provider."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.user.payment import process_successful_payment
from bot.services.crypto_pay_service import CryptoPayService
from bot.services.freekassa_service import FreeKassaService
from bot.services.severpay_service import SeverPayService
from bot.services.stars_service import StarsService
from config.settings import Settings
from db.dal import payment_dal, user_dal
from db.models import LteAccount
from test_lte_service import flow as lte_flow_fixture
from test_lte_service import invoice

# Re-export the isolated database/panel fixture for pytest's module discovery.
flow = lte_flow_fixture


def test_topup_saved_card_back_button_returns_to_package():
    from bot.keyboards.inline.user_keyboards import get_yk_autopay_choice_keyboard
    i18n = SimpleNamespace(gettext=lambda lang, key, **kw: key)
    markup = get_yk_autopay_choice_keyboard(25, 100, "ru", i18n, sale_mode="lte_topup")
    assert markup.inline_keyboard[-1][0].callback_data == "lte_package:25"


async def test_rub_and_stars_only_packages_are_both_visible(flow, monkeypatch):
    from bot.handlers.user.subscription import payments_subscription as handler
    from bot.middlewares.i18n import JsonI18n
    from db.models import Subscription
    from pathlib import Path
    edit = AsyncMock()
    monkeypatch.setattr(handler, "safe_edit_text", edit)
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=False)
    callback = SimpleNamespace(message=SimpleNamespace(), from_user=SimpleNamespace(id=1), answer=AsyncMock())
    async with flow.factory() as session:
        session.add(Subscription(user_id=1, panel_user_uuid="42", panel_subscription_uuid="short",
                                 end_date=datetime.now(timezone.utc) + timedelta(days=1), is_active=True))
        flow.settings.traffic_packages = {50: 200}
        flow.settings.stars_traffic_packages = {100: 300}
        await session.commit()
        await handler.show_lte_packages(callback, flow.settings, {"i18n_instance": i18n}, session)
    markup = edit.await_args.kwargs["reply_markup"]
    buttons = {button.callback_data: button.text for row in markup.inline_keyboard for button in row}
    assert "RUB" in buttons["lte_package:50"]
    assert "⭐" in buttons["lte_package:100"]


@pytest.mark.parametrize("devices,traffic_mode,value", [(None, True, 100), (5, False, 3)])
async def test_stars_only_offer_opens_checkout_with_fiat_providers_enabled(
        flow, monkeypatch, devices, traffic_mode, value):
    from bot.handlers.user.subscription import payments_subscription as handler
    from bot.middlewares.i18n import JsonI18n
    from pathlib import Path
    from sqlalchemy import select

    edit = AsyncMock()
    monkeypatch.setattr(handler, "safe_edit_text", edit)
    i18n = JsonI18n(str(Path(__file__).resolve().parents[1] / "locales"), premium_emoji=False)
    callback = SimpleNamespace(message=SimpleNamespace(), from_user=SimpleNamespace(id=1), answer=AsyncMock())
    base = Settings(_env_file=None, BOT_TOKEN="test", STARS_ENABLED=True, YOOKASSA_ENABLED=True,
                    PAYMENT_METHODS_ORDER="yookassa,stars", DEVICE_PLANS_STARS="5:0,500,0,0",
                    STARS_TRAFFIC_PACKAGES="100:300")
    async with flow.factory() as session:
        await handler._show_payment_methods_screen(
            callback, settings=base, i18n=i18n, current_lang="ru",
            get_text=lambda key, **kw: i18n.gettext("ru", key, **kw), session=session,
            months=value, devices=devices, traffic_mode=traffic_mode, promo_code_service=None)
    edit.assert_awaited_once()
    markup = edit.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert sum(data.startswith("pay_stars:") for data in callbacks) == 1
    assert not any(data.startswith("pay_yk:") for data in callbacks)


@pytest.mark.parametrize("provider", ["freekassa", "severpay"])
async def test_fiat_provider_routes_topup_without_referral_days(provider, monkeypatch):
    payment = SimpleNamespace(payment_id=7, user_id=1, amount=100, currency="RUB",
                              status="pending", subscription_duration_months=0, product_type="lte_topup",
                              lte_traffic_bytes=25 * 2**30, promo_code_id=None, hwid_device_limit=None, user=None)
    session = AsyncMock()
    session.__aenter__.return_value = session
    subscription = SimpleNamespace(activate_subscription=AsyncMock(return_value={
        "end_date": datetime.now(timezone.utc) + timedelta(days=30),
        "subscription_url": "https://example.test/sub/key", "applied_promo_bonus_days": 0,
    }))
    referral = SimpleNamespace(apply_referral_bonuses_for_payment=AsyncMock())
    settings = Settings(_env_file=None, BOT_TOKEN="test", LOG_PAYMENTS=False,
                        FREEKASSA_ENABLED=True, FREEKASSA_MERCHANT_ID="1", FREEKASSA_API_KEY="test-key",
                        FREEKASSA_SECOND_SECRET="test-secret", FREEKASSA_PAYMENT_IP="127.0.0.1",
                        SEVERPAY_ENABLED=True, SEVERPAY_MID=1, SEVERPAY_TOKEN="test-token")
    kwargs = dict(bot=SimpleNamespace(send_message=AsyncMock()), settings=settings,
                  i18n=SimpleNamespace(gettext=lambda lang, key, **kw: key),
                  async_session_factory=lambda: session, subscription_service=subscription, referral_service=referral)
    monkeypatch.setattr(payment_dal, "get_payment_by_db_id", AsyncMock(return_value=payment))
    monkeypatch.setattr(payment_dal, "mark_provider_payment_succeeded_once", AsyncMock(return_value=True))
    monkeypatch.setattr(user_dal, "get_user_by_id", AsyncMock(return_value=SimpleNamespace(
        language_code="ru", first_name="Test", username=None, referred_by_id=None)))
    if provider == "freekassa":
        service = FreeKassaService(**kwargs)
        body = {"MERCHANT_ID": "1", "MERCHANT_ORDER_ID": "7", "AMOUNT": "100", "intid": "tx-1"}
        body["SIGN"] = hashlib.md5(b"1:100:test-secret:7", usedforsecurity=False).hexdigest()
    else:
        service = SeverPayService(**kwargs, default_return_url="test_bot")
        body = {"type": "payin", "data": {"id": "tx-1", "order_id": "7", "status": "success",
                                           "amount": "100", "currency": "RUB"}}
        body["sign"] = service._sign_payload(body)
    request = SimpleNamespace(json=AsyncMock(return_value=body), post=AsyncMock(return_value=body), app={})
    response = await service.webhook_route(request)
    assert response.status == 200
    subscription.activate_subscription.assert_awaited_once()
    call = subscription.activate_subscription.await_args
    assert call.args[2] == 0
    assert call.kwargs["sale_mode"] == "lte_topup"
    assert call.kwargs["traffic_gb"] == 25
    referral.apply_referral_bonuses_for_payment.assert_not_awaited()


async def test_stars_invoice_freezes_topup_and_duplicate_payment_is_idempotent(flow):
    flow.settings.STARS_PROVIDER_TOKEN = ""
    flow.settings.SUBSCRIPTION_MINI_APP_URL = None
    flow.settings.LOG_PAYMENTS = False
    flow.bot.send_invoice = AsyncMock()
    referral = SimpleNamespace(apply_referral_bonuses_for_payment=AsyncMock())
    stars = StarsService(flow.bot, flow.settings, flow.lte.i18n, flow.subscription, referral)
    async with flow.factory() as session:
        payment_id = await stars.create_invoice(session, 1, 25, 100, "Traffic top-up", sale_mode="lte_topup")
        assert payment_id is not None
        payment = await payment_dal.get_payment_by_db_id(session, payment_id)
        assert payment.subscription_duration_months == 0
        assert payment.lte_traffic_bytes == 25 * 2**30
        message = SimpleNamespace(from_user=SimpleNamespace(id=1),
                                  successful_payment=SimpleNamespace(provider_payment_charge_id="charge-1"))
        # Stale client-side product/months must not change the issued invoice.
        await stars.process_successful_payment(session, message, payment_id, 999, 100,
                                                {"i18n_instance": flow.lte.i18n}, sale_mode="subscription")
        await stars.process_successful_payment(session, message, payment_id, 999, 100,
                                                {"i18n_instance": flow.lte.i18n}, sale_mode="subscription")
        assert (await session.get(LteAccount, 1)).credited_bytes == 25 * 2**30
        referral.apply_referral_bonuses_for_payment.assert_not_awaited()


async def test_cryptopay_uses_database_product_even_with_stale_metadata(flow):
    flow.settings.CRYPTOPAY_ASSET = "RUB"
    flow.settings.SUBSCRIPTION_MINI_APP_URL = None
    flow.settings.LOG_PAYMENTS = False
    async with flow.factory() as session:
        payment = await invoice(session, "lte_topup", 25, None)
        payment.provider = "cryptopay"
        payment.status = "pending_cryptopay"
        payment_id = payment.payment_id
        await session.commit()
    payload = json.dumps({"user_id": "1", "payment_db_id": str(payment_id),
                          "subscription_months": "999", "sale_mode": "subscription"})
    update = SimpleNamespace(payload=SimpleNamespace(payload=payload, fiat="RUB", asset=None,
                                                     paid_asset=None, amount="100", invoice_id=55))
    referral = SimpleNamespace(apply_referral_bonuses_for_payment=AsyncMock())
    app = dict(async_session_factory=flow.factory, bot=flow.bot, settings=flow.settings,
               i18n=flow.lte.i18n, subscription_service=flow.subscription, referral_service=referral)
    service = CryptoPayService.__new__(CryptoPayService)
    await service._invoice_paid_handler(update, app)
    await service._invoice_paid_handler(update, app)
    async with flow.factory() as session:
        assert (await session.get(LteAccount, 1)).credited_bytes == 25 * 2**30
    referral.apply_referral_bonuses_for_payment.assert_not_awaited()


async def test_yookassa_uses_database_product_even_with_stale_metadata(flow):
    settings = Settings(_env_file=None, BOT_TOKEN="test", LOG_PAYMENTS=False,
                        YOOKASSA_AUTOPAYMENTS_ENABLED=False)
    referral = SimpleNamespace(apply_referral_bonuses_for_payment=AsyncMock())
    async with flow.factory() as session:
        payment = await invoice(session, "lte_topup", 25, None)
        payment.provider = "yookassa"
        payment.status = "pending_yookassa"
        payment.yookassa_payment_id = "yk-1"
        payment.provider_payment_id = "yk-1"
        await session.commit()
        payload = {"id": "yk-1", "status": "succeeded", "paid": True,
                   "amount": {"value": "100.00", "currency": "RUB"},
                   "metadata": {"user_id": "1", "subscription_months": "999", "sale_mode": "subscription",
                                "payment_db_id": str(payment.payment_id)}}
        assert await process_successful_payment(session, flow.bot, payload, flow.lte.i18n, settings,
                                                flow.panel, flow.subscription, referral)
        assert await process_successful_payment(session, flow.bot, payload, flow.lte.i18n, settings,
                                                flow.panel, flow.subscription, referral)
        assert (await session.get(LteAccount, 1)).credited_bytes == 25 * 2**30
    referral.apply_referral_bonuses_for_payment.assert_not_awaited()
