"""Billing and network-access regressions without contacting a real panel."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.services.lte_service import LteService
from bot.services.subscription_service import SubscriptionService
from db.dal import lte_dal, payment_dal
from db.models import Base, User, LteAccount, Subscription, CurrencyRate, Payment

GIB = 2**30


class FakePanel:
    def __init__(self):
        self.user = dict(id=42, shortUuid="short", telegramId=1, username="tg_1", status="ACTIVE",
                         expireAt=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
                         activeInternalSquads=[{"uuid": "foreign"}, {"uuid": "lte"}],
                         subscriptionUrl="https://example.test/sub/short", trafficLimitBytes=0,
                         userTraffic={"lifetimeUsedTrafficBytes": 10000})
        self.updates = []
        self.reject_patch = False

    async def get_user_by_ref(self, ref):
        return deepcopy(self.user)

    async def get_all_panel_users(self):
        return [deepcopy(self.user)]

    async def update_user_details_on_panel(self, ref, payload):
        self.updates.append(deepcopy(payload))
        if self.reject_patch:
            return {"error": True}
        self.user.update(deepcopy(payload))
        return deepcopy(self.user)


@pytest.fixture
async def flow():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(
        LTE_ENFORCEMENT_ENABLED=True, parsed_lte_squad_uuids=["lte"],
        parsed_user_squad_uuids=["foreign", "lte"], parsed_user_external_squad_uuid=None,
        USER_HWID_DEVICE_LIMIT=2, USER_TRAFFIC_STRATEGY="NO_RESET", user_traffic_limit_bytes=0,
        DEFAULT_LANGUAGE="ru", yookassa_autopayments_active=False,
        STARS_ENABLED=True, SUBSCRIPTION_DOMAIN=None, CRYPT4_ENABLED=False, CRYPT4_REDIRECT_URL=None,
        TARIFF_SWITCH_UPGRADE_ENABLED=True, TARIFF_SWITCH_DOWNGRADE_ENABLED=True,
        device_plans_active=True, base_device_limit=2, device_tiers=[2, 5, 10],
        device_subscription_options={2: {3: 300}, 5: {3: 500}, 10: {3: 1000}},
        device_stars_options={2: {3: 300}, 5: {3: 500}, 10: {3: 1000}},
        device_plan_lte_bytes={months: 50 * months * GIB for months in (1, 3, 6, 12)},
        traffic_packages={25: 100}, stars_traffic_packages={25: 100},
        TRIAL_ENABLED=True, TRIAL_DURATION_DAYS=3, TRIAL_TRAFFIC_LIMIT_GB=10,
        trial_traffic_limit_bytes=10 * GIB,
    )
    settings.get_plan_price = lambda devices, months: settings.device_subscription_options.get(devices, {}).get(months)
    settings.get_plan_stars_price = lambda devices, months: settings.device_stars_options.get(devices, {}).get(months)
    settings.tier_monthly_rate = lambda devices: max(settings.device_subscription_options.get(devices, {}).values(), default=0) / 3
    factory.configure(info={"settings": settings})
    async with factory() as session:
        session.add(User(user_id=1, panel_user_uuid="42", language_code="ru"))
        session.add_all([CurrencyRate(currency="RUB", rate=1), CurrencyRate(currency="XTR", rate=1)])
        await session.commit()
    panel = FakePanel()
    bot = SimpleNamespace(send_message=AsyncMock())
    i18n = SimpleNamespace(gettext=lambda lang, key, **kw: key)
    lte = LteService(settings, panel, bot, i18n)
    subscription = SubscriptionService(settings, panel, bot, i18n)
    subscription._get_or_create_panel_user_link_details = AsyncMock(return_value=("42", "short", "short", False))
    yield SimpleNamespace(factory=factory, settings=settings, panel=panel, bot=bot,
                          lte=lte, subscription=subscription)
    await engine.dispose()


async def invoice(session, product="subscription", value=3, devices=5, amount=None):
    return await payment_dal.create_payment_record(session, dict(
        user_id=1, amount=amount if amount is not None else (500 if product == "subscription" else 100),
        currency="RUB", status="succeeded",
        provider="test", product_type=product, purchase_value=value,
        subscription_duration_months=value, hwid_device_limit=devices), settings=session.info["settings"])


async def activate(flow, session, payment):
    return await flow.subscription.activate_subscription(session, 1, 999, payment.amount,
                                                        payment.payment_id, provider="test")


async def test_purchase_uses_frozen_exact_allowance_and_unlimited_panel_account(flow):
    async with flow.factory() as session:
        payment = await invoice(session)
        await session.commit()
        # Previously issued DB-catalog invoices keep their exact LTE snapshot.
        payment.lte_traffic_bytes = 77 * GIB
        flow.settings.device_subscription_options.clear()
        await session.commit()
        result = await activate(flow, session, payment)
        await session.commit()
        account = await session.get(LteAccount, 1)
        assert account.credited_bytes == 77 * GIB
        assert account.used_bytes == 0
        assert result["device_limit"] == 5
        assert flow.panel.updates[-1]["trafficLimitBytes"] == 0
        assert flow.panel.updates[-1]["trafficLimitStrategy"] == "NO_RESET"


async def test_depletion_removes_only_lte_and_notifies_once(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 50 * GIB, "test:credit")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 50 * GIB
    await flow.lte.run_checks(flow.factory)
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    assert flow.panel.user["status"] == "ACTIVE"
    flow.bot.send_message.assert_awaited_once()
    markup = flow.bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "lte:packages"


async def test_renewal_restores_lte_and_accumulates_without_resetting_usage(flow):
    async with flow.factory() as session:
        payment = await invoice(session)
        await activate(flow, session, payment)
        await session.commit()
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 150 * GIB
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    async with flow.factory() as session:
        payment = await invoice(session)
        result = await activate(flow, session, payment)
        await session.commit()
        account = await session.get(LteAccount, 1)
        assert account.credited_bytes == 300 * GIB
        assert account.used_bytes == 150 * GIB
        assert account.remaining_bytes == 150 * GIB
        assert result["end_date"] > datetime.now(timezone.utc) + timedelta(days=170)
    assert set(flow.panel.user["activeInternalSquads"]) == {"foreign", "lte"}


async def test_topup_retains_expired_subscription_and_credit(flow):
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    flow.panel.user.update(expireAt=expired, status="EXPIRED")
    async with flow.factory() as session:
        payment = await invoice(session, "lte_topup", 25, None)
        result = await activate(flow, session, payment)
        await session.commit()
        assert result["end_date"].isoformat() == expired
        assert (await session.get(LteAccount, 1)).remaining_bytes == 25 * GIB
        assert not list((await session.execute(select(Subscription))).scalars())
    assert flow.panel.user["expireAt"] == expired
    assert flow.panel.user["status"] == "EXPIRED"
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]


async def test_issued_fractional_topup_invoice_retains_its_frozen_allowance(flow):
    expiry = flow.panel.user["expireAt"]
    async with flow.factory() as session:
        payment = Payment(user_id=1, amount=100, currency="RUB", status="succeeded",
                          product_type="lte_topup", lte_traffic_bytes=GIB // 2,
                          subscription_duration_months=0)
        session.add(payment)
        await session.flush()
        await activate(flow, session, payment)
        assert (await session.get(LteAccount, 1)).remaining_bytes == GIB // 2
    assert flow.panel.user["expireAt"] == expiry
    assert flow.panel.user["description"] == "0.5 ГБ"


async def test_trial_uses_environment_days_devices_and_ten_gb_with_no_global_cap(flow):
    async with flow.factory() as session:
        result = await flow.subscription.activate_trial_subscription(session, 1)
        assert result["activated"] is True
        assert result["traffic_gb"] == 10
        account = await session.get(LteAccount, 1)
        assert account.credited_bytes == 10 * GIB
        payload = flow.panel.updates[-1]
        assert payload["hwidDeviceLimit"] == 2
        assert payload["trafficLimitBytes"] == 0
        second = await flow.subscription.activate_trial_subscription(session, 1)
        assert second["activated"] is False
        assert account.credited_bytes == 10 * GIB


async def test_failed_squad_update_retries_without_losing_depletion(flow):
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 50 * GIB, "test:credit")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 50 * GIB
    flow.panel.reject_patch = True
    await flow.lte.run_checks(flow.factory)
    flow.bot.send_message.assert_not_awaited()
    assert flow.panel.user["activeInternalSquads"] == [{"uuid": "foreign"}, {"uuid": "lte"}]
    flow.panel.reject_patch = False
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    flow.bot.send_message.assert_awaited_once()
    async with flow.factory() as session:
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 50 * GIB
        assert not account.access_sync_pending


async def test_control_disabled_does_not_read_or_change_panel(flow):
    flow.settings.LTE_ENFORCEMENT_ENABLED = False
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 50, "test:credit")
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    assert not flow.panel.updates


@pytest.mark.parametrize("mode", ["disabled", "legacy"])
@pytest.mark.parametrize("lte_present", [False, True])
async def test_renewal_restores_ordinary_squads_without_changing_unmanaged_lte(flow, mode, lte_present):
    flow.settings.LTE_ENFORCEMENT_ENABLED = mode != "disabled"
    flow.settings.parsed_user_squad_uuids = ["foreign", "foreign-new", "lte"]
    flow.panel.user["activeInternalSquads"] = ["custom"] + (["lte"] if lte_present else [])
    async with flow.factory() as session:
        session.add(LteAccount(user_id=1, managed=mode != "legacy", credited_bytes=0, used_bytes=0))
        session.add(Subscription(user_id=1, panel_user_uuid="42", panel_subscription_uuid="short",
                                 end_date=datetime.fromisoformat(flow.panel.user["expireAt"]),
                                 is_active=True, hwid_device_limit=5))
        await session.commit()
        payment = await invoice(session)
        result = await activate(flow, session, payment)
        await session.commit()
        assert result is not None
        account = await session.get(LteAccount, 1)
        assert account.last_panel_counter is None
        assert account.used_bytes == 0
    assert set(flow.panel.user["activeInternalSquads"]) == {
        "custom", "foreign", "foreign-new", *(["lte"] if lte_present else [])}


async def test_bonus_restores_ordinary_squads_without_reenabling_exhausted_lte(flow):
    flow.settings.parsed_user_squad_uuids = ["foreign", "foreign-new", "lte"]
    flow.panel.user["activeInternalSquads"] = ["custom", "lte"]
    async with flow.factory() as session:
        session.add(LteAccount(user_id=1, managed=True, credited_bytes=0, used_bytes=0))
        await session.commit()
        end_date = await flow.subscription.extend_active_subscription_days(session, 1, 5, "test bonus")
        await session.commit()
        assert end_date is not None
    assert set(flow.panel.user["activeInternalSquads"]) == {"custom", "foreign", "foreign-new"}


@pytest.mark.parametrize("reason", ["referral bonus from a friend", "promo code bonus", "admin bonus"])
@pytest.mark.parametrize("lte_enabled", [False, True])
async def test_bonus_extension_preserves_a_ban_and_does_not_reopen_foreign_access(flow, reason, lte_enabled):
    flow.settings.LTE_ENFORCEMENT_ENABLED = lte_enabled
    flow.panel.user["status"] = "DISABLED"
    old_end = datetime.now(timezone.utc) + timedelta(days=10)
    async with flow.factory() as session:
        (await session.get(User, 1)).is_banned = True
        session.add(Subscription(user_id=1, panel_user_uuid="42", panel_subscription_uuid="short",
                                 end_date=old_end, is_active=True, provider="yookassa", hwid_device_limit=5))
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        await session.commit()
        new_end = await flow.subscription.extend_active_subscription_days(session, 1, 3, reason)
        await session.commit()
        assert new_end == old_end + timedelta(days=3)
        assert (await session.get(User, 1)).is_banned
        assert (await session.get(LteAccount, 1)).credited_bytes == 10 * GIB
    assert flow.panel.user["status"] == "DISABLED"
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["status"] == "DISABLED"
    flow.bot.send_message.assert_not_awaited()


async def test_bonus_extension_still_reactivates_an_expired_unbanned_subscription(flow):
    flow.panel.user.update(status="EXPIRED", expireAt=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
    async with flow.factory() as session:
        await lte_dal.credit(session, 1, 10 * GIB, "initial")
        new_end = await flow.subscription.extend_active_subscription_days(session, 1, 3, "referral bonus")
        await session.commit()
        assert new_end > datetime.now(timezone.utc)
    assert flow.panel.user["status"] == "ACTIVE"
    assert set(flow.panel.user["activeInternalSquads"]) == {"foreign", "lte"}


async def test_legacy_account_is_not_restricted_or_metered(flow):
    async with flow.factory() as session:
        session.add(LteAccount(user_id=1, managed=False, credited_bytes=0, used_bytes=0))
        await session.commit()
    await flow.lte.run_checks(flow.factory)
    assert not flow.panel.updates
    flow.bot.send_message.assert_not_awaited()


async def test_topup_restores_depleted_access_without_extending_subscription(flow):
    async with flow.factory() as session:
        payment = await invoice(session)
        await activate(flow, session, payment)
        await session.commit()
    expiry = flow.panel.user["expireAt"]
    flow.panel.user["userTraffic"]["lifetimeUsedTrafficBytes"] += 150 * GIB
    await flow.lte.run_checks(flow.factory)
    assert flow.panel.user["activeInternalSquads"] == ["foreign"]
    async with flow.factory() as session:
        payment = await invoice(session, "lte_topup", 25, None)
        flow.settings.traffic_packages.clear()
        await session.commit()
        await activate(flow, session, payment)
        await session.commit()
        account = await session.get(LteAccount, 1)
        assert account.used_bytes == 150 * GIB
        assert account.remaining_bytes == 25 * GIB
    assert flow.panel.user["expireAt"] == expiry
    assert set(flow.panel.user["activeInternalSquads"]) == {"foreign", "lte"}


async def test_purchase_does_not_enroll_a_legacy_customer_into_enforcement(flow):
    async with flow.factory() as session:
        session.add(LteAccount(user_id=1, managed=False, credited_bytes=0, used_bytes=0))
        await session.commit()
        payment = await invoice(session)
        await activate(flow, session, payment)
        await session.commit()
        account = await session.get(LteAccount, 1)
        assert not account.managed
        assert account.credited_bytes == 150 * GIB
        assert account.last_panel_counter is None
    await flow.lte.run_checks(flow.factory)
    assert {s["uuid"] for s in flow.panel.user["activeInternalSquads"]} == {"foreign", "lte"}


async def test_an_admin_trial_reset_allows_one_new_trial_grant(flow):
    from sqlalchemy import delete
    async with flow.factory() as session:
        await flow.subscription.activate_trial_subscription(session, 1)
        await session.execute(delete(Subscription).where(Subscription.user_id == 1))
        await session.commit()
        result = await flow.subscription.activate_trial_subscription(session, 1)
        assert result["activated"]
        assert (await session.get(LteAccount, 1)).credited_bytes == 20 * GIB


async def test_device_tier_switch_keeps_day_conversion_and_lte_does_not_depend_on_devices(flow):
    async with flow.factory() as session:
        payment = await invoice(session)
        await activate(flow, session, payment)
        subscription = (await session.execute(select(Subscription))).scalar_one()
        subscription.end_date = datetime.now(timezone.utc) + timedelta(days=30, hours=12)
        flow.panel.user["expireAt"] = subscription.end_date.isoformat()
        await session.commit()
        preview = await flow.subscription.compute_switch_preview(session, 1, 10, 3)
        assert preview["direction"] == "upgrade"
        payment = await invoice(session, devices=10, amount=1000)
        result = await activate(flow, session, payment)
        await session.commit()
        account = await session.get(LteAccount, 1)
        assert account.credited_bytes == 300 * GIB
        assert account.used_bytes == 0
        assert abs((result["end_date"] - preview["projected_end_date"]).total_seconds()) < 2
        assert flow.panel.updates[-1]["hwidDeviceLimit"] == 10
