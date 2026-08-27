"""Inline keyboards for the partner (affiliate) cabinet — the /partner flow.

Labels are plain words: the icon is supplied by ``icon_custom_emoji_id``, so a
glyph in the text would render beside the premium icon instead of being it.
``::token::`` markers are HTML-body-only and never belong here either.
"""

from typing import List, Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.constants.premium_emoji import (
    PREMIUM_EMOJI_BACK,
    PREMIUM_EMOJI_DOCUMENT,
    PREMIUM_EMOJI_NEXT,
    PREMIUM_EMOJI_NUMBERS,
    PREMIUM_EMOJI_PAY,
    PREMIUM_EMOJI_SUPPORT,
    PREMIUM_EMOJI_TAG,
)


def _pagination_row(
    _, current_page: int, total_pages: int, callback_prefix: str
) -> List[InlineKeyboardButton]:
    """`callback_prefix` receives the page number appended with ':'."""
    row: List[InlineKeyboardButton] = []
    if total_pages <= 1:
        return row
    if current_page > 0:
        row.append(
            InlineKeyboardButton(
                text=_("prev_page_button"),
                callback_data=f"{callback_prefix}:{current_page - 1}",
                icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
            )
        )
    row.append(
        InlineKeyboardButton(
            text=f"{current_page + 1}/{total_pages}",
            callback_data="partner:noop",
            icon_custom_emoji_id=PREMIUM_EMOJI_NUMBERS,
        )
    )
    if current_page < total_pages - 1:
        row.append(
            InlineKeyboardButton(
                text=_("next_page_button"),
                callback_data=f"{callback_prefix}:{current_page + 1}",
                icon_custom_emoji_id=PREMIUM_EMOJI_NEXT,
            )
        )
    return row


def get_partner_no_programs_keyboard(
    i18n_instance, lang: str, support_link: Optional[str]
) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    if support_link:
        builder.button(
            text=_("menu_support_button"),
            url=support_link,
            icon_custom_emoji_id=PREMIUM_EMOJI_SUPPORT,
        )
    builder.button(
        text=_("back_to_main_menu_button"),
        callback_data="main_action:back_to_main",
        icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
    )
    builder.adjust(1)
    return builder.as_markup()


def get_partner_programs_keyboard(
    i18n_instance, lang: str, campaigns: list
) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    for campaign in campaigns:
        builder.button(
            text=f"{campaign.source} · {campaign.partner_percent or 0:g}%",
            callback_data=f"partner:card:{campaign.ad_campaign_id}",
            icon_custom_emoji_id=PREMIUM_EMOJI_TAG,
        )
    builder.button(
        text=_("back_to_main_menu_button"),
        callback_data="main_action:back_to_main",
        icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
    )
    builder.adjust(1)
    return builder.as_markup()


def get_partner_card_keyboard(
    i18n_instance,
    lang: str,
    campaign_id: int,
    *,
    show_back_to_list: bool,
    support_link: Optional[str],
) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=_("partner_purchases_button"),
        callback_data=f"partner:sales:{campaign_id}:0",
        icon_custom_emoji_id=PREMIUM_EMOJI_DOCUMENT,
    )
    builder.button(
        text=_("partner_payouts_button"),
        callback_data=f"partner:payouts:{campaign_id}:0",
        icon_custom_emoji_id=PREMIUM_EMOJI_PAY,
    )
    if support_link:
        builder.button(
            text=_("partner_request_payout_button"),
            url=support_link,
            icon_custom_emoji_id=PREMIUM_EMOJI_SUPPORT,
        )
    if show_back_to_list:
        builder.button(
            text=_("partner_back_to_list_button"),
            callback_data="partner:list",
            icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
        )
    builder.button(
        text=_("back_to_main_menu_button"),
        callback_data="main_action:back_to_main",
        icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
    )
    builder.adjust(1)
    return builder.as_markup()


def get_partner_payouts_keyboard(
    i18n_instance,
    lang: str,
    campaign_id: int,
    payouts: list,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    for payout in payouts:
        date_text = payout.created_at.strftime("%d.%m.%Y") if payout.created_at else "—"
        builder.button(
            text=f"{date_text} · {payout.amount:.2f} {payout.currency}",
            callback_data=f"partner:payout:{campaign_id}:{payout.payout_id}:{current_page}",
            icon_custom_emoji_id=PREMIUM_EMOJI_PAY,
        )
    builder.adjust(1)

    row = _pagination_row(
        _, current_page, total_pages, f"partner:payouts:{campaign_id}"
    )
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text=_("partner_back_to_card_button"),
            callback_data=f"partner:card:{campaign_id}",
            icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
        )
    )
    return builder.as_markup()


def get_partner_purchases_keyboard(
    i18n_instance,
    lang: str,
    campaign_id: int,
    accruals: list,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    for accrual in accruals:
        date_text = accrual.paid_at.strftime("%d.%m.%Y") if accrual.paid_at else "—"
        builder.button(
            text=f"{date_text} · {accrual.amount:.2f} {accrual.currency}",
            callback_data=f"partner:sale:{campaign_id}:{accrual.accrual_id}:{current_page}",
            icon_custom_emoji_id=PREMIUM_EMOJI_DOCUMENT,
        )
    builder.adjust(1)

    row = _pagination_row(_, current_page, total_pages, f"partner:sales:{campaign_id}")
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text=_("partner_back_to_card_button"),
            callback_data=f"partner:card:{campaign_id}",
            icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
        )
    )
    return builder.as_markup()


def get_partner_detail_keyboard(
    i18n_instance, lang: str, back_callback: str
) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=_("partner_back_button"),
        callback_data=back_callback,
        icon_custom_emoji_id=PREMIUM_EMOJI_BACK,
    )
    builder.adjust(1)
    return builder.as_markup()
