import logging
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, func, and_, or_, case
from sqlalchemy.orm import selectinload

from config.settings import Settings
from db.models import Payment
from .lte_dal import GIB, traffic_bytes


def _purchase_terms(settings: Optional[Settings], product_type: str, value: Any,
                    devices: Optional[int], currency: str) -> Dict[str, Any]:
    """Freeze environment-owned offer terms before an invoice is issued."""
    if settings is None:
        raise ValueError("Purchase settings are required")
    if product_type == "lte_topup":
        amount = traffic_bytes(value)
        prices = settings.stars_traffic_packages if currency == "XTR" else settings.traffic_packages
        price = prices.get(amount / GIB)
        months, device_limit = 0, None
    elif product_type == "subscription":
        try:
            period = Decimal(str(value))
            if not period.is_finite() or period not in (1, 3, 6, 12):
                raise ValueError("Invalid subscription period")
            months = int(period)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Invalid subscription period") from exc
        if devices is None:
            raise ValueError("A device tier is required")
        price = (settings.get_plan_stars_price(devices, months) if currency == "XTR"
                 else settings.get_plan_price(devices, months))
        device_limit = devices
        amount = settings.device_plan_lte_bytes[months]
    else:
        raise ValueError("Invalid payment product")
    if price is None or not math.isfinite(float(price)) or price <= 0:
        raise ValueError("Offer is unavailable in this currency")
    return dict(product_type=product_type, original_amount=float(price),
                lte_traffic_bytes=amount, subscription_duration_months=months,
                hwid_device_limit=device_limit)


async def create_payment_record(session: AsyncSession,
                                payment_data: Dict[str, Any], *, settings: Optional[Settings] = None) -> Payment:
    payment_data = dict(payment_data)
    purchase_value = payment_data.pop("purchase_value", payment_data.get("subscription_duration_months"))
    if "product_type" in payment_data:
        payment_data.update(_purchase_terms(
            settings, payment_data["product_type"], purchase_value,
            payment_data.get("hwid_device_limit"), payment_data.get("currency", "RUB")))
        discount = payment_data["original_amount"] - float(payment_data["amount"])
        payment_data["discount_applied"] = round(discount, 2) if discount > 0 else None
    from .user_dal import get_user_by_id
    user = await get_user_by_id(session, payment_data["user_id"])
    if not user:

        raise ValueError(
            f"User with id {payment_data['user_id']} not found for creating payment."
        )
    if payment_data.get("product_type") == "lte_topup" and not user.panel_user_uuid:
        raise ValueError("A traffic purchase requires an existing subscription account")

    if payment_data.get("promo_code_id"):
        from .promo_code_dal import get_promo_code_by_id
        promo = await get_promo_code_by_id(session,
                                           payment_data["promo_code_id"])
        if not promo:
            raise ValueError(
                f"Promo code with id {payment_data['promo_code_id']} not found."
            )

    new_payment = Payment(**payment_data)
    if new_payment.base_amount is None:
        await _apply_base_amount(session, new_payment)
    session.add(new_payment)
    await session.flush()
    await session.refresh(new_payment)
    logging.info(
        f"Payment record {new_payment.payment_id} created for user {new_payment.user_id}"
    )
    return new_payment


async def _apply_base_amount(session: AsyncSession, payment: Payment) -> bool:
    """Freeze the payment's value in the base currency at purchase time.

    Leaves both fields NULL when the currency has no configured rate: a sale
    must never be blocked by missing bookkeeping config, and an unvalued
    payment is excluded from money totals rather than counted at face value.
    """
    from .currency_dal import get_rate

    try:
        rate = await get_rate(session, payment.currency)
    except Exception as e:
        # The rate table being unreachable is a bookkeeping problem, not a
        # reason to fail the purchase. The payment is recorded unvalued and
        # picked up later by revalue_unvalued_payments.
        logging.error(
            "Could not look up a conversion rate for %s: %s. The payment is "
            "recorded unvalued.",
            payment.currency,
            e,
            exc_info=True,
        )
        return False

    if rate is None:
        logging.error(
            "No conversion rate configured for %s — payment for user %s is recorded "
            "unvalued and stays out of revenue totals until a rate is added.",
            payment.currency,
            payment.user_id,
        )
        return False

    payment.fx_rate = float(rate)
    payment.base_amount = round(float(payment.amount or 0.0) * float(rate), 2)
    return True


