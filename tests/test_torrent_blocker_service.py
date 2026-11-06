"""Torrent reports notify the local owner without changing subscription access."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.constants.premium_emoji import PREMIUM_EMOJI_SUPPORT
from bot.middlewares.i18n import JsonI18n
from bot.services.panel_api_service import PanelUnavailableError
from bot.services.panel_webhook_service import PanelWebhookService, panel_webhook_route
from bot.services.torrent_blocker_service import NOTIFICATION_LOG_EVENT, TorrentBlockerService, TorrentReport
from config.settings import Settings
from db.models import Base, MessageLog, Subscription, User

pytestmark = pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")

NODE = "00000000-0000-4000-8000-000000000001"
OTHER_NODE = "00000000-0000-4000-8000-000000000002"
PROFILE = "00000000-0000-4000-8000-000000000003"
INBOUND = "00000000-0000-4000-8000-000000000004"
OTHER_INBOUND = "00000000-0000-4000-8000-000000000005"


def report():
    return {
        "event": "torrent_blocker.report", "scope": "torrent_blocker",
        "timestamp": "2026-11-07T12:00:01Z",
        "data": {
            "user": {"id": 42, "telegramId": 123},
            "node": {
                "uuid": NODE, "name": "internal-de-node",
                "configProfile": {
                    "activeConfigProfileUuid": PROFILE,
                    "activeInbounds": [{"uuid": INBOUND, "profileUuid": PROFILE, "tag": "VLESS"}],
                },
            },
            "report": {
                "actionReport": {
                    "blocked": True, "ip": "203.0.113.7", "blockDuration": 300,
                    "processedAt": "2026-11-07T12:00:00Z",
                    "willUnblockAt": "2026-11-07T12:05:00Z", "userId": "42",
                },
                "xrayReport": {"inboundTag": "VLESS"},
            },
        },
    }


def host(remark="Germany", **changes):
    value = {
        "uuid": "00000000-0000-4000-8000-000000000006", "remark": remark,
        "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": INBOUND},
        "nodes": [], "isDisabled": False,
    }
    value.update(changes)
    return value


@pytest.fixture
async def setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = SimpleNamespace(
        PANEL_WEBHOOK_SECRET="test-panel-secret", TORRENT_BLOCKER_NOTIFICATIONS_ENABLED=True,
        SUPPORT_LINK="https://t.me/test_support", DEFAULT_LANGUAGE="ru",
        SUBSCRIPTION_NOTIFICATIONS_ENABLED=False,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    panel = SimpleNamespace(get_hosts=AsyncMock(return_value=[host()]))
    i18n = JsonI18n("locales", default="ru")
    service = TorrentBlockerService(bot, settings, i18n, factory, panel)
    webhook = PanelWebhookService(
        bot, settings, i18n, factory, panel, SimpleNamespace(), torrent_blocker_service=service,
    )
    async with factory() as session:
        session.add(User(user_id=123, language_code="ru", panel_user_uuid="42"))
        await session.flush()
        session.add(Subscription(
            user_id=123, panel_user_uuid="42", panel_subscription_uuid="key-42",
            end_date=datetime.now(timezone.utc) + timedelta(days=30), is_active=True,
        ))
        await session.commit()
    try:
        yield SimpleNamespace(factory=factory, settings=settings, bot=bot, panel=panel,
                              service=service, webhook=webhook)
    finally:
        await engine.dispose()


@pytest.fixture
async def client(setup):
    app = web.Application()
    app["panel_webhook_service"] = setup.webhook
    app.router.add_post("/webhook/panel", panel_webhook_route)
    async with TestClient(TestServer(app)) as client:
        yield client


async def post(client, setup, value=None, *, raw=None, signature=None):
    raw = raw if raw is not None else json.dumps(value if value is not None else report()).encode()
    signature = signature if signature is not None else hmac.new(
        setup.settings.PANEL_WEBHOOK_SECRET.encode(), raw, hashlib.sha256,
    ).hexdigest()
    return await client.post("/webhook/panel", data=raw, headers={"X-Remnawave-Signature": signature})


async def logs(setup):
    async with setup.factory() as session:
        return list((await session.scalars(select(MessageLog))).all())


async def test_signed_report_sends_host_ip_seconds_and_support(setup, client):
    response = await post(client, setup)
    assert response.status == 200
    args, kwargs = setup.bot.send_message.call_args
    assert args[0] == 123
    assert "Germany" in args[1] and "internal-de-node" not in args[1]
    assert "203.0.113.7" in args[1] and "300" in args[1] and "секунд" in args[1]
    assert "торрент" in args[1] and "запрещен" in args[1]
    assert "<tg-emoji" in args[1] and "::" not in args[1]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Поддержка" and button.url == setup.settings.SUPPORT_LINK
    assert button.icon_custom_emoji_id == PREMIUM_EMOJI_SUPPORT
    saved = await logs(setup)
    assert len(saved) == 1 and saved[0].user_id == 123
    assert "203.0.113.7" not in (saved[0].content or "") + (saved[0].raw_update_preview or "")
    async with setup.factory() as session:
        sub = await session.scalar(select(Subscription))
        assert sub.is_active and sub.panel_subscription_uuid == "key-42"
        assert not (await session.get(User, 123)).is_banned


async def test_zero_duration_describes_indefinite_block(setup, client):
    value = report()
    value["data"]["report"]["actionReport"]["blockDuration"] = 0
    assert (await post(client, setup, value)).status == 200
    text = setup.bot.send_message.call_args.args[1]
    assert "без автоматической разблокировки" in text and "0 секунд" not in text


def test_notification_flag_defaults_off_and_accepts_env_boolean(monkeypatch):
    monkeypatch.delenv("TORRENT_BLOCKER_NOTIFICATIONS_ENABLED", raising=False)
    assert not Settings(_env_file=None, BOT_TOKEN="test").TORRENT_BLOCKER_NOTIFICATIONS_ENABLED
    monkeypatch.setenv("TORRENT_BLOCKER_NOTIFICATIONS_ENABLED", "true")
    assert Settings(_env_file=None, BOT_TOKEN="test").TORRENT_BLOCKER_NOTIFICATIONS_ENABLED
    monkeypatch.setenv("TORRENT_BLOCKER_NOTIFICATIONS_ENABLED", "false")
    assert not Settings(_env_file=None, BOT_TOKEN="test").TORRENT_BLOCKER_NOTIFICATIONS_ENABLED


async def test_duplicate_survives_service_restart_and_changed_envelope(setup, client):
    assert (await post(client, setup)).status == 200
    setup.webhook.torrent_blocker_service = TorrentBlockerService(
        setup.bot, setup.settings, setup.service.i18n, setup.factory, setup.panel,
    )
    value = report()
    value["timestamp"] = "2026-11-07T12:00:05Z"
    value["data"]["report"]["actionReport"]["processedAt"] = "2026-11-07T15:00:00+03:00"
    value["data"]["node"]["name"] = "renamed-node"
    assert (await post(client, setup, value)).status == 200
    assert setup.bot.send_message.await_count == 1
    assert setup.panel.get_hosts.await_count == 1
    assert len(await logs(setup)) == 1


@pytest.mark.parametrize("change", ["time", "node", "ip"])
async def test_distinct_block_is_not_suppressed(setup, client, change):
    await post(client, setup)
    value = report()
    if change == "time":
        value["data"]["report"]["actionReport"]["processedAt"] = "2026-11-07T12:06:00Z"
    elif change == "node":
        value["data"]["node"]["uuid"] = OTHER_NODE
    else:
        value["data"]["report"]["actionReport"]["ip"] = "2001:db8::1"
    assert (await post(client, setup, value)).status == 200
    assert setup.bot.send_message.await_count == 2


async def test_disabled_flag_does_not_read_catalog_or_notify(setup, client):
    setup.settings.TORRENT_BLOCKER_NOTIFICATIONS_ENABLED = False
    assert (await post(client, setup)).status == 200
    setup.bot.send_message.assert_not_awaited()
    setup.panel.get_hosts.assert_not_awaited()
    assert not await logs(setup)


async def test_detection_without_block_does_not_notify(setup, client):
    value = report()
    value["data"]["report"]["actionReport"]["blocked"] = False
    assert (await post(client, setup, value)).status == 200
    setup.bot.send_message.assert_not_awaited()
    setup.panel.get_hosts.assert_not_awaited()


@pytest.mark.parametrize("case", ["unknown", "telegram_conflict", "ambiguous_owner"])
async def test_owner_is_resolved_from_local_panel_reference(setup, client, case):
    value = report()
    if case == "unknown":
        value["data"]["user"]["id"] = 999
    elif case == "telegram_conflict":
        value["data"]["user"]["telegramId"] = 999
    else:
        async with setup.factory() as session:
            session.add(User(user_id=124))
            await session.flush()
            session.add(Subscription(user_id=124, panel_user_uuid="42", end_date=datetime.now(timezone.utc)))
            await session.commit()
    assert (await post(client, setup, value)).status == 200
    setup.bot.send_message.assert_not_awaited()
    setup.panel.get_hosts.assert_not_awaited()


async def test_rotated_link_and_history_do_not_break_owner_lookup(setup, client):
    async with setup.factory() as session:
        session.add(Subscription(user_id=123, panel_user_uuid="42", panel_subscription_uuid="old-key",
                                 end_date=datetime.now(timezone.utc) - timedelta(days=2), is_active=False))
        await session.commit()
    value = report()
    value["data"]["user"].pop("telegramId")
    assert (await post(client, setup, value)).status == 200
    assert setup.bot.send_message.await_count == 1


async def test_owner_is_rechecked_after_catalog_read(setup, client):
    async def reassign():
        async with setup.factory() as session:
            session.add(User(user_id=124))
            sub = await session.scalar(select(Subscription))
            sub.user_id = 124
            await session.commit()
        return [host()]
    setup.panel.get_hosts.side_effect = reassign
    assert (await post(client, setup)).status == 200
    setup.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("case", ["ambiguous", "missing_tag", "missing_profile", "no_match", "blank_name"])
async def test_unresolvable_host_uses_node_name(setup, client, case):
    value = report()
    if case == "ambiguous":
        setup.panel.get_hosts.return_value = [host("Host A"), host("Host B")]
    elif case == "missing_tag":
        value["data"]["report"]["xrayReport"]["inboundTag"] = None
    elif case == "missing_profile":
        value["data"]["node"].pop("configProfile")
    elif case == "blank_name":
        setup.panel.get_hosts.return_value = [host(" ")]
    else:
        setup.panel.get_hosts.return_value = [host(nodes=[OTHER_NODE])]
    assert (await post(client, setup, value)).status == 200
    text = setup.bot.send_message.call_args.args[1]
    assert "internal-de-node" in text and "Host A" not in text and "Host B" not in text


async def test_matches_profile_inbound_and_node_instead_of_first_host(setup, client):
    setup.panel.get_hosts.return_value = [
        host("Other node", nodes=[OTHER_NODE]),
        host("Other inbound", inbound={"configProfileUuid": PROFILE, "configProfileInboundUuid": OTHER_INBOUND}),
        host("Other profile", inbound={"configProfileUuid": OTHER_NODE, "configProfileInboundUuid": INBOUND}),
        host("Correct host", nodes=[NODE]),
    ]
    assert (await post(client, setup)).status == 200
    text = setup.bot.send_message.call_args.args[1]
    assert "Correct host" in text and "Other" not in text


@pytest.mark.parametrize("fallback", [False, True])
async def test_panel_names_are_html_escaped(setup, client, fallback):
    value = report()
    value["data"]["node"]["name"] = '<node & "name">'
    setup.panel.get_hosts.return_value = [] if fallback else [host('<host & "name">')]
    assert (await post(client, setup, value)).status == 200
    text = setup.bot.send_message.call_args.args[1]
    assert "&lt;" in text and "&amp;" in text
    assert "<host" not in text and "<node" not in text


async def test_long_host_remark_cannot_exceed_telegram_message_limit(setup, client):
    setup.panel.get_hosts.return_value = [host('"' * 5000)]
    assert (await post(client, setup)).status == 200
    assert len(setup.bot.send_message.call_args.args[1]) < 4096


@pytest.mark.parametrize("target", ["panel", "telegram"])
async def test_transient_failure_is_retryable_without_delivery_marker(setup, client, target):
    if target == "panel":
        setup.panel.get_hosts.side_effect = PanelUnavailableError("test outage")
    else:
        setup.bot.send_message.side_effect = RuntimeError("test outage")
    assert (await post(client, setup)).status == 503
    assert not await logs(setup)
    setup.panel.get_hosts.side_effect = None
    setup.bot.send_message.side_effect = None
    assert (await post(client, setup)).status == 200
    assert len(await logs(setup)) == 1


async def test_missing_catalog_is_retryable_instead_of_sending_wrong_name(setup, client):
    setup.panel.get_hosts.return_value = None
    assert (await post(client, setup)).status == 503
    setup.bot.send_message.assert_not_awaited()
    assert not await logs(setup)


async def test_second_owner_check_suppresses_concurrent_delivery(setup, client):
    async def another_delivery_finishes():
        event = report()
        marker = TorrentReport.model_validate(event["data"]).notification_marker()
        async with setup.factory() as session:
            session.add(MessageLog(user_id=123, event_type=NOTIFICATION_LOG_EVENT, raw_update_preview=marker))
            await session.commit()
        return [host()]
    setup.panel.get_hosts.side_effect = another_delivery_finishes
    assert (await post(client, setup)).status == 200
    setup.bot.send_message.assert_not_awaited()
    assert len(await logs(setup)) == 1


async def test_forbidden_recipient_is_not_retried_forever(setup, client):
    setup.bot.send_message.side_effect = TelegramForbiddenError(
        method=SendMessage(chat_id=123, text="test"), message="bot was blocked by the user",
    )
    assert (await post(client, setup)).status == 200
    assert (await post(client, setup)).status == 200
    assert setup.bot.send_message.await_count == 1
    assert len(await logs(setup)) == 1


async def test_english_without_support_link(setup, client):
    async with setup.factory() as session:
        (await session.get(User, 123)).language_code = "en"
        await session.commit()
    setup.settings.SUPPORT_LINK = None
    assert (await post(client, setup)).status == 200
    assert "seconds" in setup.bot.send_message.call_args.args[1]
    assert setup.bot.send_message.call_args.kwargs["reply_markup"] is None


@pytest.mark.parametrize("field,value", [
    ("ip", "not-an-ip"), ("blockDuration", True), ("blockDuration", 1.5),
    ("blockDuration", -1), ("blocked", "true"),
    ("processedAt", "yesterday"),
])
async def test_invalid_action_does_not_send(setup, client, field, value):
    payload = report()
    payload["data"]["report"]["actionReport"][field] = value
    assert (await post(client, setup, payload)).status == 400
    setup.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("value", [True, -1, "42"])
async def test_invalid_panel_id_does_not_use_telegram_fallback(setup, client, value):
    payload = report()
    payload["data"]["user"]["id"] = value
    assert (await post(client, setup, payload)).status == 400
    setup.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("signature", ["invalid", "0" * 64])
async def test_invalid_signature_never_reaches_torrent_processor(setup, client, signature):
    assert (await post(client, setup, signature=signature)).status == 403
    setup.panel.get_hosts.assert_not_awaited()
    setup.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("raw", [b"[]", b"null", b"42", b"{broken"])
async def test_invalid_json_envelope_returns_bad_request(setup, client, raw):
    assert (await post(client, setup, raw=raw)).status == 400
    setup.bot.send_message.assert_not_awaited()
