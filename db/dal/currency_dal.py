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
    updated_by: Optional[int] = None,
) -> CurrencyRate:
    code = normalize(currency)
    if not code:
        raise ValueError("currency_rate_invalid_code")
    if rate is None or float(rate) <= 0:
        raise ValueError("currency_rate_invalid_rate")

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


async def delete_rate(session: AsyncSession, currency: str) -> bool:
    code = normalize(currency)
    result = await session.execute(
        delete(CurrencyRate).where(CurrencyRate.currency == code)
    )
    return (result.rowcount or 0) > 0
