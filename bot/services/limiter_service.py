"""Signed remnawave-limiter inbox and subscription-scoped warning processing."""

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from pydantic import AwareDatetime, BaseModel, Field, StrictInt, ValidationError, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.middlewares.i18n import JsonI18n
from bot.constants.premium_emoji import PREMIUM_EMOJI_CONNECT, PREMIUM_EMOJI_SUPPORT
from bot.services.panel_api_service import PanelApiService, extract_panel_user_ref
from bot.services.notification_service import NotificationService
from bot.services.subscription_service import SubscriptionService, subscription_reset_marker
from bot.utils.config_link import prepare_config_links
from bot.utils.date_utils import as_utc
from db.dal import limiter_dal, subscription_dal
from db.models import LimiterViolation, Subscription, User
from config.settings import Settings

WINDOW = timedelta(hours=48)
SIGNATURE_MAX_AGE = 300
MAX_BODY = 256 * 1024
Counter = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
PanelID = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]


class LimiterUser(BaseModel):
    user_id: PanelID
    telegram_id: PanelID | None = None


class ViolationDetails(BaseModel):
    device_limit: Counter
    effective_limit: Counter
    ip_count: Counter
    device_group_count: Counter
    grouping_mode: Literal["ip", "subnet", "asn"]

    @model_validator(mode="after")
    def consistent_counts(self):
        if self.effective_limit < self.device_limit or self.device_group_count > self.ip_count:
            raise ValueError("Inconsistent limiter counts")
        return self


class LimiterPayload(BaseModel):
    event: Literal["violation_detected"]
    action_mode: Literal["manual"]
    user: LimiterUser
    violation: ViolationDetails
    timestamp: AwareDatetime


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("Nonfinite JSON number")


