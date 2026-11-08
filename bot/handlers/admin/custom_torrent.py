from aiogram import F, Router, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.constants.premium_emoji import (
    PREMIUM_EMOJI_BACK,
    PREMIUM_EMOJI_NUMBERS,
    PREMIUM_EMOJI_SETTINGS,
    PREMIUM_EMOJI_YES,
)
from bot.services.custom_torrent_reporter import TORRENT_LEVELS
from bot.utils.message_helpers import safe_edit_text
from config.settings import Settings
from db.models import TorrentReportSettings


router = Router(name="admin_custom_torrent_router")


@router.callback_query(F.data.startswith("admin_torrent:"))
async def torrent_settings(callback: types.CallbackQuery, settings: Settings, i18n_data: dict,
                           session: AsyncSession) -> None:
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer()
        return
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    action = callback.data.removeprefix("admin_torrent:")
    level = action.removeprefix("level:")
    policy = await session.get(TorrentReportSettings, 1, with_for_update=action != "open")
    if policy is None:
        await callback.answer(_("admin_custom_torrent_unavailable"), show_alert=True)
        return

    if action in ("enable", "disable") and policy.auto_response_enabled != (action == "enable"):
        policy.auto_response_enabled = action == "enable"
    elif (action.startswith("level:") and level in TORRENT_LEVELS
          and policy.auto_response_enabled and policy.minimum_level != level):
        policy.minimum_level = level
    elif action != "open":
        await callback.answer()
        return
    if action != "open":
        await session.commit()
    await callback.answer()

    receiver = "off"
    if settings.CUSTOM_TORRENT_REPORTER_ENABLED:
        receiver = "on" if settings.custom_torrent_reporter_active else "unconfigured"
    text = _("admin_custom_torrent_settings",
             receiver=_(f"admin_custom_torrent_receiver_{receiver}"),
             auto=_("admin_custom_torrent_on" if policy.auto_response_enabled else "admin_custom_torrent_off"),
             level=policy.minimum_level)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=_("admin_custom_torrent_disable" if policy.auto_response_enabled else "admin_custom_torrent_enable"),
        callback_data="admin_torrent:disable" if policy.auto_response_enabled else "admin_torrent:enable",
        icon_custom_emoji_id=PREMIUM_EMOJI_SETTINGS,
    )
    if policy.auto_response_enabled:
        for option in TORRENT_LEVELS:
            builder.button(text=option, callback_data=f"admin_torrent:level:{option}",
                           icon_custom_emoji_id=PREMIUM_EMOJI_YES if option == policy.minimum_level
                           else PREMIUM_EMOJI_NUMBERS)
    builder.button(text=_("admin_system_functions_section"), callback_data="admin_section:system_functions",
                   icon_custom_emoji_id=PREMIUM_EMOJI_BACK)
    builder.adjust(1, 2, 2, 1)
    await safe_edit_text(callback.message, text, reply_markup=builder.as_markup())
