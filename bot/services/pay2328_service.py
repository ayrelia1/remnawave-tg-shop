"""2328 hosted crypto checkout.

API contract: https://2328.io/api/payments and https://2328.io/api/webhooks.
Requests use HMAC-SHA256 of Base64-encoded compact UTF-8 JSON. Webhook
fulfillment is serialized by the local payment row lock, independently of
upstream delivery order. Remnawave calls retain the existing transaction flow.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web
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


SUCCESS_STATUSES = {"paid", "overpaid"}
PENDING_STATUSES = {"pending", "check", "awaiting_confirmation", "underpaid_check"}
FAILED_STATUSES = {"cancel", "underpaid", "aml_lock"}
REFUND_STATUSES = {"refund_process", "refund_fail", "refund_paid"}


class Pay2328Service:
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

        self.base_url = (settings.PAY2328_BASE_URL or "https://api.2328.io/api").rstrip("/")
        self.project_id = (settings.PAY2328_PROJECT_ID or "").strip()
        self.api_key = (settings.PAY2328_API_KEY or "").strip()
        self.to_currency = (settings.PAY2328_TO_CURRENCY or "").strip().upper() or None
        self.network = (settings.PAY2328_NETWORK or "").strip().upper() or None
        self.ttl_seconds = settings.PAY2328_TTL_SECONDS
        self.return_url = settings.PAY2328_RETURN_URL or f"https://t.me/{default_return_url}"
        self.success_url = settings.PAY2328_SUCCESS_URL or self.return_url
        self.callback_url = settings.pay2328_full_webhook_url

        self._timeout = ClientTimeout(total=20)
        self._session: Optional[ClientSession] = None
        self.configured = bool(
            settings.PAY2328_ENABLED and self.project_id and self.api_key
            and self.callback_url and self._is_https_url(self.base_url)
            and self._is_https_url(self.callback_url)
            and self._is_https_url(self.return_url)
            and self._is_https_url(self.success_url)
            and bool(self.to_currency) == bool(self.network)
        )
        if settings.PAY2328_ENABLED and not self.configured:
            logging.warning("2328 is enabled but its credentials, URLs or direction are incomplete; payments disabled.")

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _is_https_url(value: str) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = urlsplit(value or "")
            return bool(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)
        except ValueError:
            return False

    @staticmethod
    def _canonical_json(payload: Dict[str, Any]) -> str:
        """2328 signs compact UTF-8 JSON with unescaped Unicode and slashes."""
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _parse_json(raw_body):
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError("non-finite JSON value")

        return json.loads(raw_body, object_pairs_hook=unique_fields, parse_constant=invalid_constant)

    def _sign(self, body: str) -> str:
        return hmac.new(
            (self.api_key or "").encode("utf-8"),
            base64.b64encode(body.encode("utf-8")),
            hashlib.sha256,
        ).hexdigest()

    def verify_webhook_sign(self, raw_body: bytes) -> bool:
        if not self.api_key:
            return False
        try:
            parsed = self._parse_json(raw_body)
            if not isinstance(parsed, dict):
                return False
            received = parsed.pop("sign", None)
            if not isinstance(received, str) or not re.fullmatch(r"[0-9a-f]{64}", received):
                return False
            return hmac.compare_digest(self._sign(self._canonical_json(parsed)), received)
        except (ValueError, UnicodeDecodeError, TypeError, RecursionError):
            return False

    @staticmethod
    def _decimal_amount(value) -> Decimal:
        if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
            raise ValueError("invalid amount")
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("invalid amount")
        return amount

    async def _request(self, path: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        body = self._canonical_json(payload)
        headers = {
            "project": self.project_id or "",
            "sign": self._sign(body),
            "Content-Type": "application/json",
            "User-Agent": "RemnawaveTGShop/1.0",
        }
        try:
            http_session = await self._get_session()
            async with http_session.post(
                f"{self.base_url}{path}", data=body.encode("utf-8"), headers=headers,
                allow_redirects=False,
            ) as response:
                uncertain = response.status >= 500 or response.status == 429
                try:
                    data = self._parse_json(await response.text())
                except (ValueError, UnicodeDecodeError, RecursionError):
                    return False, {"status": response.status, "uncertain": uncertain or response.status == 200}
                if not isinstance(data, dict):
                    return False, {"status": response.status, "uncertain": uncertain or response.status == 200}
                if response.status == 200 and data.get("state") == 0 and isinstance(data.get("result"), dict) and data["result"]:
                    return True, data["result"]
                logging.warning("2328 API request failed: path=%s status=%s", path, response.status)
                return False, {"status": response.status, "uncertain": uncertain or (response.status == 200 and data.get("state") == 0)}
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            logging.warning("2328 API request did not complete: path=%s error=%s", path, type(exc).__name__)
            return False, {"message": "request_failed", "uncertain": True}

    async def get_payment_info(self, *, invoice_uuid: str = "", order_id: str = "") -> Tuple[bool, Dict[str, Any]]:
        if not self.configured or not (invoice_uuid or order_id):
            return False, {"message": "service_not_configured"}
        body = {"uuid": invoice_uuid} if invoice_uuid else {"order_id": order_id}
        return await self._request("/v1/payment/info", body)

    def _invoice_matches(self, invoice, *, order_id: str, amount: Decimal, currency: str) -> bool:
        try:
            return bool(
                isinstance(invoice.get("uuid"), str) and invoice["uuid"].strip()
                and invoice.get("order_id") == order_id
                and invoice.get("currency") == currency
                and isinstance(invoice.get("payment_status"), str)
                and invoice["payment_status"] in SUCCESS_STATUSES | PENDING_STATUSES | FAILED_STATUSES | REFUND_STATUSES
                and self._decimal_amount(invoice.get("amount")) == amount
                and self._is_https_url(invoice.get("url"))
            )
        except (ValueError, TypeError, ArithmeticError):
            return False

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
            logging.error("Pay2328Service is not configured. Cannot create invoice.")
            return False, {"message": "service_not_configured"}

        try:
            invoice_amount = self._decimal_amount(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if invoice_amount <= 0 or currency.upper() != "RUB":
                raise ValueError("invalid invoice")
        except (ValueError, ArithmeticError, AttributeError):
            return False, {"message": "invalid_invoice"}

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
                    issued = await payment_dal.get_payment_by_db_id(session, payment_db_id)
                    fallback_original = getattr(issued, "original_amount", None)
                    if fallback_original is not None:
                        original_amount = fallback_original
                        discount_amount = original_amount - amount
                    else:
                        logging.warning(
                            "2328 discount %s%% has invalid denominator and no fallback price for months=%s.",
                            discount_pct,
                            months,
                        )
                else:
                    original_amount = amount / denominator
                    discount_amount = original_amount - amount

                if original_amount is not None:
                    logging.info(
                        "Recording %s%% discount for 2328 payment: original %.2f -> final %s",
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
                            "2328: failed to update discount metadata for payment %s: %s",
                            payment_db_id,
                            e_update,
                        )

        body: Dict[str, Any] = {
            "amount": format(invoice_amount, ".2f"),
            "currency": currency.upper(),
            "order_id": str(payment_db_id),
            "ttl_seconds": int(self.ttl_seconds),
            "url_return": self.return_url,
            "url_success": self.success_url,
            "url_callback": self.callback_url,
            "description": description[:500],
        }
        if self.to_currency:
            body.update(to_currency=self.to_currency, network=self.network)
        ok, result = await self._request("/v1/payment", body)
        if ok and self._invoice_matches(result, order_id=body["order_id"], amount=invoice_amount, currency=body["currency"]):
            if result.get("payment_status") in FAILED_STATUSES | REFUND_STATUSES:
                return False, {"message": "invoice_unavailable"}
            return True, result
        if ok or result.get("uncertain"):
            # The upstream invoice may exist even when its creation response is lost.
            recovered, invoice = await self.get_payment_info(order_id=body["order_id"])
            if recovered and self._invoice_matches(invoice, order_id=body["order_id"], amount=invoice_amount, currency=body["currency"]):
                if invoice.get("payment_status") in FAILED_STATUSES | REFUND_STATUSES:
                    return False, {"message": "invoice_unavailable"}
                return True, invoice
            return False, {"message": "creation_unconfirmed", "uncertain": True}
        return False, result

    async def _resolve_payment(self, session, invoice_uuid: str, order_id: str):
        if not re.fullmatch(r"[1-9][0-9]{0,9}", order_id) or int(order_id) > 2147483647:
            return None
        payment = await payment_dal.get_payment_by_db_id(session, int(order_id), for_update=True)
        if not payment or payment.provider != "2328":
            return None
        if payment.provider_payment_id and payment.provider_payment_id != invoice_uuid:
            return None
        return payment

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured:
            return web.Response(status=503, text="2328_disabled")
        if getattr(request, "content_type", "application/json") != "application/json":
            return web.Response(status=415, text="unsupported_media_type")
        try:
            raw_body = await request.read()
            if len(raw_body) > 65536:
                return web.Response(status=413, text="payload_too_large")
            data = self._parse_json(raw_body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return web.Response(status=400, text="bad_request")
        if not isinstance(data, dict):
            return web.Response(status=400, text="bad_request")
        if not self.verify_webhook_sign(raw_body):
            return web.Response(status=401, text="forbidden")

        invoice_uuid = data.get("uuid")
        order_id = data.get("order_id")
        status = data.get("payment_status")
        currency = data.get("currency")
        if not (
            isinstance(invoice_uuid, str) and invoice_uuid.strip() and len(invoice_uuid) <= 128
            and isinstance(order_id, str) and order_id
            and isinstance(status, str) and status
            and isinstance(currency, str) and currency
        ):
            return web.Response(status=400, text="missing_fields")
        if status not in SUCCESS_STATUSES | PENDING_STATUSES | FAILED_STATUSES | REFUND_STATUSES:
            return web.Response(status=400, text="unknown_status")
        try:
            incoming = self._decimal_amount(data.get("amount"))
        except (ValueError, ArithmeticError):
            return web.Response(status=400, text="invalid_amount")

        async with self.async_session_factory() as session:
            try:
                payment = await self._resolve_payment(session, invoice_uuid, order_id)
                if not payment:
                    return web.Response(status=404, text="payment_not_found")
                if currency != str(payment.currency or "").upper():
                    return web.Response(status=400, text="currency_mismatch")
                if incoming != self._decimal_amount(payment.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
                    return web.Response(status=400, text="amount_mismatch")

                # The row lock serializes success, cancellation and concurrent callbacks.
                # Fulfillment is monotonic; even a later refund does not grant it again.
                if payment.status == "succeeded":
                    if status in REFUND_STATUSES:
                        logging.warning("2328 refund update for fulfilled payment %s: %s", payment.payment_id, status)
                    return web.Response(text="ok")

                if status in PENDING_STATUSES:
                    if payment.status in {"pending_2328", "pending_2328_unknown"}:
                        if not payment.provider_payment_id:
                            # An event identifies the invoice but does not restore its lost checkout URL.
                            await payment_dal.update_provider_payment_and_status(
                                session, payment.payment_id, invoice_uuid, payment.status,
                            )
                            await session.commit()
                    return web.Response(text="ok_pending")

                if status in REFUND_STATUSES:
                    if payment.status != status and (
                        payment.status not in {"refund_paid", "refund_fail"} or status == "refund_paid"
                    ):
                        await payment_dal.update_provider_payment_and_status(session, payment.payment_id, invoice_uuid, status)
                        await session.commit()
                    logging.warning("2328 refund update for payment %s: %s", payment.payment_id, status)
                    return web.Response(text="ok")

                if status in FAILED_STATUSES:
                    new_status = "canceled" if status == "cancel" else status
                    if payment.status in {"canceled", "underpaid", "aml_lock"} | REFUND_STATUSES:
                        return web.Response(text="ok_canceled")
                    await payment_dal.update_provider_payment_and_status(session, payment.payment_id, invoice_uuid, new_status)
                    await session.commit()
                    if status != "cancel":
                        db_user = await user_dal.get_user_by_id(session, payment.user_id)
                        lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                        try:
                            await self.bot.send_message(payment.user_id, self.i18n.gettext(lang, "payment_failed"))
                        except Exception:
                            logging.warning("2328 could not deliver failure notification for payment %s", payment.payment_id)
                    return web.Response(text="ok_canceled")

                if payment.status not in {"pending_2328", "pending_2328_unknown", "failed_creation"}:
                    # Late settlement after a final partial/blocked/cancelled event needs current evidence.
                    ok, current = await self.get_payment_info(invoice_uuid=invoice_uuid)
                    if not ok:
                        return web.Response(status=503, text="verification_unavailable")
                    if not (
                        current.get("uuid") == invoice_uuid
                        and current.get("payment_status") in SUCCESS_STATUSES
                        and self._invoice_matches(current, order_id=order_id, amount=incoming, currency=currency)
                    ):
                        return web.Response(status=409, text="state_conflict")

                payment_months = payment.subscription_duration_months or 1
                sale_mode = getattr(payment, "product_type", "subscription")
                payment_months = (payment.lte_traffic_bytes / 2**30 if sale_mode == "lte_topup"
                          else (payment.subscription_duration_months or 1))
                provider_payment_id = invoice_uuid
                marked = await payment_dal.mark_provider_payment_succeeded_once(session, payment.payment_id, invoice_uuid)
                if not marked:
                    return web.Response(text="ok")
                activation = await self.subscription_service.activate_subscription(
                    session, payment.user_id,
                    int(payment_months) if sale_mode != "lte_topup" else 0,
                    float(payment.amount), payment.payment_id,
                    promo_code_id_from_payment=payment.promo_code_id,
                    provider="2328", sale_mode=sale_mode,
                    traffic_gb=payment_months if sale_mode == "lte_topup" else None,
                    device_limit=payment.hwid_device_limit if sale_mode != "lte_topup" else None,
                )
                if not activation or not activation.get("end_date"):
                    raise RuntimeError("2328 subscription activation failed")
                referral_bonus = None
                if sale_mode != "lte_topup":
                    referral_bonus = await self.referral_service.apply_referral_bonuses_for_payment(
                        session, payment.user_id, int(payment_months),
                        current_payment_db_id=payment.payment_id,
                        skip_if_active_before_payment=False,
                    )
                await session.commit()
            except Exception:
                await session.rollback()
                logging.exception("2328 payment processing failed")
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

            if sale_mode == "lte_topup":
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
                logging.error("2328 webhook: failed to notify user %s: %s", payment.user_id, exc)

            try:
                notification_service = NotificationService(self.bot, self.settings, self.i18n)
                await notification_service.notify_payment_received(
                    user_id=payment.user_id,
                    amount=float(payment.amount),
                    currency=payment.currency or currency,
                    months=int(payment_months) if sale_mode != "lte_topup" else 0,
                    traffic_gb=payment_months if sale_mode == "lte_topup" else None,
                    payment_provider="2328",
                    username=db_user.username if db_user else None,
                    device_limit=payment.hwid_device_limit if sale_mode != "lte_topup" else None,
                )
            except Exception as exc:
                logging.error("2328 webhook: failed to notify admins: %s", exc)

            return web.Response(text="ok")


async def pay2328_webhook_route(request: web.Request) -> web.Response:
    service: Pay2328Service = request.app["pay2328_service"]
    return await service.webhook_route(request)
