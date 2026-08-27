"""Conversion rates into the base currency, managed from the admin panel.

The rate a payment was valued at is frozen on the payment itself, so editing a
rate here never revalues anything already recorded — it only affects payments
made from that point on, plus payments that were never valued at all because
their currency had no rate yet.
"""

import logging
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CurrencyRate


def normalize(currency: Optional[str]) -> str:
    return (currency or "").strip().upper()


async def get_rate(session: AsyncSession, currency: str) -> Optional[float]:
    """Rate for `currency`, or None when it has never been configured.

    None rather than 1.0 on purpose: an unknown currency must be surfaced, not
    silently counted at face value.
    """
    code = normalize(currency)
    if not code:
        return None
    row = await session.get(CurrencyRate, code)
    return float(row.rate) if row else None


async def get_rates(session: AsyncSession) -> Dict[str, float]:
    rows = (await session.execute(select(CurrencyRate))).scalars().all()
    return {row.currency: float(row.rate) for row in rows}


async def list_rates(session: AsyncSession) -> List[CurrencyRate]:
    stmt = select(CurrencyRate).order_by(CurrencyRate.currency.asc())
    return (await session.execute(stmt)).scalars().all()


async def set_rate(
    session: AsyncSession,
    currency: str,
    rate: float,
    *,
    base_currency: Optional[str] = None,
    updated_by: Optional[int] = None,
) -> CurrencyRate:
    """Write a rate, refusing anything that would break the base currency.

    Every other rate is expressed *in* the base currency, so its own rate is an
    identity — 1.0 by definition. Setting it to anything else would silently
    rescale every future payment in it, which is why this is refused here rather
    than only in the UI.
    """
    code = normalize(currency)
    if not code:
        raise ValueError("currency_rate_invalid_code")
    if rate is None or float(rate) <= 0:
        raise ValueError("currency_rate_invalid_rate")
    if base_currency and code == normalize(base_currency) and float(rate) != 1.0:
        raise ValueError("currency_rate_base_is_fixed")

    existing = await session.get(CurrencyRate, code)
    if existing:
        existing.rate = float(rate)
        existing.updated_by = updated_by
        existing.updated_at = func.now()
        row = existing
    else:
        row = CurrencyRate(currency=code, rate=float(rate), updated_by=updated_by)
        session.add(row)

    await session.flush()
    logging.info("Currency rate set: %s = %s (by %s)", code, rate, updated_by)
    return row


async def delete_rate(
    session: AsyncSession, currency: str, *, base_currency: Optional[str] = None
) -> bool:
    code = normalize(currency)
    if base_currency and code == normalize(base_currency):
        raise ValueError("currency_rate_base_is_fixed")
    result = await session.execute(
        delete(CurrencyRate).where(CurrencyRate.currency == code)
    )
    return (result.rowcount or 0) > 0


async def ensure_base_rate(session: AsyncSession, base_currency: str) -> CurrencyRate:
    """Guarantee the base currency exists at exactly 1.0."""
    code = normalize(base_currency)
    row = await session.get(CurrencyRate, code)
    if row is None:
        row = CurrencyRate(currency=code, rate=1.0)
        session.add(row)
        await session.flush()
    elif float(row.rate) != 1.0:
        logging.error(
            "Base currency %s had rate %s; forcing it back to 1.0.", code, row.rate
        )
        row.rate = 1.0
        await session.flush()
    return row
