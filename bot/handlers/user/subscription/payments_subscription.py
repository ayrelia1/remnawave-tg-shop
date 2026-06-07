import logging
import math
from typing import Callable, Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.user_keyboards import (
    get_payment_method_keyboard,
    get_tariff_switch_confirm_keyboard,
)
from bot.middlewares.i18n import JsonI18n
from bot.services.panel_api_service import PanelApiService, PanelUnavailableError
from bot.services.subscription_service import SubscriptionService
from bot.utils.message_helpers import safe_edit_text
from config.settings import Settings

router = Router(name="user_subscription_payments_selection_router")


def _format_units(val: float) -> str:
    return str(int(val)) if float(val).is_integer() else f"{val:g}"


def parse_simple_offer(data_payload: str):
    """Parse a pay_* offer payload.

    4-part = device-tier mode (devices:value:price:mode);
    3-part = legacy/traffic (value:price:mode).
    Returns (devices, value, price, sale_mode) with devices None in legacy/traffic.
    """
    parts = data_payload.split(":")
    try:
        if len(parts) >= 4:
            return int(float(parts[0])), float(parts[1]), float(parts[2]), parts[3]
        return None, float(parts[0]), float(parts[1]), parts[2] if len(parts) > 2 else "subscription"
    except (ValueError, IndexError):
        return None


def offer_back_callback(devices, value) -> str:
    """Build the back-to-period callback for a payment screen."""
    value_str = _format_units(value)
    if devices is not None:
        return f"subscribe_period:{int(devices)}:{value_str}"
    return f"subscribe_period:{value_str}"


async def ensure_panel_available_or_alert(
    callback: types.CallbackQuery,
    get_text: Callable[..., str],
    panel_service: Optional[PanelApiService],
) -> bool:
    """Ping panel right before creating a payment. If panel is unreachable,
    show a tech-works alert to the user and return False so the caller can
    abort. Returns True when the panel responded successfully (or when no
    panel_service was provided — defensive no-op)."""
    if panel_service is None:
        return True
    try:
        await panel_service.ping()
    except PanelUnavailableError as exc:
        logging.warning(
            "Panel unavailable — aborting payment creation for user %s: %s",
            callback.from_user.id, exc,
        )
        try:
            await callback.answer(get_text("panel_unavailable_alert"), show_alert=True)
        except Exception as e_ans:
            logging.debug("Suppressed answer exception: %s", e_ans)
        return False
    return True


async def resolve_fiat_offer_price_for_user(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    months: float,
    sale_mode: str,
    promo_code_service=None,
    devices: Optional[int] = None,
) -> Optional[float]:
    """Resolve offer price server-side to prevent callback payload tampering."""
    if sale_mode == "traffic":
        base_price = (getattr(settings, "traffic_packages", {}) or {}).get(months)
    elif devices is not None and settings.device_plans_active:
        base_price = settings.get_plan_price(int(devices), int(months))
    else:
        base_price = (settings.subscription_options or {}).get(months)
    if base_price is None:
        return None

    resolved_price = float(base_price)
    if promo_code_service:
        active_discount_info = await promo_code_service.get_user_active_discount(session, user_id)
        if active_discount_info:
            discount_pct, _ = active_discount_info
            resolved_price, _ = promo_code_service.calculate_discounted_price(
                resolved_price,
                discount_pct,
            )
    return resolved_price


