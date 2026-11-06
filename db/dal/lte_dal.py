"""Cumulative credit and monotonic charging, independent of subscription rows."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import LteAccount, LteGrant, User
from config.traffic import GIB, traffic_bytes

LOW_BALANCE_BYTES = 5 * GIB


class InsufficientLteBalance(ValueError):
    """Manual debits must not exceed the freshly observed remaining balance."""


def reset_depletion_notice(account: LteAccount) -> None:
    if account.remaining_bytes > 0:
        account.depleted_notified = False
        account.notification_pending = False


def rearm_low_notice(account: LteAccount) -> None:
    # Call only after observing current usage. Replenishing a stale balance may
    # appear to cross 5 GiB even when actual remaining credit never crossed it.
    if account.remaining_bytes > LOW_BALANCE_BYTES:
        account.low_notified = False
        account.low_notification_pending = False


async def lock_account(session: AsyncSession, user_id: int, *, create: bool = False) -> LteAccount | None:
    # Lock the parent first: two simultaneous first purchases must not race to
    # create an account. The same order is used by the scheduler and renewals.
    user = (await session.execute(select(User).where(User.user_id == user_id)
                                  .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if user is None:
        raise ValueError("User does not exist")
    account = (await session.execute(select(LteAccount).where(LteAccount.user_id == user_id)
                                     .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if account is None and create:
        account = LteAccount(user_id=user_id, managed=True, credited_bytes=0, used_bytes=0)
        session.add(account)
        await session.flush()
    return account


async def credit(session: AsyncSession, user_id: int, amount: int, source: str,
                 payment_id: int | None = None) -> LteAccount:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("Invalid LTE credit")
    account = await lock_account(session, user_id, create=True)
    existing = (await session.execute(select(LteGrant.id).where(LteGrant.source == source))).scalar_one_or_none()
    if existing is not None:
        return account
    if account.credited_bytes + amount > 2**63 - 1:
        raise ValueError("LTE balance is too large")
    session.add(LteGrant(user_id=user_id, source=source, payment_id=payment_id, traffic_bytes=amount))
    account.credited_bytes += amount
    account.access_sync_pending = True
    reset_depletion_notice(account)
    await session.flush()
    return account


async def adjust_balance(session: AsyncSession, user_id: int, delta: int,
                         source: str) -> tuple[LteAccount, bool]:
    """Record one signed admin adjustment; credited_bytes is net ledger credit.

    The source embeds the actor and a stable operation token. The user lock
    serializes adjustments with purchases, traffic observations and backfills.
    """
    if (isinstance(delta, bool) or not isinstance(delta, int) or delta == 0
            or abs(delta) > 2**63 - 1 or not source.startswith("admin:") or len(source) > 80):
        raise ValueError("Invalid manual LTE adjustment")
    account = await lock_account(session, user_id, create=True)
    previous = (await session.execute(select(LteGrant).where(LteGrant.source == source))).scalar_one_or_none()
    if previous:
        if previous.user_id != user_id or previous.traffic_bytes != delta:
            raise ValueError("Adjustment token was already used for different terms")
        return account, False
    if delta < 0 and -delta > account.remaining_bytes:
        raise InsufficientLteBalance("Debit exceeds remaining LTE balance")
    if account.credited_bytes + delta > 2**63 - 1:
        raise ValueError("LTE balance is too large")
    session.add(LteGrant(user_id=user_id, source=source, traffic_bytes=delta))
    account.credited_bytes += delta
    account.access_sync_pending = True
    reset_depletion_notice(account)
    await session.flush()
    return account, True


def observe_counter(account: LteAccount, panel_ref: str, counter: int) -> None:
    if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
        raise ValueError("Invalid panel traffic counter")
    if account.panel_user_ref == str(panel_ref) and account.last_panel_counter is not None:
        delta = counter - account.last_panel_counter
        # A reset/recreated counter starts a new epoch; previously charged
        # traffic is never refunded by a remote counter reset.
        account.used_bytes += delta if delta >= 0 else counter
    account.last_panel_counter = counter
    account.panel_user_ref = str(panel_ref)


async def remaining(session: AsyncSession, user_id: int) -> int | None:
    account = await session.get(LteAccount, user_id)
    return account.remaining_bytes if account and account.managed else None
