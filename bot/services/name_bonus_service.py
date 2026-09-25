"""Claim and monitor the Telegram first-name bonus for previous buyers."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from db.dal import name_bonus_dal, subscription_dal, user_dal
from bot.services.subscription_service import SubscriptionService


BONUS_DAYS = 7
COOLDOWN_DAYS = 30
NAME_TAG = "@MansurVPN_BOT"
NAME_PHRASE = "Mansur VPN"


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def matches_name(first_name: str | None) -> bool:
    if not first_name:
        return False
    return NAME_TAG in first_name or NAME_PHRASE in first_name


@dataclass(frozen=True)
class ClaimResult:
    status: str
    end_date: datetime | None = None
    next_at: datetime | None = None
    panel_synced: bool = False


class NameBonusService:
    def __init__(self, settings, bot, panel_service, i18n=None):
        self.settings = settings
        self.bot = bot
        self.panel_service = panel_service
        self.i18n = i18n

    async def claim(self, session: AsyncSession, user_id: int) -> ClaimResult:
        if not self.settings.NAME_BONUS_ENABLED:
            return ClaimResult("disabled")

        try:
            chat = await self.bot.get_chat(user_id)
        except TelegramAPIError:
            logging.warning("Could not check Telegram name for bonus claim by %s", user_id, exc_info=True)
            return ClaimResult("telegram_error")
        if not matches_name(getattr(chat, "first_name", None)):
            return ClaimResult("name_missing")

        # Serialize claims from concurrent bot workers using the existing user row.
        user = await name_bonus_dal.lock_user(session, user_id)
        if user is None:
            return ClaimResult("not_paid")

        now = datetime.now(timezone.utc)
        if not await name_bonus_dal.has_successful_purchase(session, user_id):
            return ClaimResult("not_paid")

        latest = await name_bonus_dal.get_latest_claim(session, user_id)
        if latest is not None:
            next_at = as_utc(latest.granted_at) + timedelta(days=COOLDOWN_DAYS)
            if now < next_at:
                return ClaimResult("cooldown", next_at=next_at)
            if latest.status == "active" and as_utc(latest.monitor_until) <= now:
                name_bonus_dal.complete_claim(latest)

        subscription = await subscription_dal.get_active_subscription_by_user_id(
            session, user_id, user.panel_user_uuid
        )
        if subscription is None:
            subscription_service = SubscriptionService(self.settings, self.panel_service, self.bot, self.i18n)
            try:
                panel_user_uuid, panel_subscription_uuid, _, _ = (
                    await subscription_service._get_or_create_panel_user_link_details(session, user_id, user)
                )
            except Exception:
                logging.warning("Could not prepare bonus subscription for user %s", user_id, exc_info=True)
                await session.rollback()
                return ClaimResult("panel_error")
            if not panel_user_uuid or not panel_subscription_uuid:
                await session.rollback()
                return ClaimResult("panel_error")
            # Resolving the panel may have repaired an old local panel reference.
            # Re-query before creating a bonus-only subscription so paid time survives.
            subscription = await subscription_dal.get_active_subscription_by_user_id(
                session, user_id, panel_user_uuid
            )
            if subscription is None:
                await subscription_dal.deactivate_other_active_subscriptions(
                    session, panel_user_uuid, panel_subscription_uuid
                )
                subscription = await subscription_dal.upsert_subscription(session, {
                    "user_id": user_id,
                    "panel_user_uuid": panel_user_uuid,
                    "panel_subscription_uuid": panel_subscription_uuid,
                    "start_date": now,
                    "end_date": now,
                    "duration_months": 0,
                    "is_active": True,
                    "status_from_panel": "ACTIVE_BONUS",
                    "traffic_limit_bytes": self.settings.user_traffic_limit_bytes,
                    "auto_renew_enabled": False,
                    "hwid_device_limit": self.settings.base_device_limit,
                })

        end_date = as_utc(subscription.end_date) + timedelta(days=BONUS_DAYS)
        name_bonus_dal.grant_bonus(
            session, subscription, user_id, now,
            now + timedelta(days=BONUS_DAYS), BONUS_DAYS, end_date,
        )
        await session.commit()

        panel_synced = await self.sync_panel_for_user(session, user_id)
        return ClaimResult("granted", end_date=end_date, panel_synced=panel_synced)

    async def sync_panel_for_user(self, session: AsyncSession, user_id: int) -> bool:
        """Retryable projection of the current local subscription expiry to the panel."""
        user = await name_bonus_dal.lock_user(session, user_id)
        if user is None or not user.panel_user_uuid:
            return False
        pending = await name_bonus_dal.get_pending_panel_claims(session, user_id)
        if not pending:
            return True
        subscription = await name_bonus_dal.get_subscription_for_panel(
            session, user_id, user.panel_user_uuid
        )
        if subscription is None:
            return False
        expiry = as_utc(subscription.end_date)
        payload = {
            "expireAt": expiry.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "status": "ACTIVE" if expiry > datetime.now(timezone.utc) else "DISABLED",
        }
        if subscription.traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = subscription.traffic_limit_bytes
            payload["trafficLimitStrategy"] = self.settings.USER_TRAFFIC_STRATEGY
        if subscription.hwid_device_limit is not None:
            payload["hwidDeviceLimit"] = subscription.hwid_device_limit
        try:
            updated = await self.panel_service.update_user_details_on_panel(
                user.panel_user_uuid, payload,
            )
        except Exception:
            logging.warning("Could not sync name bonus expiry for user %s", user_id, exc_info=True)
            return False
        if not updated or updated.get("error"):
            logging.warning("Panel declined name bonus expiry sync for user %s", user_id)
            return False
        name_bonus_dal.mark_panel_synced(pending)
        await session.commit()
        return True

    async def check_claim(self, session: AsyncSession, claim_id: int) -> None:
        claim = await name_bonus_dal.get_claim(session, claim_id)
        if claim is None:
            return
        user_id = claim.user_id
        now = datetime.now(timezone.utc)

        if claim.status == "active":
            if now >= as_utc(claim.monitor_until):
                name_bonus_dal.complete_claim(claim)
                await session.commit()
            else:
                try:
                    chat = await self.bot.get_chat(user_id)
                except TelegramAPIError:
                    logging.warning("Could not monitor Telegram name for user %s", user_id, exc_info=True)
                else:
                    first_name = getattr(chat, "first_name", None)
                    if first_name is None:
                        logging.warning("Telegram returned no first name while checking bonus for %s", user_id)
                    elif not matches_name(first_name):
                        await self.revoke_remaining(session, claim_id)
                        return

        if await self.sync_panel_for_user(session, user_id):
            await self.send_pending_notification(session, claim_id)

    async def revoke_remaining(self, session: AsyncSession, claim_id: int) -> None:
        claim = await name_bonus_dal.get_claim(session, claim_id)
        if claim is None:
            return
        # The user lock serializes revocation with a new claim and panel sync.
        await name_bonus_dal.lock_user(session, claim.user_id)
        await name_bonus_dal.refresh_claim(session, claim)
        if claim.status != "active":
            return
        now = datetime.now(timezone.utc)
        remaining = max(timedelta(), as_utc(claim.monitor_until) - now)
        subscription = await name_bonus_dal.get_reclaimable_subscription(session, claim.user_id, now)
        reclaimed = timedelta()
        new_end = None
        if subscription is not None and remaining:
            current_end = as_utc(subscription.end_date)
            new_end = max(now, current_end - remaining)
            reclaimed = current_end - new_end
        name_bonus_dal.record_revocation(
            claim, subscription, now, new_end, ceil(reclaimed.total_seconds())
        )
        await session.commit()
        if await self.sync_panel_for_user(session, claim.user_id):
            await self.send_pending_notification(session, claim_id)

    async def send_pending_notification(self, session: AsyncSession, claim_id: int) -> None:
        claim = await name_bonus_dal.get_claim(session, claim_id)
        if claim is None or not claim.notification_pending:
            return
        user = await user_dal.get_user_by_id(session, claim.user_id)
        lang = user.language_code if user and user.language_code else self.settings.DEFAULT_LANGUAGE
        message = (
            self.i18n.gettext(lang, "name_bonus_revoked")
            if self.i18n else "Your remaining name bonus has been revoked."
        )
        try:
            await self.bot.send_message(claim.user_id, message)
        except TelegramAPIError:
            logging.warning("Could not notify user %s of name bonus revocation", claim.user_id, exc_info=True)
            return
        name_bonus_dal.mark_notification_sent(claim)
        await session.commit()

    async def run_checks(self, session_factory) -> None:
        async with session_factory() as session:
            ids = await name_bonus_dal.list_claim_ids_needing_checks(session)
        for claim_id in ids:
            try:
                async with session_factory() as session:
                    await self.check_claim(session, claim_id)
            except Exception:
                logging.exception("Name bonus check failed for claim %s", claim_id)
