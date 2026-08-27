"""Keyboard/locale wiring for the partner program.

These catch the class of bug that only shows up in Telegram: a callback nobody
handles, a payload over the 64-byte limit, premium-emoji tokens leaking into a
plain-text button label, or a list collapsing into a single row.
"""

import ast
import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.constants.premium_emoji import TEXT_EMOJI, TEXT_EMOJI_TOKEN_RE  # noqa: E402
from bot.keyboards.inline import admin_keyboards as ak  # noqa: E402
from bot.keyboards.inline import partner_keyboards as pk  # noqa: E402

HANDLER_FILES = (
    "bot/handlers/admin/ads.py",
    "bot/handlers/admin/currency_rates.py",
    "bot/handlers/user/partner.py",
)
LOCALE_CONSUMERS = HANDLER_FILES + (
    "bot/keyboards/inline/partner_keyboards.py",
    "bot/keyboards/inline/admin_keyboards.py",
)
# Pre-existing passive page counter, deliberately unhandled.
UNHANDLED_BY_DESIGN = {"ads_page_display"}


class FakeI18n:
    def gettext(self, lang, key, **kwargs):
        return key


@pytest.fixture(scope="module")
def locales():
    return (
        json.load(io.open(REPO_ROOT / "locales/ru.json", encoding="utf-8")),
        json.load(io.open(REPO_ROOT / "locales/en.json", encoding="utf-8")),
    )


def _campaigns(n=3):
    return [
        SimpleNamespace(
            ad_campaign_id=i,
            source=f"source-{i}",
            campaign_type="partner" if i % 2 else "ad",
            partner_percent=30.0,
            partner_user_id=100,
        )
        for i in range(n)
    ]


def _payouts(n=3):
    return [
        SimpleNamespace(payout_id=i, amount=100.0 + i, currency="RUB", created_at=None)
        for i in range(n)
    ]


def _rates(n=3):
    return [
        SimpleNamespace(currency=code, rate=rate)
        for code, rate in list({"RUB": 1.0, "XTR": 1.0, "USDT": 95.0}.items())[:n]
    ]


def _accruals(n=3):
    return [
        SimpleNamespace(accrual_id=i, amount=100.0 + i, currency="RUB", paid_at=None)
        for i in range(n)
    ]


def all_markups():
    i18n = FakeI18n()
    return [
        ak.get_ads_menu_keyboard(i18n, "ru"),
        ak.get_ads_list_keyboard(i18n, "ru", _campaigns(), 0, 3),
        ak.get_ad_campaign_type_keyboard(i18n, "ru"),
        ak.get_ad_card_keyboard(i18n, "ru", 7, 1, is_partner=True),
        ak.get_ad_card_keyboard(i18n, "ru", 8, 1, is_partner=False),
        ak.get_admin_partner_pick_keyboard(i18n, "ru", _campaigns(), 1, 3),
        ak.get_admin_payouts_keyboard(i18n, "ru", 7, 1, _payouts(), 1, 3),
        pk.get_partner_no_programs_keyboard(i18n, "ru", "https://t.me/support"),
        pk.get_partner_programs_keyboard(i18n, "ru", _campaigns()),
        pk.get_partner_card_keyboard(
            i18n, "ru", 7, show_back_to_list=True, support_link="https://t.me/s"
        ),
        pk.get_partner_payouts_keyboard(i18n, "ru", 7, _payouts(), 1, 3),
        pk.get_partner_purchases_keyboard(i18n, "ru", 7, _accruals(), 1, 3),
        pk.get_partner_detail_keyboard(i18n, "ru", "partner:payouts:7:0"),
        ak.get_currency_rates_keyboard(i18n, "ru", _rates()),
        ak.get_currency_rate_cancel_keyboard(i18n, "ru"),
        ak.get_system_functions_keyboard(i18n, "ru"),
    ]


def _handler_matchers():
    exact, prefixes = set(), set()
    for path in HANDLER_FILES:
        src = io.open(REPO_ROOT / path, encoding="utf-8").read()
        exact |= set(re.findall(r'F\.data\s*==\s*"([^"]+)"', src))
        prefixes |= set(re.findall(r'F\.data\.startswith\("([^"]+)"\)', src))
    # Shared navigation targets handled outside these two modules.
    exact |= {
        "admin_action:main",
        "main_action:back_to_main",
        "admin_section:system_functions",
        # Handled by other admin modules, listed here only as navigation targets.
        "admin_action:broadcast",
        "admin_action:sync_panel",
        "admin_action:queue_status",
        "admin_action:backup_now",
        "admin_action:check_nodes",
    }
    return exact, prefixes


def test_every_callback_has_a_handler():
    exact, prefixes = _handler_matchers()
    unmatched = set()
    for markup in all_markups():
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data
                if not data or data in UNHANDLED_BY_DESIGN:
                    continue
                if data in exact or any(data.startswith(p) for p in prefixes):
                    continue
                unmatched.add(data)
    assert unmatched == set()


def test_callback_payloads_fit_telegram_limit():
    oversized = [
        button.callback_data
        for markup in all_markups()
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and len(button.callback_data.encode()) > 64
    ]
    assert oversized == []


