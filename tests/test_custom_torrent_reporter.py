import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.handlers.admin.custom_torrent import torrent_settings
from bot.middlewares.i18n import JsonI18n
from bot.services.custom_torrent_reporter import CustomTorrentReporter, torrent_webhook_route
from bot.services.panel_api_service import PanelUnavailableError
from config.settings import Settings
from db.models import Base, Subscription, TorrentReport, TorrentReportSettings, User


pytestmark = pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
CHAT = -1001234567890


def payload(batch="sample", *, user="42", level="medium", page=1, pages=1):
    return {
        "batch_id": batch, "auth": "test-secret", "node_uuid": "node",
        "period_start": 1793920200, "period_end": 1793920800,
        "page": page, "total_pages": pages, "total_alerts": 1,
        "alerts": [{
            "inbound_tag": "tls<&", "node_name": "Litva 👩‍💻", "window_start": 1793919600,
            "window_end": 1793920200, "created_at": 1793920260, "user_id": user,
            "client_ip": "203.0.113.10", "level": level, "details": {"score": 10},
        }],
    }


@pytest.fixture
async def reporter():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(User(user_id=100, language_code="ru"))
        session.add_all([
            Subscription(user_id=100, panel_user_uuid=ref, panel_subscription_uuid=f"link-{ref}",
                         end_date=NOW + timedelta(days=30), status_from_panel="ACTIVE", is_active=True)
            for ref in ("42", "43")
        ])
        session.add(TorrentReportSettings(id=1, auto_response_enabled=True, minimum_level="medium"))
    settings = Settings(_env_file=None, BOT_TOKEN="test", CUSTOM_TORRENT_REPORTER_ENABLED=True,
                        CUSTOM_TORRENT_REPORTER_SECRET="test-secret",
                        CUSTOM_TORRENT_REPORTER_THREAD_ID=123, LOG_CHAT_ID=CHAT)
    panel = AsyncMock()
    panel.get_user_by_ref.return_value = {"id": 42, "telegramId": 100, "status": "ACTIVE",
                                         "expireAt": (NOW + timedelta(days=30)).isoformat()}
    bot = AsyncMock()
    service = CustomTorrentReporter(settings, panel, bot, JsonI18n("locales", default="ru"))
    app = web.Application()
    app["settings"] = settings
    app["async_session_factory"] = factory
    app.router.add_post("/webhook/torrent", torrent_webhook_route)
    async with TestClient(TestServer(app)) as client:
        yield client, factory, service, panel, bot
    await engine.dispose()


async def reports(factory):
    async with factory() as session:
        return list(await session.scalars(select(TorrentReport).order_by(TorrentReport.id)))


async def submit(client, factory, data, *, at=NOW, key=None):
    response = await client.post("/webhook/torrent", json=data,
                                 headers={"Idempotency-Key": key} if key else {})
    assert response.status == 200
    assert (await response.json())["status"] == "accepted"
    async with factory.begin() as session:
        rows = await session.scalars(select(TorrentReport).where(
            TorrentReport.batch_id == data["batch_id"], TorrentReport.page == data["page"]))
        for row in rows:
            row.received_at = at


async def test_page_auth_validation_and_deduplication(reporter):
    client, factory, service, panel, bot = reporter
    data = payload(page=2, pages=2)
    data["alerts"].append(copy.deepcopy(data["alerts"][0]))
    data["total_alerts"] = 2
    assert (await client.post("/webhook/torrent", json={**data, "auth": "wrong"})).status == 403
    assert (await client.post("/webhook/torrent", json=data,
                              headers={"Idempotency-Key": "sample"})).status == 400
    invalid = copy.deepcopy(data)
    invalid["alerts"][1]["level"] = "critical"
    assert (await client.post("/webhook/torrent", json=invalid)).status == 400
    assert await reports(factory) == []
    await submit(client, factory, data, key="sample:2")
    assert not bot.send_message.called
    data["alerts"].append(copy.deepcopy(data["alerts"][0]))
    data["total_alerts"] = 3
    response = await client.post("/webhook/torrent", json=data)
    assert (await response.json())["status"] == "duplicate"
    rows = await reports(factory)
    assert [row.idempotency_key for row in rows] == ["sample:2", "sample:2"]
    assert [row.alert_index for row in rows] == [0, 1]
    await service.run_checks(factory, now=NOW)
    rows = await reports(factory)
    assert [row.warning_number for row in rows] == [1, None]
    assert rows[1].decision == "cooldown"
    assert sum(call.args[0] == CHAT for call in bot.send_message.await_args_list) == 2
    assert any("60 мин" in call.args[1] for call in bot.send_message.await_args_list)
    panel.get_user_by_ref.assert_not_awaited()


