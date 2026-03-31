import logging
import os
from typing import Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, InlineKeyboardMarkup, Message

_pic_file_id: Optional[str] = None
PIC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "assets", "pic.png")


async def get_pic(bot: Bot) -> Union[str, FSInputFile]:
    """Return cached file_id for pic.png, or FSInputFile for first upload."""
    global _pic_file_id
    if _pic_file_id:
        return _pic_file_id
    return FSInputFile(PIC_PATH)


def cache_pic_file_id(message: Message) -> None:
    """Cache the file_id from a successfully sent photo message."""
    global _pic_file_id
    if _pic_file_id:
        return
    if message.photo:
        _pic_file_id = message.photo[-1].file_id


async def edit_or_send_with_photo(
        message: Message,
        bot: Bot,
        caption: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = "HTML",
        is_edit: bool = True) -> None:
    """
    Send or edit a message so it always shows pic.png with the given caption.
    - is_edit=True: tries edit_media first; on failure sends a new photo message.
    - is_edit=False: sends a new photo message.
    """
    photo = await get_pic(bot)

    if is_edit:
        try:
            sent = await message.edit_media(
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode=parse_mode),
                reply_markup=reply_markup,
            )
            if sent and sent.photo:
                cache_pic_file_id(sent)
            return
        except TelegramBadRequest as e:
            logging.debug("edit_media failed (%s), sending new photo message.", e)
        except Exception as e:
            logging.warning("edit_or_send_with_photo edit_media error: %s", e)

    try:
        sent = await message.answer_photo(
            photo=photo,
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        if sent and sent.photo:
            cache_pic_file_id(sent)
    except Exception as e:
        logging.error("edit_or_send_with_photo answer_photo error: %s", e)


async def safe_edit_text(
        message: Message,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False) -> None:
    """
    Edit a message's text. If the message is a photo (caption), uses edit_caption.
    Falls back to sending a new message if both edits fail.
    """
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return
    except TelegramBadRequest as e:
        if "there is no text in the message" in str(e).lower() or "message can't be edited" in str(e).lower():
            pass
        else:
            logging.warning("safe_edit_text edit_text failed: %s", e)

    try:
        await message.edit_caption(
            caption=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception as e2:
        logging.warning("safe_edit_text edit_caption also failed: %s", e2)
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e3:
            logging.error("safe_edit_text answer fallback failed: %s", e3)