async def value_payment_if_needed(session: AsyncSession, payment_db_id: int) -> bool:
    """Second chance to value a payment: a rate may have been added since."""
    payment = await get_payment_by_db_id(session, payment_db_id)
    if payment is None or payment.base_amount is not None:
        return False
    applied = await _apply_base_amount(session, payment)
    if applied:
        await session.flush()
    return applied


async def count_unvalued_payments(session: AsyncSession) -> int:
    stmt = select(func.count(Payment.payment_id)).where(
        and_(Payment.status == "succeeded", Payment.base_amount.is_(None))
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def revalue_unvalued_payments(session: AsyncSession) -> int:
    """Value every succeeded payment left unvalued, at the current rates.

    Called after an admin adds a rate. Payments that already carry a value are
    untouched — this only fills gaps, it never revalues history.
    """
    from .currency_dal import get_rates

    rates = await get_rates(session)
    if not rates:
        return 0

    stmt = select(Payment).where(
        and_(
            Payment.status == "succeeded",
            Payment.base_amount.is_(None),
            func.upper(Payment.currency).in_(list(rates)),
        )
    )
    payments = (await session.execute(stmt)).scalars().all()
    for payment in payments:
        rate = rates[(payment.currency or "").upper()]
        payment.fx_rate = float(rate)
        payment.base_amount = round(float(payment.amount or 0.0) * float(rate), 2)

    if payments:
        await session.flush()
        logging.info("Valued %s previously unvalued payment(s).", len(payments))
    return len(payments)


async def get_payment_by_provider_payment_id(
        session: AsyncSession, provider_payment_id: str) -> Optional[Payment]:
    """Fetch a payment by provider-specific identifier."""
    stmt = select(Payment).where(
        Payment.provider_payment_id == provider_payment_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def ensure_payment_with_provider_id(
        session: AsyncSession,
        *,
        user_id: int,
        amount: float,
        currency: str,
        months: int,
        description: str,
        provider: str,
        provider_payment_id: str) -> Payment:
    """Idempotently create a payment record for a provider event.

    If a payment with the same provider_payment_id already exists, returns it.
    Otherwise creates a new pending payment with provided data.
    """
    existing = await get_payment_by_provider_payment_id(session, provider_payment_id)
    if existing:
        return existing

    pending_status = f"pending_{provider}" if provider else "pending"
    payment_payload: Dict[str, Any] = {
        "user_id": user_id,
        "amount": float(amount),
        "currency": currency,
        "status": pending_status,
        "description": description,
        "subscription_duration_months": months,
        "provider_payment_id": provider_payment_id,
        "provider": provider,
    }
    return await create_payment_record(session, payment_payload)


async def get_payment_by_db_id(session: AsyncSession,
                               payment_db_id: int,
                               *, for_update: bool = False) -> Optional[Payment]:

    stmt = select(Payment).where(Payment.payment_id == payment_db_id).options(
        selectinload(Payment.user), selectinload(Payment.promo_code_used))
    if for_update:
        # Refresh an existing identity-map entry after waiting for another webhook.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_unconfirmed_provider_payment(
        session: AsyncSession, *, user_id: int, provider: str, amount: float,
        currency: str, months: int, device_limit: Optional[int],
        product_type: Optional[str] = None, lte_traffic_bytes: Optional[int] = None) -> Optional[Payment]:
    """Reuse an identical attempt whose upstream creation result is unknown."""
    device_match = Payment.hwid_device_limit == device_limit
    if product_type == "subscription" and device_limit == 0:
        device_match = or_(
            Payment.hwid_device_limit == 0,
            Payment.hwid_device_limit.is_(None),
        )
    stmt = (
        select(Payment)
        .where(
            Payment.user_id == user_id,
            Payment.provider == provider,
            Payment.status == f"pending_{provider}_unknown",
            Payment.amount == amount,
            Payment.currency == currency,
            Payment.subscription_duration_months == (0 if product_type == "lte_topup" else months),
            device_match,
        )
        .order_by(Payment.payment_id.desc())
        .limit(1)
    )
    if product_type is not None:
        stmt = stmt.where(Payment.product_type == product_type)
    if lte_traffic_bytes is not None:
        stmt = stmt.where(Payment.lte_traffic_bytes == lte_traffic_bytes)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _record_campaign_accrual(session: AsyncSession, payment_db_id: int) -> None:
    """Write the campaign ledger row for a payment that just succeeded.

    Statistics must never be able to break payment processing, so every failure
    here is swallowed and logged: the reader-side `sync_campaign_accruals` will
    pick the payment up on the next read anyway.
    """
    try:
        from . import ad_dal

        await ad_dal.record_accrual_for_payment(session, payment_db_id)
    except Exception as e:
        logging.error(
            "Failed to record campaign accrual for payment %s: %s",
            payment_db_id,
            e,
            exc_info=True,
        )


async def update_payment_status_by_db_id(
        session: AsyncSession,
        payment_db_id: int,
        new_status: str,
        yk_payment_id: Optional[str] = None) -> Optional[Payment]:
    payment = await get_payment_by_db_id(session, payment_db_id)
    if payment:
        payment.status = new_status
        payment.updated_at = func.now()
        if yk_payment_id and payment.yookassa_payment_id is None:
            payment.yookassa_payment_id = yk_payment_id
        await session.flush()
        await session.refresh(payment)
        logging.info(
            f"Payment record {payment.payment_id} status updated to {new_status}."
        )
        if new_status == "succeeded":
            await value_payment_if_needed(session, payment.payment_id)
            await _record_campaign_accrual(session, payment.payment_id)
    else:
        logging.warning(
            f"Payment record with DB ID {payment_db_id} not found for status update."
        )
    return payment


async def get_recent_payment_logs_with_user(session: AsyncSession,
                                            limit: int = 20,
                                            offset: int = 0) -> List[Payment]:
    stmt = (select(Payment).options(selectinload(Payment.user))
            .where(Payment.status == 'succeeded')
            .order_by(Payment.created_at.desc())
            .limit(limit).offset(offset))
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_payments_count(session: AsyncSession) -> int:
    """Get total count of successful payments."""
    stmt = select(func.count(Payment.payment_id)).where(Payment.status == 'succeeded')
    result = await session.execute(stmt)
    return result.scalar() or 0


async def get_all_succeeded_payments_with_user(session: AsyncSession) -> List[Payment]:
    """Get all successful payments with user data for export."""
    stmt = (select(Payment).options(selectinload(Payment.user))
            .where(Payment.status == 'succeeded')
            .order_by(Payment.created_at.desc()))
    result = await session.execute(stmt)
    return result.scalars().all()


async def count_user_succeeded_payments(
    session: AsyncSession, user_id: int, exclude_payment_id: Optional[int] = None,
    product_type: Optional[str] = None,
) -> int:
    """Count succeeded payments for a specific user.

    If exclude_payment_id is provided, that specific payment will be excluded
    from the count. Useful to check "prior" payments while processing the
    current payment in the same transaction.
    """
    conditions = [Payment.user_id == user_id, Payment.status == 'succeeded']
    if product_type is not None:
        conditions.append(Payment.product_type == product_type)
    if exclude_payment_id is not None:
        conditions.append(Payment.payment_id != exclude_payment_id)
    stmt = select(func.count(Payment.payment_id)).where(and_(*conditions))
    result = await session.execute(stmt)
    return result.scalar() or 0


async def update_provider_payment_and_status(
        session: AsyncSession, payment_db_id: int,
        provider_payment_id: str, new_status: str) -> Optional[Payment]:
    payment = await get_payment_by_db_id(session, payment_db_id)
    if payment:
        payment.status = new_status
        payment.provider_payment_id = provider_payment_id
        payment.updated_at = func.now()
        await session.flush()
        await session.refresh(payment)
        logging.info(
            f"Payment record {payment.payment_id} updated with provider id {provider_payment_id} and status {new_status}."
        )
    else:
        logging.warning(
            f"Payment record with DB ID {payment_db_id} not found for provider update."
        )
    return payment


async def mark_provider_payment_succeeded_once(
        session: AsyncSession,
        payment_db_id: int,
        provider_payment_id: str) -> bool:
    """Atomically mark payment as succeeded only once.

    Returns True only for the first successful transition to "succeeded".
    Returns False when payment is missing or already succeeded.
    """
    stmt = (
        update(Payment)
        .where(
            Payment.payment_id == payment_db_id,
            Payment.status != "succeeded",
        )
        .values(
            status="succeeded",
            provider_payment_id=provider_payment_id,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    updated = (result.rowcount or 0) > 0
    if updated:
        logging.info(
            "Payment record %s atomically marked as succeeded (provider id %s).",
            payment_db_id,
            provider_payment_id,
        )
        await value_payment_if_needed(session, payment_db_id)
        await _record_campaign_accrual(session, payment_db_id)
    return updated


async def mark_provider_payment_processing_once(
        session: AsyncSession,
        payment_db_id: int,
        provider_payment_id: str,
        expected_status_prefix: Optional[str] = None) -> bool:
    """Atomically claim payment for processing exactly once.

    Returns True only when status is changed from a non-terminal state to
    "processing". This prevents duplicate activation when concurrent
    webhooks arrive for the same payment.
    """
    conditions = [
        Payment.payment_id == payment_db_id,
        Payment.status != "succeeded",
        Payment.status != "processing",
    ]
    if expected_status_prefix:
        if expected_status_prefix == "pending":
            # YooKassa may keep authorized payments in waiting_for_capture
            # before reporting a final successful capture webhook.
            conditions.append(
                Payment.status.in_(("waiting_for_capture", "pending", "pending_yookassa"))
            )
        else:
            conditions.append(Payment.status.like(f"{expected_status_prefix}%"))

    stmt = (
        update(Payment)
        .where(*conditions)
        .values(
            status="processing",
            provider_payment_id=provider_payment_id,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    updated = (result.rowcount or 0) > 0
    if updated:
        logging.info(
            "Payment record %s atomically marked as processing (provider id %s).",
            payment_db_id,
            provider_payment_id,
        )
    return updated


async def rollback_provider_payment_processing(
        session: AsyncSession,
        payment_db_id: int,
        rollback_status: str,
        provider_payment_id: Optional[str] = None) -> bool:
    """Atomically rollback temporary processing status.

    Returns True only if payment is currently in "processing" state.
    """
    values = {
        "status": rollback_status,
        "updated_at": func.now(),
    }
    if provider_payment_id:
        values["provider_payment_id"] = provider_payment_id

    stmt = (
        update(Payment)
        .where(
            Payment.payment_id == payment_db_id,
            Payment.status == "processing",
        )
        .values(**values)
    )
    result = await session.execute(stmt)
    updated = (result.rowcount or 0) > 0
    if updated:
        logging.info(
            "Payment record %s rolled back from processing to %s.",
            payment_db_id,
            rollback_status,
        )
    return updated


async def update_payment_discount_info(
        session: AsyncSession,
        payment_db_id: int,
        original_amount: Optional[float],
        discount_applied: Optional[float],
        promo_code_id: Optional[int]) -> Optional[Payment]:
    """Update payment record with discount metadata."""
    payment = await get_payment_by_db_id(session, payment_db_id)
    if payment:
        payment.original_amount = original_amount
        payment.discount_applied = discount_applied
        payment.promo_code_id = promo_code_id
        payment.updated_at = func.now()
        await session.flush()
        await session.refresh(payment)
        logging.info(
            f"Payment record {payment.payment_id} updated with discount info: "
            f"original {original_amount}, discount {discount_applied}, promo {promo_code_id}"
        )
    else:
        logging.warning(
            f"Payment record with DB ID {payment_db_id} not found for discount info update."
        )
    return payment


async def get_financial_statistics(session: AsyncSession) -> Dict[str, Any]:
    """Get comprehensive financial statistics."""
    
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)
    
    # Revenue figures sum the normalised column, so mixed currencies add up
    # correctly. Payments whose currency has no rate are NULL here and stay out.
    stmt_today = select(func.sum(Payment.base_amount)).where(
        and_(
            Payment.status == 'succeeded',
            Payment.created_at >= today_start
        )
    )
    today_revenue = await session.execute(stmt_today)
    today_amount = today_revenue.scalar() or 0
    
    # Week revenue
    stmt_week = select(func.sum(Payment.base_amount)).where(
        and_(
            Payment.status == 'succeeded',
            Payment.created_at >= week_start
        )
    )
    week_revenue = await session.execute(stmt_week)
    week_amount = week_revenue.scalar() or 0
    
    # Month revenue
    stmt_month = select(func.sum(Payment.base_amount)).where(
        and_(
            Payment.status == 'succeeded',
            Payment.created_at >= month_start
        )
    )
    month_revenue = await session.execute(stmt_month)
    month_amount = month_revenue.scalar() or 0
    
    # All time revenue
    stmt_all = select(func.sum(Payment.base_amount)).where(Payment.status == 'succeeded')
    all_revenue = await session.execute(stmt_all)
    all_amount = all_revenue.scalar() or 0
    
    # Count of successful payments today
    stmt_count_today = select(func.count(Payment.payment_id)).where(
        and_(
            Payment.status == 'succeeded',
            Payment.created_at >= today_start
        )
    )
    today_count = await session.execute(stmt_count_today)
    today_payments_count = today_count.scalar() or 0

    # Package sales are already included in the totals above. Aggregate their
    # breakdown using the same UTC boundaries and frozen base-currency amounts.
    periods = {"today": today_start, "week": week_start,
               "month": month_start, "all_time": None}
    columns = []
    for period, start in periods.items():
        in_period = Payment.created_at >= start if start is not None else True
        columns.extend([
            func.count(case((in_period, Payment.payment_id))).label(f"{period}_count"),
            func.sum(case((in_period, Payment.lte_traffic_bytes), else_=0)).label(f"{period}_traffic_bytes"),
            func.sum(case((in_period, Payment.base_amount), else_=0)).label(f"{period}_revenue"),
            func.count(case((and_(in_period, Payment.base_amount.is_(None)), Payment.payment_id)))
                .label(f"{period}_unvalued_count"),
        ])
    sales_result = await session.execute(select(*columns).where(
        Payment.status == "succeeded", Payment.product_type == "lte_topup"
    ))
    sales = sales_result.mappings().one()
    lte_topups = {period: {
        "count": int(sales[f"{period}_count"] or 0),
        "traffic_bytes": int(sales[f"{period}_traffic_bytes"] or 0),
        "revenue": float(sales[f"{period}_revenue"] or 0),
        "unvalued_count": int(sales[f"{period}_unvalued_count"] or 0),
    } for period in periods}
    
    return {
        "today_revenue": float(today_amount),
        "week_revenue": float(week_amount),
        "month_revenue": float(month_amount),
        "all_time_revenue": float(all_amount),
        "today_payments_count": today_payments_count,
        "unvalued_payments": await count_unvalued_payments(session),
        "lte_topups": lte_topups,
    }


async def get_user_total_paid(session: AsyncSession, user_id: int) -> float:
    """Get total amount paid by a specific user, in the base currency."""
    stmt = select(func.sum(Payment.base_amount)).where(
        and_(
            Payment.user_id == user_id,
            Payment.status == 'succeeded'
        )
    )
    result = await session.execute(stmt)
    total = result.scalar()
    return float(total or 0)


async def get_referral_revenue(session: AsyncSession, referrer_id: int) -> float:
    """Get total revenue generated from referred users' payments.
    
    This calculates the sum of all succeeded payments made by users
    where referred_by_id equals the referrer_id.
    """
    from db.models import User
    
    stmt = select(func.sum(Payment.base_amount)).join(
        User, Payment.user_id == User.user_id
    ).where(
        and_(
            User.referred_by_id == referrer_id,
            Payment.status == 'succeeded'
        )
    )
    result = await session.execute(stmt)
    total = result.scalar()
    return float(total or 0)
