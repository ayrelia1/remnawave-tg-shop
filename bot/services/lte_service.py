"""Project cumulative LTE credit into internal squad membership.

The panel remains the only traffic collector. This service never resets its
counters, changes node multipliers, or limits the whole panel account.
"""

import logging
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.constants.premium_emoji import PREMIUM_EMOJI_PACKAGE
from db.dal import lte_dal
from db.models import LteAccount, User
from .panel_api_service import PanelUnavailableError


def squad_ids(panel_user: dict) -> list[str]:
    return [item["uuid"] if isinstance(item, dict) else item
            for item in panel_user.get("activeInternalSquads", [])]


def remaining_description(remaining_bytes: int) -> str:
    gb = f"{remaining_bytes / 2**30:.2f}".rstrip("0").rstrip(".")
    return f"{gb} ГБ"


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

    async def reconcile(self, session: AsyncSession, user_id: int) -> None:
        if not self.enabled:
            return
        account = await lte_dal.lock_account(session, user_id)
        if account is None or not account.managed:
            return
        user = await session.get(User, user_id)
        if not user.panel_user_uuid:
            return
        panel_user = await self.panel.get_user_by_ref(user.panel_user_uuid)
        if not panel_user or panel_user.get("error"):
            raise RuntimeError("Cannot read panel account for LTE synchronization")
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
        if payload:
            updated = await self.panel.update_user_details_on_panel(
                user.panel_user_uuid, payload)
            if not updated or updated.get("error"):
                raise RuntimeError("Panel declined LTE synchronization")
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
        await session.flush()

    async def run_checks(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        if not self.enabled:
            return
        cursor = 0
        while True:
            async with session_factory() as session:
                ids = list((await session.execute(select(LteAccount.user_id).where(
                    LteAccount.managed.is_(True), LteAccount.user_id > cursor)
                    .order_by(LteAccount.user_id).limit(100))).scalars())
            if not ids:
                break
            for user_id in ids:
                try:
                    async with session_factory() as session:
                        await self.reconcile(session, user_id)
                        await session.commit()
                    await self._notify(session_factory, user_id)
                except PanelUnavailableError:
                    logging.warning("LTE checks deferred: panel unavailable; will retry next interval")
                    return
                except Exception:
                    logging.warning("LTE synchronization failed for user %s; will retry", user_id, exc_info=True)
            cursor = ids[-1]

    async def _notify(self, session_factory, user_id):
        if self.bot is None or self.i18n is None:
            return
        async with session_factory() as session:
            account = await lte_dal.lock_account(session, user_id)
            if not account:
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
