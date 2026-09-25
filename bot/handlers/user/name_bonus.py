"""User-facing claim action for the Telegram first-name bonus."""

from aiogram import F, Router, Bot, types
from aiogram.exceptions import TelegramAPIError
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.name_bonus_service import NameBonusService
from bot.services.panel_api_service import PanelApiService
from config.settings import Settings


router = Router(name="user_name_bonus_router")


@router.callback_query(F.data == "name_bonus:claim")
async def claim_name_bonus(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    bot: Bot,
    panel_service: PanelApiService,
    session: AsyncSession,
) -> None:
    i18n = i18n_data.get("i18n_instance")
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    _ = lambda key, **kw: i18n.gettext(lang, key, **kw) if i18n else key
    service = NameBonusService(settings, bot, panel_service, i18n)
    result = await service.claim(session, callback.from_user.id)

    if result.status == "granted":
        await callback.answer(_("name_bonus_claimed_alert"), show_alert=True)
        key = "name_bonus_claimed" if result.panel_synced else "name_bonus_claimed_pending"
        try:
            await bot.send_message(
                callback.from_user.id,
                _(key, end_date=result.end_date.strftime("%d.%m.%Y %H:%M UTC") if result.end_date else ""),
            )
        except TelegramAPIError:
            logging.warning("Could not send name bonus confirmation to %s", callback.from_user.id, exc_info=True)
        return

    if result.status == "cooldown" and result.next_at:
        text = _("name_bonus_cooldown", next_at=result.next_at.strftime("%d.%m.%Y %H:%M"))
    else:
        text = _({
            "disabled": "name_bonus_disabled",
            "not_paid": "name_bonus_purchase_required",
            "name_missing": "name_bonus_name_required",
            "telegram_error": "name_bonus_telegram_error",
            "panel_error": "name_bonus_panel_error",
        }.get(result.status, "name_bonus_panel_error"))
    await callback.answer(text, show_alert=True)
