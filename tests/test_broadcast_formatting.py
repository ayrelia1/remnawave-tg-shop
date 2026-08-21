"""Broadcast formatting: the admin's own formatting must survive the resend.

Premium (custom) emoji kept arriving as their plain fallback glyph. Two causes,
both pinned here:

1. The send forwarded the admin's `entities`. That can never work in this bot:
   it builds `Bot` with `DefaultBotProperties(parse_mode=HTML)`, so every
   request carries `parse_mode` even when the caller omits it, and Telegram
   drops `entities` whenever `parse_mode` is present. The formatting has to
   travel as HTML tags instead.
2. aiogram renders custom emoji as `<tg-emoji emoji_id="...">`, but Telegram's
   HTML parser only accepts the hyphenated `emoji-id` — the form
   `bot/constants/premium_emoji.py` already emits for the locale strings.
"""

from aiogram.types import Chat, Message, MessageEntity, PhotoSize, Sticker, User

from bot.constants.premium_emoji import TEXT_EMOJI
from bot.utils import render_message_html


CHAT = Chat(id=1, type="private")
SENDER = User(id=1, is_bot=False, first_name="admin")
EMOJI_ID = "5368324170671202286"


def _message(**kwargs) -> Message:
    return Message(message_id=1, date=0, chat=CHAT, from_user=SENDER, **kwargs)


def _custom_emoji(offset=0, length=2, emoji_id=EMOJI_ID):
    return MessageEntity(
        type="custom_emoji", offset=offset, length=length, custom_emoji_id=emoji_id
    )


def test_custom_emoji_uses_the_hyphenated_attribute():
    """`emoji_id` (aiogram's spelling) is rejected by Telegram's parser."""
    message = _message(text="👍 Привет", entities=[_custom_emoji()])

    rendered = render_message_html(message)

    assert rendered.startswith(f'<tg-emoji emoji-id="{EMOJI_ID}">')
    assert "emoji_id=" not in rendered


def test_rendered_tag_matches_what_the_locale_strings_emit():
    """The /start menu already sends premium emoji this exact way."""
    emoji_id, glyph = TEXT_EMOJI["bolt"]
    message = _message(
        text=f"{glyph} тест",
        entities=[_custom_emoji(offset=0, length=len(glyph), emoji_id=emoji_id)],
    )

    assert render_message_html(message).startswith(
        f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'
    )


def test_custom_emoji_and_bold_travel_together():
    message = _message(
        text="👍 Скидка для вернувшихся",
        entities=[_custom_emoji(), MessageEntity(type="bold", offset=3, length=6)],
    )

    assert render_message_html(message) == (
        f'<tg-emoji emoji-id="{EMOJI_ID}">👍</tg-emoji> <b>Скидка</b> для вернувшихся'
    )


def test_photo_caption_entities_are_rendered_too():
    message = _message(
        photo=[PhotoSize(file_id="F", file_unique_id="U", width=1, height=1)],
        caption="👍 Акция",
        caption_entities=[_custom_emoji()],
    )

    assert render_message_html(message) == (
        f'<tg-emoji emoji-id="{EMOJI_ID}">👍</tg-emoji> Акция'
    )


def test_other_formatting_survives():
    message = _message(
        text="секрет ссылка code",
        entities=[
            MessageEntity(type="spoiler", offset=0, length=6),
            MessageEntity(type="text_link", offset=7, length=6, url="https://ex.com"),
            MessageEntity(type="code", offset=14, length=4),
        ],
    )

    assert render_message_html(message) == (
        '<tg-spoiler>секрет</tg-spoiler> <a href="https://ex.com">ссылка</a> <code>code</code>'
    )


def test_leading_blank_lines_do_not_shift_anything():
    """The old entity path mis-anchored offsets after the text was stripped."""
    message = _message(text="\n\n👍 Привет", entities=[_custom_emoji(offset=2)])

    assert render_message_html(message) == (
        f'<tg-emoji emoji-id="{EMOJI_ID}">👍</tg-emoji> Привет'
    )


def test_literal_angle_brackets_are_escaped_when_entities_exist():
    message = _message(
        text="цена < 100 руб", entities=[MessageEntity(type="bold", offset=0, length=5)]
    )

    assert render_message_html(message) == "<b>цена </b>&lt; 100 руб"


def test_message_without_entities_is_passed_through():
    """An admin hand-writing raw HTML must keep working."""
    message = _message(text="<b>жирный</b> руками")

    assert render_message_html(message) == "<b>жирный</b> руками"


def test_plain_text_is_trimmed():
    assert render_message_html(_message(text="  привет  ")) == "привет"


def test_sticker_has_no_text():
    message = _message(
        sticker=Sticker(
            file_id="STK",
            file_unique_id="U",
            type="regular",
            width=512,
            height=512,
            is_animated=False,
            is_video=True,
        )
    )

    assert render_message_html(message) == ""
