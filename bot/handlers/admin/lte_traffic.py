"""Admin LTE balance checks and idempotent, confirmed manual adjustments."""

import logging
from uuid import uuid4

from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.constants.premium_emoji import (
    PREMIUM_EMOJI_ADD,
    PREMIUM_EMOJI_BACK,
    PREMIUM_EMOJI_CANCEL,
    PREMIUM_EMOJI_DELETE,
    PREMIUM_EMOJI_REFRESH,
)
from bot.services.lte_service import LteService
from bot.states.admin_states import AdminStates
from bot.utils.message_helpers import safe_edit_text
from db.dal import lte_dal
from db.dal.lte_dal import GIB, traffic_bytes
from db.models import LteAccount, User

router = Router(name="admin_lte_traffic_router")


def _text(settings, i18n_data):
    i18n = i18n_data["i18n_instance"]
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    return lambda key, **kw: i18n.gettext(lang, key, **kw)


def _balance_screen(user_id, account, service, _, notice=""):
    text = _("admin_lte_balance", user_id=user_id,
             gb=f"{account.remaining_bytes / GIB:.2f}" if account else "0.00",
             credited=f"{account.credited_bytes / GIB:.2f}" if account else "0.00",
             used=f"{account.used_bytes / GIB:.2f}" if account else "0.00")
    if account and not account.managed:
        text += "\n\n" + _("admin_lte_legacy")
    elif not service.enabled:
        text += "\n\n" + _("admin_lte_paused")
    if notice:
        text = notice + "\n\n" + text
    builder = InlineKeyboardBuilder()
    builder.button(text=_("admin_lte_add"), callback_data=f"admin_lte:edit:add:{user_id}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_ADD)
    builder.button(text=_("admin_lte_remove"), callback_data=f"admin_lte:edit:remove:{user_id}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_DELETE)
    builder.button(text=_("admin_user_refresh_button"), callback_data=f"admin_lte:show:{user_id}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_REFRESH)
    builder.button(text=_("admin_user_back_to_card_button"), callback_data=f"admin_lte:back:{user_id}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_BACK)
    builder.adjust(2, 1, 1)
    return text, builder.as_markup()


async def _target(session, raw):
    try:
        user_id = int(raw)
    except (ValueError, TypeError):
        return None
    if not 0 < user_id <= 2**63 - 1:
        return None
    return await session.get(User, user_id)


@router.callback_query(F.data.startswith("admin_lte:show:"))
async def show_balance(callback: types.CallbackQuery, state: FSMContext, settings,
                        i18n_data, session, panel_service):
    _ = _text(settings, i18n_data)
    user = await _target(session, callback.data.removeprefix("admin_lte:show:"))
    if user is None:
        await callback.answer(_("admin_user_not_found_action"), show_alert=True)
        return
    user_id = user.user_id
    await state.clear()
    await callback.answer()
    service = LteService(settings, panel_service)
    notice = ""
    try:
        await service.refresh_balance(session, user_id)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.warning("Admin LTE refresh failed for user %s (%s)", user_id, type(exc).__name__)
        notice = _("admin_lte_saved_balance")
    account = await session.get(LteAccount, user_id)
    text, markup = _balance_screen(user_id, account, service, _, notice)
    await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_lte:edit:"))
async def prompt_adjustment(callback: types.CallbackQuery, state: FSMContext, settings,
                             i18n_data, session):
    _ = _text(settings, i18n_data)
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[2] not in {"add", "remove"}:
        await callback.answer(_("error_try_again"), show_alert=True)
        return
    user = await _target(session, parts[3])
    if user is None:
        await callback.answer(_("admin_user_not_found_action"), show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_for_lte_amount)
    await state.update_data(lte_user_id=user.user_id, lte_operation=parts[2], lte_token=uuid4().hex)
    builder = InlineKeyboardBuilder()
    builder.button(text=_("cancel_button"), callback_data=f"admin_lte:show:{user.user_id}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_CANCEL)
    await safe_edit_text(callback.message, _("admin_lte_amount_prompt", user_id=user.user_id,
                         action=_("admin_lte_" + parts[2])), reply_markup=builder.as_markup())
    await callback.answer()


@router.message(StateFilter(AdminStates.waiting_for_lte_amount), F.text)
async def enter_adjustment(message: types.Message, state: FSMContext, settings,
                            i18n_data, session, panel_service):
    _ = _text(settings, i18n_data)
    data = await state.get_data()
    user_id, operation = data.get("lte_user_id"), data.get("lte_operation")
    if not user_id or operation not in {"add", "remove"} or not data.get("lte_token"):
        await state.clear()
        await message.answer(_("admin_lte_expired_message"))
        return
    try:
        raw = message.text.strip().replace(",", ".")
        if raw.startswith(("+", "-")):
            raise ValueError("A signed amount is not accepted")
        amount = traffic_bytes(raw)
    except (ValueError, ArithmeticError):
        await message.answer(_("admin_lte_invalid_amount"))
        return
    service = LteService(settings, panel_service)
    try:
        account = await service.refresh_balance(session, user_id)
        remaining = account.remaining_bytes if account else 0
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.warning("Admin LTE amount check failed for user %s (%s)", user_id, type(exc).__name__)
        await message.answer(_("admin_lte_panel_error"))
        return
    if operation == "remove" and amount > remaining:
        await message.answer(_("admin_lte_insufficient", gb=f"{remaining / GIB:.2f}"))
        return
    await state.update_data(lte_amount=amount)
    await state.set_state(AdminStates.confirming_lte_adjustment)
    builder = InlineKeyboardBuilder()
    builder.button(text=_("admin_lte_" + operation), callback_data=f"admin_lte:confirm:{data['lte_token']}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_ADD if operation == "add" else PREMIUM_EMOJI_DELETE)
    builder.button(text=_("cancel_button"), callback_data=f"admin_lte:show:{user_id}",
                   icon_custom_emoji_id=PREMIUM_EMOJI_CANCEL)
    builder.adjust(1)
    await message.answer(_("admin_lte_confirm", user_id=user_id, action=_("admin_lte_" + operation),
                           gb=f"{amount / GIB:g}", remaining=f"{remaining / GIB:.2f}"),
                         reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("admin_lte:confirm:"))
async def confirm_adjustment(callback: types.CallbackQuery, state: FSMContext, settings,
                              i18n_data, session, panel_service):
    _ = _text(settings, i18n_data)
    data = await state.get_data()
    token = callback.data.removeprefix("admin_lte:confirm:")
    if (await state.get_state() != AdminStates.confirming_lte_adjustment.state
            or token != data.get("lte_token") or not isinstance(data.get("lte_amount"), int)
            or data["lte_amount"] <= 0 or data["lte_amount"] % GIB != 0
            or data.get("lte_operation") not in {"add", "remove"}):
        await callback.answer(_("admin_lte_expired"), show_alert=True)
        return
    user_id = data["lte_user_id"]
    amount = data["lte_amount"] * (1 if data["lte_operation"] == "add" else -1)
    source = f"admin:{callback.from_user.id}:{token}"
    service = LteService(settings, panel_service)
    await callback.answer()
    try:
        account, changed = await service.adjust_balance(session, user_id, amount, source)
        await session.commit()
    except lte_dal.InsufficientLteBalance:
        # Preserve fresh usage even though no manual adjustment was made.
        await session.commit()
        account = await session.get(LteAccount, user_id)
        await callback.message.answer(_("admin_lte_insufficient", gb=f"{account.remaining_bytes / GIB:.2f}"))
        return
    except ValueError:
        await session.rollback()
        await callback.message.answer(_("admin_lte_invalid_amount"))
        return
    except Exception as exc:
        await session.rollback()
        logging.warning("Admin LTE adjustment failed for user %s (%s)", user_id, type(exc).__name__)
        await callback.message.answer(_("admin_lte_panel_error"))
        return
    await state.clear()
    notice = _("admin_lte_applied" if changed else "admin_lte_already_applied")
    try:
        await service.reconcile(session, user_id)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.warning("Admin LTE access sync pending for user %s (%s)", user_id, type(exc).__name__)
        notice += "\n" + _("admin_lte_sync_pending")
    account = await session.get(LteAccount, user_id)
    text, markup = _balance_screen(user_id, account, service, _, notice)
    await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin_lte:back:"))
async def back_to_user(callback: types.CallbackQuery, state: FSMContext, settings,
                        i18n_data, session, subscription_service):
    from .user_management import handle_refresh_user_card
    _ = _text(settings, i18n_data)
    user = await _target(session, callback.data.removeprefix("admin_lte:back:"))
    if user is None:
        await callback.answer(_("admin_user_not_found_action"), show_alert=True)
        return
    await state.clear()
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    await handle_refresh_user_card(callback, user, subscription_service, session,
                                    i18n_data["i18n_instance"], lang)
