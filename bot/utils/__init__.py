# Bot utilities package

from dataclasses import dataclass
from typing import Optional, Dict, Any
from aiogram import types


@dataclass
class MessageContent:
    """Класс для хранения информации о контенте сообщения"""
    content_type: str
    file_id: Optional[str] = None
    text: Optional[str] = None


# Словари поддерживаемых параметров для каждого типа сообщения
SUPPORTED_PARAMS = {
    "text": {"parse_mode", "entities", "disable_web_page_preview", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id"},
    "photo": {"caption", "parse_mode", "caption_entities", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id", "has_spoiler"},
    "video": {"duration", "width", "height", "thumbnail", "caption", "parse_mode", "caption_entities", "supports_streaming", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id", "has_spoiler"},
    "animation": {"duration", "width", "height", "thumbnail", "caption", "parse_mode", "caption_entities", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id", "has_spoiler"},
    "document": {"thumbnail", "caption", "parse_mode", "caption_entities", "disable_content_type_detection", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id"},
    "audio": {"caption", "parse_mode", "caption_entities", "duration", "performer", "title", "thumbnail", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id"},
    "voice": {"caption", "parse_mode", "caption_entities", "duration", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id"},
    "sticker": {"disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id"},
    "video_note": {"duration", "length", "thumbnail", "disable_notification", "protect_content", "reply_markup", "reply_to_message_id", "allow_sending_without_reply", "message_thread_id"},
}


def filter_kwargs(content_type: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Фильтрует kwargs, оставляя только поддерживаемые параметры для данного типа сообщения"""
    supported = SUPPORTED_PARAMS.get(content_type, set())
    return {k: v for k, v in kwargs.items() if k in supported}


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units - the unit Telegram entity offsets use."""
    return len(text.encode("utf-16-le")) // 2


def realign_entities_after_strip(raw_text: str, stripped_text: str, entities):
    """Re-anchor entity offsets after get_message_content() trimmed the text.

    Entity offsets are UTF-16 code units counted against the *original* text,
    so trimming leading whitespace shifts every one of them left. Sending the
    untouched offsets makes Telegram paint the formatting onto the wrong
    characters or reject the message outright.

    Formatting entities that overhang the trimmed text are clamped; a
    custom_emoji is dropped instead, because its length has to match the
    placeholder emoji exactly and a clamped one is invalid.
    """
    if not entities:
        return []
    if raw_text == stripped_text:
        return list(entities)

    shift = _utf16_len(raw_text[: len(raw_text) - len(raw_text.lstrip())])
    limit = _utf16_len(stripped_text)

    realigned = []
    for entity in entities:
        start = entity.offset - shift
        end = start + entity.length
        if start < 0 or end > limit:
            if entity.type == "custom_emoji":
                continue
            start, end = max(0, start), min(limit, end)
            if end <= start:
                continue
        realigned.append(entity.model_copy(update={"offset": start, "length": end - start}))
    return realigned


def formatting_kwargs(content_type: str, entities) -> Dict[str, Any]:
    """Pick between forwarding entities and asking Telegram to parse HTML.

    The two are mutually exclusive: when a request carries both `parse_mode`
    and `entities`, Telegram honours `parse_mode` and silently drops the
    entities - which is how premium (custom) emoji degrade to plain glyphs.
    So entities win whenever the admin's message has any, and HTML parsing is
    offered only for a message that carries no formatting of its own.
    """
    if entities:
        key = "entities" if content_type == "text" else "caption_entities"
        return {key: list(entities)}
    return {"parse_mode": "HTML"}


def get_message_content(message: types.Message) -> MessageContent:
    """
    Определяет тип контента сообщения и возвращает его данные.
    Использует match/case вместо длинных if-elif цепочек.
    """
    text = (message.text or message.caption or "").strip()
    
    # Проверяем наличие медиа-контента
    media_content = None
    if message.photo:
        media_content = ("photo", message.photo[-1].file_id)
    elif message.video:
        media_content = ("video", message.video.file_id)
    elif message.animation:
        media_content = ("animation", message.animation.file_id)
    elif message.document:
        media_content = ("document", message.document.file_id)
    elif message.audio:
        media_content = ("audio", message.audio.file_id)
    elif message.voice:
        media_content = ("voice", message.voice.file_id)
    elif message.sticker:
        media_content = ("sticker", message.sticker.file_id)
    elif message.video_note:
        media_content = ("video_note", message.video_note.file_id)
    
    # Используем match/case для определения типа контента
    match media_content:
        case (content_type, file_id):
            return MessageContent(content_type=content_type, file_id=file_id, text=text)
        case None:
            return MessageContent(content_type="text", text=text)
        case _:
            return MessageContent(content_type="text", text=text)


async def send_message_by_type(bot, chat_id: int, content: MessageContent, **kwargs) -> None:
    """
    Отправляет сообщение указанного типа.
    Использует match/case вместо длинных if-elif цепочек.
    Автоматически фильтрует неподдерживаемые параметры.
    """
    # Фильтруем kwargs для данного типа сообщения
    filtered_kwargs = filter_kwargs(content.content_type, kwargs)
    
    match content.content_type:
        case "text":
            await bot.send_message(
                chat_id=chat_id,
                text=content.text,
                **filtered_kwargs
            )
        case "photo":
            await bot.send_photo(
                chat_id=chat_id,
                photo=content.file_id,
                caption=content.text or None,
                **filtered_kwargs
            )
        case "video":
            await bot.send_video(
                chat_id=chat_id,
                video=content.file_id,
                caption=content.text or None,
                **filtered_kwargs
            )
        case "animation":
            await bot.send_animation(
                chat_id=chat_id,
                animation=content.file_id,
                caption=content.text or None,
                **filtered_kwargs
            )
        case "document":
            await bot.send_document(
                chat_id=chat_id,
                document=content.file_id,
                caption=content.text or None,
                **filtered_kwargs
            )
        case "audio":
            await bot.send_audio(
                chat_id=chat_id,
                audio=content.file_id,
                caption=content.text or None,
                **filtered_kwargs
            )
        case "voice":
            await bot.send_voice(
                chat_id=chat_id,
                voice=content.file_id,
                caption=content.text or None,
                **filtered_kwargs
            )
        case "sticker":
            await bot.send_sticker(
                chat_id=chat_id,
                sticker=content.file_id,
                **filtered_kwargs
            )
        case "video_note":
            await bot.send_video_note(
                chat_id=chat_id,
                video_note=content.file_id,
                **filtered_kwargs
            )
        case _:
            # Fallback для неизвестных типов - отправляем как текст
            text_kwargs = filter_kwargs("text", kwargs)
            await bot.send_message(
                chat_id=chat_id,
                text=content.text or "Unknown content type",
                **text_kwargs
            )


async def send_message_via_queue(queue_manager, uid: int, content: MessageContent, **kwargs) -> None:
    """
    Отправляет сообщение через очередь в зависимости от типа контента.
    Использует match/case вместо длинных if-elif цепочек.
    Автоматически фильтрует неподдерживаемые параметры.
    """
    # Фильтруем kwargs для данного типа сообщения
    filtered_kwargs = filter_kwargs(content.content_type, kwargs)
    
    match content.content_type:
        case "text":
            await queue_manager.send_message(
                chat_id=uid, text=content.text, **filtered_kwargs
            )
        case "photo":
            await queue_manager.send_photo(
                chat_id=uid, photo=content.file_id, caption=content.text or None, **filtered_kwargs
            )
        case "video":
            await queue_manager.send_video(
                chat_id=uid, video=content.file_id, caption=content.text or None, **filtered_kwargs
            )
        case "animation":
            await queue_manager.send_animation(
                chat_id=uid, animation=content.file_id, caption=content.text or None, **filtered_kwargs
            )
        case "document":
            await queue_manager.send_document(
                chat_id=uid, document=content.file_id, caption=content.text or None, **filtered_kwargs
            )
        case "audio":
            await queue_manager.send_audio(
                chat_id=uid, audio=content.file_id, caption=content.text or None, **filtered_kwargs
            )
        case "voice":
            await queue_manager.send_voice(
                chat_id=uid, voice=content.file_id, caption=content.text or None, **filtered_kwargs
            )
        case "sticker":
            await queue_manager.send_sticker(
                chat_id=uid, sticker=content.file_id, **filtered_kwargs
            )
        case "video_note":
            await queue_manager.send_video_note(
                chat_id=uid, video_note=content.file_id, **filtered_kwargs
            )
        case _:
            # Fallback для неизвестных типов - отправляем как текст
            text_kwargs = filter_kwargs("text", kwargs)
            await queue_manager.send_message(
                chat_id=uid, text=content.text or "Unknown content type", **text_kwargs
            )


async def send_direct_message(bot, chat_id: int, content: MessageContent, extra_text: str = "", **kwargs) -> None:
    """
    Отправляет прямое сообщение с дополнительной обработкой для sticker и video_note.
    Для этих типов медиа отправляется отдельное текстовое сообщение, т.к. они не поддерживают caption.
    Автоматически фильтрует неподдерживаемые параметры.
    """
    match content.content_type:
        case "sticker":
            # Отправляем стикер с отфильтрованными параметрами
            sticker_kwargs = filter_kwargs("sticker", kwargs)
            await bot.send_sticker(
                chat_id=chat_id,
                sticker=content.file_id,
                **sticker_kwargs
            )
            # Если есть текст с подписью, отправляем отдельно
            if content.text or extra_text:
                text_to_send = (content.text + extra_text) if content.text else extra_text
                text_kwargs = filter_kwargs("text", kwargs)
                await bot.send_message(
                    chat_id,
                    text_to_send,
                    **text_kwargs
                )
        case "video_note":
            # Отправляем видео-заметку с отфильтрованными параметрами
            video_note_kwargs = filter_kwargs("video_note", kwargs)
            await bot.send_video_note(
                chat_id=chat_id,
                video_note=content.file_id,
                **video_note_kwargs
            )
            # Если есть текст с подписью, отправляем отдельно
            if content.text or extra_text:
                text_to_send = (content.text + extra_text) if content.text else extra_text
                text_kwargs = filter_kwargs("text", kwargs)
                await bot.send_message(
                    chat_id,
                    text_to_send,
                    **text_kwargs
                )
        case "text":
            # Для текста объединяем с extra_text
            final_text = (content.text + extra_text) if content.text else extra_text
            text_kwargs = filter_kwargs("text", kwargs)
            await bot.send_message(
                chat_id=chat_id,
                text=final_text,
                **text_kwargs
            )
        case _:
            # Для остальных типов медиа используем caption
            final_caption = (content.text + extra_text) if content.text else None
            await send_message_by_type(
                bot, chat_id, 
                MessageContent(content.content_type, content.file_id, final_caption),
                **kwargs
            )