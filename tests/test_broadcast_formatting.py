"""Broadcast text formatting: entities vs parse_mode, and offset realignment.

Two bugs are pinned here, both of which silently degraded premium (custom)
emoji to plain glyphs:

1. The send carried `parse_mode="HTML"` *and* `entities`. Telegram honours
   `parse_mode` when both are present and drops the entities, so the admin's
   custom emoji never survived the trip.
2. `get_message_content()` strips the text while entity offsets stay anchored
   to the untrimmed original, so a message starting with a blank line shifted
   every entity onto the wrong characters.
"""

from aiogram.types import MessageEntity

from bot.utils import formatting_kwargs, realign_entities_after_strip


def _custom_emoji(offset, length=2, emoji_id="5368324170671202286"):
    return MessageEntity(
        type="custom_emoji", offset=offset, length=length, custom_emoji_id=emoji_id
    )


# ------------------------------------------------- entities vs parse_mode


def test_entities_are_sent_without_parse_mode():
    """Passing both makes Telegram ignore the entities."""
    entities = [_custom_emoji(0)]

    kwargs = formatting_kwargs("text", entities)

    assert kwargs == {"entities": entities}
    assert "parse_mode" not in kwargs


def test_media_captions_use_caption_entities():
    entities = [_custom_emoji(0)]

    kwargs = formatting_kwargs("photo", entities)

    assert kwargs == {"caption_entities": entities}


def test_plain_message_still_gets_html_parsing():
    """An admin who types raw <b>...</b> must keep working."""
    assert formatting_kwargs("text", []) == {"parse_mode": "HTML"}
    assert formatting_kwargs("photo", None) == {"parse_mode": "HTML"}


# ------------------------------------------------------ offset realignment


def test_untouched_text_leaves_entities_alone():
    entities = [_custom_emoji(0), MessageEntity(type="bold", offset=3, length=6)]

    assert realign_entities_after_strip("👍 Привет", "👍 Привет", entities) == entities


def test_leading_whitespace_shifts_every_offset():
    raw = "\n\n👍 Привет"
    stripped = "👍 Привет"
    entities = [_custom_emoji(2), MessageEntity(type="bold", offset=5, length=6)]

    realigned = realign_entities_after_strip(raw, stripped, entities)

    assert [(e.type, e.offset, e.length) for e in realigned] == [
        ("custom_emoji", 0, 2),
        ("bold", 3, 6),
    ]


def test_shift_counts_utf16_units_not_python_characters():
    """A leading emoji is two UTF-16 units, which is what offsets are in."""
    raw = " 👍 хвост"
    stripped = "👍 хвост"
    # "хвост" sits at UTF-16 offset 4 in raw: space(1) + emoji(2) + space(1)
    entities = [MessageEntity(type="bold", offset=4, length=5)]

    realigned = realign_entities_after_strip(raw, stripped, entities)

    assert (realigned[0].offset, realigned[0].length) == (3, 5)
    start = realigned[0].offset
    assert stripped.encode("utf-16-le")[start * 2 : (start + 5) * 2].decode("utf-16-le") == "хвост"


def test_custom_emoji_that_no_longer_fits_is_dropped_not_clamped():
    """Its length must match the placeholder exactly; a clipped one is invalid."""
    raw = "текст 👍  "
    stripped = "текст 👍"
    entities = [_custom_emoji(6), MessageEntity(type="bold", offset=0, length=5)]

    realigned = realign_entities_after_strip(raw, stripped, entities)

    # The custom emoji still fits here, so both survive.
    assert len(realigned) == 2

    # Now make it overhang the trimmed end.
    overhanging = [_custom_emoji(7, length=3)]
    assert realign_entities_after_strip(raw, stripped, overhanging) == []


def test_formatting_entity_overhanging_the_trim_is_clamped():
    raw = "жирный текст\n\n"
    stripped = "жирный текст"
    entities = [MessageEntity(type="bold", offset=0, length=14)]

    realigned = realign_entities_after_strip(raw, stripped, entities)

    assert (realigned[0].offset, realigned[0].length) == (0, 12)


def test_entity_falling_entirely_outside_is_dropped():
    raw = "текст\n\n"
    stripped = "текст"
    entities = [MessageEntity(type="italic", offset=5, length=2)]

    assert realign_entities_after_strip(raw, stripped, entities) == []


def test_empty_entities_stay_empty():
    assert realign_entities_after_strip("  a  ", "a", []) == []
    assert realign_entities_after_strip("  a  ", "a", None) == []
