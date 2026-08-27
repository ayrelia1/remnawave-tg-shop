"""Partner (affiliate) cabinet: /partner.

A partner owns one or more campaign labels (`ad_campaigns` rows of type
"partner"). Everything shown here is derived from payments on read — there is
no stored balance — so the numbers can never drift away from the ledger.
"""

import html
import logging
from typing import Optional

from aiogram import Bot, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from config.currency import BASE_CURRENCY
from bot.middlewares.i18n import JsonI18n
from db.dal import ad_dal, user_dal
from db.models import AdCampaign, CampaignAccrual, PartnerPayout
from bot.keyboards.inline.partner_keyboards import (
    get_partner_card_keyboard,
    get_partner_detail_keyboard,
    get_partner_no_programs_keyboard,
    get_partner_payouts_keyboard,
    get_partner_programs_keyboard,
    get_partner_purchases_keyboard,
)

router = Router(name="user_partner_router")

HISTORY_PAGE_SIZE = 8


def _esc(value) -> str:
    """Campaign names are admin-entered — escape before they hit an HTML body."""
    return html.escape(str(value if value is not None else ""))


def _l(i18n: JsonI18n, lang: str):
    return lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)


async def _campaign_link(bot: Bot, campaign: AdCampaign) -> str:
    try:
        me = await bot.me()
        if me and me.username:
            return f"https://t.me/{me.username}?start={campaign.start_param}"
    except Exception as e:
        logging.debug("Failed to resolve bot username for partner link: %s", e)
    return campaign.start_param


async def _owned_campaign(
    session: AsyncSession, campaign_id: int, user_id: int
) -> Optional[AdCampaign]:
    """Load a campaign only if this user really owns it."""
    campaign = await ad_dal.get_campaign_by_id(session, campaign_id)
    if not campaign or not campaign.is_partner:
        return None
    if campaign.partner_user_id != user_id:
        logging.warning(
            "User %s tried to open partner campaign %s owned by %s",
            user_id,
            campaign_id,
            campaign.partner_user_id,
        )
        return None
    return campaign


def _format_tariff(accrual: CampaignAccrual, _) -> str:
    parts = []
    if accrual.subscription_duration_months:
        parts.append(_("partner_sale_months", months=accrual.subscription_duration_months))
    if accrual.hwid_device_limit:
        parts.append(_("partner_sale_devices", devices=accrual.hwid_device_limit))
    return ", ".join(parts) if parts else "—"


async def _build_card(
    session: AsyncSession,
    settings: Settings,
    i18n: JsonI18n,
    lang: str,
    bot: Bot,
    campaign: AdCampaign,
) -> str:
    _ = _l(i18n, lang)
    stats = await ad_dal.get_partner_stats(
        session, campaign.ad_campaign_id
    )
    currency = BASE_CURRENCY
    purchases = stats["purchases"]
    starts = stats["starts"]

    return _(
        "partner_card",
        source=_esc(campaign.source),
        link=await _campaign_link(bot, campaign),
        percent=f"{campaign.partner_percent or 0:g}",
        starts_total=starts["total"],
        starts_day=starts["day"],
        starts_week=starts["week"],
        starts_month=starts["month"],
        trials=stats["trials"],
        buyers=stats["payers"],
        buys_total=purchases["total"]["count"],
        sum_total=f"{purchases['total']['amount']:.2f}",
        buys_day=purchases["day"]["count"],
        sum_day=f"{purchases['day']['amount']:.2f}",
        buys_week=purchases["week"]["count"],
        sum_week=f"{purchases['week']['amount']:.2f}",
        buys_month=purchases["month"]["count"],
        sum_month=f"{purchases['month']['amount']:.2f}",
        accrued=f"{stats['accrued']:.2f}",
        paid_out=f"{stats['paid_out']:.2f}",
        balance=f"{stats['balance']:.2f}",
        min_payout=f"{settings.PARTNER_MIN_PAYOUT:.0f}",
        currency=currency,
    )


