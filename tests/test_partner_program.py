"""Tests for the partner (affiliate) program.

Unlike the panel tests, these run against a real (in-memory SQLite) database so
the money math is exercised as SQL, not as a stub: windowed aggregates, the
per-currency conversion, and the accrued/paid/balance arithmetic.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.dal import ad_dal, currency_dal, payment_dal, user_dal  # noqa: E402
from db.models import (  # noqa: E402
    AdAttribution,
    Base,
    CampaignAccrual,
    CurrencyRate,
    CAMPAIGN_TYPE_AD,
    CAMPAIGN_TYPE_PARTNER,
    PartnerPayout,
    Payment,
    User,
)

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
# Rates live in the database now; XTR is deliberately not 1.0 here so that a
# missing conversion would show up as a wrong number rather than a coincidence.
RATES = {"RUB": 1.0, "XTR": 1.5}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        for code, rate in RATES.items():
            db.add(CurrencyRate(currency=code, rate=rate))
        await db.flush()
        yield db
    await engine.dispose()


async def add_user(session, user_id: int, username: str = None) -> User:
    user = User(user_id=user_id, username=username)
    session.add(user)
    await session.flush()
    return user


async def attribute(
    session,
    user_id: int,
    campaign_id: int,
    *,
    ago_hours: float = 1000,
    trial: bool = False,
    is_new_user: bool = True,
):
    attribution = AdAttribution(
        user_id=user_id,
        ad_campaign_id=campaign_id,
        first_start_at=NOW - timedelta(hours=ago_hours),
        trial_activated_at=NOW - timedelta(hours=ago_hours - 1) if trial else None,
        is_new_user=is_new_user,
    )
    session.add(attribution)
    await session.flush()
    return attribution


async def add_payment(
    session,
    user_id: int,
    amount: float,
    *,
    ago_hours: float,
    status: str = "succeeded",
    currency: str = "RUB",
    months: int = 1,
    devices: int = None,
) -> Payment:
    rate = await currency_dal.get_rate(session, currency)
    payment = Payment(
        user_id=user_id,
        amount=amount,
        currency=currency,
        base_amount=round(amount * rate, 2) if rate is not None else None,
        fx_rate=rate,
        status=status,
        provider="yookassa",
        subscription_duration_months=months,
        hwid_device_limit=devices,
        created_at=NOW - timedelta(hours=ago_hours),
    )
    session.add(payment)
    await session.flush()
    return payment


@pytest_asyncio.fixture
async def partner_campaign(session):
    await add_user(session, 100, "partner_owner")
    campaign = await ad_dal.create_campaign(
        session,
        source="blogger",
        start_param="blog1",
        campaign_type=CAMPAIGN_TYPE_PARTNER,
        partner_user_id=100,
        partner_percent=30.0,
    )
    await session.commit()
    return campaign


# --------------------------------------------------------------------------- #
# Creation & validation
# --------------------------------------------------------------------------- #


async def test_ad_campaign_defaults_to_ad_type(session):
    campaign = await ad_dal.create_campaign(
        session, source="tg-channel", start_param="tgc", cost=5000.0
    )
    await session.commit()

    assert campaign.campaign_type == CAMPAIGN_TYPE_AD
    assert campaign.is_partner is False
    assert campaign.partner_user_id is None
    assert campaign.partner_percent is None
    assert campaign.cost == 5000.0


async def test_partner_campaign_creation(partner_campaign):
    assert partner_campaign.campaign_type == CAMPAIGN_TYPE_PARTNER
    assert partner_campaign.is_partner is True
    assert partner_campaign.partner_user_id == 100
    assert partner_campaign.partner_percent == 30.0
    # A partner label has no ad spend of its own.
    assert partner_campaign.cost == 0.0


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"campaign_type": "nonsense"}, "ad_campaign_invalid_type"),
        (
            {"campaign_type": CAMPAIGN_TYPE_PARTNER, "partner_percent": 10.0},
            "ad_campaign_partner_user_required",
        ),
        (
            {"campaign_type": CAMPAIGN_TYPE_PARTNER, "partner_user_id": 100},
            "ad_campaign_invalid_percent",
        ),
        (
            {
                "campaign_type": CAMPAIGN_TYPE_PARTNER,
                "partner_user_id": 100,
                "partner_percent": 0,
            },
            "ad_campaign_invalid_percent",
        ),
        (
            {
                "campaign_type": CAMPAIGN_TYPE_PARTNER,
                "partner_user_id": 100,
                "partner_percent": 101,
            },
            "ad_campaign_invalid_percent",
        ),
    ],
)
async def test_create_campaign_validation(session, kwargs, expected):
    await add_user(session, 100)
    with pytest.raises(ValueError) as exc:
        await ad_dal.create_campaign(
            session, source="x", start_param="xx", **kwargs
        )
    assert str(exc.value) == expected


async def test_duplicate_start_param_rejected(session, partner_campaign):
    with pytest.raises(ValueError) as exc:
        await ad_dal.create_campaign(session, source="other", start_param="blog1")
    assert str(exc.value) == "ad_campaign_start_param_exists"


async def test_partner_fields_ignored_for_ad_type(session):
    await add_user(session, 100)
    campaign = await ad_dal.create_campaign(
        session,
        source="ad",
        start_param="adx",
        cost=10.0,
        campaign_type=CAMPAIGN_TYPE_AD,
        partner_user_id=100,
        partner_percent=50.0,
    )
    assert campaign.partner_user_id is None
    assert campaign.partner_percent is None


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


async def test_partner_stats_windows(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id

    # Four users attributed at different times.
    for user_id, ago in ((1, 2), (2, 30), (3, 24 * 5), (4, 24 * 40)):
        await add_user(session, user_id)
        await attribute(session, user_id, campaign_id, ago_hours=ago, trial=user_id in (1, 3))

    # Purchases spread across the windows.
    await add_payment(session, 1, 500.0, ago_hours=1)          # 24h, week, month
    await add_payment(session, 2, 1000.0, ago_hours=25)        # week, month
    await add_payment(session, 3, 300.0, ago_hours=24 * 4)     # week, month
    await add_payment(session, 4, 200.0, ago_hours=24 * 20)    # month only
    await add_payment(session, 4, 100.0, ago_hours=24 * 35)    # all time only
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )

    # user 3 landed 5 days ago -> inside the week window; user 4 (40 days) is
    # only in the all-time count.
    assert stats["starts"] == {"total": 4, "day": 1, "week": 3, "month": 3}
    assert stats["trials"] == 2
    assert stats["payers"] == 4

    assert stats["purchases"]["total"] == {"count": 5, "amount": 2100.0}
    assert stats["purchases"]["day"] == {"count": 1, "amount": 500.0}
    assert stats["purchases"]["week"] == {"count": 3, "amount": 1800.0}
    assert stats["purchases"]["month"] == {"count": 4, "amount": 2000.0}

    assert stats["revenue"] == 2100.0
    assert stats["accrued"] == 630.0
    assert stats["paid_out"] == 0.0
    assert stats["balance"] == 630.0


async def test_payments_before_attribution_and_unsuccessful_are_excluded(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)

    await add_payment(session, 1, 999.0, ago_hours=72)                      # pre-attribution
    await add_payment(session, 1, 777.0, ago_hours=10, status="pending_yookassa")
    await add_payment(session, 1, 111.0, ago_hours=10, status="failed")
    await add_payment(session, 1, 400.0, ago_hours=10)                      # counted
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )
    assert stats["purchases"]["total"] == {"count": 1, "amount": 400.0}
    assert stats["accrued"] == 120.0  # 30% of the campaign fixture


async def test_other_campaign_payments_are_not_counted(session, partner_campaign):
    other = await ad_dal.create_campaign(
        session, source="other", start_param="other1", cost=1.0
    )
    await add_user(session, 1)
    await add_user(session, 2)
    await attribute(session, 1, partner_campaign.ad_campaign_id, ago_hours=48)
    await attribute(session, 2, other.ad_campaign_id, ago_hours=48)
    await add_payment(session, 1, 100.0, ago_hours=1)
    await add_payment(session, 2, 900.0, ago_hours=1)
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, partner_campaign.ad_campaign_id, now=NOW
    )
    assert stats["revenue"] == 100.0
    assert stats["purchases"]["total"]["count"] == 1


async def test_mixed_currencies_are_converted_not_summed_at_face_value(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)

    await add_payment(session, 1, 1000.0, ago_hours=2, currency="RUB")
    await add_payment(session, 1, 200.0, ago_hours=2, currency="XTR")   # 200 * 1.5 = 300
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )
    assert stats["revenue"] == 1300.0
    assert stats["accrued"] == 390.0  # 30% of 1300

    rows = (await session.execute(select(CampaignAccrual))).scalars().all()
    assert {(r.currency, r.base_amount) for r in rows} == {
        ("RUB", 1000.0),
        ("XTR", 300.0),
    }


async def test_currency_without_a_rate_is_excluded_not_valued_at_face_value(
    session, partner_campaign
):
    """5 USDT must never quietly become 5 RUB — the payment is surfaced instead."""
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    unvalued_payment = await add_payment(session, 1, 5.0, ago_hours=1, currency="USDT")
    await add_payment(session, 1, 100.0, ago_hours=1, currency="RUB")
    await session.commit()

    assert unvalued_payment.base_amount is None and unvalued_payment.fx_rate is None

    stats = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert stats["revenue"] == 100.0
    assert stats["purchases"]["total"]["count"] == 1
    assert stats["unvalued"] == 1

    # The admin adds the rate; the payment is valued and joins the ledger.
    await currency_dal.set_rate(session, "USDT", 95.0, updated_by=7)
    valued = await payment_dal.revalue_unvalued_payments(session)
    await session.commit()
    assert valued == 1

    stats = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert stats["revenue"] == 575.0
    assert stats["unvalued"] == 0


async def test_empty_campaign_stats_are_zero(session, partner_campaign):
    stats = await ad_dal.get_partner_stats(
        session, partner_campaign.ad_campaign_id, now=NOW
    )
    assert stats["starts"] == {"total": 0, "day": 0, "week": 0, "month": 0}
    assert stats["purchases"]["total"] == {"count": 0, "amount": 0.0}
    assert stats["revenue"] == 0.0
    assert stats["accrued"] == 0.0
    assert stats["balance"] == 0.0
    assert stats["unvalued"] == 0


async def test_legacy_ad_stats_are_unchanged(session):
    """The pre-existing ad statistics must keep behaving exactly as before."""
    campaign = await ad_dal.create_campaign(
        session, source="tg", start_param="tg1", cost=1000.0
    )
    await add_user(session, 1)
    await add_user(session, 2)
    await attribute(session, 1, campaign.ad_campaign_id, ago_hours=100, trial=True)
    await attribute(session, 2, campaign.ad_campaign_id, ago_hours=100)
    await add_payment(session, 1, 300.0, ago_hours=10)
    await add_payment(session, 1, 200.0, ago_hours=5)
    await add_payment(session, 2, 500.0, ago_hours=200)  # before attribution
    await session.commit()

    await ad_dal.sync_campaign_accruals(session, campaign.ad_campaign_id)
    stats = await ad_dal.get_campaign_stats(session, campaign.ad_campaign_id)
    assert stats == {
        "starts": 2, "trials": 1, "payers": 1, "revenue": 500.0, "unvalued": 0,
    }


# --------------------------------------------------------------------------- #
# Payouts and balance
# --------------------------------------------------------------------------- #


async def test_balance_is_accrued_minus_payouts(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    await add_payment(session, 1, 10000.0, ago_hours=2)

    await ad_dal.create_payout(
        session, campaign_id=campaign_id, amount=1000.0
    )
    await ad_dal.create_payout(
        session, campaign_id=campaign_id, amount=200.0, currency="XTR"
    )  # 200 * 1.5 = 300
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )
    assert stats["accrued"] == 3000.0
    assert stats["paid_out"] == 1300.0
    assert stats["balance"] == 1700.0


async def test_balance_can_go_negative_when_overpaid(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await ad_dal.create_payout(
        session, campaign_id=campaign_id, amount=500.0
    )
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )
    assert stats["paid_out"] == 500.0
    assert stats["balance"] == -500.0


async def test_payout_amount_must_be_positive(session, partner_campaign):
    for bad in (0, -10.0):
        with pytest.raises(ValueError) as exc:
            await ad_dal.create_payout(
                session, campaign_id=partner_campaign.ad_campaign_id, amount=bad
            )
        assert str(exc.value) == "partner_payout_invalid_amount"


async def test_payout_currency_is_normalised(session, partner_campaign):
    payout = await ad_dal.create_payout(
        session,
        campaign_id=partner_campaign.ad_campaign_id,
        amount=1.0,
        currency="xtr",
    )
    assert payout.currency == "XTR"
    assert payout.fx_rate == 1.5
    assert payout.base_amount == 1.5


async def test_payout_in_a_currency_without_a_rate_is_refused(session, partner_campaign):
    with pytest.raises(ValueError) as exc:
        await ad_dal.create_payout(
            session,
            campaign_id=partner_campaign.ad_campaign_id,
            amount=10.0,
            currency="USDT",
        )
    assert str(exc.value) == "partner_payout_unknown_currency"


async def test_payout_listing_pagination_and_lookup(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    for i in range(5):
        payout = await ad_dal.create_payout(
            session, campaign_id=campaign_id, amount=100.0 + i, created_by=7
        )
        payout.created_at = NOW - timedelta(days=i)
    await session.commit()

    assert await ad_dal.count_payouts(session, campaign_id) == 5

    page0 = await ad_dal.list_payouts_paged(session, campaign_id, page=0, page_size=2)
    page1 = await ad_dal.list_payouts_paged(session, campaign_id, page=1, page_size=2)
    page2 = await ad_dal.list_payouts_paged(session, campaign_id, page=2, page_size=2)
    assert [p.amount for p in page0] == [100.0, 101.0]  # newest first
    assert [p.amount for p in page1] == [102.0, 103.0]
    assert [p.amount for p in page2] == [104.0]

    found = await ad_dal.get_payout(session, campaign_id, page0[0].payout_id)
    assert found is not None and found.created_by == 7
    # A payout of another campaign is never reachable through this campaign.
    other = await ad_dal.create_campaign(session, source="o", start_param="o1")
    assert await ad_dal.get_payout(session, other.ad_campaign_id, page0[0].payout_id) is None


async def test_delete_payout_is_scoped_to_campaign(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    payout = await ad_dal.create_payout(
        session, campaign_id=campaign_id, amount=100.0
    )
    other = await ad_dal.create_campaign(session, source="o", start_param="o1")
    await session.commit()

    assert await ad_dal.delete_payout(session, other.ad_campaign_id, payout.payout_id) is False
    assert await ad_dal.delete_payout(session, campaign_id, payout.payout_id) is True
    await session.commit()
    assert await ad_dal.count_payouts(session, campaign_id) == 0


async def test_deleting_campaign_removes_its_payouts(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=10)
    await ad_dal.create_payout(
        session, campaign_id=campaign_id, amount=100.0
    )
    await ad_dal.sync_campaign_accruals(session, campaign_id)
    await session.commit()

    assert await ad_dal.delete_campaign(session, campaign_id) is True
    await session.commit()

    remaining = (await session.execute(select(PartnerPayout))).scalars().all()
    assert remaining == []
    attributions = (await session.execute(select(AdAttribution))).scalars().all()
    assert attributions == []
    accruals = (await session.execute(select(CampaignAccrual))).scalars().all()
    assert accruals == []


async def test_totals_include_partner_payouts(session, partner_campaign):
    ad_campaign = await ad_dal.create_campaign(
        session, source="tg", start_param="tg1", cost=2500.0
    )
    await add_user(session, 1)
    await attribute(session, 1, ad_campaign.ad_campaign_id, ago_hours=100)
    await add_payment(session, 1, 700.0, ago_hours=1)
    await ad_dal.create_payout(
        session,
        campaign_id=partner_campaign.ad_campaign_id,
        amount=100.0,
        currency="XTR",
    )
    await ad_dal.sync_campaign_accruals(session, ad_campaign.ad_campaign_id)
    await session.commit()

    totals = await ad_dal.get_totals(session)
    assert totals["cost"] == 2500.0
    assert totals["revenue"] == 700.0
    assert totals["payouts"] == 150.0


# --------------------------------------------------------------------------- #
# Listing, history and ownership
# --------------------------------------------------------------------------- #


async def test_list_partner_campaigns_for_user_filters_by_owner_and_type(session):
    await add_user(session, 100)
    await add_user(session, 200)
    mine_a = await ad_dal.create_campaign(
        session, source="a", start_param="a1",
        campaign_type=CAMPAIGN_TYPE_PARTNER, partner_user_id=100, partner_percent=10,
    )
    mine_b = await ad_dal.create_campaign(
        session, source="b", start_param="b1",
        campaign_type=CAMPAIGN_TYPE_PARTNER, partner_user_id=100, partner_percent=20,
    )
    await ad_dal.create_campaign(
        session, source="c", start_param="c1",
        campaign_type=CAMPAIGN_TYPE_PARTNER, partner_user_id=200, partner_percent=30,
    )
    await ad_dal.create_campaign(session, source="d", start_param="d1", cost=1.0)
    await session.commit()

    mine = await ad_dal.list_partner_campaigns_for_user(session, 100)
    assert [c.ad_campaign_id for c in mine] == [mine_a.ad_campaign_id, mine_b.ad_campaign_id]

    assert await ad_dal.count_partner_campaigns(session) == 3
    paged = await ad_dal.list_partner_campaigns_paged(session, page=0, page_size=2)
    assert len(paged) == 2
    assert all(c.campaign_type == CAMPAIGN_TYPE_PARTNER for c in paged)


async def test_campaign_payment_history(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1, "buyer")
    await attribute(session, 1, campaign_id, ago_hours=200)
    for i in range(3):
        await add_payment(session, 1, 100.0 + i, ago_hours=10 + i, months=i + 1, devices=5)
    await session.commit()

    await ad_dal.sync_campaign_accruals(session, campaign_id)
    assert await ad_dal.count_campaign_accruals(session, campaign_id) == 3

    page = await ad_dal.list_campaign_accruals_paged(
        session, campaign_id, page=0, page_size=2
    )
    assert [a.amount for a in page] == [100.0, 101.0]  # newest first
    assert page[0].user_id == 1

    detail = await ad_dal.get_campaign_accrual(session, campaign_id, page[0].accrual_id)
    assert detail is not None
    assert detail.subscription_duration_months == 1
    assert detail.hwid_device_limit == 5
    assert detail.percent == 30.0
    assert detail.earned_amount == 30.0


async def test_ledger_lookup_is_scoped_to_the_campaign(session, partner_campaign):
    """A ledger row of another campaign must not be reachable through this one."""
    other = await ad_dal.create_campaign(session, source="o", start_param="o1")
    await add_user(session, 1)
    await add_user(session, 2)
    await attribute(session, 1, partner_campaign.ad_campaign_id, ago_hours=100)
    await attribute(session, 2, other.ad_campaign_id, ago_hours=100)
    await add_payment(session, 2, 500.0, ago_hours=1)
    await session.commit()

    await ad_dal.sync_campaign_accruals(session, other.ad_campaign_id)
    foreign = (await session.execute(select(CampaignAccrual))).scalars().one()

    assert (
        await ad_dal.get_campaign_accrual(
            session, partner_campaign.ad_campaign_id, foreign.accrual_id
        )
        is None
    )


async def test_owned_campaign_guard(session, partner_campaign):
    from bot.handlers.user.partner import _owned_campaign

    await add_user(session, 999)
    ad_campaign = await ad_dal.create_campaign(
        session, source="plain", start_param="plain1", cost=5.0
    )
    await session.commit()

    assert await _owned_campaign(session, partner_campaign.ad_campaign_id, 100) is not None
    # Not the owner.
    assert await _owned_campaign(session, partner_campaign.ad_campaign_id, 999) is None
    # Not a partner campaign at all.
    assert await _owned_campaign(session, ad_campaign.ad_campaign_id, 100) is None
    # Missing campaign.
    assert await _owned_campaign(session, 424242, 100) is None


# --------------------------------------------------------------------------- #
# Attribution & user lifecycle
# --------------------------------------------------------------------------- #


async def test_only_users_who_registered_through_the_label_are_credited(
    session, partner_campaign
):
    """An already-registered user clicking the link must not inflate the label."""
    campaign_id = partner_campaign.ad_campaign_id

    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48, trial=True, is_new_user=True)
    await add_payment(session, 1, 1000.0, ago_hours=2)

    # Same label, but this one was already a user before clicking.
    await add_user(session, 2)
    await attribute(session, 2, campaign_id, ago_hours=48, trial=True, is_new_user=False)
    await add_payment(session, 2, 5000.0, ago_hours=2)
    await session.commit()

    stats = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert stats["starts"]["total"] == 1
    assert stats["trials"] == 1
    assert stats["payers"] == 1
    assert stats["revenue"] == 1000.0
    assert stats["accrued"] == 300.0

    legacy = await ad_dal.get_campaign_stats(session, campaign_id)
    assert legacy == {
        "starts": 1, "trials": 1, "payers": 1, "revenue": 1000.0, "unvalued": 0,
    }


async def test_no_accrual_is_written_for_a_pre_existing_user(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 2)
    await attribute(session, 2, campaign_id, ago_hours=48, is_new_user=False)
    payment = await add_payment(session, 2, 500.0, ago_hours=1)
    await session.commit()

    assert await ad_dal.record_accrual_for_payment(session, payment.payment_id) is None
    assert await ad_dal.sync_campaign_accruals(session, campaign_id) == 0
    assert await ad_dal.count_campaign_accruals(session, campaign_id) == 0
    # Not "unvalued" either — it is simply out of scope for the campaign.
    assert await ad_dal.count_unvalued_payments(session, campaign_id) == 0


async def test_ensure_attribution_records_the_flag(session, partner_campaign):
    await add_user(session, 1)
    await add_user(session, 2)

    fresh = await ad_dal.ensure_attribution(
        session, user_id=1, campaign_id=partner_campaign.ad_campaign_id, is_new_user=True
    )
    existing = await ad_dal.ensure_attribution(
        session, user_id=2, campaign_id=partner_campaign.ad_campaign_id
    )
    await session.commit()

    assert fresh.is_new_user is True
    # Defaults to False: crediting a campaign has to be an explicit decision.
    assert existing.is_new_user is False


async def test_the_flag_is_not_changed_by_a_later_click(session, partner_campaign):
    other = await ad_dal.create_campaign(session, source="o", start_param="o1")
    await add_user(session, 1)

    first = await ad_dal.ensure_attribution(
        session, user_id=1, campaign_id=partner_campaign.ad_campaign_id, is_new_user=False
    )
    second = await ad_dal.ensure_attribution(
        session, user_id=1, campaign_id=other.ad_campaign_id, is_new_user=True
    )
    await session.commit()

    assert second is first
    assert first.is_new_user is False


async def test_attribution_stays_with_the_first_campaign(session, partner_campaign):
    other = await ad_dal.create_campaign(session, source="o", start_param="o1")
    await add_user(session, 1)

    first = await ad_dal.ensure_attribution(
        session, user_id=1, campaign_id=partner_campaign.ad_campaign_id
    )
    second = await ad_dal.ensure_attribution(
        session, user_id=1, campaign_id=other.ad_campaign_id
    )
    await session.commit()

    assert second.ad_campaign_id == first.ad_campaign_id == partner_campaign.ad_campaign_id
    rows = (await session.execute(select(AdAttribution))).scalars().all()
    assert len(rows) == 1


async def test_deleting_partner_user_detaches_but_keeps_the_campaign(session, partner_campaign):
    """The FK on partner_user_id must not block user deletion."""
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=100)
    await add_payment(session, 1, 1000.0, ago_hours=2)
    await ad_dal.sync_campaign_accruals(session, campaign_id)
    await session.commit()

    assert await user_dal.delete_user_and_relations(session, 100) is True
    await session.commit()

    campaign = await ad_dal.get_campaign_by_id(session, campaign_id)
    assert campaign is not None
    assert campaign.partner_user_id is None
    assert campaign.is_active is False
    assert campaign.partner_percent == 30.0
    # Revenue history survives the owner, even though the payments are gone.
    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )
    assert stats["revenue"] == 1000.0
    assert stats["accrued"] == 300.0
    assert await ad_dal.list_partner_campaigns_for_user(session, 100) == []


# --------------------------------------------------------------------------- #
# The accrual ledger
# --------------------------------------------------------------------------- #


async def _pending_payment(session, user_id, amount, currency="RUB", status="pending_yookassa"):
    """Create through the real DAL, so the base value is applied where it is in prod."""
    payment = await payment_dal.create_payment_record(
        session,
        {
            "user_id": user_id,
            "amount": amount,
            "currency": currency,
            "status": status,
            "provider": "yookassa",
            "subscription_duration_months": 1,
        },
    )
    payment.created_at = NOW - timedelta(hours=1)
    await session.flush()
    return payment


async def test_succeeding_a_payment_writes_the_ledger_row(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    payment = await _pending_payment(session, 1, 250.0, currency="XTR")
    await session.commit()

    assert await payment_dal.mark_provider_payment_succeeded_once(
        session, payment.payment_id, "provider-1"
    ) is True
    await session.commit()

    accrual = await ad_dal.get_accrual_by_payment(session, payment.payment_id)
    assert accrual is not None
    assert accrual.ad_campaign_id == campaign_id
    assert accrual.user_id == 1
    assert accrual.amount == 250.0
    assert accrual.currency == "XTR"
    assert accrual.base_amount == 375.0  # copied from payments.base_amount
    assert accrual.percent == 30.0
    assert accrual.earned_amount == 112.5

    payment = await payment_dal.get_payment_by_db_id(session, payment.payment_id)
    assert payment.fx_rate == 1.5
    assert payment.base_amount == 375.0


async def test_status_update_path_also_writes_the_ledger_row(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    payment = await _pending_payment(session, 1, 500.0)
    await session.commit()

    await payment_dal.update_payment_status_by_db_id(
        session, payment.payment_id, "succeeded"
    )
    await session.commit()

    accrual = await ad_dal.get_accrual_by_payment(session, payment.payment_id)
    assert accrual is not None and accrual.base_amount == 500.0


async def test_a_replayed_webhook_cannot_double_credit(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    payment = await _pending_payment(session, 1, 1000.0)
    await session.commit()

    await payment_dal.mark_provider_payment_succeeded_once(session, payment.payment_id, "p1")
    # The provider retries; the second transition is a no-op and so is the accrual.
    await payment_dal.mark_provider_payment_succeeded_once(session, payment.payment_id, "p1")
    await payment_dal.update_payment_status_by_db_id(session, payment.payment_id, "succeeded")
    await ad_dal.record_accrual_for_payment(session, payment.payment_id)
    await ad_dal.sync_campaign_accruals(session, campaign_id)
    await session.commit()

    rows = (await session.execute(select(CampaignAccrual))).scalars().all()
    assert len(rows) == 1

    stats = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert stats["revenue"] == 1000.0
    assert stats["accrued"] == 300.0


async def test_unattributed_payments_are_not_accrued(session):
    await add_user(session, 1)
    payment = await _pending_payment(session, 1, 100.0)
    await session.commit()

    await payment_dal.mark_provider_payment_succeeded_once(session, payment.payment_id, "p1")
    await session.commit()

    assert await ad_dal.get_accrual_by_payment(session, payment.payment_id) is None


async def test_payment_made_before_attribution_is_not_accrued(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=1)
    payment = await _pending_payment(session, 1, 100.0)  # created 1h ago, attributed 1h ago
    payment.created_at = NOW - timedelta(hours=48)
    await session.commit()

    await payment_dal.mark_provider_payment_succeeded_once(session, payment.payment_id, "p1")
    await session.commit()

    assert await ad_dal.get_accrual_by_payment(session, payment.payment_id) is None


async def test_changing_the_rate_does_not_revalue_history(session, partner_campaign):
    """The whole point: yesterday's money keeps yesterday's rate."""
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    await add_payment(session, 1, 200.0, ago_hours=10, currency="XTR")
    await session.commit()

    before = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert before["revenue"] == 300.0  # 200 * 1.5
    assert before["accrued"] == 90.0

    # The admin doubles the star rate.
    await currency_dal.set_rate(session, "XTR", 3.0, updated_by=7)
    await session.commit()

    after = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert after["revenue"] == 300.0
    assert after["accrued"] == 90.0

    # ...but a payment made afterwards is valued at the new rate.
    await add_payment(session, 1, 100.0, ago_hours=1, currency="XTR")
    await session.commit()
    latest = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert latest["revenue"] == 600.0  # 300 frozen + 100 * 3.0
    assert latest["accrued"] == 180.0


async def test_changing_the_percent_does_not_revalue_history(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    await add_payment(session, 1, 1000.0, ago_hours=10)
    await session.commit()

    first = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert first["accrued"] == 300.0  # 30%

    partner_campaign.partner_percent = 50.0
    await session.flush()
    await add_payment(session, 1, 1000.0, ago_hours=1)
    await session.commit()

    second = await ad_dal.get_partner_stats(session, campaign_id, now=NOW)
    assert second["revenue"] == 2000.0
    assert second["accrued"] == 800.0  # 300 frozen + 500 at the new rate


async def test_ad_campaign_rows_are_accrued_with_zero_percent(session):
    campaign = await ad_dal.create_campaign(
        session, source="tg", start_param="tg1", cost=100.0
    )
    await add_user(session, 1)
    await attribute(session, 1, campaign.ad_campaign_id, ago_hours=48)
    await add_payment(session, 1, 700.0, ago_hours=1, currency="XTR")
    await session.commit()

    await ad_dal.sync_campaign_accruals(session, campaign.ad_campaign_id)
    row = (await session.execute(select(CampaignAccrual))).scalars().one()
    assert row.percent == 0.0
    assert row.earned_amount == 0.0
    assert row.base_amount == 1050.0  # 700 * 1.5 — the ad card is currency-correct now

    stats = await ad_dal.get_campaign_stats(session, campaign.ad_campaign_id)
    assert stats["revenue"] == 1050.0


async def test_sync_is_idempotent_and_reports_what_it_added(session, partner_campaign):
    campaign_id = partner_campaign.ad_campaign_id
    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    await add_payment(session, 1, 100.0, ago_hours=1)
    await add_payment(session, 1, 200.0, ago_hours=2)
    await session.commit()

    assert await ad_dal.sync_campaign_accruals(session, campaign_id) == 2
    assert await ad_dal.sync_campaign_accruals(session, campaign_id) == 0
    assert await ad_dal.count_campaign_accruals(session, campaign_id) == 2


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def make_settings(**overrides):
    from config.settings import Settings

    base = {"BOT_TOKEN": "test:token", "_env_file": None}
    base.update(overrides)
    return Settings(**base)


def test_partner_defaults():
    settings = make_settings()
    assert settings.PARTNER_MIN_PAYOUT == 500.0
    assert settings.PARTNER_PROGRAM_ENABLED is True


def test_base_currency_is_a_constant_not_a_setting():
    """One definition, deliberately not configurable per deployment."""
    from config.currency import BASE_CURRENCY
    from config.settings import Settings

    assert BASE_CURRENCY == "RUB"
    # Nothing may reintroduce it as an env-tunable knob.
    assert not any("PAYOUT_CURRENCY" in name for name in Settings.model_fields)
    assert not any("BASE_CURRENCY" in name for name in Settings.model_fields)


async def test_stars_are_accrued_one_to_one_at_the_shipped_rate(session, partner_campaign):
    """A 250-star purchase must earn the partner the same as a 250 RUB one."""
    # What migration 0007 seeds into currency_rates.
    await currency_dal.set_rate(session, "XTR", 1.0)
    campaign_id = partner_campaign.ad_campaign_id

    await add_user(session, 1)
    await attribute(session, 1, campaign_id, ago_hours=48)
    await add_payment(session, 1, 250.0, ago_hours=1, currency="XTR")
    await add_payment(session, 1, 250.0, ago_hours=1, currency="RUB")
    await session.commit()

    stats = await ad_dal.get_partner_stats(
        session, campaign_id, now=NOW
    )
    assert stats["revenue"] == 500.0
    assert stats["accrued"] == 150.0


async def test_currency_rates_are_read_from_the_database(session):
    assert await currency_dal.get_rate(session, "XTR") == 1.5
    assert await currency_dal.get_rate(session, "rub") == 1.0
    # Never invented for an unconfigured currency.
    assert await currency_dal.get_rate(session, "TON") is None
    assert await currency_dal.get_rate(session, None) is None

    await currency_dal.set_rate(session, "ton", 300.0, updated_by=1)
    assert await currency_dal.get_rate(session, "TON") == 300.0
    assert await currency_dal.get_rates(session) == {
        "RUB": 1.0,
        "XTR": 1.5,
        "TON": 300.0,
    }


@pytest.mark.parametrize("bad_rate", [0, -1, None])
async def test_currency_rate_must_be_positive(session, bad_rate):
    with pytest.raises(ValueError) as exc:
        await currency_dal.set_rate(session, "USDT", bad_rate)
    assert str(exc.value) == "currency_rate_invalid_rate"


async def test_payment_creation_freezes_the_rate(session):
    """The conversion happens on the payment, at purchase time."""
    await add_user(session, 1)
    payment = await payment_dal.create_payment_record(
        session,
        {
            "user_id": 1,
            "amount": 250.0,
            "currency": "XTR",
            "status": "pending_stars",
            "provider": "telegram_stars",
        },
    )
    assert payment.fx_rate == 1.5
    assert payment.base_amount == 375.0


async def test_payment_in_an_unconfigured_currency_is_still_created(session):
    """A missing rate must never block a sale — it only defers valuation."""
    await add_user(session, 1)
    payment = await payment_dal.create_payment_record(
        session,
        {
            "user_id": 1,
            "amount": 5.0,
            "currency": "USDT",
            "status": "pending_cryptopay",
            "provider": "cryptopay",
        },
    )
    assert payment.payment_id is not None
    assert payment.base_amount is None and payment.fx_rate is None
