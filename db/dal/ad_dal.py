import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, delete, func, and_, case

from ..models import (
    AdCampaign,
    AdAttribution,
    CampaignAccrual,
    Payment,
    PartnerPayout,
    CAMPAIGN_TYPE_AD,
    CAMPAIGN_TYPE_PARTNER,
)


# Rolling windows used by the campaign statistics, in hours.
WINDOW_HOURS: Dict[str, int] = {"day": 24, "week": 24 * 7, "month": 24 * 30}


async def create_campaign(
    session: AsyncSession,
    *,
    source: str,
    start_param: str,
    cost: float = 0.0,
    campaign_type: str = CAMPAIGN_TYPE_AD,
    partner_user_id: Optional[int] = None,
    partner_percent: Optional[float] = None,
) -> AdCampaign:
    existing = await get_campaign_by_start_param(session, start_param)
    if existing:
        raise ValueError("ad_campaign_start_param_exists")

    if campaign_type not in (CAMPAIGN_TYPE_AD, CAMPAIGN_TYPE_PARTNER):
        raise ValueError("ad_campaign_invalid_type")

    if campaign_type == CAMPAIGN_TYPE_PARTNER:
        if partner_user_id is None:
            raise ValueError("ad_campaign_partner_user_required")
        if partner_percent is None or not (0 < float(partner_percent) <= 100):
            raise ValueError("ad_campaign_invalid_percent")
    else:
        partner_user_id = None
        partner_percent = None

    campaign = AdCampaign(
        source=source,
        start_param=start_param,
        cost=float(cost or 0.0),
        campaign_type=campaign_type,
        partner_user_id=partner_user_id,
        partner_percent=float(partner_percent) if partner_percent is not None else None,
    )
    session.add(campaign)
    await session.flush()
    await session.refresh(campaign)
    logging.info(
        f"AdCampaign created id={campaign.ad_campaign_id}, type={campaign_type}, "
        f"source={source}, start={start_param}, cost={cost}, "
        f"partner={partner_user_id}, percent={partner_percent}"
    )
    return campaign


