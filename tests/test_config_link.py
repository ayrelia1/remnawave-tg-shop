"""Subscription domains preserve panel-issued keys across connection flows."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bot.utils import config_link
from config.settings import Settings


PANEL_LINK = "https://panel.example.com/api/sub/short%2Fabc?client=happ&name=a%20b#setup"
CDN_LINK = "https://cdn.example.com/api/sub/short%2Fabc?client=happ&name=a%20b#setup"


def make_settings(domain=None, **overrides):
    fields = {
        "BOT_TOKEN": "test:token",
        "_env_file": None,
        "SUBSCRIPTION_DOMAIN": domain,
        "CRYPT4_ENABLED": False,
        "CRYPT4_REDIRECT_URL": None,
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.mark.parametrize("domain", [None, "", "   "])
async def test_unset_domain_preserves_the_panel_link(domain):
    settings = make_settings(domain)

    assert settings.SUBSCRIPTION_DOMAIN is None
    assert await config_link.prepare_config_links(settings, f"  {PANEL_LINK}  ") == (
        PANEL_LINK, PANEL_LINK,
    )


@pytest.mark.parametrize(
    "domain, expected",
    [
        ("https://cdn.example.com", "https://cdn.example.com"),
        (" https://cdn.example.com/ ", "https://cdn.example.com"),
        ("http://cdn.example.com:8080/", "http://cdn.example.com:8080"),
        ("https://[2001:db8::1]:8443/", "https://[2001:db8::1]:8443"),
    ],
)
async def test_domain_replaces_only_the_origin(domain, expected):
    settings = make_settings(domain, PANEL_API_URL="https://internal.example.com/api")
    expected_link = f"{expected}/api/sub/short%2Fabc?client=happ&name=a%20b#setup"

    assert settings.SUBSCRIPTION_DOMAIN == expected
    assert await config_link.prepare_config_links(settings, PANEL_LINK) == (
        expected_link, expected_link,
    )
    assert settings.PANEL_API_URL == "https://internal.example.com/api"


async def test_root_subscription_path_is_preserved():
    settings = make_settings("https://cdn.example.com/")

    assert await config_link.prepare_config_links(settings, "https://panel.example.com/KEY") == (
        "https://cdn.example.com/KEY", "https://cdn.example.com/KEY",
    )


@pytest.mark.parametrize("raw_link", [None, "", "   "])
async def test_domain_does_not_create_a_link_without_a_panel_key(raw_link):
    assert await config_link.prepare_config_links(
        make_settings("https://cdn.example.com"), raw_link,
    ) == (None, None)


@pytest.mark.parametrize(
    "raw_link", ["not-a-url", "/sub/KEY", "happ://crypt4/payload", "https://[invalid/sub/KEY"],
)
async def test_invalid_panel_url_is_not_turned_into_a_cdn_link(raw_link, caplog):
    assert await config_link.prepare_config_links(
        make_settings("https://cdn.example.com"), raw_link,
    ) == (None, None)
    assert raw_link not in caplog.text


async def test_crypt4_encrypts_the_rewritten_link_before_wrapping_the_button(monkeypatch):
    settings = make_settings(
        "https://cdn.example.com",
        CRYPT4_ENABLED=True,
        CRYPT4_REDIRECT_URL="https://redirect.example.com/?url=",
    )
    encrypt = AsyncMock(return_value="happ://crypt4/encrypted")
    monkeypatch.setattr(config_link, "_encrypt_raw_link", encrypt)

    assert await config_link.prepare_config_links(settings, PANEL_LINK) == (
        "happ://crypt4/encrypted",
        "https://redirect.example.com/?url=happ://crypt4/encrypted",
    )
    encrypt.assert_awaited_once_with(settings, CDN_LINK)


async def test_crypt4_failure_keeps_the_rewritten_link(monkeypatch):
    settings = make_settings("https://cdn.example.com", CRYPT4_ENABLED=True)
    encrypt = AsyncMock(return_value=None)
    monkeypatch.setattr(config_link, "_encrypt_raw_link", encrypt)

    assert await config_link.prepare_config_links(settings, PANEL_LINK) == (CDN_LINK, CDN_LINK)
    encrypt.assert_awaited_once_with(settings, CDN_LINK)


@pytest.mark.parametrize(
    "domain",
    [
        "cdn.example.com",
        "//cdn.example.com",
        "ftp://cdn.example.com",
        "https://",
        "https://cdn.example.com/sub",
        "https://cdn.example.com/?client=happ",
        "https://cdn.example.com/#setup",
        "https://user:password@cdn.example.com",
        "https://cdn.example.com:99999",
        "https://[invalid",
    ],
)
def test_domain_rejects_values_that_are_not_http_origins(domain):
    with pytest.raises(ValidationError):
        make_settings(domain)


def test_domain_is_loaded_from_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('BOT_TOKEN=test:token\nSUBSCRIPTION_DOMAIN="https://cdn.example.com/"\n')

    settings = Settings(_env_file=env_file)

    assert settings.SUBSCRIPTION_DOMAIN == "https://cdn.example.com"
