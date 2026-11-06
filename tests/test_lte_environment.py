"""Environment pricing and frozen LTE invoices, without database catalog rows."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import Settings
from db.models import Base, User, LteAccount
from db.dal import lte_dal, payment_dal


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(User(user_id=1))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, BOT_TOKEN="test",
                    DEVICE_PLANS_RUB="2:100,300,600,1200|5:200,600,1200,2400|10:400,1200,2400,4800",
                    DEVICE_PLANS_STARS="2:100,300,600,1200", STARS_ENABLED=True,
                    TRAFFIC_PACKAGES="25:100", STARS_TRAFFIC_PACKAGES="25:150")


@pytest.mark.parametrize("months", [1, 3, 6, 12])
@pytest.mark.parametrize("devices", [2, 5, 10])
def test_lte_allowance_depends_only_on_purchased_months(settings, months, devices):
    terms = payment_dal._purchase_terms(settings, "subscription", months, devices, "RUB")
    assert terms["lte_traffic_bytes"] == 50 * months * 2**30
    assert terms["subscription_duration_months"] == months
    assert terms["hwid_device_limit"] == devices
    assert terms["original_amount"] == settings.get_plan_price(devices, months)


def test_disabled_period_cannot_be_purchased(settings):
    settings.DEVICE_PLANS_RUB = "2:0,300,600,1200"
    with pytest.raises(ValueError):
        payment_dal._purchase_terms(settings, "subscription", 1, 2, "RUB")


@pytest.mark.parametrize("currency", ["RUB", "XTR"])
def test_exact_configured_lte_allowances_are_used(settings, currency):
    settings.DEVICE_PLANS_LTE_GB = "7,21,0,123"
    for months, gb in zip((1, 3, 6, 12), (7, 21, 0, 123)):
        terms = payment_dal._purchase_terms(settings, "subscription", months, 2, currency)
        assert terms["lte_traffic_bytes"] == int(gb * 2**30)


@pytest.mark.parametrize("raw", ["", "50", "50,150,300", "50,150,300,600,700",
    "-1,150,300,600", "nan,150,300,600", "inf,150,300,600", "x,150,300,600",
    "50,,300,600", "999999999999,150,300,600", "50,150.5,300,600"])
def test_invalid_lte_configuration_is_rejected(raw):
    with pytest.raises(ValueError):
        Settings(_env_file=None, BOT_TOKEN="test", DEVICE_PLANS_LTE_GB=raw)


@pytest.mark.parametrize("field", ["TRAFFIC_PACKAGES", "STARS_TRAFFIC_PACKAGES"])
@pytest.mark.parametrize("gb", ["0.1", "0.5", "1.6", "0", "-1", "nan", "inf", "8589934592"])
def test_invalid_package_sizes_are_rejected_before_they_can_be_displayed(field, gb):
    with pytest.raises(ValueError):
        Settings(_env_file=None, BOT_TOKEN="test", **{field: f"{gb}:100"})


def test_integer_package_sizes_keep_fractional_prices_and_use_integer_keys():
    settings = Settings(_env_file=None, BOT_TOKEN="test", TRAFFIC_PACKAGES="25:100.5,50.0:200",
                        STARS_TRAFFIC_PACKAGES="25:100,50.0:200")
    assert settings.traffic_packages == {25: 100.5, 50: 200}
    assert settings.stars_traffic_packages == {25: 100, 50: 200}
    assert all(type(gb) is int for gb in settings.traffic_packages)
    assert all(type(gb) is int for gb in settings.stars_traffic_packages)


@pytest.mark.parametrize("gb", [0.1, 0.5, 1.6, -1, "nan", "inf", "8589934592"])
def test_trial_allowance_rejects_fractional_or_invalid_gb(gb):
    with pytest.raises(ValueError):
        Settings(_env_file=None, BOT_TOKEN="test", TRIAL_TRAFFIC_LIMIT_GB=gb)


@pytest.mark.parametrize("gb", [None, 0, 10, "10.0"])
def test_trial_allows_zero_or_whole_gb(gb):
    settings = Settings(_env_file=None, BOT_TOKEN="test", TRIAL_TRAFFIC_LIMIT_GB=gb)
    assert settings.trial_traffic_limit_bytes == (10 if gb in (10, "10.0") else 0) * 2**30


@pytest.mark.parametrize("gb", ["0.1", "0.5", "1.6", "0", "-1", True])
def test_manual_and_checkout_traffic_parser_rejects_non_positive_integer_gb(gb):
    with pytest.raises(ValueError):
        lte_dal.traffic_bytes(gb)


def test_empty_tariffs_offer_no_subscription():
    settings = Settings(_env_file=None, BOT_TOKEN="test")
    assert not settings.device_plans_active
    assert settings.device_tiers == []
    with pytest.raises(ValueError):
        payment_dal._purchase_terms(settings, "subscription", 1, None, "RUB")


@pytest.mark.parametrize("currency", ["RUB", "XTR"])
def test_subscription_always_requires_a_device_tier(settings, currency):
    with pytest.raises(ValueError, match="device tier"):
        payment_dal._purchase_terms(settings, "subscription", 1, None, currency)


@pytest.mark.parametrize("payload", ["1:129", "1:129:subscription", "0:1:129:subscription",
                                     "0.5:100:lte_topup"])
def test_removed_single_tier_checkout_payload_is_rejected(payload):
    from bot.handlers.user.subscription.payments_subscription import parse_simple_offer
    assert parse_simple_offer(payload) is None


@pytest.mark.parametrize("payload", ["subscribe_period:1", "subscribe_period:0:1"])
def test_subscription_period_requires_a_positive_device_tier(payload):
    from bot.handlers.user.subscription.payments_subscription import _parse_period_callback
    with pytest.raises(ValueError):
        _parse_period_callback(payload)


async def test_checkout_resolvers_do_not_offer_a_subscription_without_devices(settings):
    from bot.handlers.user.subscription.payments_subscription import resolve_fiat_offer_price_for_user
    from bot.services.stars_service import StarsService
    stars = StarsService(None, settings, None, None, None)
    assert stars._resolve_base_stars_price(1, "subscription") is None
    assert stars._resolve_base_stars_price(1, "subscription", 2) == 100
    assert await resolve_fiat_offer_price_for_user(None, settings, 1, 1, "subscription") is None
    assert await resolve_fiat_offer_price_for_user(None, settings, 1, 1, "subscription", devices=2) == 100


def test_topup_does_not_purchase_subscription_time(settings):
    terms = payment_dal._purchase_terms(settings, "lte_topup", 25, None, "RUB")
    assert terms["product_type"] == "lte_topup"
    assert terms["subscription_duration_months"] == 0
    assert terms["lte_traffic_bytes"] == 25 * 2**30
    assert terms["hwid_device_limit"] is None
    assert terms["original_amount"] == 100


@pytest.mark.parametrize("product,value,devices", [("unknown", 1, 2), ("subscription", 1.5, 2),
    ("subscription", 1, None), ("subscription", 1, 99), ("lte_topup", 99, None),
    ("lte_topup", float("nan"), None)])
def test_unknown_offers_are_rejected(settings, product, value, devices):
    with pytest.raises(ValueError):
        payment_dal._purchase_terms(settings, product, value, devices, "RUB")


async def test_credit_is_idempotent_and_accumulates(session):
    session = session
    await lte_dal.credit(session, 1, 77, "test:one")
    await lte_dal.credit(session, 1, 77, "test:one")
    account = await lte_dal.credit(session, 1, 23, "test:two")
    assert account.credited_bytes == 100


async def test_legacy_customer_remains_exempt_after_purchase(session):
    session = session
    session.add(LteAccount(user_id=1, managed=False, credited_bytes=0, used_bytes=0))
    await session.flush()
    account = await lte_dal.credit(session, 1, 50, "test:legacy")
    assert account.managed is False
    assert account.credited_bytes == 50


def test_counter_reset_preserves_already_charged_usage():
    account = LteAccount(user_id=1, managed=True, credited_bytes=1000, used_bytes=40,
                         last_panel_counter=200, panel_user_ref="42")
    lte_dal.observe_counter(account, "42", 230)
    assert account.used_bytes == 70
    lte_dal.observe_counter(account, "42", 10)
    assert account.used_bytes == 80
    lte_dal.observe_counter(account, "42", 20)
    assert account.used_bytes == 90


def test_first_observation_excludes_historical_usage():
    account = LteAccount(user_id=1, managed=True, credited_bytes=100, used_bytes=0)
    lte_dal.observe_counter(account, "42", 10000)
    assert account.used_bytes == 0
    lte_dal.observe_counter(account, "42", 10010)
    assert account.used_bytes == 10


async def test_unknown_device_invoice_can_be_retried(session):
    settings = Settings(_env_file=None, BOT_TOKEN="test", DEVICE_PLANS_RUB="2:100,0,0,0")
    payment = await payment_dal.create_payment_record(session, dict(
        user_id=1, amount=100, currency="RUB", status="pending_2328_unknown", provider="2328",
        subscription_duration_months=1, product_type="subscription", purchase_value=1,
        hwid_device_limit=2), settings=settings)
    assert payment.hwid_device_limit == 2
    retry = await payment_dal.get_unconfirmed_provider_payment(
        session, user_id=1, provider="2328", amount=100, currency="RUB", months=1,
        device_limit=2, product_type="subscription")
    assert retry.payment_id == payment.payment_id


async def test_invoice_keeps_price_and_lte_after_environment_changes(session, settings):
    payment = await payment_dal.create_payment_record(session, dict(
        user_id=1, amount=300, currency="RUB", status="pending", provider="test",
        product_type="subscription", purchase_value=3, hwid_device_limit=2), settings=settings)
    await session.commit()
    settings.DEVICE_PLANS_RUB = "2:999,999,999,999"
    settings.DEVICE_PLANS_LTE_GB = "1,2,3,4"
    await session.refresh(payment)
    assert payment.original_amount == payment.amount == 300
    assert payment.lte_traffic_bytes == 150 * 2**30
    assert payment.subscription_duration_months == 3