async def test_threshold_unknown_user_and_auto_off(reporter):
    client, factory, service, panel, bot = reporter
    await submit(client, factory, payload("low", level="low"))
    await submit(client, factory, payload("unknown", user="99", level="highest"))
    await service.run_checks(factory, now=NOW)
    async with factory.begin() as session:
        (await session.get(TorrentReportSettings, 1)).auto_response_enabled = False
    await submit(client, factory, payload("off", level="highest"))
    await service.run_checks(factory, now=NOW)
    assert [row.decision for row in await reports(factory)] == ["below_threshold", "user_not_found", "auto_off"]
    assert [call.args[0] for call in bot.send_message.await_args_list] == [CHAT] * 3
    text = bot.send_message.await_args_list[1].args[1]
    assert "не найдена" in text and "203.0.113.10" in text and "tls&lt;&amp;" in text
    panel.get_user_by_ref.assert_not_awaited()


async def test_fixed_window_cooldown_and_scoped_disable(reporter):
    client, factory, service, panel, bot = reporter
    for index, offset in enumerate((timedelta(), timedelta(days=4), timedelta(days=5),
                                    timedelta(days=5, hours=1), timedelta(days=5, hours=2),
                                    timedelta(days=5, hours=3))):
        at = NOW + offset
        await submit(client, factory, payload(str(index), level="highest"), at=at)
        await service.run_checks(factory, now=at)
    rows = await reports(factory)
    assert [row.warning_number for row in rows] == [1, 2, 1, 2, 3, None]
    assert rows[-1].decision == "window_limit"
    panel.update_user_status_on_panel.assert_awaited_once_with("42", False, log_response=False)
    panel.revoke_user_subscription.assert_not_awaited()
    async with factory() as session:
        subs = list(await session.scalars(select(Subscription).order_by(Subscription.panel_user_uuid)))
        assert [(s.is_active, s.status_from_panel) for s in subs] == [(False, "DISABLED"), (True, "ACTIVE")]
        assert not (await session.get(User, 100)).is_banned
    assert [call.args[0] for call in bot.send_message.await_args_list].count(100) == 5


async def test_panel_outage_resumes_same_warning_without_second_admin_alert(reporter):
    client, factory, service, panel, bot = reporter
    for index in range(2):
        at = NOW + timedelta(hours=index)
        await submit(client, factory, payload(str(index)), at=at)
        await service.run_checks(factory, now=at)
    at = NOW + timedelta(hours=2)
    await submit(client, factory, payload("third"), at=at)
    panel.get_user_by_ref.side_effect = PanelUnavailableError("test outage")
    await service.run_checks(factory, now=at)
    row = (await reports(factory))[-1]
    assert (row.warning_number, row.processing_stage) == (3, "admin_sent")
    assert "ожидает" in bot.send_message.await_args_list[-1].args[1]
    panel.get_user_by_ref.side_effect = None
    await service.run_checks(factory, now=at + timedelta(seconds=30))
    assert (await reports(factory))[-1].processing_stage == "done"
    assert [call.args[0] for call in bot.send_message.await_args_list].count(CHAT) == 3
    panel.update_user_status_on_panel.assert_awaited_once()


async def test_user_notice_retry_does_not_disable_a_renewed_subscription(reporter):
    client, factory, service, panel, bot = reporter
    for index in range(2):
        at = NOW + timedelta(hours=index)
        await submit(client, factory, payload(str(index)), at=at)
        await service.run_checks(factory, now=at)
    at = NOW + timedelta(hours=2)
    await submit(client, factory, payload("third"), at=at)

    async def send(chat_id, text, **kwargs):
        if chat_id != CHAT:
            raise RuntimeError("Telegram unavailable")

    bot.send_message.side_effect = send
    await service.run_checks(factory, now=at)
    row = (await reports(factory))[-1]
    assert (row.decision, row.processing_stage) == ("disabled", "admin_sent")
    async with factory.begin() as session:
        sub = await session.get(Subscription, row.subscription_id)
        sub.is_active = True
        sub.status_from_panel = "ACTIVE"
    bot.send_message.reset_mock(side_effect=True)
    await service.run_checks(factory, now=at + timedelta(seconds=30))
    panel.update_user_status_on_panel.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert (await reports(factory))[-1].processing_stage == "done"