async def _send_or_edit(
    event: types.Message | types.CallbackQuery, text: str, markup
) -> None:
    if isinstance(event, types.CallbackQuery):
        if not event.message:
            return
        try:
            await event.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logging.debug("Failed to edit partner message: %s", e)
        return
    await event.answer(
        text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True
    )


async def _show_programs(
    event: types.Message | types.CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    i18n: JsonI18n,
    lang: str,
    bot: Bot,
    user_id: int,
) -> None:
    _ = _l(i18n, lang)
    campaigns = await ad_dal.list_partner_campaigns_for_user(session, user_id)

    if not campaigns:
        await _send_or_edit(
            event,
            _("partner_no_programs"),
            get_partner_no_programs_keyboard(i18n, lang, settings.SUPPORT_LINK),
        )
        return

    if len(campaigns) == 1:
        campaign = campaigns[0]
        text = await _build_card(session, settings, i18n, lang, bot, campaign)
        await _send_or_edit(
            event,
            text,
            get_partner_card_keyboard(
                i18n,
                lang,
                campaign.ad_campaign_id,
                show_back_to_list=False,
                support_link=settings.SUPPORT_LINK,
            ),
        )
        return

    await _send_or_edit(
        event,
        _("partner_programs_header", count=len(campaigns)),
        get_partner_programs_keyboard(i18n, lang, campaigns),
    )