async def get_campaign_by_id(session: AsyncSession, campaign_id: int) -> Optional[AdCampaign]:
    stmt = select(AdCampaign).where(AdCampaign.ad_campaign_id == campaign_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_campaign_by_start_param(session: AsyncSession, start_param: str) -> Optional[AdCampaign]:
    clean = start_param.strip()
    stmt = select(AdCampaign).where(AdCampaign.start_param == clean)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_campaigns(session: AsyncSession, *, only_active: bool = False) -> List[AdCampaign]:
    stmt = select(AdCampaign).order_by(AdCampaign.created_at.desc())
    if only_active:
        stmt = stmt.where(AdCampaign.is_active == True)
    result = await session.execute(stmt)
    return result.scalars().all()


async def toggle_campaign_active(session: AsyncSession, campaign_id: int, is_active: bool) -> bool:
    stmt = (
        update(AdCampaign)
        .where(AdCampaign.ad_campaign_id == campaign_id)
        .values(is_active=is_active)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def ensure_attribution(
    session: AsyncSession,
    *,
    user_id: int,
    campaign_id: int,
    is_new_user: bool = False,
) -> AdAttribution:
    """Record the first-touch label for a user.

    `is_new_user` must be True only when this /start is also the user's
    registration. Campaign statistics count those rows exclusively, so an
    already-registered user clicking a link is stored for reference but never
    credited to the campaign.
    """
    existing = await get_attribution_for_user(session, user_id)
    if existing:
        return existing
    attrib = AdAttribution(
        user_id=user_id, ad_campaign_id=campaign_id, is_new_user=bool(is_new_user)
    )
    session.add(attrib)
    await session.flush()
    await session.refresh(attrib)
    logging.info(
        "AdAttribution created for user %s -> campaign %s (new_user=%s)",
        user_id,
        campaign_id,
        bool(is_new_user),
    )
    return attrib


async def get_attribution_for_user(session: AsyncSession, user_id: int) -> Optional[AdAttribution]:
    stmt = select(AdAttribution).where(AdAttribution.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def mark_trial_activated(session: AsyncSession, user_id: int) -> bool:
    stmt = (
        update(AdAttribution)
        .where(and_(AdAttribution.user_id == user_id, AdAttribution.trial_activated_at.is_(None)))
        .values(trial_activated_at=func.now())
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


# --------------------------------------------------------------------------- #
# Accrual ledger
# --------------------------------------------------------------------------- #


def _campaign_users(campaign_id: int):
    """Users a campaign is credited for: attributed *and* registered through it."""
    return and_(
        AdAttribution.ad_campaign_id == campaign_id,
        AdAttribution.is_new_user.is_(True),
    )


def _eligible_payments_stmt(campaign_id: Optional[int] = None):
    """Succeeded payments of attributed users, made after they were attributed."""
    stmt = (
        select(Payment, AdAttribution.ad_campaign_id)
        .join(AdAttribution, AdAttribution.user_id == Payment.user_id)
        .where(
            and_(
                AdAttribution.is_new_user.is_(True),
                Payment.status == "succeeded",
                Payment.created_at >= AdAttribution.first_start_at,
            )
        )
    )
    if campaign_id is not None:
        stmt = stmt.where(AdAttribution.ad_campaign_id == campaign_id)
    return stmt


def _build_accrual(
    payment: Payment, campaign_id: int, percent: Optional[float]
) -> CampaignAccrual:
    """Copy the payment's already-normalised value and freeze the campaign share."""
    base_amount = float(payment.base_amount or 0.0)
    percent_value = float(percent or 0.0)
    return CampaignAccrual(
        ad_campaign_id=campaign_id,
        payment_id=payment.payment_id,
        user_id=payment.user_id,
        amount=float(payment.amount or 0.0),
        currency=(payment.currency or "").upper(),
        base_amount=base_amount,
        percent=percent_value,
        earned_amount=round(base_amount * percent_value / 100.0, 2),
        provider=payment.provider,
        subscription_duration_months=payment.subscription_duration_months,
        hwid_device_limit=payment.hwid_device_limit,
        paid_at=payment.created_at,
    )


async def get_accrual_by_payment(session: AsyncSession, payment_id: int) -> Optional[CampaignAccrual]:
    stmt = select(CampaignAccrual).where(CampaignAccrual.payment_id == payment_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_accrual_for_payment(
    session: AsyncSession, payment_id: int
) -> Optional[CampaignAccrual]:
    """Write the ledger row for a payment that has just succeeded.

    Idempotent: a payment already in the ledger is left untouched, so replayed
    provider webhooks cannot double-credit a partner. Returns None when the
    payment is not attributed, not eligible, already accrued, or still unvalued
    because its currency has no configured rate.
    """
    payment = (
        await session.execute(
            select(Payment)
            .where(Payment.payment_id == payment_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None or payment.status != "succeeded":
        return None

    attribution = await get_attribution_for_user(session, payment.user_id)
    if attribution is None or not attribution.is_new_user:
        return None
    if (
        payment.created_at is not None
        and attribution.first_start_at is not None
        and payment.created_at < attribution.first_start_at
    ):
        return None

    if await get_accrual_by_payment(session, payment_id) is not None:
        return None

    if payment.base_amount is None:
        logging.error(
            "Payment %s has no base value (currency %s has no configured rate), so it "
            "stays out of campaign %s statistics until one is added.",
            payment_id,
            payment.currency,
            attribution.ad_campaign_id,
        )
        return None

    campaign = await get_campaign_by_id(session, attribution.ad_campaign_id)
    if campaign is None:
        return None

    accrual = _build_accrual(payment, campaign.ad_campaign_id, campaign.partner_percent)
    session.add(accrual)
    await session.flush()
    logging.info(
        "CampaignAccrual recorded: payment=%s campaign=%s %s %s -> %s (earned %s)",
        payment_id,
        campaign.ad_campaign_id,
        accrual.amount,
        accrual.currency,
        accrual.base_amount,
        accrual.earned_amount,
    )
    return accrual


async def sync_campaign_accruals(session: AsyncSession, campaign_id: int) -> int:
    """Materialise ledger rows for eligible payments that have none yet.

    A safety net: the ledger is normally written when a payment succeeds, but a
    read must never under-report because one of those writes was missed.
    Payments still unvalued (no rate for their currency) are skipped and left
    for `count_unpriced_payments` to surface. Returns the number of rows added.
    """
    campaign = await get_campaign_by_id(session, campaign_id)
    if campaign is None:
        return 0

    accrued_subq = select(CampaignAccrual.payment_id).where(
        CampaignAccrual.payment_id.is_not(None)
    )
    stmt = (
        _eligible_payments_stmt(campaign_id)
        .where(Payment.payment_id.not_in(accrued_subq))
        .where(Payment.base_amount.is_not(None))
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return 0

    for payment, _campaign_id in rows:
        session.add(_build_accrual(payment, campaign_id, campaign.partner_percent))

    await session.flush()
    logging.info("Backfilled %s accrual(s) for campaign %s", len(rows), campaign_id)
    return len(rows)


async def count_unpriced_payments(session: AsyncSession, campaign_id: int) -> int:
    """Eligible payments the ledger cannot value — a missing currency rate."""
    stmt = (
        select(func.count())
        .select_from(Payment)
        .join(AdAttribution, AdAttribution.user_id == Payment.user_id)
        .where(
            and_(
                _campaign_users(campaign_id),
                Payment.status == "succeeded",
                Payment.created_at >= AdAttribution.first_start_at,
                Payment.base_amount.is_(None),
            )
        )
    )
    return int((await session.execute(stmt)).scalar() or 0)


# --------------------------------------------------------------------------- #
# Statistics (read from the ledger)
# --------------------------------------------------------------------------- #


def _window_cutoffs(now: Optional[datetime] = None) -> Dict[str, datetime]:
    now = now or datetime.now(timezone.utc)
    return {name: now - timedelta(hours=hours) for name, hours in WINDOW_HOURS.items()}


async def get_campaign_stats(session: AsyncSession, campaign_id: int) -> Dict[str, Any]:
    starts_stmt = select(func.count(AdAttribution.user_id)).where(
        _campaign_users(campaign_id)
    )
    starts = (await session.execute(starts_stmt)).scalar() or 0

    trials_stmt = select(func.count(AdAttribution.user_id)).where(
        and_(
            _campaign_users(campaign_id),
            AdAttribution.trial_activated_at.is_not(None),
        )
    )
    trials = (await session.execute(trials_stmt)).scalar() or 0

    ledger_stmt = select(
        func.count(func.distinct(CampaignAccrual.user_id)),
        func.coalesce(func.sum(CampaignAccrual.base_amount), 0.0),
    ).where(CampaignAccrual.ad_campaign_id == campaign_id)
    payers, revenue = (await session.execute(ledger_stmt)).one()

    return {
        "starts": int(starts),
        "trials": int(trials),
        "payers": int(payers or 0),
        "revenue": round(float(revenue or 0.0), 2),
    }


async def count_campaigns(session: AsyncSession, *, only_active: bool = False) -> int:
    stmt = select(func.count(AdCampaign.ad_campaign_id))
    if only_active:
        stmt = stmt.where(AdCampaign.is_active == True)
    return int((await session.execute(stmt)).scalar() or 0)


async def list_campaigns_paged(
    session: AsyncSession, *, page: int, page_size: int, only_active: bool = False
) -> List[AdCampaign]:
    offset = max(0, page) * max(1, page_size)
    stmt = select(AdCampaign).order_by(AdCampaign.created_at.desc()).offset(offset).limit(page_size)
    if only_active:
        stmt = stmt.where(AdCampaign.is_active == True)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_totals(session: AsyncSession) -> Dict[str, float]:
    total_cost = float(
        (await session.execute(select(func.coalesce(func.sum(AdCampaign.cost), 0.0)))).scalar()
        or 0.0
    )
    total_revenue = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(CampaignAccrual.base_amount), 0.0))
            )
        ).scalar()
        or 0.0
    )
    total_payouts = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(PartnerPayout.base_amount), 0.0))
            )
        ).scalar()
        or 0.0
    )
    return {
        "cost": round(total_cost, 2),
        "revenue": round(total_revenue, 2),
        "payouts": round(total_payouts, 2),
    }