async def test_admin_topic_failure_does_not_block_or_repeat_personal_notices(reporter):
    client, factory, service, panel, bot = reporter

    async def send(chat_id, text, **kwargs):
        if chat_id == CHAT:
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text="test"),
                                     message="message thread not found")

    bot.send_message.side_effect = send
    for index in range(3):
        at = NOW + timedelta(hours=index)
        await submit(client, factory, payload(f"topic-{index}"), at=at)
        await service.run_checks(factory, now=at)

    notices = [call.args[1] for call in bot.send_message.await_args_list if call.args[0] == 100]
    assert len(notices) == 3
    assert all(f"{number}/3" in notice for number, notice in enumerate(notices, 1))
    panel.update_user_status_on_panel.assert_awaited_once()
    bot.send_message.reset_mock(side_effect=True)
    await service.run_checks(factory, now=at + timedelta(seconds=30))
    assert [call.args[0] for call in bot.send_message.await_args_list] == [CHAT] * 3
    assert all(row.processing_stage == "done" for row in await reports(factory))


async def test_pending_user_warnings_delay_disable_without_hiding_admin_alerts(reporter):
    client, factory, service, panel, bot = reporter

    async def send(chat_id, text, **kwargs):
        if chat_id == 100:
            raise RuntimeError("Personal delivery temporarily unavailable")

    bot.send_message.side_effect = send
    for index in range(3):
        at = NOW + timedelta(hours=index)
        await submit(client, factory, payload(f"user-notice-{index}"), at=at)
        await service.run_checks(factory, now=at)

    panel.update_user_status_on_panel.assert_not_awaited()
    assert [call.args[0] for call in bot.send_message.await_args_list].count(CHAT) == 3
    bot.send_message.reset_mock(side_effect=True)
    await service.run_checks(factory, now=at + timedelta(seconds=30))
    assert [call.args[0] for call in bot.send_message.await_args_list] == [100] * 3
    assert all(f"{number}/3" in call.args[1] for number, call in enumerate(bot.send_message.await_args_list, 1))
    panel.update_user_status_on_panel.assert_awaited_once()


@pytest.mark.parametrize("new_policy, retry_at, result", [
    ({"auto_response_enabled": False}, timedelta(hours=3), "auto_off"),
    ({"minimum_level": "highest"}, timedelta(hours=3), "below_threshold"),
    ({}, timedelta(days=10), "window_expired"),
])
async def test_pending_disable_rechecks_policy_and_age(reporter, new_policy, retry_at, result):
    client, factory, service, panel, bot = reporter
    for index in range(3):
        at = NOW + timedelta(hours=index)
        if index == 2:
            panel.get_user_by_ref.side_effect = PanelUnavailableError("test outage")
        await submit(client, factory, payload(f"old-{index}"), at=at)
        await service.run_checks(factory, now=at)
    assert (await reports(factory))[-1].decision == "disable_pending"

    async with factory.begin() as session:
        policy = await session.get(TorrentReportSettings, 1)
        for key, value in new_policy.items():
            setattr(policy, key, value)
    panel.get_user_by_ref.side_effect = None
    await service.run_checks(factory, now=NOW + retry_at)
    panel.update_user_status_on_panel.assert_not_awaited()
    row = (await reports(factory))[-1]
    assert (row.decision, row.warning_number, row.processing_stage) == (result, None, "done")


async def test_switch_off_applies_to_remaining_reports_in_same_pass(reporter):
    client, factory, service, panel, bot = reporter
    await submit(client, factory, payload("before-switch"))
    await submit(client, factory, payload("after-switch", user="43"))

    async def send(chat_id, text, **kwargs):
        if chat_id == CHAT:
            async with factory.begin() as session:
                (await session.get(TorrentReportSettings, 1)).auto_response_enabled = False

    bot.send_message.side_effect = send
    await service.run_checks(factory, now=NOW)
    assert [row.warning_number for row in await reports(factory)] == [1, None]
    assert (await reports(factory))[-1].decision == "auto_off"