def test_button_labels_are_plain_text():
    """Button labels are not HTML, so tokens and tags must never reach them."""
    bad = [
        button.text
        for markup in all_markups()
        for row in markup.inline_keyboard
        for button in row
        if "::" in button.text or "<" in button.text
    ]
    assert bad == []


EMOJI_RE = re.compile("[\U0001f000-\U0001faff←-⇿⌀-➿️⬀-⯿]")


def test_button_labels_carry_no_unicode_emoji(locales):
    """Button icons come from `icon_custom_emoji_id`, never from the label text.

    A glyph inside the label would render next to the premium icon instead of
    replacing it, so every `*_button` string stays plain words.
    """
    offenders = {
        key: value
        for table in locales
        for key, value in table.items()
        if key.endswith("_button") and isinstance(value, str) and EMOJI_RE.search(value)
    }
    assert offenders == {}


def test_generated_button_titles_carry_no_unicode_emoji():
    """Same rule for labels the keyboards build themselves (campaign names, dates)."""
    offenders = [
        button.text
        for markup in all_markups()
        for row in markup.inline_keyboard
        for button in row
        if EMOJI_RE.search(button.text)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "markup, expected_first_rows",
    [
        (ak.get_ads_list_keyboard(FakeI18n(), "ru", _campaigns(), 0, 3), 3),
        (ak.get_admin_partner_pick_keyboard(FakeI18n(), "ru", _campaigns(), 0, 1), 3),
        (pk.get_partner_programs_keyboard(FakeI18n(), "ru", _campaigns()), 3),
        (pk.get_partner_payouts_keyboard(FakeI18n(), "ru", 7, _payouts(), 0, 1), 3),
        (pk.get_partner_purchases_keyboard(FakeI18n(), "ru", 7, _accruals(), 0, 1), 3),
        (ak.get_currency_rates_keyboard(FakeI18n(), "ru", _rates()), 3),
    ],
)
def test_list_entries_get_one_row_each(markup, expected_first_rows):
    for row in markup.inline_keyboard[:expected_first_rows]:
        assert len(row) == 1


def test_partner_card_hides_back_to_list_for_a_single_program():
    markup = pk.get_partner_card_keyboard(
        FakeI18n(), "ru", 7, show_back_to_list=False, support_link=None
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "partner_back_to_list_button" not in labels
    # No support link configured -> no payout request button.
    assert "partner_request_payout_button" not in labels


def test_no_programs_keyboard_without_support_link():
    markup = pk.get_partner_no_programs_keyboard(FakeI18n(), "ru", None)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["back_to_main_menu_button"]


def _locale_calls(path):
    """Yield (lineno, key, passed_kwargs) for every `_( "key", ...)` call."""
    tree = ast.parse(io.open(REPO_ROOT / path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_"):
            continue
        key = None
        if node.args and isinstance(node.args[0], ast.Constant):
            key = node.args[0].value
        else:
            for kw in node.keywords:
                if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                    key = kw.value.value
        if isinstance(key, str):
            passed = {kw.arg for kw in node.keywords if kw.arg and kw.arg != "key"}
            yield node.lineno, key, passed


def test_all_referenced_locale_keys_exist(locales):
    ru, en = locales
    missing = [
        (path, lineno, key)
        for path in LOCALE_CONSUMERS
        for lineno, key, _passed in _locale_calls(path)
        if key not in ru or key not in en
    ]
    assert missing == []


def test_locale_placeholders_match_the_call_sites(locales):
    problems = []
    for path in HANDLER_FILES:
        for lineno, key, passed in _locale_calls(path):
            for name, table in zip(("ru", "en"), locales):
                if key not in table:
                    continue
                expected = set(re.findall(r"\{([a-z_]+)[}:!]", table[key]))
                if expected - passed:
                    problems.append((path, lineno, key, name, "missing", sorted(expected - passed)))
                if passed - expected:
                    problems.append((path, lineno, key, name, "unused", sorted(passed - expected)))
    assert problems == []


def test_premium_emoji_tokens_resolve(locales):
    unknown = {
        (key, token)
        for table in locales
        for key, value in table.items()
        if isinstance(value, str)
        for token in TEXT_EMOJI_TOKEN_RE.findall(value)
        if token not in TEXT_EMOJI
    }
    assert unknown == set()


def test_alert_strings_stay_plain_text(locales):
    """`callback.answer(show_alert=True)` renders raw — no HTML, no tokens."""
    alert_keys = {
        "admin_ads_not_found",
        "admin_ads_no_partners",
        "admin_ads_payout_saved",
        "admin_ads_payout_not_found",
        "admin_ads_payout_deleted",
        "admin_ads_deleted_success",
        "admin_ads_partner_disabled",
        "admin_ads_invalid_percent",
        "partner_program_not_found",
        "partner_sale_not_found",
        "partner_payout_not_found",
        "admin_currency_rate_invalid_code",
    }
    for table in locales:
        for key in alert_keys:
            value = table[key]
            assert "::" not in value, key
            assert "<" not in value, key
