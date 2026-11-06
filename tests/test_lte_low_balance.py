"""The 5 GiB alert is durable, retried and rearmed only above the threshold."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services.lte_service import LteService
from config.settings import Settings
from db.dal import lte_dal
from db.dal.lte_dal import GIB
from db.models import LteAccount, User
from test_lte_service import flow as lte_flow_fixture

flow = lte_flow_fixture


@pytest.fixture
async def monitored(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
    flow.lte.i18n = SimpleNamespace(gettext=lambda lang, key, **kw: key + ":" + kw.get("gb", ""))
    await flow.lte.run_checks(flow.factory)
    return flow


async def consume(flow, gb):
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += gb * GIB
    await flow.lte.run_checks(flow.factory)


async def test_exactly_five_gb_warns_once_and_lower_usage_does_not_repeat(monitored):
    await consume(monitored, 5)
    await consume(monitored, 1)
    await monitored.lte.run_checks(monitored.factory)
    monitored.bot.send_message.assert_awaited_once()
    assert monitored.bot.send_message.await_args.args[1] == "lte_low_balance_message:5.00"
    markup = monitored.bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "lte:packages"
    assert "lte" in [s["uuid"] if isinstance(s, dict) else s for s in monitored.panel.user["activeInternalSquads"]]


async def test_low_flag_survives_service_restart(monitored):
    await consume(monitored, 6)
    restarted = LteService(monitored.settings, monitored.panel, monitored.bot, monitored.lte.i18n)
    await restarted.run_checks(monitored.factory)
    monitored.bot.send_message.assert_awaited_once()


async def test_topup_above_threshold_rearms_the_next_crossing(monitored):
    await consume(monitored, 5)
    async with monitored.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "topup:1")
        await session.commit()
    await monitored.lte.run_checks(monitored.factory)
    async with monitored.factory() as session:
        assert not (await session.get(LteAccount, 1)).low_notified
    await consume(monitored, 10)
    await monitored.lte.run_checks(monitored.factory)
    assert monitored.bot.send_message.await_count == 2
    assert all(call.args[1] == "lte_low_balance_message:5.00" for call in monitored.bot.send_message.await_args_list)


async def test_small_topup_still_below_threshold_does_not_spam(monitored):
    await consume(monitored, 9)
    async with monitored.factory() as session:
        await lte_dal.credit(session, 1, GIB, "topup:small")
        await session.commit()
    await monitored.lte.run_checks(monitored.factory)
    await consume(monitored, 1)
    monitored.bot.send_message.assert_awaited_once()


async def test_stale_counter_cannot_rearm_a_small_topup_that_stays_below_five(monitored):
    await consume(monitored, 6)  # Last observed balance: 4 GiB; warning delivered.
    # Two more GiB have actually been consumed but not observed by the bot yet.
    monitored.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 2 * GIB
    async with monitored.factory() as session:
        await lte_dal.credit(session, 1, 2 * GIB, "topup:stale")
        await session.commit()
    await monitored.lte.run_checks(monitored.factory)
    monitored.bot.send_message.assert_awaited_once()
    async with monitored.factory() as session:
        assert (await session.get(LteAccount, 1)).remaining_bytes == 4 * GIB


async def test_exhaustion_sends_only_depletion_not_both_notices(monitored):
    await consume(monitored, 10)
    await monitored.lte.run_checks(monitored.factory)
    monitored.bot.send_message.assert_awaited_once()
    assert monitored.bot.send_message.await_args.args[1].startswith("lte_depleted_message:")


async def test_depletion_after_low_warning_is_a_separate_once_only_notice(monitored):
    await consume(monitored, 5)
    await consume(monitored, 5)
    await monitored.lte.run_checks(monitored.factory)
    assert monitored.bot.send_message.await_count == 2
    assert monitored.bot.send_message.await_args_list[0].args[1].startswith("lte_low_balance_message:")
    assert monitored.bot.send_message.await_args_list[1].args[1].startswith("lte_depleted_message:")


async def test_failed_telegram_delivery_remains_pending_and_retries(monitored):
    monitored.bot.send_message.side_effect = RuntimeError("temporary Telegram outage")
    await consume(monitored, 5)
    async with monitored.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.low_notification_pending and not account.low_notified
    monitored.bot.send_message.side_effect = None
    await monitored.lte.run_checks(monitored.factory)
    await monitored.lte.run_checks(monitored.factory)
    assert monitored.bot.send_message.await_count == 2
    async with monitored.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.low_notified and not account.low_notification_pending


@pytest.mark.parametrize("state", ["banned", "expired", "disabled"])
async def test_unavailable_subscription_does_not_receive_low_warning(monitored, state):
    if state == "banned":
        async with monitored.factory() as session:
            user = await session.get(User, 1)
            user.is_banned = True
            await session.commit()
    elif state == "expired":
        monitored.panel.user["expireAt"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    else:
        monitored.panel.user["status"] = "DISABLED"
    await consume(monitored, 5)
    monitored.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("configured,expected", [(30, 60), (60, 60), (120, 120)])
def test_old_poll_intervals_are_normalized_to_at_least_one_minute(configured, expected):
    settings = Settings(_env_file=None, BOT_TOKEN="test", LTE_CHECK_INTERVAL_SECONDS=configured)
    assert settings.LTE_CHECK_INTERVAL_SECONDS == expected
