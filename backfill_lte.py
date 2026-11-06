"""One-time legacy LTE allowance transfer. Preview by default; --apply writes.

Run after database migrations, with purchases/subscription edits paused and
node consumption multipliers already configured. This script only reads the
panel: it never resets traffic, edits users or changes squad memberships.
"""

import argparse
import asyncio
import csv
import logging
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.services.panel_api_service import PanelApiService, index_panel_users
from bot.utils.async_utils import run_bounded
from bot.utils.date_utils import as_utc
from config.settings import Settings
from db.dal import lte_dal
from db.dal.lte_dal import GIB
from db.models import LteGrant, Payment, Subscription, User


@dataclass
class TransferResult:
    user_id: int
    status: str
    subscription_end: str = ""
    allowance_bytes: int = 0
    existing_credit_bytes: int = 0
    panel_counter: int | None = None
    detail: str = ""


def allowance_for_expiry(end_date: datetime | None, at: datetime) -> int:
    """Prorate 75 GiB per 30 days, with a 10 GiB minimum while unexpired."""
    if end_date is None:
        return 0
    remaining = as_utc(end_date) - as_utc(at)
    microseconds = max(0, (remaining.days * 86400 + remaining.seconds) * 1_000_000
                       + remaining.microseconds)
    if not microseconds:
        return 0
    amount = max(10 * GIB, 75 * GIB * microseconds // (30 * 86400 * 1_000_000))
    if amount > 2**63 - 1:
        raise ValueError("Allowance is too large")
    return amount


async def transfer_user(session: AsyncSession, panel, user_id: int,
                        at: datetime, *, apply: bool,
                        panel_users: dict[str, dict] | None = None) -> TransferResult:
    """The caller commits one user at a time; errors roll the whole user back."""
    account = await lte_dal.lock_account(session, user_id)
    user = await session.get(User, user_id)
    result = TransferResult(user_id, "preview", existing_credit_bytes=account.credited_bytes if account else 0)
    source = f"legacy_lte_v1:{user_id}"
    if (await session.execute(select(LteGrant.id).where(LteGrant.source == source))).scalar_one_or_none():
        if account and account.managed:
            result.status = "already_managed"
        else:
            result.status, result.detail = "needs_review", "Transfer was already recorded; managed was changed afterwards"
        return result
    if account and account.managed:
        any_grant = (await session.execute(select(LteGrant.id).where(
            LteGrant.user_id == user_id).limit(1))).scalar_one_or_none()
        if account.credited_bytes or account.used_bytes or account.last_panel_counter is not None or any_grant:
            result.status = "already_managed"
            return result
        # A newly imported account may already use the new managed=True default
        # but have no ledger or baseline yet. It still needs the one-time grant.
    # Paid plan/trial credits may cover the same days as the prorated grant.
    # Never guess a conversion or discard bought top-ups to resolve that overlap.
    overlap = (await session.execute(select(LteGrant.id).outerjoin(
        Payment, Payment.payment_id == LteGrant.payment_id).where(
            LteGrant.user_id == user_id,
            (Payment.product_type == "subscription") | LteGrant.source.startswith("trial:"),
        ).limit(1))).scalar_one_or_none()
    if overlap is not None or (account and (account.used_bytes or account.last_panel_counter is not None)):
        result.status, result.detail = "needs_review", "Existing plan/trial credits or recorded usage require review"
        return result

    subscription = (await session.execute(select(Subscription).where(
        Subscription.user_id == user_id, Subscription.is_active.is_(True), Subscription.end_date > at,
    ).order_by(Subscription.end_date.desc()).limit(1))).scalar_one_or_none()
    if subscription:
        result.subscription_end = as_utc(subscription.end_date).isoformat()
        result.allowance_bytes = allowance_for_expiry(subscription.end_date, at)
    panel_ref = user.panel_user_uuid
    if subscription and (not panel_ref or subscription.panel_user_uuid != panel_ref):
        result.status, result.detail = "needs_review", "Subscription and user panel references must be synchronized first"
        return result
    if panel_ref:
        panel_user = (await panel.get_user_by_ref(panel_ref) if panel_users is None
                      else panel_users.get(str(panel_ref)))
        if not panel_user or panel_user.get("error"):
            result.status, result.detail = "error", "Cannot read the panel user; no changes applied"
            return result
        counter = (panel_user.get("userTraffic") or {}).get("lifetimeUsedTrafficBytes")
        if isinstance(counter, bool) or not isinstance(counter, int) or not 0 <= counter <= 2**63 - 1:
            result.status, result.detail = "error", "Invalid panel counter; no changes applied"
            return result
        result.panel_counter = counter

    if apply:
        account = await lte_dal.credit(session, user_id, result.allowance_bytes, source)
        # Existing top-ups and manual credits remain intact. Historical panel
        # traffic is only a baseline and must never be copied into used_bytes.
        account.last_panel_counter = result.panel_counter
        account.panel_user_ref = str(panel_ref) if panel_ref else None
        account.managed = True
        account.access_enabled = None
        account.access_sync_pending = True
        await session.flush()
        result.status = "applied"
    return result


async def backfill_users(factory, panel, *, apply: bool, emit) -> Counter:
    counts = Counter()
    at = datetime.now(timezone.utc)
    cursor = None
    try:
        users = await panel.get_all_panel_users()
        if users is None:
            raise ValueError("Incomplete panel snapshot")
        panel_users = index_panel_users(users)
    except Exception:
        emit(TransferResult(0, "error", detail="Cannot read complete panel snapshot; no changes applied"))
        counts["error"] += 1
        return counts

    async def transfer(user_id):
        try:
            async with factory() as session:
                result = await transfer_user(session, panel, user_id, at, apply=apply,
                                             panel_users=panel_users)
                if apply and result.status == "applied":
                    await session.commit()
                else:
                    await session.rollback()
        except Exception as exc:
            # SQL/HTTP exception strings can contain credentials or private
            # response data. Emit only the class, never a raw traceback.
            result = TransferResult(user_id, "error", detail=f"Rolled back ({type(exc).__name__})")
        # Report immediately after each user's transaction, not after the full
        # batch: interruption must not hide already committed work in memory.
        emit(result)
        counts[result.status] += 1

    while True:
        async with factory() as session:
            query = select(User.user_id).order_by(User.user_id).limit(1000)
            if cursor is not None:
                query = query.where(User.user_id > cursor)
            ids = list((await session.execute(query)).scalars())
        if not ids:
            return counts
        await run_bounded(ids, transfer)
        cursor = ids[-1]


async def run(args, emit) -> int:
    settings = Settings(_env_file=Path(__file__).resolve().parent / ".env")
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        if not {"users", "subscriptions", "payments", "lte_accounts", "lte_grants"} <= tables:
            print("Apply database migrations before running this script.", file=sys.stderr)
            return 1
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with PanelApiService(settings) as panel:
            counts = await backfill_users(factory, panel, apply=args.apply, emit=emit)
        print(f"{'APPLY' if args.apply else 'PREVIEW'}: " + ", ".join(
            f"{key}={value}" for key, value in sorted(counts.items())), file=sys.stderr)
        return 1 if counts["error"] or counts["needs_review"] else 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write allowances and enable managed accounts")
    parser.add_argument("--report", type=Path, help="Create a CSV report; existing files are never overwritten")
    args = parser.parse_args()
    # The CLI reports safe per-user outcomes, not panel payloads or DB URLs.
    logging.basicConfig(level=logging.CRITICAL)
    with ExitStack() as stack:
        output = stack.enter_context(args.report.open("x", encoding="utf-8", newline="")) if args.report else sys.stdout
        writer = csv.DictWriter(output, fieldnames=list(TransferResult.__dataclass_fields__))
        writer.writeheader()

        def emit(result):
            writer.writerow(asdict(result))
            output.flush()

        try:
            return asyncio.run(run(args, emit))
        except KeyboardInterrupt:
            print("Stopped. Committed users are retained; rerun to continue.", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"Stopped ({type(exc).__name__}). Committed users are retained; rerun to continue.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