async def delete_campaign(session: AsyncSession, campaign_id: int) -> bool:
    """Delete ad campaign by id along with its attributions, ledger and payouts."""
    try:
        campaign = await session.get(AdCampaign, campaign_id)
        if not campaign:
            return False
        await session.delete(campaign)
        await session.flush()
        logging.info(f"AdCampaign deleted id={campaign_id}")
        return True
    except Exception as e:
        logging.error(f"Failed to delete AdCampaign id={campaign_id}: {e}", exc_info=True)
        raise


# --------------------------------------------------------------------------- #
# Partner programs
# --------------------------------------------------------------------------- #


async def list_partner_campaigns_for_user(session: AsyncSession, user_id: int) -> List[AdCampaign]:
    """All partner campaigns owned by a user, oldest first (stable ordering)."""
    stmt = (
        select(AdCampaign)
        .where(
            and_(
                AdCampaign.campaign_type == CAMPAIGN_TYPE_PARTNER,
                AdCampaign.partner_user_id == user_id,
            )
        )
        .order_by(AdCampaign.ad_campaign_id.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def count_partner_campaigns(session: AsyncSession) -> int:
    stmt = select(func.count(AdCampaign.ad_campaign_id)).where(
        AdCampaign.campaign_type == CAMPAIGN_TYPE_PARTNER
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def list_partner_campaigns_paged(
    session: AsyncSession, *, page: int, page_size: int
) -> List[AdCampaign]:
    offset = max(0, page) * max(1, page_size)
    stmt = (
        select(AdCampaign)
        .where(AdCampaign.campaign_type == CAMPAIGN_TYPE_PARTNER)
        .order_by(AdCampaign.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def _starts_breakdown(
    session: AsyncSession, campaign_id: int, cutoffs: Dict[str, datetime]
) -> Dict[str, int]:
    columns = [func.count(AdAttribution.user_id)]
    columns.extend(
        func.count(
            case((AdAttribution.first_start_at >= cutoffs[name], AdAttribution.user_id))
        )
        for name in WINDOW_HOURS
    )
    stmt = select(*columns).where(_campaign_users(campaign_id))
    row = (await session.execute(stmt)).one()
    result = {"total": int(row[0] or 0)}
    for idx, name in enumerate(WINDOW_HOURS, start=1):
        result[name] = int(row[idx] or 0)
    return result


async def _ledger_breakdown(
    session: AsyncSession, campaign_id: int, cutoffs: Dict[str, datetime]
) -> Dict[str, Dict[str, float]]:
    """Counts and base-currency sums per window, in one query over the ledger."""
    columns = [
        func.count(CampaignAccrual.accrual_id),
        func.coalesce(func.sum(CampaignAccrual.base_amount), 0.0),
        func.coalesce(func.sum(CampaignAccrual.earned_amount), 0.0),
    ]
    for name in WINDOW_HOURS:
        cutoff = cutoffs[name]
        columns.append(
            func.count(case((CampaignAccrual.paid_at >= cutoff, CampaignAccrual.accrual_id)))
        )
        columns.append(
            func.coalesce(
                func.sum(
                    case((CampaignAccrual.paid_at >= cutoff, CampaignAccrual.base_amount), else_=0.0)
                ),
                0.0,
            )
        )

    stmt = select(*columns).where(CampaignAccrual.ad_campaign_id == campaign_id)
    row = (await session.execute(stmt)).one()

    breakdown = {
        "total": {"count": int(row[0] or 0), "amount": round(float(row[1] or 0.0), 2)},
        "earned": round(float(row[2] or 0.0), 2),
    }
    for idx, name in enumerate(WINDOW_HOURS):
        breakdown[name] = {
            "count": int(row[3 + idx * 2] or 0),
            "amount": round(float(row[4 + idx * 2] or 0.0), 2),
        }
    return breakdown


async def get_total_paid_out(session: AsyncSession, campaign_id: int) -> float:
    stmt = select(func.coalesce(func.sum(PartnerPayout.base_amount), 0.0)).where(
        PartnerPayout.ad_campaign_id == campaign_id
    )
    return round(float((await session.execute(stmt)).scalar() or 0.0), 2)


async def get_partner_stats(
    session: AsyncSession,
    campaign_id: int,
    *,
    now: Optional[datetime] = None,
    sync: bool = True,
) -> Dict[str, Any]:
    """Detailed statistics for one campaign label, read from the ledger.

    Every figure comes from `campaign_accruals`, whose rows carry the value the
    payment was normalised to and the partner share in force at that moment.
    Changing a rate or a percent therefore only affects payments recorded
    afterwards — the history a partner has already been paid against cannot move.
    """
    if sync:
        await sync_campaign_accruals(session, campaign_id)

    cutoffs = _window_cutoffs(now)
    starts = await _starts_breakdown(session, campaign_id, cutoffs)
    ledger = await _ledger_breakdown(session, campaign_id, cutoffs)

    trials_stmt = select(func.count(AdAttribution.user_id)).where(
        and_(
            _campaign_users(campaign_id),
            AdAttribution.trial_activated_at.is_not(None),
        )
    )
    trials = int((await session.execute(trials_stmt)).scalar() or 0)

    payers_stmt = select(func.count(func.distinct(CampaignAccrual.user_id))).where(
        CampaignAccrual.ad_campaign_id == campaign_id
    )
    payers = int((await session.execute(payers_stmt)).scalar() or 0)

    purchases = {
        window: ledger[window] for window in ("total", *WINDOW_HOURS)
    }
    accrued = ledger["earned"]
    paid_out = await get_total_paid_out(session, campaign_id)

    return {
        "starts": starts,
        "trials": trials,
        "payers": payers,
        "purchases": purchases,
        "revenue": purchases["total"]["amount"],
        "accrued": accrued,
        "paid_out": paid_out,
        "balance": round(accrued - paid_out, 2),
        "unpriced": await count_unpriced_payments(session, campaign_id),
    }


async def count_campaign_accruals(session: AsyncSession, campaign_id: int) -> int:
    stmt = select(func.count(CampaignAccrual.accrual_id)).where(
        CampaignAccrual.ad_campaign_id == campaign_id
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def list_campaign_accruals_paged(
    session: AsyncSession, campaign_id: int, *, page: int, page_size: int
) -> List[CampaignAccrual]:
    offset = max(0, page) * max(1, page_size)
    stmt = (
        select(CampaignAccrual)
        .where(CampaignAccrual.ad_campaign_id == campaign_id)
        .order_by(CampaignAccrual.paid_at.desc(), CampaignAccrual.accrual_id.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_campaign_accrual(
    session: AsyncSession, campaign_id: int, accrual_id: int
) -> Optional[CampaignAccrual]:
    """Fetch one ledger row, but only if it belongs to this campaign."""
    stmt = select(CampaignAccrual).where(
        and_(
            CampaignAccrual.accrual_id == accrual_id,
            CampaignAccrual.ad_campaign_id == campaign_id,
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Payouts
# --------------------------------------------------------------------------- #


async def create_payout(
    session: AsyncSession,
    *,
    campaign_id: int,
    amount: float,
    currency: str = "RUB",
    comment: Optional[str] = None,
    created_by: Optional[int] = None,
) -> PartnerPayout:
    if amount is None or float(amount) <= 0:
        raise ValueError("partner_payout_invalid_amount")

    from .currency_dal import get_rate

    code = (currency or "RUB").upper()
    rate = await get_rate(session, code)
    if rate is None:
        raise ValueError("partner_payout_unknown_currency")

    payout = PartnerPayout(
        ad_campaign_id=campaign_id,
        amount=float(amount),
        currency=code,
        fx_rate=float(rate),
        base_amount=round(float(amount) * float(rate), 2),
        comment=comment,
        created_by=created_by,
    )
    session.add(payout)
    await session.flush()
    await session.refresh(payout)
    logging.info(
        f"PartnerPayout created id={payout.payout_id}, campaign={campaign_id}, "
        f"amount={payout.amount} {payout.currency} (={payout.base_amount} base), by={created_by}"
    )
    return payout


async def count_payouts(session: AsyncSession, campaign_id: int) -> int:
    stmt = select(func.count(PartnerPayout.payout_id)).where(
        PartnerPayout.ad_campaign_id == campaign_id
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def list_payouts_paged(
    session: AsyncSession, campaign_id: int, *, page: int, page_size: int
) -> List[PartnerPayout]:
    offset = max(0, page) * max(1, page_size)
    stmt = (
        select(PartnerPayout)
        .where(PartnerPayout.ad_campaign_id == campaign_id)
        .order_by(PartnerPayout.created_at.desc(), PartnerPayout.payout_id.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_payout(
    session: AsyncSession, campaign_id: int, payout_id: int
) -> Optional[PartnerPayout]:
    stmt = select(PartnerPayout).where(
        and_(
            PartnerPayout.payout_id == payout_id,
            PartnerPayout.ad_campaign_id == campaign_id,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def delete_payout(session: AsyncSession, campaign_id: int, payout_id: int) -> bool:
    stmt = delete(PartnerPayout).where(
        and_(
            PartnerPayout.payout_id == payout_id,
            PartnerPayout.ad_campaign_id == campaign_id,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0
