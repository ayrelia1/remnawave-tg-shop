"""Heleket crypto payment gateway.

Contract source: https://doc.heleket.com (verified against the August 2026 docs).

Two facts drive the shape of this module:

* **Every request is signed as `md5(base64(body) + API_KEY)`** and the signature
  travels in the `sign` header next to a `merchant` header. Because the hash is
  taken over the *serialised body*, the exact bytes that were signed have to be
  the exact bytes that go on the wire — hence `data=` with a pre-rendered
  string rather than aiohttp's `json=`, which would re-serialise.
* **The webhook signs the same way**, except the signature sits *inside* the
  JSON body under `sign` and covers the original body with that field removed.
  The raw request bytes must be preserved: parsing and re-serialising JSON can
  change escaping, whitespace or number formatting and invalidate a genuine
  callback.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.services.referral_service import ReferralService
from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
from bot.services.notification_service import NotificationService
from db.dal import payment_dal, user_dal
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display
from bot.utils.config_link import prepare_config_links


# Heleket payment statuses, per https://doc.heleket.com/methods/payments/payment-statuses
# Only these two mean the money is in and the order must be fulfilled.
SUCCESS_STATUSES = {"paid", "paid_over"}
# Terminal failures — the invoice will never be paid.
FAILED_STATUSES = {
    "fail",
    "cancel",
    "system_fail",
    "wrong_amount",
    "locked",
    "refund_process",
    "refund_fail",
    "refund_paid",
}
# Everything else ("check", "confirm_check", "process", "wrong_amount_waiting")
# is an intermediate state: acknowledge it and wait for the next callback.


class HeleketService:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
        default_return_url: str,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.subscription_service = subscription_service
        self.referral_service = referral_service

        self.base_url = (settings.HELEKET_BASE_URL or "https://api.heleket.com").rstrip("/")
        self.merchant_id = settings.HELEKET_MERCHANT_ID
        self.api_key = settings.HELEKET_API_KEY
        self.to_currency = (settings.HELEKET_TO_CURRENCY or "").strip().upper() or None
        self.lifetime_seconds = settings.HELEKET_LIFETIME_SECONDS
        self.return_url = settings.HELEKET_RETURN_URL or f"https://t.me/{default_return_url}"
        self.success_url = settings.HELEKET_SUCCESS_URL or self.return_url
        self.callback_url = settings.heleket_full_webhook_url

        self._timeout = ClientTimeout(total=20)
        self._session: Optional[ClientSession] = None
        self.configured: bool = bool(
            settings.HELEKET_ENABLED and self.merchant_id and self.api_key
        )
        if not self.configured:
            logging.warning("HeleketService initialized but not fully configured. Payments disabled.")
        elif not self.callback_url:
            logging.warning(
                "HeleketService is configured but WEBHOOK_BASE_URL is empty: Heleket "
                "has nowhere to deliver payment callbacks, so paid invoices will not activate."
            )

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ signing

    @staticmethod
    def _canonical_json(payload: Dict[str, Any], escape_slashes: bool = True) -> str:
        """Render `payload` the way PHP's json_encode would.

        Heleket hashes the compact form with no spaces between tokens. PHP also
        escapes forward slashes unless JSON_UNESCAPED_SLASHES is passed, and the
        docs explicitly call this out as the usual cross-language mismatch.
        """
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return rendered.replace("/", "\\/") if escape_slashes else rendered

    def _sign(self, body: str) -> str:
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return hashlib.md5(f"{encoded}{self.api_key or ''}".encode("utf-8")).hexdigest()

    @staticmethod
    def _strip_webhook_sign(raw_body: str, received: str) -> Optional[str]:
        """Remove ``sign`` without re-serialising the rest of the JSON body."""
        pattern = re.compile(r'"sign"\s*:\s*"' + re.escape(received) + r'"')
        matches = list(pattern.finditer(raw_body))
        if len(matches) != 1:
            return None

        match = matches[0]
        start, end = match.start(), match.end()

        # Heleket normally appends sign as the last field. Consume the leading
        # comma and whitespace so the exact unsigned body is reconstructed.
        field_start = start
        while field_start > 0 and raw_body[field_start - 1] in " \t\n\r":
            field_start -= 1
        if field_start > 0 and raw_body[field_start - 1] == ",":
            return raw_body[: field_start - 1] + raw_body[end:]

        # Also handle a sign placed first by consuming its following comma.
        field_end = end
        while field_end < len(raw_body) and raw_body[field_end] in " \t\n\r":
            field_end += 1
        if field_end < len(raw_body) and raw_body[field_end] == ",":
            return raw_body[:start] + raw_body[field_end + 1 :]

        # If sign is the only field, the signed representation is an empty
        # object with the surrounding braces left intact.
        return raw_body[:start] + raw_body[end:]

    def verify_webhook_sign(
        self,
        raw_body: bytes,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Check the webhook signature against the original request bytes."""
        try:
            body_text = raw_body.decode("utf-8")
            parsed = data if data is not None else json.loads(body_text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False

        if not isinstance(parsed, dict):
            return False

        received = parsed.get("sign")
        if not isinstance(received, str) or not received:
            return False

        unsigned_body = self._strip_webhook_sign(body_text, received)
        if unsigned_body is None:
            return False

        candidate = self._sign(unsigned_body)
        return hmac.compare_digest(candidate, received)

    # ------------------------------------------------------------------ invoices

    async def create_invoice(
        self,
        *,
        payment_db_id: int,
        user_id: int,
        months: float,
        amount: float,
        currency: str,
        description: str,
        promo_code_service=None,
        session=None,
    ) -> Tuple[bool, Dict[str, Any]]:
        if not self.configured:
            logging.error("HeleketService is not configured. Cannot create invoice.")
            return False, {"message": "service_not_configured"}

        # Mirrors the other gateways: the price arriving here is already
        # discounted, so the original is reconstructed only to store metadata.
        original_amount = None
        discount_amount = None
        promo_code_id = None

        if promo_code_service and session:
            from db.dal import active_discount_dal

            active_discount = await active_discount_dal.get_active_discount(session, user_id)
            if active_discount:
                discount_pct = active_discount.discount_percentage
                promo_code_id = active_discount.promo_code_id
                denominator = 1 - discount_pct / 100
                if denominator <= 0:
                    traffic_mode = bool(getattr(self.settings, "traffic_sale_mode", False))
                    price_source = (
                        getattr(self.settings, "traffic_packages", {}) or {}
                        if traffic_mode
                        else (self.settings.subscription_options or {})
                    )
                    fallback_original = price_source.get(months)
                    if fallback_original is not None:
                        original_amount = fallback_original
                        discount_amount = original_amount - amount
                    else:
                        logging.warning(
                            "Heleket discount %s%% has invalid denominator and no fallback price for months=%s.",
                            discount_pct,
                            months,
                        )
                else:
                    original_amount = amount / denominator
                    discount_amount = original_amount - amount

                if original_amount is not None:
                    logging.info(
                        "Recording %s%% discount for Heleket payment: original %.2f -> final %s",
                        discount_pct,
                        original_amount,
                        amount,
                    )
                    try:
                        await payment_dal.update_payment_discount_info(
                            session,
                            payment_db_id,
                            original_amount,
                            discount_amount,
                            promo_code_id,
                        )
                        await session.commit()
                    except Exception as e_update:
                        logging.warning(
                            "Heleket: failed to update discount metadata for payment %s: %s",
                            payment_db_id,
                            e_update,
                        )

        # order_id must be unique and 1..128 chars of [A-Za-z0-9_-]; the payment
        # row id satisfies both and is what the webhook is resolved back through.
        body: Dict[str, Any] = {
            "amount": f"{Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
            "currency": currency.upper(),
            "order_id": str(payment_db_id),
            "lifetime": int(self.lifetime_seconds),
            "url_return": self.return_url,
            "url_success": self.success_url,
            "additional_data": description[:255],
        }
        if self.callback_url:
            body["url_callback"] = self.callback_url
        if self.to_currency:
            body["to_currency"] = self.to_currency

        # The signature covers these exact bytes, so they are what gets posted.
        rendered_body = self._canonical_json(body)
        headers = {
            "merchant": self.merchant_id or "",
            "sign": self._sign(rendered_body),
            "Content-Type": "application/json",
        }

        http_session = await self._get_session()
        url = f"{self.base_url}/v1/payment"
        try:
            async with http_session.post(
                url, data=rendered_body.encode("utf-8"), headers=headers
            ) as response:
                response_text = await response.text()
                try:
                    response_data = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    logging.error("Heleket create_invoice: invalid JSON response: %s", response_text)
                    return False, {
                        "status": response.status,
                        "message": "invalid_json",
                        "raw": response_text,
                    }

                # Heleket wraps every answer as {"state": 0|1, "result": {...}}.
                state = response_data.get("state")
                result = response_data.get("result") or {}
                if response.status != 200 or state != 0 or not result:
                    logging.error(
                        "Heleket create_invoice: API returned error (status=%s, body=%s)",
                        response.status,
                        response_data,
                    )
                    return False, {"status": response.status, "message": response_data}

                return True, result
        except Exception as exc:
            logging.error("Heleket create_invoice: request failed: %s", exc, exc_info=True)
            return False, {"message": str(exc)}

    # ------------------------------------------------------------------ webhook

    async def _resolve_payment(self, session, invoice_uuid: str, order_id: str):
        """Find the payment row a callback belongs to.

        `order_id` is authoritative — this service puts the payment row id there
        — but the uuid is tried first because that is what gets stored on the
        row when the invoice is created.
        """
        if invoice_uuid:
            payment = await payment_dal.get_payment_by_provider_payment_id(session, invoice_uuid)
            if payment:
                return payment
        if order_id.isdigit():
            return await payment_dal.get_payment_by_db_id(session, int(order_id))
        return None

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured:
            return web.Response(status=503, text="heleket_disabled")

        try:
            raw_body = await request.read()
            data = json.loads(raw_body)
        except Exception as exc:
            logging.error("Heleket webhook: failed to parse JSON: %s", exc)
            return web.Response(status=400, text="bad_request")

        if not isinstance(data, dict):
            logging.error("Heleket webhook: unexpected payload type %s", type(data).__name__)
            return web.Response(status=400, text="bad_request")

        if not self.verify_webhook_sign(raw_body, data):
            logging.error(
                "Heleket webhook: signature check failed for order %s / uuid %s",
                data.get("order_id"),
                data.get("uuid"),
            )
            return web.Response(status=403, text="forbidden")

        invoice_uuid = str(data.get("uuid") or "").strip()
        order_id = str(data.get("order_id") or "").strip()
        status = str(data.get("status") or "").lower()
        currency = str(data.get("currency") or "").upper()
        amount_raw = data.get("amount")

        if not status or (not invoice_uuid and not order_id):
            logging.error("Heleket webhook: missing identifiers or status in payload: %s", data)
            return web.Response(status=400, text="missing_fields")

        if status not in SUCCESS_STATUSES and status not in FAILED_STATUSES:
            # check / confirm_check / process / wrong_amount_waiting
            logging.info(
                "Heleket webhook: intermediate status '%s' for order %s, waiting.",
                status,
                order_id,
            )
            return web.Response(text="ok_pending")

        async with self.async_session_factory() as session:
            payment = await self._resolve_payment(session, invoice_uuid, order_id)
            if not payment:
                logging.error(
                    "Heleket webhook: payment not found (uuid=%s, order_id=%s)",
                    invoice_uuid,
                    order_id,
                )
                return web.Response(status=404, text="payment_not_found")

            if payment.status == "succeeded" and status in SUCCESS_STATUSES:
                return web.Response(text="ok")

            payment_months = payment.subscription_duration_months or 1
            sale_mode = "traffic" if self.settings.traffic_sale_mode else "subscription"
            provider_payment_id = invoice_uuid or str(payment.payment_id)

            if status in FAILED_STATUSES:
                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        provider_payment_id,
                        "canceled",
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error(
                        "Heleket webhook: failed to cancel payment %s: %s", payment.payment_id, exc
                    )
                    return web.Response(status=500, text="processing_error")

                db_user = await user_dal.get_user_by_id(session, payment.user_id)
                lang = (
                    db_user.language_code
                    if db_user and db_user.language_code
                    else self.settings.DEFAULT_LANGUAGE
                )
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k
                try:
                    await self.bot.send_message(payment.user_id, _("payment_failed"))
                except Exception as exc:
                    logging.debug(
                        "Heleket webhook: failed to notify user %s about cancellation: %s",
                        payment.user_id,
                        exc,
                    )
                return web.Response(text="ok_canceled")

            # --- status is paid or paid_over ---------------------------------
            expected_currency = str(payment.currency or "").upper()
            if currency and expected_currency and currency != expected_currency:
                logging.error(
                    "Heleket webhook: currency mismatch for payment %s (expected %s, got %s)",
                    payment.payment_id,
                    expected_currency,
                    currency,
                )
                return web.Response(status=400, text="currency_mismatch")

            # `amount` is the invoice amount in the invoice currency, so it must
            # match what was ordered. `payment_amount` is denominated in the coin
            # the customer chose and is deliberately not compared here.
            if amount_raw is not None:
                try:
                    incoming = Decimal(str(amount_raw)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    expected = Decimal(str(payment.amount)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    if incoming < expected - Decimal("0.01"):
                        logging.error(
                            "Heleket webhook: invoice amount below order for payment %s (expected %s, got %s)",
                            payment.payment_id,
                            expected,
                            incoming,
                        )
                        return web.Response(status=400, text="amount_mismatch")
                except Exception as exc:
                    # The signature already proves the callback is genuine; a
                    # weird amount format must not cost the customer their order.
                    logging.error(
                        "Heleket webhook: failed to compare amounts for %s: %s",
                        payment.payment_id,
                        exc,
                    )

            try:
                marked = await payment_dal.mark_provider_payment_succeeded_once(
                    session,
                    payment.payment_id,
                    provider_payment_id,
                )
                if not marked:
                    logging.info(
                        "Heleket webhook: payment %s already processed atomically",
                        payment.payment_id,
                    )
                    return web.Response(text="ok")

                activation = await self.subscription_service.activate_subscription(
                    session,
                    payment.user_id,
                    int(payment_months) if sale_mode != "traffic" else 0,
                    float(payment.amount),
                    payment.payment_id,
                    promo_code_id_from_payment=payment.promo_code_id,
                    provider="heleket",
                    sale_mode=sale_mode,
                    traffic_gb=payment_months if sale_mode == "traffic" else None,
                    device_limit=payment.hwid_device_limit if sale_mode != "traffic" else None,
                )
                if not activation or not activation.get("end_date"):
                    raise RuntimeError(
                        f"Heleket webhook: activation failed for payment {payment.payment_id}"
                    )

                referral_bonus = None
                if sale_mode != "traffic":
                    referral_bonus = await self.referral_service.apply_referral_bonuses_for_payment(
                        session,
                        payment.user_id,
                        int(payment_months),
                        current_payment_db_id=payment.payment_id,
                        skip_if_active_before_payment=False,
                    )

                await session.commit()
            except Exception as exc:
                await session.rollback()
                logging.error(
                    "Heleket webhook: failed to process payment %s: %s",
                    provider_payment_id,
                    exc,
                    exc_info=True,
                )
                return web.Response(status=500, text="processing_error")

            db_user = await user_dal.get_user_by_id(session, payment.user_id)
            lang = (
                db_user.language_code
                if db_user and db_user.language_code
                else self.settings.DEFAULT_LANGUAGE
            )
            _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k

            raw_config_link = activation.get("subscription_url") if activation else None
            config_link_display, connect_button_url = await prepare_config_links(
                self.settings, raw_config_link
            )
            config_link_text = config_link_display or _("config_link_not_available")
            final_end = activation.get("end_date") if activation else None
            applied_days = 0
            applied_promo_days = (
                activation.get("applied_promo_bonus_days", 0) if activation else 0
            )

            if referral_bonus and referral_bonus.get("referee_new_end_date"):
                final_end = referral_bonus["referee_new_end_date"]
                applied_days = referral_bonus.get("referee_bonus_applied_days", 0)

            days_total = (
                max(0, (final_end - datetime.now(timezone.utc)).days) if final_end else 0
            )
            traffic_label = (
                str(int(payment_months))
                if float(payment_months).is_integer()
                else f"{payment_months:g}"
            )

            if sale_mode == "traffic":
                text = _(
                    "payment_successful_traffic_full",
                    traffic_gb=traffic_label,
                    end_date=final_end.strftime("%d.%m.%Y") if final_end else "",
                    config_link=config_link_text,
                )
            elif applied_days:
                inviter_name_display = _("friend_placeholder")
                if db_user and db_user.referred_by_id:
                    inviter = await user_dal.get_user_by_id(session, db_user.referred_by_id)
                    if inviter:
                        safe_name = (
                            sanitize_display_name(inviter.first_name)
                            if inviter.first_name
                            else None
                        )
                        if safe_name:
                            inviter_name_display = safe_name
                        elif inviter.username:
                            inviter_name_display = username_for_display(
                                inviter.username, with_at=False
                            )

                text = _(
                    "payment_successful_with_referral_bonus_full",
                    months=payment_months,
                    days=days_total,
                    base_end_date=activation["end_date"].strftime("%d.%m.%Y")
                    if activation and activation.get("end_date")
                    else final_end.strftime("%d.%m.%Y")
                    if final_end
                    else "",
                    bonus_days=applied_days,
                    final_end_date=final_end.strftime("%d.%m.%Y") if final_end else "",
                    inviter_name=inviter_name_display,
                    config_link=config_link_text,
                )
            elif applied_promo_days and final_end:
                text = _(
                    "payment_successful_with_promo_full",
                    months=payment_months,
                    days=days_total,
                    bonus_days=applied_promo_days,
                    end_date=final_end.strftime("%d.%m.%Y"),
                    config_link=config_link_text,
                )
            else:
                text = _(
                    "payment_successful_full",
                    months=payment_months,
                    days=days_total,
                    end_date=final_end.strftime("%d.%m.%Y") if final_end else "",
                    config_link=config_link_text,
                )

            markup = get_connect_and_main_keyboard(
                lang,
                self.i18n,
                self.settings,
                config_link_display,
                connect_button_url=connect_button_url,
                preserve_message=True,
            )
            try:
                await self.bot.send_message(
                    payment.user_id,
                    text,
                    reply_markup=markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                logging.error("Heleket webhook: failed to notify user %s: %s", payment.user_id, exc)

            try:
                notification_service = NotificationService(self.bot, self.settings, self.i18n)
                await notification_service.notify_payment_received(
                    user_id=payment.user_id,
                    amount=float(payment.amount),
                    currency=payment.currency or currency,
                    months=int(payment_months) if sale_mode != "traffic" else 0,
                    traffic_gb=payment_months if sale_mode == "traffic" else None,
                    payment_provider="heleket",
                    username=db_user.username if db_user else None,
                    device_limit=payment.hwid_device_limit if sale_mode != "traffic" else None,
                )
            except Exception as exc:
                logging.error("Heleket webhook: failed to notify admins: %s", exc)

            return web.Response(text="ok")


async def heleket_webhook_route(request: web.Request) -> web.Response:
    service: HeleketService = request.app["heleket_service"]
    return await service.webhook_route(request)
