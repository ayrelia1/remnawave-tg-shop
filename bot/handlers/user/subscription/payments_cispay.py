import logging
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.user.subscription.payments_subscription import (
    ensure_panel_available_or_alert,
    offer_back_callback,
    parse_simple_offer,
    resolve_fiat_offer_price_for_user,
)
from bot.keyboards.inline.user_keyboards import get_payment_url_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.cispay_service import CisPayService
from bot.services.panel_api_service import PanelApiService
from bot.utils.message_helpers import safe_edit_text
from config.settings import Settings
from db.dal import payment_dal

router = Router(name="user_subscription_payments_cispay_router")


@router.callback_query(F.data.startswith("pay_cispay:"))
async def pay_cispay_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    cispay_service: CisPayService,
    panel_service: PanelApiService,
    session: AsyncSession,
    promo_code_service=None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: (
        i18n.gettext(current_lang, key, **kwargs) if i18n else key
    )

    if not i18n or not callback.message:
        try:
            await callback.answer(
                get_text("error_occurred_try_again"), show_alert=True
            )
        except Exception as exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", exc
            )
        return

    if not await ensure_panel_available_or_alert(callback, get_text, panel_service):
        return

    if not cispay_service or not cispay_service.configured:
        logging.error("cisPay service is not configured or unavailable.")
        try:
            await callback.answer(
                get_text("payment_service_unavailable_alert"), show_alert=True
            )
        except Exception as exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", exc
            )
        try:
            await safe_edit_text(
                callback.message, get_text("payment_service_unavailable")
            )
        except Exception as exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", exc
            )
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        parsed = parse_simple_offer(data_payload)
        if not parsed:
            raise ValueError("bad offer payload")
        devices, months, callback_price_rub, sale_mode = parsed
    except (ValueError, IndexError):
        logging.error("Invalid pay_cispay data in callback: %s", callback.data)
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", exc
            )
        return

    user_id = callback.from_user.id
    resolved_price_rub = await resolve_fiat_offer_price_for_user(
        session=session,
        settings=settings,
        user_id=user_id,
        months=months,
        sale_mode=sale_mode,
        promo_code_service=promo_code_service,
        devices=devices,
    )
    if resolved_price_rub is None:
        logging.warning(
            "cisPay: no server-side price for user %s, value=%s, mode=%s",
            user_id,
            months,
            sale_mode,
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", exc
            )
        return

    if abs(resolved_price_rub - callback_price_rub) > 0.01:
        logging.warning(
            "cisPay: callback price mismatch for user %s, value=%s, mode=%s, callback=%.2f, resolved=%.2f",
            user_id,
            months,
            sale_mode,
            callback_price_rub,
            resolved_price_rub,
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", exc
            )
        return

    price_rub = resolved_price_rub
    human_value = (
        str(int(months)) if float(months).is_integer() else f"{months:g}"
    )
    payment_description = (
        get_text("payment_description_traffic", traffic_gb=human_value)
        if sale_mode == "traffic"
        else get_text("payment_description_subscription", months=int(months))
    )

    payment_record_payload = {
        "user_id": user_id,
        "amount": price_rub,
        "original_amount": None,
        "discount_applied": None,
        "currency": "RUB",
        "status": "pending_cispay",
        "description": payment_description,
        "subscription_duration_months": int(months),
        "provider": "cispay",
        "promo_code_id": None,
        "hwid_device_limit": devices if sale_mode != "traffic" else None,
    }

    try:
        payment_record = await payment_dal.create_payment_record(
            session, payment_record_payload
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.error(
            "cisPay: failed to create payment record for user %s: %s",
            user_id,
            exc,
            exc_info=True,
        )
        try:
            await safe_edit_text(
                callback.message, get_text("error_creating_payment_record")
            )
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as notify_exc:
            logging.debug(
                "Suppressed exception in payments_cispay.py: %s", notify_exc
            )
        return

    success, response_data = await cispay_service.create_payment(
        payment_db_id=payment_record.payment_id,
        user_id=user_id,
        months=months,
        amount=price_rub,
        currency="RUB",
        description=payment_description,
        promo_code_service=promo_code_service,
        session=session,
    )

    if success:
        transaction_id = response_data.get("id")
        payment_url = response_data.get("payment_url")
        if transaction_id and payment_url:
            try:
                await payment_dal.update_provider_payment_and_status(
                    session,
                    payment_record.payment_id,
                    str(transaction_id),
                    "pending_cispay",
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logging.error(
                    "cisPay: failed to store transaction id for payment %s: %s",
                    payment_record.payment_id,
                    exc,
                    exc_info=True,
                )

            message_key = (
                "payment_link_message_traffic"
                if sale_mode == "traffic"
                else "payment_link_message"
            )
            keyboard = get_payment_url_keyboard(
                payment_url,
                current_lang,
                i18n,
                back_callback=offer_back_callback(devices, months),
                back_text_key="back_to_payment_methods_button",
            )
            try:
                await safe_edit_text(
                    callback.message,
                    get_text(
                        message_key,
                        months=int(months),
                        traffic_gb=human_value,
                    ),
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                )
            except Exception as exc:
                logging.warning(
                    "cisPay: failed to display payment link (%s), sending new message.",
                    exc,
                )
                try:
                    await callback.message.answer(
                        get_text(
                            message_key,
                            months=int(months),
                            traffic_gb=human_value,
                        ),
                        reply_markup=keyboard,
                        disable_web_page_preview=False,
                    )
                except Exception as notify_exc:
                    logging.debug(
                        "Suppressed exception in payments_cispay.py: %s",
                        notify_exc,
                    )
            try:
                await callback.answer()
            except Exception as exc:
                logging.debug(
                    "Suppressed exception in payments_cispay.py: %s", exc
                )
            return

        logging.error(
            "cisPay: payment created but missing transaction id or payment URL for payment %s",
            payment_record.payment_id,
        )

    try:
        await payment_dal.update_payment_status_by_db_id(
            session,
            payment_record.payment_id,
            "failed_creation",
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.error(
            "cisPay: failed to mark payment %s as failed_creation: %s",
            payment_record.payment_id,
            exc,
            exc_info=True,
        )

    try:
        await safe_edit_text(callback.message, get_text("error_payment_gateway"))
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception as exc:
        logging.debug("Suppressed exception in payments_cispay.py: %s", exc)
