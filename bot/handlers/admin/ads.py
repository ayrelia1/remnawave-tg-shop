import html
import logging
import re
from aiogram import Router, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from config.currency import BASE_CURRENCY
from bot.middlewares.i18n import JsonI18n
from db.dal import ad_dal, currency_dal, user_dal
from db.models import AdCampaign, CAMPAIGN_TYPE_AD, CAMPAIGN_TYPE_PARTNER
from bot.states.admin_states import AdminStates

router = Router(name="admin_ads_router")


PAGE_SIZE = 5
PAYOUTS_PAGE_SIZE = 8

START_PARAM_RE = re.compile(r"^[A-Za-z0-9_\-]{2,64}$")
# "1500" / "1500 rub" / "12,5 usdt" — currency is optional and defaults to the
# configured payout currency.
PAYOUT_AMOUNT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([A-Za-z]{2,10})?$")
# Rouble suffixes an admin is likely to type; they mean "the default currency".
DEFAULT_CURRENCY_SUFFIX_RE = re.compile(
    r"\s*(?:₽|р|р\.|руб|руб\.|рублей)$", re.IGNORECASE
)


def _esc(value) -> str:
    """Admin-entered names land in HTML bodies — escape them."""
    return html.escape(str(value if value is not None else ""))


def _l(i18n: Optional[JsonI18n], lang: str):
    return lambda key, **kwargs: i18n.gettext(lang, key, **kwargs) if i18n else key


def _lang_and_i18n(settings: Settings, i18n_data: dict) -> Tuple[str, Optional[JsonI18n]]:
    return (
        i18n_data.get("current_language", settings.DEFAULT_LANGUAGE),
        i18n_data.get("i18n_instance"),
    )


async def _overview_text(session: AsyncSession, settings: Settings, _) -> str:
    totals = await ad_dal.get_totals(session)
    return _(
        "admin_ads_overview",
        revenue=f"{totals.get('revenue', 0.0):.2f}",
        cost=f"{totals.get('cost', 0.0):.2f}",
        payouts=f"{totals.get('payouts', 0.0):.2f}",
        currency=BASE_CURRENCY,
    )


async def _render_ads_list(
    session: AsyncSession, settings: Settings, i18n: JsonI18n, lang: str, page: int
):
    _ = _l(i18n, lang)
    text = await _overview_text(session, settings, _)
    total_count = await ad_dal.count_campaigns(session)

    if total_count == 0:
        from bot.keyboards.inline.admin_keyboards import get_ads_menu_keyboard

        return text + "\n\n" + _("admin_ads_empty"), get_ads_menu_keyboard(i18n, lang)

    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    campaigns = await ad_dal.list_campaigns_paged(session, page=page, page_size=PAGE_SIZE)

    from bot.keyboards.inline.admin_keyboards import get_ads_list_keyboard

    return (
        text + "\n\n" + _("admin_ads_header"),
        get_ads_list_keyboard(i18n, lang, campaigns, page, total_pages),
    )


