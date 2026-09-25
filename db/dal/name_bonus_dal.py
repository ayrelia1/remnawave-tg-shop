"""Database operations for Telegram first-name bonus claims."""

from datetime import datetime
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import NameBonusClaim, Payment, Subscription, User


async def lock_user(session: AsyncSession, user_id: int) -> Optional[User]:
    result = await session.execute(
        select(User).where(User.user_id == user_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def has_successful_purchase(session: AsyncSession, user_id: int) -> bool:
    result = await session.execute(
        select(Payment.payment_id).where(
            Payment.user_id == user_id,
            Payment.status == "succeeded",
            Payment.amount > 0,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_latest_claim(session: AsyncSession, user_id: int) -> Optional[NameBonusClaim]:
    result = await session.execute(
        select(NameBonusClaim)
        .where(NameBonusClaim.user_id == user_id)
        .order_by(NameBonusClaim.granted_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_claim(session: AsyncSession, claim_id: int) -> Optional[NameBonusClaim]:
    return await session.get(NameBonusClaim, claim_id)


async def refresh_claim(session: AsyncSession, claim: NameBonusClaim) -> None:
    await session.refresh(claim)


def complete_claim(claim: NameBonusClaim) -> None:
    claim.status = "completed"


def grant_bonus(
    session: AsyncSession,
    subscription: Subscription,
    user_id: int,
    granted_at: datetime,
    monitor_until: datetime,
    bonus_days: int,
    end_date: datetime,
) -> NameBonusClaim:
    subscription.end_date = end_date
    subscription.last_notification_sent = None
    claim = NameBonusClaim(
        user_id=user_id,
        subscription_id=subscription.subscription_id,
        granted_at=granted_at,
        monitor_until=monitor_until,
        bonus_days=bonus_days,
        status="active",
        panel_sync_pending=True,
        notification_pending=False,
    )
    session.add(claim)
    return claim


async def get_pending_panel_claims(session: AsyncSession, user_id: int) -> list[NameBonusClaim]:
    result = await session.execute(
        select(NameBonusClaim).where(
            NameBonusClaim.user_id == user_id,
            NameBonusClaim.panel_sync_pending.is_(True),
        )
    )
    return list(result.scalars().all())


async def get_subscription_for_panel(
    session: AsyncSession, user_id: int, panel_user_uuid: str
) -> Optional[Subscription]:
    result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.panel_user_uuid == panel_user_uuid,
            Subscription.is_active.is_(True),
        ).order_by(Subscription.end_date.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def mark_panel_synced(claims: list[NameBonusClaim]) -> None:
    for claim in claims:
        claim.panel_sync_pending = False


async def get_reclaimable_subscription(
    session: AsyncSession, user_id: int, now: datetime
) -> Optional[Subscription]:
    result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.is_active.is_(True),
            Subscription.end_date > now,
        ).order_by(Subscription.end_date.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def record_revocation(
    claim: NameBonusClaim,
    subscription: Optional[Subscription],
    now: datetime,
    new_end: Optional[datetime],
    reclaimed_seconds: int,
) -> None:
    if subscription is not None and new_end is not None:
        subscription.end_date = new_end
        subscription.last_notification_sent = None
        claim.panel_sync_pending = reclaimed_seconds > 0
    claim.status = "revoked"
    claim.revoked_at = now
    claim.reclaimed_seconds = reclaimed_seconds
    claim.notification_pending = True


def mark_notification_sent(claim: NameBonusClaim) -> None:
    claim.notification_pending = False


async def list_claim_ids_needing_checks(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(NameBonusClaim.id).where(or_(
            NameBonusClaim.status == "active",
            NameBonusClaim.panel_sync_pending.is_(True),
            NameBonusClaim.notification_pending.is_(True),
        )).order_by(NameBonusClaim.id)
    )
    return list(result.scalars().all())


async def get_bonus_statistics(session: AsyncSession, now: datetime) -> dict[str, int]:
    result = await session.execute(
        select(
            func.count(NameBonusClaim.id),
            func.count(func.distinct(NameBonusClaim.user_id)),
            func.count(NameBonusClaim.id).filter(
                NameBonusClaim.status == "active",
                NameBonusClaim.monitor_until > now,
            ),
            func.count(NameBonusClaim.id).filter(NameBonusClaim.status == "revoked"),
            func.coalesce(func.sum(NameBonusClaim.bonus_days), 0),
            func.coalesce(func.sum(NameBonusClaim.reclaimed_seconds), 0),
        )
    )
    total, users, monitoring, revoked, granted_days, reclaimed_seconds = result.one()
    return {
        "total_claims": total,
        "users": users,
        "monitoring": monitoring,
        "revoked": revoked,
        "granted_days": granted_days,
        "reclaimed_seconds": reclaimed_seconds,
    }
