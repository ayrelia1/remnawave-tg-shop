"""Project cumulative LTE credit into internal squad membership.

The panel remains the only traffic collector. This service never resets its
counters, changes node multipliers, or limits the whole panel account.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.constants.premium_emoji import PREMIUM_EMOJI_PACKAGE
from bot.utils.async_utils import run_bounded
from db.dal import lte_dal
from db.models import LteAccount, Subscription, User
from .panel_api_service import PanelUnavailableError, index_panel_users


def squad_ids(panel_user: dict) -> list[str]:
    return [item["uuid"] if isinstance(item, dict) else item
            for item in panel_user.get("activeInternalSquads", [])]


def remaining_description(remaining_bytes: int) -> str:
    gb = f"{remaining_bytes / 2**30:.2f}".rstrip("0").rstrip(".")
    return f"{gb} ГБ"


def account_state(account: LteAccount) -> tuple:
    return tuple(getattr(account, column.key) for column in LteAccount.__table__.columns)


def subscription_state_query():
    # Bonus days can change expiry without changing the wallet. Include them in
    # the snapshot guard, so an old EXPIRED snapshot cannot undo a renewal.
    return select(Subscription.user_id, Subscription.subscription_id,
                  Subscription.panel_user_uuid, Subscription.end_date,
                  Subscription.is_active, Subscription.status_from_panel).order_by(
                      Subscription.user_id, Subscription.subscription_id)


@dataclass
class CheckSnapshot:
    user: User
    account: LteAccount
    account_state: tuple
    subscriptions: tuple


@dataclass
class CheckStats:
    users: int = 0
    changed: int = 0
    synced: int = 0
    patch_attempts: int = 0
    access_read_attempts: int = 0
    deferred: int = 0
    missing: int = 0
    errors: int = 0
    pending_notices: int = 0


class LteService:
    def __init__(self, settings, panel_service, bot=None, i18n=None):
        self.settings = settings
        self.panel = panel_service
        self.bot = bot
        self.i18n = i18n

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "LTE_ENFORCEMENT_ENABLED", False)
                    and self.settings.parsed_lte_squad_uuids)

    async def refresh_balance(self, session: AsyncSession, user_id: int) -> LteAccount | None:
        """Read current usage for an admin balance check, without editing squads."""
        account = await lte_dal.lock_account(session, user_id)
        if account is None or not account.managed or not self.enabled:
            return account
        user = await session.get(User, user_id)
        if not user.panel_user_uuid:
            return account
        panel_user = await self.panel.get_user_by_ref(user.panel_user_uuid)
        if not panel_user or panel_user.get("error"):
            raise RuntimeError("Cannot refresh LTE balance from panel")
        before = account.remaining_bytes
        try:
            lte_dal.observe_counter(account, str(user.panel_user_uuid),
                                    (panel_user.get("userTraffic") or {}).get("lifetimeUsedTrafficBytes"))
        except ValueError as exc:
            raise RuntimeError("Panel returned invalid LTE traffic statistics") from exc
        if before > 0 and account.remaining_bytes == 0:
            account.access_sync_pending = True
        lte_dal.rearm_low_notice(account)
        # Production sessions disable autoflush. Persist observed usage before
        # another lock_account(populate_existing=True) can refresh this row.
        await session.flush()
        return account

    async def adjust_balance(self, session: AsyncSession, user_id: int, delta: int, source: str):
        await lte_dal.lock_account(session, user_id, create=True)
        await self.refresh_balance(session, user_id)
        account, changed = await lte_dal.adjust_balance(session, user_id, delta, source)
        if self.enabled and account.managed:
            lte_dal.rearm_low_notice(account)
            await session.flush()
        return account, changed

    async def project_payload(self, session, user_id, panel_ref, payload, panel_user=None):
        lte_squads = self.settings.parsed_lte_squad_uuids
        ordinary_squads = [s for s in (self.settings.parsed_user_squad_uuids or [])
                           if s not in lte_squads]
        account = await lte_dal.lock_account(session, user_id)
        enforce_lte = self.enabled and account is not None and account.managed
        if not ordinary_squads and not enforce_lte:
            return payload
        panel_user = panel_user or await self.panel.get_user_by_ref(panel_ref)
        if not panel_user or panel_user.get("error"):
            raise RuntimeError("Cannot read panel account for LTE synchronization")
        current = squad_ids(panel_user)
        if not enforce_lte:
            # Renewals must restore ordinary access even while LTE is paused or
            # exempt. Preserve existing memberships, including unmanaged LTE.
            desired = list(dict.fromkeys(current + ordinary_squads))
            if set(current) != set(desired):
                payload["activeInternalSquads"] = desired
            return payload
        counter = (panel_user.get("userTraffic") or {}).get("lifetimeUsedTrafficBytes")
        lte_dal.observe_counter(account, str(panel_ref), counter)
        lte_dal.rearm_low_notice(account)
        user = await session.get(User, user_id)
        allowed = account.remaining_bytes > 0 and not user.is_banned
        desired = list(dict.fromkeys(
            [s for s in current if s not in lte_squads]
            + ordinary_squads
            + (lte_squads if allowed else [])))
        payload["activeInternalSquads"] = desired
        payload["trafficLimitBytes"] = 0
        payload["trafficLimitStrategy"] = "NO_RESET"
        description = remaining_description(account.remaining_bytes)
        if panel_user.get("description") != description:
            payload["description"] = description
        account.access_sync_pending = True
        return payload

    async def reconcile(self, session: AsyncSession, user_id: int, *,
                        panel_user: dict | None = None,
                        snapshot: CheckSnapshot | None = None,
                        stats: CheckStats | None = None) -> bool:
        if not self.enabled:
            return False
        account = await lte_dal.lock_account(session, user_id)
        if account is None or not account.managed:
            return False
        user = await session.get(User, user_id)
        if not user.panel_user_uuid:
            return False
        if snapshot is not None:
            current_subscriptions = tuple(await session.execute(
                subscription_state_query().where(Subscription.user_id == user_id)))
            if (account_state(account) != snapshot.account_state
                    or user.panel_user_uuid != snapshot.user.panel_user_uuid
                    or user.is_banned != snapshot.user.is_banned
                    or current_subscriptions != snapshot.subscriptions):
                # Do not apply an old snapshot after a purchase/admin action.
                # The next bulk cycle will catch up without another panel GET.
                return False
        if panel_user is None:
            panel_user = await self.panel.get_user_by_ref(user.panel_user_uuid)
        if not panel_user or panel_user.get("error"):
            raise RuntimeError("Cannot read panel account for LTE synchronization")
        payload = self._project_check(account, user, panel_user)
        if snapshot is not None and "activeInternalSquads" in payload:
            # Membership PATCH replaces the entire list. A bulk snapshot can
            # predate panel-admin edits, so refresh only actual access changes.
            if stats is not None:
                stats.access_read_attempts += 1
            panel_user = await self.panel.get_user_by_ref(user.panel_user_uuid)
            if not panel_user or panel_user.get("error"):
                raise RuntimeError("Cannot refresh panel account for LTE access update")
            payload = self._project_check(account, user, panel_user)
        if payload:
            if stats is not None:
                stats.patch_attempts += 1
            updated = await self.panel.update_user_details_on_panel(
                user.panel_user_uuid, payload)
            if not updated or updated.get("error"):
                raise RuntimeError("Panel declined LTE synchronization")
        await session.flush()
        return True

    def _project_check(self, account: LteAccount, user: User, panel_user: dict) -> dict:
        """Apply the same rules to detached snapshots and locked DB accounts."""
        counter = (panel_user.get("userTraffic") or {}).get("lifetimeUsedTrafficBytes")
        lte_dal.observe_counter(account, str(user.panel_user_uuid), counter)
        expiry = datetime.fromisoformat(panel_user["expireAt"].replace("Z", "+00:00"))
        active = (not user.is_banned and panel_user.get("status") == "ACTIVE"
                  and expiry > datetime.now(timezone.utc))
        allowed = account.remaining_bytes > 0 and active
        current = squad_ids(panel_user)
        lte_squads = self.settings.parsed_lte_squad_uuids
        desired = list(dict.fromkeys([s for s in current if s not in lte_squads]
                                     + (lte_squads if allowed else [])))
        changed = set(current) != set(desired)
        payload = {}
        if changed:
            payload["activeInternalSquads"] = desired
        description = remaining_description(account.remaining_bytes)
        if panel_user.get("description") != description:
            payload["description"] = description
        account.access_sync_pending = False
        account.access_enabled = allowed
        if active and account.remaining_bytes == 0 and not account.depleted_notified:
            account.notification_pending = True
        if account.remaining_bytes > 0:
            account.depleted_notified = False
            account.notification_pending = False
        if not active:
            account.notification_pending = False
            account.low_notification_pending = False
        elif account.remaining_bytes == 0:
            # An exhausted account gets the depletion notice, never both notices.
            account.low_notification_pending = False
        elif account.remaining_bytes <= lte_dal.LOW_BALANCE_BYTES and not account.low_notified:
            account.low_notification_pending = True
        lte_dal.rearm_low_notice(account)
        return payload

    async def run_checks(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        if not self.enabled:
            return
        started = monotonic()
        stats = CheckStats()
        status = "failed"
        try:
            status = await self._run_checks(session_factory, stats)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            stats.errors += 1
            raise
        finally:
            # Include DB work, panel reads/writes, notification delivery and
            # cleanup, even when a cycle exits early or is cancelled.
            logging.info(
                "LTE checks: status=%s duration=%.2fs users=%s changed=%s synced=%s "
                "patch_attempts=%s access_read_attempts=%s deferred=%s missing=%s "
                "errors=%s pending_notices=%s",
                status, monotonic() - started, stats.users, stats.changed, stats.synced,
                stats.patch_attempts, stats.access_read_attempts, stats.deferred,
                stats.missing, stats.errors, stats.pending_notices)

    async def _run_checks(self, session_factory: async_sessionmaker[AsyncSession],
                          stats: CheckStats) -> str:
        # Read DB state BEFORE the panel snapshot. Only changed rows need a
        # transaction; a guarded reconciliation serializes them with purchases.
        async with session_factory() as session:
            rows = list(await session.execute(select(User, LteAccount).join(
                LteAccount, LteAccount.user_id == User.user_id).where(
                    LteAccount.managed.is_(True), User.panel_user_uuid.is_not(None))))
            stats.users = len(rows)
            if not rows:
                return "empty"
            subscriptions: dict[int, list[tuple]] = {}
            for row in await session.execute(subscription_state_query().join(
                    LteAccount, LteAccount.user_id == Subscription.user_id).where(
                        LteAccount.managed.is_(True))):
                subscriptions.setdefault(row.user_id, []).append(tuple(row))
            snapshots = [CheckSnapshot(user, account, account_state(account),
                                       tuple(subscriptions.get(user.user_id, [])))
                         for user, account in rows]
        try:
            users = await self.panel.get_all_panel_users()
            if users is None:
                raise RuntimeError("Incomplete panel snapshot")
            indexed = index_panel_users(users)
        except Exception:
            stats.errors += 1
            logging.warning("LTE checks deferred: cannot read a complete panel snapshot")
            return "panel_error"

        changed = []
        ready_to_notify: set[int] = set()
        for snapshot in snapshots:
            panel_user = indexed.get(str(snapshot.user.panel_user_uuid))
            if panel_user is None:
                # A deleted/unlinked panel user is not a zero traffic counter.
                stats.missing += 1
                continue
            try:
                payload = self._project_check(snapshot.account, snapshot.user, panel_user)
                if payload or account_state(snapshot.account) != snapshot.account_state:
                    changed.append((snapshot, panel_user))
                else:
                    ready_to_notify.add(snapshot.user.user_id)
            except Exception:
                stats.errors += 1
                logging.warning("Invalid LTE snapshot for user %s; will retry", snapshot.user.user_id)

        stats.changed = len(changed)
        unavailable = False

        async def update(item):
            nonlocal unavailable
            if unavailable:
                stats.deferred += 1
                return
            snapshot, panel_user = item
            user_id = snapshot.user.user_id
            try:
                async with session_factory() as session:
                    reconciled = await self.reconcile(
                        session, user_id, panel_user=panel_user, snapshot=snapshot, stats=stats)
                    if not reconciled:
                        stats.deferred += 1
                        return
                    await session.commit()
                stats.synced += 1
                ready_to_notify.add(user_id)
            except PanelUnavailableError:
                stats.errors += 1
                unavailable = True
                logging.warning("LTE updates deferred: panel unavailable; will retry next interval")
            except Exception:
                stats.errors += 1
                logging.warning("LTE synchronization failed for user %s; will retry", user_id, exc_info=True)

        await run_bounded(changed, update)
        # Successful sends clear durable pending flags. Unchanged users do not
        # need a per-user notification transaction on every polling cycle.
        async with session_factory() as session:
            pending = list((await session.execute(select(LteAccount.user_id).join(
                User, User.user_id == LteAccount.user_id).where(
                    LteAccount.managed.is_(True), User.is_banned.is_(False),
                    or_(LteAccount.notification_pending.is_(True),
                        LteAccount.low_notification_pending.is_(True)))
                .order_by(LteAccount.user_id))).scalars())
        stats.pending_notices = len(pending)
        for user_id in pending:
            if user_id not in ready_to_notify:
                continue
            try:
                await self._notify(session_factory, user_id)
            except Exception:
                stats.errors += 1
                logging.warning("LTE notification failed for user %s; will retry", user_id, exc_info=True)
        return "partial" if stats.errors or stats.deferred or stats.missing else "ok"

    async def _notify(self, session_factory, user_id):
        if self.bot is None or self.i18n is None:
            return
        async with session_factory() as session:
            account = await lte_dal.lock_account(session, user_id)
            if not account or not account.managed:
                return
            user = await session.get(User, user_id)
            if user.is_banned:
                return
            depleted = account.remaining_bytes == 0 and account.notification_pending
            low = (0 < account.remaining_bytes <= lte_dal.LOW_BALANCE_BYTES
                   and account.low_notification_pending and not account.low_notified)
            if not depleted and not low:
                return
            lang = user.language_code or self.settings.DEFAULT_LANGUAGE
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=self.i18n.gettext(lang, "lte_topup_button"), callback_data="lte:packages",
                icon_custom_emoji_id=PREMIUM_EMOJI_PACKAGE)]])
            key = "lte_depleted_message" if depleted else "lte_low_balance_message"
            await self.bot.send_message(user_id, self.i18n.gettext(
                lang, key, gb=f"{account.remaining_bytes / 2**30:.2f}"), reply_markup=markup)
            if depleted:
                account.notification_pending = False
                account.depleted_notified = True
            else:
                account.low_notification_pending = False
                account.low_notified = True
            await session.commit()