async def test_switch_off_during_panel_lookup_cancels_disable(reporter):
    client, factory, service, panel, _ = reporter
    for index in range(2):
        at = NOW + timedelta(hours=index)
        await submit(client, factory, payload(f"warn-{index}"), at=at)
        await service.run_checks(factory, now=at)

    async def lookup(*args, **kwargs):
        async with factory.begin() as session:
            (await session.get(TorrentReportSettings, 1)).auto_response_enabled = False
        return panel.get_user_by_ref.return_value

    panel.get_user_by_ref.side_effect = lookup
    at = NOW + timedelta(hours=2)
    await submit(client, factory, payload("switch-during-lookup"), at=at)
    await service.run_checks(factory, now=at)
    panel.update_user_status_on_panel.assert_not_awaited()
    assert (await reports(factory))[-1].decision == "auto_off"


async def test_panel_retries_do_not_starve_fresh_alerts(reporter):
    client, factory, service, panel, bot = reporter
    async with factory.begin() as session:
        session.add_all([User(user_id=1000 + index) for index in range(20)])
        session.add_all([
            Subscription(user_id=1000 + index, panel_user_uuid=str(1000 + index),
                         end_date=NOW + timedelta(days=30), is_active=True)
            for index in range(20)
        ])
    panel.get_user_by_ref.side_effect = PanelUnavailableError("test outage")
    for index in range(3):
        data = payload(f"batch-{index}")
        data["alerts"] = [{**data["alerts"][0], "user_id": str(1000 + user)} for user in range(20)]
        data["total_alerts"] = 20
        at = NOW + timedelta(hours=index)
        await submit(client, factory, data, at=at)
        await service.run_checks(factory, now=at)

    assert panel.get_user_by_ref.await_count == 20
    await submit(client, factory, payload("fresh", user="9999", level="low"), at=at)
    await service.run_checks(factory, now=at + timedelta(seconds=5))
    assert (await reports(factory))[-1].processing_stage == "done"
    assert panel.get_user_by_ref.await_count == 20
    await service.run_checks(factory, now=at + timedelta(seconds=30))
    assert panel.get_user_by_ref.await_count == 40


async def test_worker_moves_past_a_retry_backlog_larger_than_the_delay_window(reporter):
    client, factory, service, _, _ = reporter
    data = payload("backlog")
    data["alerts"] *= 140
    data["total_alerts"] = 140
    await submit(client, factory, data)
    await submit(client, factory, payload("after-backlog"))
    last_id = (await reports(factory))[-1].id

    service._process = AsyncMock(return_value=False)
    for tick in range(8):
        await service.run_checks(factory, now=NOW + timedelta(seconds=5 * tick))
    assert last_id in {call.args[1] for call in service._process.await_args_list}


async def test_webhook_disabled_or_unconfigured_does_not_store_alerts(reporter):
    client, factory, service, _, _ = reporter
    service.settings.CUSTOM_TORRENT_REPORTER_ENABLED = False
    assert (await client.post("/webhook/torrent", json=payload())).status == 503
    service.settings.CUSTOM_TORRENT_REPORTER_ENABLED = True
    service.settings.CUSTOM_TORRENT_REPORTER_THREAD_ID = None
    assert (await client.post("/webhook/torrent", json=payload())).status == 503
    assert await reports(factory) == []


async def test_admin_controls_persist_policy(reporter):
    _, factory, service, _, _ = reporter
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    i18n_data = {"current_language": "ru", "i18n_instance": service.i18n}

    async def click(action):
        callback.data = f"admin_torrent:{action}"
        async with factory() as session:
            await torrent_settings(callback, service.settings, i18n_data, session)

    await click("level:highest")
    async with factory() as session:
        assert (await session.get(TorrentReportSettings, 1)).minimum_level == "highest"
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert [button.text for row in markup.inline_keyboard for button in row][1:5] == [
        "low", "medium", "high", "highest",
    ]
    assert all(button.icon_custom_emoji_id for row in markup.inline_keyboard for button in row)
    await click("disable")
    await click("level:low")
    async with factory() as session:
        policy = await session.get(TorrentReportSettings, 1)
        assert (policy.auto_response_enabled, policy.minimum_level) == (False, "highest")
    await click("enable")
    async with factory() as session:
        assert (await session.get(TorrentReportSettings, 1)).auto_response_enabled