async def _show_payment_methods_screen(
    callback: types.CallbackQuery,
    *,
    settings: Settings,
    i18n: JsonI18n,
    current_lang: str,
    get_text,
    session: AsyncSession,
    months: float,
    devices: Optional[int],
    traffic_mode: bool,
    promo_code_service,
) -> None:
    """Compute prices (with active discount) and render the payment-method keyboard."""
    if traffic_mode:
        price_rub = (getattr(settings, "traffic_packages", {}) or {}).get(months)
        stars_price = (getattr(settings, "stars_traffic_packages", {}) or {}).get(months)
        price_source = getattr(settings, "traffic_packages", {}) or {}
    elif devices is not None and settings.device_plans_active:
        price_rub = settings.get_plan_price(int(devices), int(months))
        stars_price = settings.get_plan_stars_price(int(devices), int(months))
        price_source = settings.device_subscription_options.get(int(devices), {})
    else:
        price_rub = (settings.subscription_options or {}).get(months)
        stars_price = (settings.stars_subscription_options or {}).get(months)
        price_source = settings.subscription_options or {}

    currency_symbol_val = "RUB"
    discount_text = ""
    if promo_code_service and (price_rub is not None or stars_price is not None):
        active_discount_info = await promo_code_service.get_user_active_discount(
            session, callback.from_user.id
        )
        if active_discount_info:
            discount_pct, promo_code = active_discount_info
            if price_rub is not None:
                original_price_rub = price_rub
                price_rub, discount_amt = promo_code_service.calculate_discounted_price(
                    price_rub, discount_pct
                )
                discount_text = get_text(
                    "active_discount_notice",
                    code=promo_code,
                    discount_pct=discount_pct,
                    original_price=original_price_rub,
                    discounted_price=price_rub,
                    discount_amount=discount_amt,
                    currency_symbol=currency_symbol_val,
                )
            if stars_price is not None:
                original_stars_price = stars_price
                discounted_stars_price, _ = promo_code_service.calculate_discounted_price(
                    float(stars_price), discount_pct
                )
                discounted_stars_price = math.ceil(discounted_stars_price)
                stars_price = discounted_stars_price
                if not discount_text:
                    discount_amt = original_stars_price - discounted_stars_price
                    discount_text = get_text(
                        "active_discount_notice",
                        code=promo_code,
                        discount_pct=discount_pct,
                        original_price=original_stars_price,
                        discounted_price=discounted_stars_price,
                        discount_amount=discount_amt,
                        currency_symbol="⭐",
                    )

    if price_rub is None:
        if traffic_mode and not price_source and stars_price is not None:
            currency_methods_enabled = any(
                [
                    settings.FREEKASSA_ENABLED,
                    settings.PLATEGA_ENABLED,
                    settings.SEVERPAY_ENABLED,
                    settings.YOOKASSA_ENABLED,
                    settings.CRYPTOPAY_ENABLED,
                ]
            )
            if currency_methods_enabled:
                logging.error(
                    "Currency price missing for traffic option %s while fiat providers are enabled.",
                    months,
                )
                try:
                    await callback.answer(get_text("error_try_again"), show_alert=True)
                except Exception as exc:
                    logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
                return
            price_rub = 0.0
            currency_symbol_val = "⭐"
        else:
            logging.error(
                "Price not found for option %s (devices=%s, traffic=%s).",
                months, devices, traffic_mode,
            )
            try:
                await callback.answer(get_text("error_try_again"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
            return

    text_content = get_text("choose_payment_method_traffic") if traffic_mode else get_text("choose_payment_method")
    if discount_text:
        text_content = f"{discount_text}\n\n{text_content}"

    reply_markup = get_payment_method_keyboard(
        months,
        price_rub,
        stars_price,
        currency_symbol_val,
        current_lang,
        i18n,
        settings,
        sale_mode="traffic" if traffic_mode else "subscription",
        devices=devices,
    )

    try:
        await safe_edit_text(callback.message, text_content, reply_markup=reply_markup)
    except Exception as e_edit:
        logging.warning(
            f"Edit message for payment method selection failed: {e_edit}. Sending new one."
        )
        await callback.message.answer(text_content, reply_markup=reply_markup)
    try:
        await callback.answer()
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)


def _parse_period_callback(data: str):
    """Parse subscribe_period payload. Returns (devices, value) where devices is
    None in legacy/traffic mode (1 segment) and set in device mode (2 segments)."""
    parts = data.split(":")[1:]
    if len(parts) >= 2:
        return int(float(parts[0])), float(parts[1])
    return None, float(parts[0])


@router.callback_query(F.data.startswith("subscribe_period:"))
async def select_subscription_period_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: Optional[SubscriptionService] = None,
    promo_code_service=None,  # Injected from dispatcher
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return

    stars_traffic_packages = getattr(settings, "stars_traffic_packages", {}) or {}
    traffic_mode = bool(getattr(settings, "traffic_sale_mode", False) or stars_traffic_packages)
    try:
        devices, months = _parse_period_callback(callback.data)
    except (ValueError, IndexError):
        logging.error(f"Invalid subscription period in callback_data: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return

    # Device-tier switch preview: warn before payment when switching to a
    # different tier (days will be recalculated value-preserving).
    if (
        not traffic_mode
        and devices is not None
        and settings.device_plans_active
        and subscription_service is not None
    ):
        preview = await subscription_service.compute_switch_preview(
            session, callback.from_user.id, int(devices), int(months)
        )
        direction = preview.get("direction")
        if direction in ("upgrade", "downgrade"):
            if not subscription_service.is_switch_allowed(direction):
                try:
                    await callback.answer(get_text("tariff_switch_disabled"), show_alert=True)
                except Exception as exc:
                    logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
                return
            price = settings.get_plan_price(int(devices), int(months))
            text = get_text(
                "tariff_switch_preview",
                current_devices=preview.get("current_devices"),
                new_devices=int(devices),
                remaining_days=preview.get("remaining_days"),
                total_days=preview.get("total_days"),
                end_date=preview["projected_end_date"].strftime("%Y-%m-%d"),
                price=_format_units(price) if price is not None else "-",
                currency_symbol="RUB",
            )
            if direction == "downgrade":
                text += "\n\n" + get_text("tariff_switch_downgrade_warning", devices=int(devices))
            markup = get_tariff_switch_confirm_keyboard(int(devices), int(months), current_lang, i18n)
            try:
                await safe_edit_text(callback.message, text, reply_markup=markup)
            except Exception as e_edit:
                logging.warning(f"Failed to show tariff switch preview: {e_edit}")
                await callback.message.answer(text, reply_markup=markup)
            try:
                await callback.answer()
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
            return

    await _show_payment_methods_screen(
        callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        get_text=get_text,
        session=session,
        months=months,
        devices=devices,
        traffic_mode=traffic_mode,
        promo_code_service=promo_code_service,
    )


@router.callback_query(F.data.startswith("confirm_switch:"))
async def confirm_tariff_switch_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: Optional[SubscriptionService] = None,
    promo_code_service=None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return

    try:
        parts = callback.data.split(":")
        devices = int(float(parts[1]))
        months = float(parts[2])
    except (ValueError, IndexError):
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return

    # Re-check the switch direction here too: the confirm button may be stale
    # (clicked after the policy changed) or crafted manually.
    if settings.device_plans_active and subscription_service is not None:
        preview = await subscription_service.compute_switch_preview(
            session, callback.from_user.id, int(devices), int(months)
        )
        direction = preview.get("direction")
        if direction in ("upgrade", "downgrade") and not subscription_service.is_switch_allowed(direction):
            try:
                await callback.answer(get_text("tariff_switch_disabled"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
            return

    await _show_payment_methods_screen(
        callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        get_text=get_text,
        session=session,
        months=months,
        devices=devices,
        traffic_mode=False,
        promo_code_service=promo_code_service,
    )
