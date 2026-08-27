"""Admin screen for the conversion rates payments are normalised with.

Editing a rate here never revalues anything already recorded — every payment
froze its own rate at purchase time. A new or changed rate applies to payments
made from now on, plus payments that were never valued at all because their
currency had no rate yet; those are filled in right after a save.
"""

import logging
import re
from typing import Optional, Tuple

from aiogram import Router, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from config.currency import BASE_CURRENCY
from bot.middlewares.i18n import JsonI18n
from bot.states.admin_states import AdminStates
from db.dal import currency_dal, payment_dal

router = Router(name="admin_currency_rates_router")

CURRENCY_RE = re.compile(r"^[A-Za-z]{2,16}$")
# "USDT 95", "USDT=95", "usdt 95,5"
NEW_CURRENCY_RE = re.compile(r"^([A-Za-z]{2,16})\s*[=\s]\s*(\d+(?:[.,]\d+)?)$")


def _lang_and_i18n(settings: Settings, i18n_data: dict) -> Tuple[str, Optional[JsonI18n]]:
    return (
        i18n_data.get("current_language", settings.DEFAULT_LANGUAGE),
        i18n_data.get("i18n_instance"),
    )


def _parse_rate(raw: str) -> Optional[float]:
    try:
        value = float(raw.replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 1e9:
        return None
    return value


async def _render(session: AsyncSession, i18n: JsonI18n, lang: str):
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    rates = await currency_dal.list_rates(session)
    unvalued = await payment_dal.count_unvalued_payments(session)

    text = _("admin_currency_rates_header", base=BASE_CURRENCY)
    if not rates:
        text += "\n\n" + _("admin_currency_rates_empty")
    if unvalued:
        text += "\n\n" + _("admin_currency_rates_unvalued", count=unvalued)

    from bot.keyboards.inline.admin_keyboards import get_currency_rates_keyboard

    return text, get_currency_rates_keyboard(
        i18n, lang, rates, BASE_CURRENCY
    )


async def _safe_edit(callback: types.CallbackQuery, text: str, markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logging.debug("Failed to edit currency rates message: %s", e)


@router.callback_query(F.data == "admin_action:currency_rates")
async def show_rates(
    callback: types.CallbackQuery,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    await state.clear()
    # Self-heal: the base currency must always be present at exactly 1.0.
    await currency_dal.ensure_base_rate(session, BASE_CURRENCY)
    text, markup = await _render(session, i18n, current_lang)
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == "admin_rates:base")
async def base_currency_is_locked(
    callback: types.CallbackQuery, settings: Settings, i18n_data: dict
):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    await callback.answer(
        _("admin_currency_base_locked", currency=BASE_CURRENCY),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("admin_rates:edit:"))
async def prompt_edit(
    callback: types.CallbackQuery,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    code = currency_dal.normalize(callback.data.split(":", 2)[2])
    if not CURRENCY_RE.match(code):
        await callback.answer(_("admin_currency_rate_invalid_code"), show_alert=True)
        return
    if code == currency_dal.normalize(BASE_CURRENCY):
        await callback.answer(
            _("admin_currency_base_locked", currency=code), show_alert=True
        )
        return

    current = await currency_dal.get_rate(session, code)
    await state.set_state(AdminStates.waiting_for_currency_rate)
    await state.set_data({"currency_code": code})

    from bot.keyboards.inline.admin_keyboards import get_currency_rate_cancel_keyboard

    await _safe_edit(
        callback,
        _(
            "admin_currency_rate_prompt",
            currency=code,
            base=BASE_CURRENCY,
            current=f"{current:g}" if current is not None else "—",
        ),
        get_currency_rate_cancel_keyboard(i18n, current_lang),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_rates:add")
async def prompt_add(
    callback: types.CallbackQuery,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_new_currency)

    from bot.keyboards.inline.admin_keyboards import get_currency_rate_cancel_keyboard

    await _safe_edit(
        callback,
        _("admin_currency_rate_add_prompt", base=BASE_CURRENCY),
        get_currency_rate_cancel_keyboard(i18n, current_lang),
    )
    await callback.answer()


async def _save(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    i18n: JsonI18n,
    lang: str,
    code: str,
    rate: float,
) -> None:
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    try:
        await currency_dal.set_rate(
            session,
            code,
            rate,
            base_currency=BASE_CURRENCY,
            updated_by=message.from_user.id,
        )
        # A rate may unlock payments that could not be valued before.
        valued = await payment_dal.revalue_unvalued_payments(session)
        await session.commit()
    except ValueError as ve:
        # Validation happens before anything is written, so there is nothing to
        # roll back — and rolling back here would discard unrelated pending work.
        if str(ve) == "currency_rate_base_is_fixed":
            await message.answer(
                _("admin_currency_base_locked", currency=code), parse_mode="HTML"
            )
        else:
            await message.answer(_("admin_currency_rate_invalid_value"))
        return
    except Exception as e:
        await session.rollback()
        logging.error("Failed to save currency rate %s: %s", code, e, exc_info=True)
        await message.answer(_("error_occurred_try_again"))
        return

    await state.clear()
    text = _(
        "admin_currency_rate_saved",
        currency=code,
        rate=f"{rate:g}",
        base=BASE_CURRENCY,
    )
    if valued:
        text += "\n" + _("admin_currency_rate_valued", count=valued)
    await message.answer(text, parse_mode="HTML")

    list_text, markup = await _render(session, i18n, lang)
    await message.answer(list_text, reply_markup=markup, parse_mode="HTML")


@router.message(StateFilter(AdminStates.waiting_for_currency_rate), F.text)
async def receive_rate(
    message: types.Message,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    if not i18n:
        return

    data = await state.get_data()
    code = data.get("currency_code")
    if not code:
        await state.clear()
        await message.answer(_("error_try_again"))
        return

    rate = _parse_rate(message.text)
    if rate is None:
        await message.answer(_("admin_currency_rate_invalid_value"))
        return

    await _save(message, state, session, i18n, current_lang, code, rate)


@router.message(StateFilter(AdminStates.waiting_for_new_currency), F.text)
async def receive_new_currency(
    message: types.Message,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    if not i18n:
        return

    match = NEW_CURRENCY_RE.match(message.text.strip())
    if not match:
        await message.answer(_("admin_currency_rate_add_invalid"))
        return

    code = currency_dal.normalize(match.group(1))
    rate = _parse_rate(match.group(2))
    if rate is None:
        await message.answer(_("admin_currency_rate_invalid_value"))
        return

    await _save(message, state, session, i18n, current_lang, code, rate)