@router.message(Command("partner"))
async def partner_command(
    message: types.Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.answer("Language service error.")
        return

    if not settings.PARTNER_PROGRAM_ENABLED:
        await message.answer(
            i18n.gettext(current_lang, "partner_disabled"), parse_mode="HTML"
        )
        return

    await state.clear()
    await _show_programs(
        message, session, settings, i18n, current_lang, bot, message.from_user.id
    )


@router.callback_query(F.data == "partner:list")
async def partner_list_callback(
    callback: types.CallbackQuery,
    bot: Bot,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return

    await _show_programs(
        callback, session, settings, i18n, current_lang, bot, callback.from_user.id
    )
    await callback.answer()


@router.callback_query(F.data.startswith("partner:card:"))
async def partner_card_callback(
    callback: types.CallbackQuery,
    bot: Bot,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = _l(i18n, current_lang)

    try:
        campaign_id = int(callback.data.split(":")[2])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await _owned_campaign(session, campaign_id, callback.from_user.id)
    if not campaign:
        await callback.answer(_("partner_program_not_found"), show_alert=True)
        return

    owned = await ad_dal.list_partner_campaigns_for_user(session, callback.from_user.id)
    text = await _build_card(session, settings, i18n, current_lang, bot, campaign)
    await _send_or_edit(
        callback,
        text,
        get_partner_card_keyboard(
            i18n,
            current_lang,
            campaign_id,
            show_back_to_list=len(owned) > 1,
            support_link=settings.SUPPORT_LINK,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("partner:sales:"))
async def partner_sales_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = _l(i18n, current_lang)

    try:
        parts = callback.data.split(":")
        campaign_id = int(parts[2])
        page = int(parts[3])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await _owned_campaign(session, campaign_id, callback.from_user.id)
    if not campaign:
        await callback.answer(_("partner_program_not_found"), show_alert=True)
        return

    await ad_dal.sync_campaign_accruals(session, campaign_id)
    total = await ad_dal.count_campaign_accruals(session, campaign_id)
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    accruals = await ad_dal.list_campaign_accruals_paged(
        session, campaign_id, page=page, page_size=HISTORY_PAGE_SIZE
    )

    text = _("partner_purchases_header", source=_esc(campaign.source), count=total)
    if total == 0:
        text += "\n\n" + _("partner_purchases_empty")

    await _send_or_edit(
        callback,
        text,
        get_partner_purchases_keyboard(
            i18n, current_lang, campaign_id, accruals, page, total_pages
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("partner:sale:"))
async def partner_sale_detail(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = _l(i18n, current_lang)

    try:
        parts = callback.data.split(":")
        campaign_id = int(parts[2])
        accrual_id = int(parts[3])
        back_page = int(parts[4])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await _owned_campaign(session, campaign_id, callback.from_user.id)
    if not campaign:
        await callback.answer(_("partner_program_not_found"), show_alert=True)
        return

    accrual = await ad_dal.get_campaign_accrual(session, campaign_id, accrual_id)
    if not accrual:
        await callback.answer(_("partner_sale_not_found"), show_alert=True)
        return

    client = str(accrual.user_id)
    buyer = await user_dal.get_user_by_id(session, accrual.user_id)
    if buyer and buyer.username:
        client = f"@{buyer.username} ({accrual.user_id})"

    text = _(
        "partner_sale_detail",
        id=accrual.accrual_id,
        client=_esc(client),
        tariff=_esc(_format_tariff(accrual, _)),
        amount=f"{accrual.amount:.2f}",
        payment_currency=accrual.currency,
        date=accrual.paid_at.strftime("%d.%m.%Y %H:%M") if accrual.paid_at else "—",
        percent=f"{accrual.percent:g}",
        earned=f"{accrual.earned_amount:.2f}",
        currency=BASE_CURRENCY,
    )

    await _send_or_edit(
        callback,
        text,
        get_partner_detail_keyboard(
            i18n, current_lang, f"partner:sales:{campaign_id}:{back_page}"
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("partner:payouts:"))
async def partner_payouts_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = _l(i18n, current_lang)

    try:
        parts = callback.data.split(":")
        campaign_id = int(parts[2])
        page = int(parts[3])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await _owned_campaign(session, campaign_id, callback.from_user.id)
    if not campaign:
        await callback.answer(_("partner_program_not_found"), show_alert=True)
        return

    total = await ad_dal.count_payouts(session, campaign_id)
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    payouts = await ad_dal.list_payouts_paged(
        session, campaign_id, page=page, page_size=HISTORY_PAGE_SIZE
    )
    paid_out = await ad_dal.get_total_paid_out(session, campaign_id)

    text = _(
        "partner_payouts_header",
        source=_esc(campaign.source),
        count=total,
        paid_out=f"{paid_out:.2f}",
        currency=BASE_CURRENCY,
    )
    if total == 0:
        text += "\n\n" + _("partner_payouts_empty")

    await _send_or_edit(
        callback,
        text,
        get_partner_payouts_keyboard(
            i18n, current_lang, campaign_id, payouts, page, total_pages
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("partner:payout:"))
async def partner_payout_detail(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = _l(i18n, current_lang)

    try:
        parts = callback.data.split(":")
        campaign_id = int(parts[2])
        payout_id = int(parts[3])
        back_page = int(parts[4])
    except Exception:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    campaign = await _owned_campaign(session, campaign_id, callback.from_user.id)
    if not campaign:
        await callback.answer(_("partner_program_not_found"), show_alert=True)
        return

    payout: Optional[PartnerPayout] = await ad_dal.get_payout(
        session, campaign_id, payout_id
    )
    if not payout:
        await callback.answer(_("partner_payout_not_found"), show_alert=True)
        return

    text = _(
        "partner_payout_detail",
        id=payout.payout_id,
        source=_esc(campaign.source),
        amount=f"{payout.amount:.2f}",
        currency=payout.currency,
        date=payout.created_at.strftime("%d.%m.%Y %H:%M") if payout.created_at else "—",
        comment=_esc(payout.comment) if payout.comment else _("partner_payout_no_comment"),
    )

    await _send_or_edit(
        callback,
        text,
        get_partner_detail_keyboard(
            i18n, current_lang, f"partner:payouts:{campaign_id}:{back_page}"
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "partner:noop")
async def partner_noop(callback: types.CallbackQuery):
    await callback.answer()