async def _render_campaign_card(
    session: AsyncSession,
    settings: Settings,
    i18n: JsonI18n,
    lang: str,
    campaign: AdCampaign,
    back_page: int,
):
    _ = _l(i18n, lang)
    from bot.keyboards.inline.admin_keyboards import get_ad_card_keyboard

    if campaign.is_partner:
        stats = await ad_dal.get_partner_stats(
            session, campaign.ad_campaign_id
        )
        partner_label = str(campaign.partner_user_id)
        partner_user = (
            await user_dal.get_user_by_id(session, campaign.partner_user_id)
            if campaign.partner_user_id
            else None
        )
        if partner_user and partner_user.username:
            partner_label = f"@{partner_user.username} ({campaign.partner_user_id})"

        text = _(
            "admin_ads_partner_card",
            id=campaign.ad_campaign_id,
            source=_esc(campaign.source),
            start_param=_esc(campaign.start_param),
            partner=_esc(partner_label),
            percent=f"{campaign.partner_percent or 0:g}",
            active=_("csv_yes") if campaign.is_active else _("csv_no"),
            starts=stats["starts"]["total"],
            trials=stats["trials"],
            payers=stats["payers"],
            purchases=stats["purchases"]["total"]["count"],
            revenue=f"{stats['revenue']:.2f}",
            accrued=f"{stats['accrued']:.2f}",
            paid_out=f"{stats['paid_out']:.2f}",
            balance=f"{stats['balance']:.2f}",
            currency=BASE_CURRENCY,
        )
        if stats["unpriced"]:
            text += "\n\n" + _("admin_ads_unpriced_warning", count=stats["unpriced"])
    else:
        await ad_dal.sync_campaign_accruals(
            session, campaign.ad_campaign_id
        )
        stats = await ad_dal.get_campaign_stats(session, campaign.ad_campaign_id)
        unpriced = await ad_dal.count_unpriced_payments(session, campaign.ad_campaign_id)
        text = _(
            "admin_ads_card",
            id=campaign.ad_campaign_id,
            source=_esc(campaign.source),
            start_param=_esc(campaign.start_param),
            cost=f"{campaign.cost:.2f}",
            active=_("csv_yes") if campaign.is_active else _("csv_no"),
            starts=stats["starts"],
            trials=stats["trials"],
            payers=stats["payers"],
            revenue=f"{stats['revenue']:.2f}",
            currency=BASE_CURRENCY,
        )
        if unpriced:
            text += "\n\n" + _("admin_ads_unpriced_warning", count=unpriced)

    markup = get_ad_card_keyboard(
        i18n, lang, campaign.ad_campaign_id, back_page, is_partner=campaign.is_partner
    )
    return text, markup


