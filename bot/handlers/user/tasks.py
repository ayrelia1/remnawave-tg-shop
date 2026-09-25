"""User task catalog and task detail screens."""

from typing import Callable

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardMarkup

from bot.middlewares.i18n import JsonI18n
from bot.keyboards.inline.user_keyboards import (
    get_name_bonus_task_keyboard,
    get_tasks_keyboard,
)
from bot.utils.message_helpers import safe_edit_text
from config.settings import Settings


router = Router(name="user_tasks_router")


async def _show_task_screen(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    text_key: str,
    keyboard_factory: Callable[[str, JsonI18n], InlineKeyboardMarkup],
) -> None:
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    if i18n is None:
        await callback.answer("⚠️ Could not open tasks. Please try again.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer(f"⚠️ {i18n.gettext(lang, 'error_displaying_menu')}", show_alert=True)
        return
    if not settings.NAME_BONUS_ENABLED:
        await callback.answer(i18n.gettext(lang, "name_bonus_disabled"), show_alert=True)
        return

    await safe_edit_text(
        callback.message,
        i18n.gettext(lang, text_key),
        reply_markup=keyboard_factory(lang, i18n),
    )
    await callback.answer()


@router.callback_query(F.data == "tasks:menu")
async def show_tasks(callback: types.CallbackQuery, settings: Settings, i18n_data: dict) -> None:
    await _show_task_screen(callback, settings, i18n_data, "tasks_intro", get_tasks_keyboard)


@router.callback_query(F.data == "tasks:name_bonus")
async def show_name_bonus_task(callback: types.CallbackQuery, settings: Settings, i18n_data: dict) -> None:
    await _show_task_screen(
        callback, settings, i18n_data,
        "name_bonus_task_description", get_name_bonus_task_keyboard,
    )
