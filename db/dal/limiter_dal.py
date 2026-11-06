"""Queries for the durable limiter inbox; panel references own warning windows."""

from datetime import datetime

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from db.models import LimiterViolation

TERMINAL_STAGES = ("done", "ignored")
ACTION_STAGES = ("pending", "resetting", "disabling")


async def find_event(session: AsyncSession, event_hash: str) -> LimiterViolation | None:
    return await session.scalar(select(LimiterViolation).where(LimiterViolation.event_hash == event_hash))


async def latest_counted(session: AsyncSession, event: LimiterViolation) -> LimiterViolation | None:
    return await session.scalar(select(LimiterViolation).where(
        LimiterViolation.panel_user_ref == event.panel_user_ref,
        LimiterViolation.id < event.id,
        LimiterViolation.warning_number.is_not(None),
        LimiterViolation.stage != "ignored",
    ).order_by(LimiterViolation.id.desc()).limit(1))


async def ready_ids(session: AsyncSession, now: datetime, limit: int = 100,
                    *, event_hash: str | None = None) -> list[int]:
    # Actions stay ordered even during backoff. A failed notification must not
    # prevent the next reset/disable once the earlier action is already applied.
    earlier = aliased(LimiterViolation)
    blocked = exists(select(earlier.id).where(
        earlier.panel_user_ref == LimiterViolation.panel_user_ref,
        earlier.id < LimiterViolation.id,
        earlier.stage.in_(ACTION_STAGES),
    ))
    statement = select(LimiterViolation.id).where(
        LimiterViolation.stage.not_in(TERMINAL_STAGES),
        ~blocked,
    )
    if event_hash is None:
        statement = statement.where(LimiterViolation.next_attempt_at <= now)
    else:
        # A signed redelivery may resume its own failed event immediately;
        # scheduled retries still respect backoff and account action ordering.
        statement = statement.where(LimiterViolation.event_hash == event_hash)
    return list((await session.scalars(statement.order_by(LimiterViolation.id).limit(limit))).all())


async def has_later_action(session: AsyncSession, event: LimiterViolation) -> bool:
    return bool(await session.scalar(select(exists().where(
        LimiterViolation.panel_user_ref == event.panel_user_ref,
        LimiterViolation.id > event.id,
        LimiterViolation.stage.in_(("applied", "done")),
    ))))


async def prune(session: AsyncSession, before: datetime) -> None:
    pending = aliased(LimiterViolation)
    needed_by_pending = exists(select(pending.id).where(
        pending.panel_user_ref == LimiterViolation.panel_user_ref,
        pending.stage.not_in(TERMINAL_STAGES),
        pending.received_at >= LimiterViolation.window_started_at,
        pending.received_at < LimiterViolation.window_expires_at,
    ))
    await session.execute(delete(LimiterViolation).where(
        LimiterViolation.stage.in_(TERMINAL_STAGES),
        LimiterViolation.received_at < before,
        ~needed_by_pending,
    ))