async def _safe_edit(callback: types.CallbackQuery, text: str, markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logging.debug("Failed to edit ads message: %s", e)


@router.callback_query(F.data == "ads_page_display")
async def ads_page_display_noop(callback: types.CallbackQuery):
    """Passive "page X/Y" label — answer so the client stops spinning."""
    await callback.answer()


@router.callback_query(F.data == "admin_action:ads")
async def show_ads_menu(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    text, markup = await _render_ads_list(session, settings, i18n, current_lang, 0)
    await _safe_edit(callback, text, markup)
    try:
        await callback.answer()
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/admin/ads.py: %s", exc)


@router.callback_query(F.data.startswith("admin_ads:page:"))
async def ads_list_pagination(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    await state.clear()
    try:
        page = int(callback.data.split(":")[2])
    except Exception:
        page = 0

    text, markup = await _render_ads_list(session, settings, i18n, current_lang, page)
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:card:"))
async def show_ad_card(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    # This is also the "No" target of the payout confirmation, so drop any
    # half-finished payout/creation input instead of leaving the FSM armed.
    await state.clear()

    parts = callback.data.split(":")
    camp_id = int(parts[2])
    back_page = int(parts[3]) if len(parts) > 3 else 0

    camp = await ad_dal.get_campaign_by_id(session, camp_id)
    if not camp:
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return

    try:
        text, markup = await _render_campaign_card(
            session, settings, i18n, current_lang, camp, back_page
        )
    except Exception as e:
        logging.error(f"Failed to build ad card {camp_id}: {e}", exc_info=True)
        await callback.answer(_("error_occurred_try_again"), show_alert=True)
        return

    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:delete:"))
async def ads_delete_prompt(callback: types.CallbackQuery, settings: Settings, i18n_data: dict):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        _prefix, _action, camp_id_str, back_page_str = callback.data.split(":", 3)
        camp_id = int(camp_id_str)
        back_page = int(back_page_str)
    except Exception:
        await callback.answer(i18n.gettext(current_lang, "error_try_again"), show_alert=True)
        return

    from bot.keyboards.inline.admin_keyboards import get_confirmation_keyboard

    confirm_text = i18n.gettext(current_lang, "admin_ads_delete_confirm", id=camp_id)
    kb = get_confirmation_keyboard(
        yes_callback_data=f"admin_ads:delete_confirm:{camp_id}:{back_page}",
        no_callback_data=f"admin_ads:delete_cancel:{camp_id}:{back_page}",
        i18n_instance=i18n,
        lang=current_lang,
    )
    await _safe_edit(callback, confirm_text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:delete_cancel:"))
async def ads_delete_cancel(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":", 3)
        camp_id = int(parts[2])
        back_page = int(parts[3])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    camp = await ad_dal.get_campaign_by_id(session, camp_id)
    if not camp:
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return

    text, markup = await _render_campaign_card(
        session, settings, i18n, current_lang, camp, back_page
    )
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:delete_confirm:"))
async def ads_delete_confirm(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":", 3)
        camp_id = int(parts[2])
        back_page = int(parts[3])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    existed = await ad_dal.delete_campaign(session, camp_id)
    if not existed:
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return
    await session.commit()

    text, markup = await _render_ads_list(session, settings, i18n, current_lang, back_page)
    await _safe_edit(callback, text, markup)
    await callback.answer(_("admin_ads_deleted_success"), show_alert=True)


# --------------------------------------------------------------------------- #
# Campaign creation
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "admin_action:ads_create")
async def ads_create_start(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    await state.clear()
    from bot.keyboards.inline.admin_keyboards import get_ad_campaign_type_keyboard

    await _safe_edit(
        callback,
        _("admin_ads_create_type_prompt"),
        get_ad_campaign_type_keyboard(i18n, current_lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:new:"))
async def ads_create_pick_type(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    campaign_type = callback.data.split(":")[2]
    if campaign_type not in (CAMPAIGN_TYPE_AD, CAMPAIGN_TYPE_PARTNER):
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    if campaign_type == CAMPAIGN_TYPE_PARTNER and not settings.PARTNER_PROGRAM_ENABLED:
        await callback.answer(_("admin_ads_partner_disabled"), show_alert=True)
        return

    await state.set_data({"ad_campaign_type": campaign_type})
    await state.set_state(AdminStates.waiting_for_ad_source)
    await _safe_edit(callback, _("admin_ads_create_source_prompt"), None)
    await callback.answer()


@router.message(
    StateFilter(
        AdminStates.waiting_for_ad_source,
        AdminStates.waiting_for_ad_start_param,
        AdminStates.waiting_for_ad_cost,
        AdminStates.waiting_for_partner_user_id,
        AdminStates.waiting_for_partner_percent,
    ),
    F.text,
)
async def ads_create_flow(message: types.Message, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_state = await state.get_state()
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)

    data = await state.get_data()
    campaign_type = data.get("ad_campaign_type", CAMPAIGN_TYPE_AD)

    if current_state == AdminStates.waiting_for_ad_source.state:
        source = message.text.strip()
        if not source or len(source) > 64:
            await message.answer(_("admin_ads_invalid_source"))
            return
        await state.update_data(ad_source=source)
        await state.set_state(AdminStates.waiting_for_ad_start_param)
        await message.answer(_("admin_ads_create_start_param_prompt"))
        return

    if current_state == AdminStates.waiting_for_ad_start_param.state:
        start_param = message.text.strip()
        if not START_PARAM_RE.match(start_param):
            await message.answer(_("admin_ads_invalid_start_param"))
            return
        if await ad_dal.get_campaign_by_start_param(session, start_param):
            await message.answer(_("admin_ads_start_param_exists"))
            return
        await state.update_data(ad_start_param=start_param)
        if campaign_type == CAMPAIGN_TYPE_PARTNER:
            await state.set_state(AdminStates.waiting_for_partner_user_id)
            await message.answer(_("admin_ads_create_partner_user_prompt"))
        else:
            await state.set_state(AdminStates.waiting_for_ad_cost)
            await message.answer(
                _("admin_ads_create_cost_prompt", currency=BASE_CURRENCY)
            )
        return

    if current_state == AdminStates.waiting_for_partner_user_id.state:
        raw = message.text.strip().lstrip("@")
        partner_user = None
        if raw.isdigit():
            partner_user = await user_dal.get_user_by_id(session, int(raw))
        else:
            partner_user = await user_dal.get_user_by_username(session, raw)
        if not partner_user:
            await message.answer(_("admin_ads_partner_user_not_found"))
            return
        await state.update_data(partner_user_id=partner_user.user_id)
        await state.set_state(AdminStates.waiting_for_partner_percent)
        await message.answer(_("admin_ads_create_percent_prompt"))
        return

    if current_state == AdminStates.waiting_for_partner_percent.state:
        raw = message.text.replace(",", ".").strip().rstrip("%").strip()
        try:
            percent = float(raw)
            if not (0 < percent <= 100):
                raise ValueError()
        except Exception:
            await message.answer(_("admin_ads_invalid_percent"))
            return
        await _finish_campaign_creation(
            message, state, session, settings, i18n, current_lang, percent=percent
        )
        return

    if current_state == AdminStates.waiting_for_ad_cost.state:
        raw = message.text.replace(",", ".").strip()
        try:
            cost = float(raw)
            if cost < 0 or cost > 1e8:
                raise ValueError()
        except Exception:
            await message.answer(_("admin_ads_invalid_cost"))
            return
        await _finish_campaign_creation(
            message, state, session, settings, i18n, current_lang, cost=cost
        )
        return


async def _finish_campaign_creation(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    i18n: JsonI18n,
    lang: str,
    *,
    cost: float = 0.0,
    percent: Optional[float] = None,
) -> None:
    _ = _l(i18n, lang)
    data = await state.get_data()
    campaign_type = data.get("ad_campaign_type", CAMPAIGN_TYPE_AD)

    try:
        campaign = await ad_dal.create_campaign(
            session,
            source=data.get("ad_source", "unknown"),
            start_param=data.get("ad_start_param", "NA"),
            cost=cost,
            campaign_type=campaign_type,
            partner_user_id=data.get("partner_user_id"),
            partner_percent=percent,
        )
        await session.commit()
    except ValueError as ve:
        await session.rollback()
        reason = str(ve)
        if reason == "ad_campaign_start_param_exists":
            await message.answer(_("admin_ads_start_param_exists"))
        elif reason == "ad_campaign_invalid_percent":
            await message.answer(_("admin_ads_invalid_percent"))
        else:
            await message.answer(_("error_occurred_try_again"))
        return
    except Exception as e:
        await session.rollback()
        logging.error(f"Failed to create ad campaign: {e}", exc_info=True)
        await message.answer(_("error_occurred_try_again"))
        return

    await state.clear()

    bot_username = (await message.bot.me()).username
    link = f"https://t.me/{bot_username}?start={campaign.start_param}" if bot_username else campaign.start_param

    if campaign.is_partner:
        text = _(
            "admin_ads_partner_created_success",
            id=campaign.ad_campaign_id,
            source=_esc(campaign.source),
            start_param=_esc(campaign.start_param),
            partner=campaign.partner_user_id,
            percent=f"{campaign.partner_percent or 0:g}",
            link=link,
        )
    else:
        text = _(
            "admin_ads_created_success",
            id=campaign.ad_campaign_id,
            source=_esc(campaign.source),
            start_param=_esc(campaign.start_param),
            cost=f"{campaign.cost:.2f}",
            link=link,
            currency=BASE_CURRENCY,
        )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    from bot.keyboards.inline.admin_keyboards import get_ads_menu_keyboard

    await message.answer(
        _("admin_ads_back_to_menu_hint"),
        reply_markup=get_ads_menu_keyboard(i18n, lang),
    )


# --------------------------------------------------------------------------- #
# Payouts
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("admin_ads:payout_pick:"))
async def ads_payout_pick(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    await state.clear()
    try:
        page = int(callback.data.split(":")[2])
    except Exception:
        page = 0

    total = await ad_dal.count_partner_campaigns(session)
    if total == 0:
        await callback.answer(_("admin_ads_no_partners"), show_alert=True)
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    campaigns = await ad_dal.list_partner_campaigns_paged(
        session, page=page, page_size=PAGE_SIZE
    )

    from bot.keyboards.inline.admin_keyboards import get_admin_partner_pick_keyboard

    await _safe_edit(
        callback,
        _("admin_ads_payout_pick_prompt"),
        get_admin_partner_pick_keyboard(i18n, current_lang, campaigns, page, total_pages),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:payout_start:"))
async def ads_payout_start(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":")
        camp_id = int(parts[2])
        back_page = int(parts[3])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await ad_dal.get_campaign_by_id(session, camp_id)
    if not campaign or not campaign.is_partner:
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return

    stats = await ad_dal.get_partner_stats(
        session, camp_id
    )

    await state.set_state(AdminStates.waiting_for_partner_payout_amount)
    await state.set_data({"payout_campaign_id": camp_id, "payout_back_page": back_page})

    await _safe_edit(
        callback,
        _(
            "admin_ads_payout_amount_prompt",
            source=_esc(campaign.source),
            balance=f"{stats['balance']:.2f}",
            currency=BASE_CURRENCY,
        ),
        None,
    )
    await callback.answer()


@router.message(StateFilter(AdminStates.waiting_for_partner_payout_amount), F.text)
async def ads_payout_amount(message: types.Message, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)

    data = await state.get_data()
    camp_id = data.get("payout_campaign_id")
    back_page = int(data.get("payout_back_page", 0))
    if not camp_id:
        await state.clear()
        await message.answer(_("error_try_again"))
        return

    raw = DEFAULT_CURRENCY_SUFFIX_RE.sub("", message.text.strip())
    match = PAYOUT_AMOUNT_RE.match(raw)
    if not match:
        await message.answer(_("admin_ads_payout_invalid_amount"))
        return

    try:
        amount = float(match.group(1).replace(",", "."))
        if amount <= 0 or amount > 1e8:
            raise ValueError()
    except Exception:
        await message.answer(_("admin_ads_payout_invalid_amount"))
        return

    currency = (match.group(2) or BASE_CURRENCY).upper()
    if await currency_dal.get_rate(session, currency) is None:
        await message.answer(_("admin_ads_payout_unknown_currency", currency=currency))
        return

    campaign = await ad_dal.get_campaign_by_id(session, camp_id)
    if not campaign or not campaign.is_partner:
        await state.clear()
        await message.answer(_("admin_ads_not_found"))
        return

    await state.update_data(payout_amount=amount, payout_currency=currency)

    from bot.keyboards.inline.admin_keyboards import get_confirmation_keyboard

    await message.answer(
        _(
            "admin_ads_payout_confirm",
            source=_esc(campaign.source),
            partner=campaign.partner_user_id,
            amount=f"{amount:.2f}",
            currency=currency,
        ),
        parse_mode="HTML",
        reply_markup=get_confirmation_keyboard(
            yes_callback_data=f"admin_ads:payout_save:{camp_id}:{back_page}",
            no_callback_data=f"admin_ads:card:{camp_id}:{back_page}",
            i18n_instance=i18n,
            lang=current_lang,
        ),
    )


@router.callback_query(F.data.startswith("admin_ads:payout_save:"))
async def ads_payout_save(callback: types.CallbackQuery, state: FSMContext, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":")
        camp_id = int(parts[2])
        back_page = int(parts[3])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    data = await state.get_data()
    amount = data.get("payout_amount")
    currency = data.get("payout_currency", BASE_CURRENCY)
    if data.get("payout_campaign_id") != camp_id or not amount:
        await state.clear()
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await ad_dal.get_campaign_by_id(session, camp_id)
    if not campaign or not campaign.is_partner:
        await state.clear()
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return

    try:
        await ad_dal.create_payout(
            session,
            campaign_id=camp_id,
            amount=float(amount),
            currency=currency,
            created_by=callback.from_user.id,
        )
        await session.commit()
    except ValueError as ve:
        await session.rollback()
        await state.clear()
        key = (
            "admin_ads_payout_unknown_currency"
            if str(ve) == "partner_payout_unknown_currency"
            else "admin_ads_payout_invalid_amount"
        )
        await callback.answer(_(key, currency=currency), show_alert=True)
        return
    except Exception as e:
        await session.rollback()
        logging.error(f"Failed to create partner payout: {e}", exc_info=True)
        await state.clear()
        await callback.answer(_("error_occurred_try_again"), show_alert=True)
        return

    await state.clear()
    text, markup = await _render_campaign_card(
        session, settings, i18n, current_lang, campaign, back_page
    )
    await _safe_edit(callback, text, markup)
    await callback.answer(_("admin_ads_payout_saved"), show_alert=True)


async def _render_payouts(
    session: AsyncSession,
    settings: Settings,
    i18n: JsonI18n,
    lang: str,
    campaign: AdCampaign,
    back_page: int,
    page: int,
):
    _ = _l(i18n, lang)
    camp_id = campaign.ad_campaign_id
    total = await ad_dal.count_payouts(session, camp_id)
    total_pages = max(1, (total + PAYOUTS_PAGE_SIZE - 1) // PAYOUTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    payouts = await ad_dal.list_payouts_paged(
        session, camp_id, page=page, page_size=PAYOUTS_PAGE_SIZE
    )
    paid_out = await ad_dal.get_total_paid_out(session, camp_id)

    from bot.keyboards.inline.admin_keyboards import get_admin_payouts_keyboard

    text = _(
        "admin_ads_payouts_header",
        source=_esc(campaign.source),
        count=total,
        paid_out=f"{paid_out:.2f}",
        currency=BASE_CURRENCY,
    )
    if total == 0:
        text += "\n\n" + _("admin_ads_payouts_empty")

    return text, get_admin_payouts_keyboard(
        i18n, lang, camp_id, back_page, payouts, page, total_pages
    )


@router.callback_query(F.data.startswith("admin_ads:payouts:"))
async def ads_payouts_list(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":")
        camp_id = int(parts[2])
        back_page = int(parts[3])
        page = int(parts[4])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await ad_dal.get_campaign_by_id(session, camp_id)
    if not campaign or not campaign.is_partner:
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return

    text, markup = await _render_payouts(
        session, settings, i18n, current_lang, campaign, back_page, page
    )
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:payout_del:"))
async def ads_payout_delete_prompt(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":")
        camp_id, back_page, payout_id, page = (
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
            int(parts[5]),
        )
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    payout = await ad_dal.get_payout(session, camp_id, payout_id)
    if not payout:
        await callback.answer(_("admin_ads_payout_not_found"), show_alert=True)
        return

    from bot.keyboards.inline.admin_keyboards import get_confirmation_keyboard

    await _safe_edit(
        callback,
        _(
            "admin_ads_payout_delete_confirm",
            id=payout.payout_id,
            amount=f"{payout.amount:.2f}",
            currency=payout.currency,
            date=payout.created_at.strftime("%d.%m.%Y %H:%M") if payout.created_at else "—",
        ),
        get_confirmation_keyboard(
            yes_callback_data=f"admin_ads:payout_del_yes:{camp_id}:{back_page}:{payout_id}:{page}",
            no_callback_data=f"admin_ads:payouts:{camp_id}:{back_page}:{page}",
            i18n_instance=i18n,
            lang=current_lang,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_ads:payout_del_yes:"))
async def ads_payout_delete_confirm(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang, i18n = _lang_and_i18n(settings, i18n_data)
    _ = _l(i18n, current_lang)
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    try:
        parts = callback.data.split(":")
        camp_id, back_page, payout_id, page = (
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
            int(parts[5]),
        )
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await ad_dal.get_campaign_by_id(session, camp_id)
    if not campaign or not campaign.is_partner:
        await callback.answer(_("admin_ads_not_found"), show_alert=True)
        return

    deleted = await ad_dal.delete_payout(session, camp_id, payout_id)
    if not deleted:
        await callback.answer(_("admin_ads_payout_not_found"), show_alert=True)
        return
    await session.commit()

    text, markup = await _render_payouts(
        session, settings, i18n, current_lang, campaign, back_page, page
    )
    await _safe_edit(callback, text, markup)
    await callback.answer(_("admin_ads_payout_deleted"), show_alert=True)
