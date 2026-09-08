import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.notification_service import NotificationService
from bot.services.referral_service import ReferralService
from bot.services.subscription_service import SubscriptionService
from bot.utils.config_link import prepare_config_links
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display
from config.settings import Settings
from db.dal import payment_dal, user_dal


class CisPayService:
    ORDER_ID_PREFIX = "cispay-"

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

        self.base_url = (settings.CISPAY_BASE_URL or "https://api.cispay.app").rstrip("/")
        self.shop_id = (settings.CISPAY_SHOP_ID or "").strip()
        self.api_key = (settings.CISPAY_API_KEY or "").strip()
        self.return_url = settings.CISPAY_RETURN_URL or f"https://t.me/{default_return_url}"
        self.failed_url = settings.CISPAY_FAILED_URL or self.return_url

        self._timeout = ClientTimeout(total=20)
        self._session: Optional[ClientSession] = None
        self._auth_headers = {
            "X-Shop-ID": self.shop_id,
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        self.configured = bool(settings.CISPAY_ENABLED and self.shop_id and self.api_key)
        if not self.configured:
            logging.warning("CisPayService initialized but not fully configured. Payments disabled.")

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _amount_to_kopecks(amount: float) -> int:
        return int(
            (Decimal(str(amount)) * Decimal("100")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    @classmethod
    def _order_id(cls, payment_db_id: int) -> str:
        return f"{cls.ORDER_ID_PREFIX}{payment_db_id}"

    @classmethod
    def _payment_db_id_from_order(cls, order_id: str) -> Optional[int]:
        if not order_id.startswith(cls.ORDER_ID_PREFIX):
            return None
        raw_id = order_id[len(cls.ORDER_ID_PREFIX) :]
        if not raw_id.isdigit():
            return None
        return int(raw_id)

    def verify_webhook_signature(self, raw_body: bytes, received_signature: str) -> bool:
        if not self.api_key or not received_signature:
            return False
        expected = hmac.new(
            self.api_key.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, received_signature)

    async def _record_discount_metadata(
        self,
        *,
        payment_db_id: int,
        user_id: int,
        months: float,
        amount: float,
        promo_code_service,
        session,
    ) -> None:
        if not promo_code_service or not session:
            return

        from db.dal import active_discount_dal

        active_discount = await active_discount_dal.get_active_discount(session, user_id)
        if not active_discount:
            return

        discount_pct = active_discount.discount_percentage
        promo_code_id = active_discount.promo_code_id
        denominator = 1 - discount_pct / 100
        original_amount = None
        discount_amount = None

        if denominator <= 0:
            traffic_mode = bool(getattr(self.settings, "traffic_sale_mode", False))
            price_source = (
                getattr(self.settings, "traffic_packages", {}) or {}
                if traffic_mode
                else (self.settings.subscription_options or {})
            )
            original_amount = price_source.get(months)
            if original_amount is not None:
                discount_amount = original_amount - amount
            else:
                logging.warning(
                    "cisPay discount %s%% has invalid denominator and no fallback price for months=%s.",
                    discount_pct,
                    months,
                )
                return
        else:
            original_amount = amount / denominator
            discount_amount = original_amount - amount

        logging.info(
            "Recording %s%% discount for cisPay payment: original %.2f -> final %s",
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
        except Exception as exc:
            logging.warning(
                "cisPay: failed to update discount metadata for payment %s: %s",
                payment_db_id,
                exc,
            )

    async def create_payment(
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
            logging.error("CisPayService is not configured. Cannot create payment.")
            return False, {"message": "service_not_configured"}

        await self._record_discount_metadata(
            payment_db_id=payment_db_id,
            user_id=user_id,
            months=months,
            amount=amount,
            promo_code_service=promo_code_service,
            session=session,
        )

        order_id = self._order_id(payment_db_id)
        body: Dict[str, Any] = {
            "amount": self._amount_to_kopecks(amount),
            "currency": currency.upper(),
            "order_id": order_id,
            "payment_method": "SBP",
            "customer_id": str(user_id),
            "redirect_success_url": self.return_url,
            "redirect_fail_url": self.failed_url,
            "description": description[:512],
        }

        http_session = await self._get_session()
        url = f"{self.base_url}/payments"
        try:
            async with http_session.post(
                url, json=body, headers=self._auth_headers
            ) as response:
                response_text = await response.text()
                try:
                    response_data = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    logging.error(
                        "cisPay create_payment: invalid JSON response (status=%s)",
                        response.status,
                    )
                    return False, {
                        "status": response.status,
                        "message": "invalid_json",
                    }

                if response.status != 201:
                    logging.error(
                        "cisPay create_payment: API returned error (status=%s, body=%s)",
                        response.status,
                        response_data,
                    )
                    return False, {"status": response.status, "message": response_data}

                if str(response_data.get("order_id") or "") != order_id:
                    logging.error(
                        "cisPay create_payment: response order_id does not match payment %s",
                        payment_db_id,
                    )
                    return False, {"message": "order_id_mismatch"}

                return True, response_data
        except Exception as exc:
            logging.error("cisPay create_payment: request failed: %s", exc, exc_info=True)
            return False, {"message": str(exc)}

    async def _resolve_payment(self, session, transaction_id: str, order_id: str):
        payment = None
        if transaction_id:
            payment = await payment_dal.get_payment_by_provider_payment_id(
                session, transaction_id
            )

        if not payment:
            payment_db_id = self._payment_db_id_from_order(order_id)
            if payment_db_id is not None:
                payment = await payment_dal.get_payment_by_db_id(session, payment_db_id)

        if not payment or payment.provider != "cispay":
            return None
        if order_id != self._order_id(payment.payment_id):
            return None
        if (
            payment.provider_payment_id
            and transaction_id
            and payment.provider_payment_id != transaction_id
        ):
            return None
        return payment

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured:
            return web.Response(status=503, text="cispay_disabled")

        try:
            raw_body = await request.read()
            data = json.loads(raw_body)
        except Exception as exc:
            logging.error("cisPay webhook: failed to parse JSON: %s", exc)
            return web.Response(status=400, text="bad_request")

        if not isinstance(data, dict):
            return web.Response(status=400, text="bad_request")

        received_signature = (request.headers.get("X-Signature") or "").strip()
        if not self.verify_webhook_signature(raw_body, received_signature):
            logging.error("cisPay webhook: invalid signature")
            return web.Response(status=403, text="forbidden")

        transaction_id = str(data.get("id") or "").strip()
        order_id = str(data.get("order_id") or "").strip()
        store_id = str(data.get("store_id") or "").strip()
        status = str(data.get("status") or "").upper()

        if not transaction_id or not order_id or not status or not store_id:
            logging.error("cisPay webhook: required fields are missing")
            return web.Response(status=400, text="missing_fields")
        if store_id != self.shop_id:
            logging.error("cisPay webhook: store_id mismatch")
            return web.Response(status=403, text="store_mismatch")

        async with self.async_session_factory() as session:
            payment = await self._resolve_payment(session, transaction_id, order_id)
            if not payment:
                logging.error(
                    "cisPay webhook: payment not found (transaction=%s, order=%s)",
                    transaction_id,
                    order_id,
                )
                return web.Response(status=404, text="payment_not_found")

            if payment.status == "succeeded" and status == "PAID":
                return web.Response(text="ok")

            if status != "PAID":
                logging.info(
                    "cisPay webhook: status '%s' ignored for payment %s",
                    status,
                    payment.payment_id,
                )
                return web.Response(status=202, text="status_ignored")

            payment_method = str(data.get("payment_method") or "").upper()
            if payment_method != "SBP":
                logging.error(
                    "cisPay webhook: payment method mismatch for payment %s (got %s)",
                    payment.payment_id,
                    payment_method,
                )
                return web.Response(status=400, text="payment_method_mismatch")

            currency = str(data.get("currency") or "").upper()
            expected_currency = str(payment.currency or "").upper()
            if not currency or currency != expected_currency:
                logging.error(
                    "cisPay webhook: currency mismatch for payment %s (expected %s, got %s)",
                    payment.payment_id,
                    expected_currency,
                    currency,
                )
                return web.Response(status=400, text="currency_mismatch")

            amount_raw = data.get("amount")
            try:
                incoming_kopecks = Decimal(str(amount_raw))
                if incoming_kopecks != incoming_kopecks.to_integral_value():
                    raise ValueError("amount must be an integer number of kopecks")
                expected_kopecks = self._amount_to_kopecks(payment.amount)
                if incoming_kopecks < expected_kopecks:
                    logging.error(
                        "cisPay webhook: underpayment for payment %s (expected at least %s kopecks, got %s)",
                        payment.payment_id,
                        expected_kopecks,
                        incoming_kopecks,
                    )
                    return web.Response(status=400, text="amount_mismatch")
            except Exception as exc:
                logging.error(
                    "cisPay webhook: invalid amount for payment %s: %s",
                    payment.payment_id,
                    exc,
                )
                return web.Response(status=400, text="amount_validation_error")

            charged_amount_raw = data.get("charged_amount")
            if charged_amount_raw is not None:
                try:
                    charged_kopecks = Decimal(str(charged_amount_raw))
                    if charged_kopecks > incoming_kopecks:
                        logging.info(
                            "cisPay webhook: payment %s includes customer commission (order %s kopecks, charged %s kopecks)",
                            payment.payment_id,
                            incoming_kopecks,
                            charged_kopecks,
                        )
                except Exception as exc:
                    logging.warning(
                        "cisPay webhook: could not inspect charged_amount for payment %s: %s",
                        payment.payment_id,
                        exc,
                    )

            payment_months = payment.subscription_duration_months or 1
            sale_mode = "traffic" if self.settings.traffic_sale_mode else "subscription"
            try:
                marked = await payment_dal.mark_provider_payment_succeeded_once(
                    session,
                    payment.payment_id,
                    transaction_id,
                )
                if not marked:
                    logging.info(
                        "cisPay webhook: payment %s already processed atomically",
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
                    provider="cispay",
                    sale_mode=sale_mode,
                    traffic_gb=payment_months if sale_mode == "traffic" else None,
                    device_limit=(
                        payment.hwid_device_limit if sale_mode != "traffic" else None
                    ),
                )
                if not activation or not activation.get("end_date"):
                    raise RuntimeError(
                        f"cisPay webhook: activation failed for payment {payment.payment_id}"
                    )

                referral_bonus = None
                if sale_mode != "traffic":
                    referral_bonus = (
                        await self.referral_service.apply_referral_bonuses_for_payment(
                            session,
                            payment.user_id,
                            int(payment_months),
                            current_payment_db_id=payment.payment_id,
                            skip_if_active_before_payment=False,
                        )
                    )

                await session.commit()
            except Exception as exc:
                await session.rollback()
                logging.error(
                    "cisPay webhook: failed to process payment %s: %s",
                    transaction_id,
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
            _ = lambda key, **kwargs: (
                self.i18n.gettext(lang, key, **kwargs) if self.i18n else key
            )

            raw_config_link = activation.get("subscription_url")
            config_link_display, connect_button_url = await prepare_config_links(
                self.settings, raw_config_link
            )
            config_link_text = config_link_display or _("config_link_not_available")
            final_end = activation.get("end_date")
            applied_days = 0
            applied_promo_days = activation.get("applied_promo_bonus_days", 0)

            if referral_bonus and referral_bonus.get("referee_new_end_date"):
                final_end = referral_bonus["referee_new_end_date"]
                applied_days = referral_bonus.get("referee_bonus_applied_days", 0)

            days_total = (
                max(0, (final_end - datetime.now(timezone.utc)).days)
                if final_end
                else 0
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
                    inviter = await user_dal.get_user_by_id(
                        session, db_user.referred_by_id
                    )
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
                    base_end_date=(
                        activation["end_date"].strftime("%d.%m.%Y")
                        if activation.get("end_date")
                        else final_end.strftime("%d.%m.%Y") if final_end else ""
                    ),
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
                logging.error(
                    "cisPay webhook: failed to notify user %s: %s",
                    payment.user_id,
                    exc,
                )

            try:
                notification_service = NotificationService(
                    self.bot, self.settings, self.i18n
                )
                await notification_service.notify_payment_received(
                    user_id=payment.user_id,
                    amount=float(payment.amount),
                    currency=payment.currency,
                    months=int(payment_months) if sale_mode != "traffic" else 0,
                    traffic_gb=payment_months if sale_mode == "traffic" else None,
                    payment_provider="cispay",
                    username=db_user.username if db_user else None,
                    device_limit=(
                        payment.hwid_device_limit if sale_mode != "traffic" else None
                    ),
                )
            except Exception as exc:
                logging.error("cisPay webhook: failed to notify admins: %s", exc)

            return web.Response(text="ok")


async def cispay_webhook_route(request: web.Request) -> web.Response:
    service: CisPayService = request.app["cispay_service"]
    return await service.webhook_route(request)