def verified_signature(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    if abs(datetime.now(timezone.utc).timestamp() - int(timestamp)) > SIGNATURE_MAX_AGE:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class LimiterActionError(RuntimeError):
    """Safe internal reason code; never contains panel credentials or URLs."""


class LimiterService:
    def __init__(self, settings: Settings, panel_service: PanelApiService,
                 subscription_service: SubscriptionService, bot: Bot, i18n: JsonI18n,
                 *, notification_service: NotificationService | None = None):
        self.settings = settings
        self.panel = panel_service
        self.subscriptions = subscription_service
        self.bot = bot
        self.i18n = i18n
        self.notification_service = notification_service

    @property
    def secret(self) -> str:
        return (self.settings.LIMITER_WEBHOOK_SECRET or "").strip()

    async def enqueue(self, session_factory: async_sessionmaker[AsyncSession], raw: bytes,
                      *, received_at: datetime | None = None) -> str:
        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        if data.get("event") != "violation_detected" or data.get("action_mode") != "manual":
            return "ignored"
        payload = LimiterPayload.model_validate(data)
        details = payload.violation
        if details.device_group_count <= details.effective_limit:
            return "ignored"
        digest = hashlib.sha256(raw).hexdigest()
        ref = str(payload.user.user_id)
        async with session_factory() as session:
            if await limiter_dal.find_event(session, digest):
                return "duplicate"
            sub = await subscription_dal.get_subscription_for_panel_ref(session, ref)
            if sub is None or payload.user.telegram_id not in (None, sub.user_id):
                return "ignored"
            owner_id = sub.user_id
            user = await session.scalar(select(User).where(User.user_id == owner_id).with_for_update())
            # Resolve again after the owner lock; imports/deletion may have changed it.
            sub = await subscription_dal.get_subscription_for_panel_ref(session, ref)
            if user is None or sub is None or sub.user_id != owner_id:
                return "ignored"
            if await limiter_dal.find_event(session, digest):
                return "duplicate"
            received_at = received_at or datetime.now(timezone.utc)
            event = LimiterViolation(
                subscription_id=sub.subscription_id, panel_user_ref=ref, telegram_id=owner_id,
                event_hash=digest, received_at=received_at, reported_at=payload.timestamp,
                device_limit=details.device_limit, effective_limit=details.effective_limit,
                ip_count=details.ip_count, device_count=details.device_group_count,
                grouping_mode=details.grouping_mode, next_attempt_at=received_at,
            )
            try:
                async with session.begin_nested():
                    session.add(event)
                    await session.flush()
            except IntegrityError:
                if await limiter_dal.find_event(session, digest):
                    return "duplicate"
                raise
            await session.commit()
        return "accepted"

    async def run_checks(self, session_factory: async_sessionmaker[AsyncSession],
                         *, now: datetime | None = None) -> None:
        if not self.secret:
            return
        now = now or datetime.now(timezone.utc)
        async with session_factory() as session:
            ids = await limiter_dal.ready_ids(session, now)
        for event_id in ids:
            await self._process_event(session_factory, event_id, now)
        async with session_factory() as session:
            # A small grace also covers an accepted HTTP transaction crossing
            # the exact expiry boundary before its inbox row becomes visible.
            await limiter_dal.prune(session, now - WINDOW - timedelta(seconds=SIGNATURE_MAX_AGE))
            await session.commit()
        if ids:
            logging.info("Limiter checks: selected=%s", len(ids))

    async def process_received_event(self, session_factory: async_sessionmaker[AsyncSession],
                                     event_hash: str) -> bool:
        """Run a received event now; an older unfinished action keeps priority."""
        if not self.secret:
            return False
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            ids = await limiter_dal.ready_ids(session, now, limit=1, event_hash=event_hash)
            if not ids:
                event = await limiter_dal.find_event(session, event_hash)
                # Let the sender retry if an older unfinished action is in the
                # way. A completed duplicate needs no panel or Telegram call.
                return event is None or event.stage in limiter_dal.TERMINAL_STAGES
        return await self._process_event(session_factory, ids[0], now, retry_now=True)

    async def _process_event(self, session_factory: async_sessionmaker[AsyncSession],
                             event_id: int, now: datetime, *, retry_now: bool = False) -> bool:
        try:
            # Reacquire/reload under the owner lock after every checkpoint.
            # Webhook redeliveries and the scheduler share the same steps.
            for _ in range(4):
                if await self._step(session_factory, event_id, now, retry_now=retry_now):
                    return True
        except Exception as exc:
            logging.warning("Limiter event %s deferred (%s)", event_id, type(exc).__name__)
            async with session_factory() as session:
                event = await session.get(LimiterViolation, event_id, with_for_update=True)
                if event is not None and event.stage not in limiter_dal.TERMINAL_STAGES:
                    event.attempts += 1
                    event.next_attempt_at = now + timedelta(seconds=60 * 2**min(event.attempts - 1, 6))
                    event.last_error = str(exc) if isinstance(exc, LimiterActionError) else type(exc).__name__
                    await session.commit()
        return False

    @staticmethod
    def _finish(event: LimiterViolation, now: datetime, reason: str | None = None) -> None:
        event.stage = "ignored" if reason else "done"
        event.last_error = reason
        event.processed_at = now

    async def _step(self, session_factory: async_sessionmaker[AsyncSession],
                    event_id: int, now: datetime, *, retry_now: bool = False) -> bool:
        async with session_factory() as session:
            snapshot = await session.get(LimiterViolation, event_id)
            if snapshot is None:
                return True
            user = await session.scalar(select(User).where(
                User.user_id == snapshot.telegram_id).with_for_update())
            event = await session.get(LimiterViolation, event_id, with_for_update=True, populate_existing=True)
            if event is None or event.stage in limiter_dal.TERMINAL_STAGES:
                return True
            if not retry_now and as_utc(event.next_attempt_at) > now:
                return True
            previous_stage = event.stage
            sub = await subscription_dal.get_subscription_for_panel_ref(session, event.panel_user_ref)
            if user is None or sub is None or sub.user_id != event.telegram_id or user.is_banned:
                self._finish(event, now, "owner_unavailable")
            else:
                if event.subscription_id is None:
                    event.subscription_id = sub.subscription_id
                if event.stage == "pending":
                    await self._prepare(session, event, now)
                elif event.stage == "resetting":
                    await self._reset(session, event, now)
                elif event.stage == "disabling":
                    await self._disable(session, event, now)
                elif event.stage == "applied":
                    await self._notify(session, user, event, now)
                else:
                    raise LimiterActionError("unknown_stage")
            await session.commit()
            if (self.notification_service is not None and event.stage == "applied"
                    and previous_stage in ("resetting", "disabling")):
                # Emit only on the committed account action, not on user-message
                # retries. Admin alerts use the same best-effort queue as other logs.
                try:
                    await self.notification_service.notify_limiter_action(
                        user_id=event.telegram_id, panel_user_ref=event.panel_user_ref,
                        warning_number=event.warning_number, device_limit=event.device_limit,
                        device_count=event.device_count, ip_count=event.ip_count,
                        grouping_mode=event.grouping_mode, username=user.username,
                        first_name=user.first_name,
                    )
                except Exception as exc:
                    logging.warning("Limiter admin alert failed (%s)", type(exc).__name__)
            return event.stage in limiter_dal.TERMINAL_STAGES

    async def _panel_user(self, event: LimiterViolation) -> dict | None:
        panel_user = await self.panel.get_user_by_ref(event.panel_user_ref)
        if not panel_user:
            return None
        if (extract_panel_user_ref(panel_user) != event.panel_user_ref
                or panel_user.get("telegramId") not in (None, 0, event.telegram_id)):
            return None
        return panel_user

    @staticmethod
    def _active(panel_user: dict, now: datetime) -> bool:
        expiry = panel_user.get("expireAt")
        if panel_user.get("status") != "ACTIVE" or not isinstance(expiry, str):
            return False
        return as_utc(datetime.fromisoformat(expiry.replace("Z", "+00:00"))) > now

    async def _prepare(self, session: AsyncSession, event: LimiterViolation, now: datetime) -> None:
        if as_utc(event.received_at) + WINDOW <= now:
            self._finish(event, now, "expired_event")
            return
        panel_user = await self._panel_user(event)
        if not panel_user or not self._active(panel_user, now):
            self._finish(event, now, "inactive_subscription")
            return
        # Limiter caches user limits. A changed plan must not inherit a stale penalty.
        if panel_user.get("hwidDeviceLimit") != event.device_limit:
            self._finish(event, now, "device_limit_changed")
            return
        token = panel_user.get("subscriptionUuid") or panel_user.get("shortUuid")
        sub = await subscription_dal.get_subscription_for_panel_ref(session, event.panel_user_ref, token)
        if sub is None or sub.user_id != event.telegram_id:
            self._finish(event, now, "owner_unavailable")
            return
        event.subscription_id = sub.subscription_id
        previous = await limiter_dal.latest_counted(session, event)
        if previous and as_utc(previous.window_expires_at) > as_utc(event.received_at):
            event.window_started_at = previous.window_started_at
            event.window_expires_at = previous.window_expires_at
            number = min(3, previous.warning_number + 1)
        else:
            event.window_started_at = event.received_at
            event.window_expires_at = as_utc(event.received_at) + WINDOW
            number = 1
        event.warning_number = number
        if number < 3:
            event.reset_marker = subscription_reset_marker(panel_user)
            if event.reset_marker is None:
                raise LimiterActionError("reset_marker_missing")
            event.stage = "resetting"
        else:
            event.stage = "disabling"

    async def _reset(self, session: AsyncSession, event: LimiterViolation, now: datetime) -> None:
        panel_user = await self._panel_user(event)
        if panel_user is None:
            self._finish(event, now, "panel_user_missing")
            return
        already_reset = subscription_reset_marker(panel_user) != event.reset_marker
        if not already_reset and (as_utc(event.received_at) + WINDOW <= now
                                  or not self._active(panel_user, now)
                                  or panel_user.get("hwidDeviceLimit") != event.device_limit):
            self._finish(event, now, "stale_reset")
            return
        result = await self.subscriptions.reset_subscription_keys(
            session, event.telegram_id, event.panel_user_ref,
            previous_marker=event.reset_marker, require_hwid_cleanup=True)
        if not result:
            raise LimiterActionError("reset_incomplete")
        event.stage = "applied"

    async def _disable(self, session: AsyncSession, event: LimiterViolation, now: datetime) -> None:
        panel_user = await self._panel_user(event)
        if panel_user is None:
            self._finish(event, now, "panel_user_missing")
            return
        token = panel_user.get("subscriptionUuid") or panel_user.get("shortUuid")
        sub = await subscription_dal.get_subscription_for_panel_ref(session, event.panel_user_ref, token)
        if sub is None or sub.user_id != event.telegram_id:
            self._finish(event, now, "owner_unavailable")
            return
        event.subscription_id = sub.subscription_id
        if panel_user.get("status") != "DISABLED":
            if (as_utc(event.received_at) + WINDOW <= now or not self._active(panel_user, now)
                    or panel_user.get("hwidDeviceLimit") != event.device_limit):
                self._finish(event, now, "stale_disable")
                return
            if not await self.panel.update_user_status_on_panel(event.panel_user_ref, False):
                raise LimiterActionError("disable_incomplete")
        # Only this panel account is disabled; other subscriptions and User.is_banned remain untouched.
        await session.execute(update(Subscription).where(
            Subscription.panel_user_uuid == event.panel_user_ref,
            Subscription.user_id == event.telegram_id,
            (Subscription.is_active.is_(True) | (Subscription.subscription_id == event.subscription_id)),
        ).values(is_active=False, status_from_panel="DISABLED"))
        event.stage = "applied"

    async def _notify(self, session: AsyncSession, user: User,
                      event: LimiterViolation, now: datetime) -> None:
        if await limiter_dal.has_later_action(session, event):
            self._finish(event, now)
            event.last_error = "superseded_notice"
            return
        if event.warning_number == 3:
            panel_user = await self._panel_user(event)
            if not panel_user or panel_user.get("status") != "DISABLED":
                self._finish(event, now)
                event.last_error = "stale_disable_notice"
                return
        lang = user.language_code or self.settings.DEFAULT_LANGUAGE
        _ = lambda key, **kwargs: self.i18n.gettext(lang, key, **kwargs)
        buttons = []
        if event.warning_number < 3:
            panel_user = await self._panel_user(event)
            if panel_user is None:
                self._finish(event, now, "panel_user_missing")
                return
            links = await prepare_config_links(self.settings, panel_user.get("subscriptionUrl"))
            link = links[1]
            if not link:
                raise LimiterActionError("connection_link_missing")
            buttons.append([InlineKeyboardButton(text=_("connect_button"), url=link,
                                                 icon_custom_emoji_id=PREMIUM_EMOJI_CONNECT)])
        buttons.append([InlineKeyboardButton(text=_("menu_support_button"), url=self.settings.SUPPORT_LINK,
                                             icon_custom_emoji_id=PREMIUM_EMOJI_SUPPORT)])
        text = _("limiter_disabled" if event.warning_number == 3 else "limiter_warning",
                 number=event.warning_number, device_limit=event.device_limit,
                 device_count=event.device_count, ip_count=event.ip_count,
                 measure=_("limiter_measure_" + event.grouping_mode))
        try:
            await self.bot.send_message(user.user_id, text, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except TelegramForbiddenError:
            # The account action is complete even if its owner blocked the bot.
            self._finish(event, now)
            event.last_error = "telegram_forbidden"
            return
        self._finish(event, now)


async def limiter_webhook_route(request: web.Request) -> web.Response:
    service = request.app.get("limiter_service")
    if service is None or not service.secret:
        return web.json_response({"status": "disabled"}, status=503)
    if request.content_type != "application/json":
        return web.json_response({"status": "invalid_content_type"}, status=415)
    raw = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            return web.json_response({"status": "body_too_large"}, status=413)
    body = bytes(raw)
    if not verified_signature(service.secret, body, request.headers.get("X-Timestamp", ""),
                              request.headers.get("X-Signature-V2", "")):
        return web.json_response({"status": "invalid_signature"}, status=403)
    try:
        result = await service.enqueue(request.app["async_session_factory"], body)
    except (ValueError, ValidationError):
        return web.json_response({"status": "invalid_payload"}, status=400)
    except Exception as exc:
        logging.error("Limiter inbox unavailable (%s)", type(exc).__name__)
        return web.json_response({"status": "retry"}, status=503)
    if result != "ignored":
        try:
            completed = await service.process_received_event(
                request.app["async_session_factory"], hashlib.sha256(body).hexdigest())
        except Exception as exc:
            logging.error("Limiter processing unavailable (%s)", type(exc).__name__)
            completed = False
        if not completed:
            return web.json_response({"status": "retry"}, status=503)
    return web.json_response({"status": result})
