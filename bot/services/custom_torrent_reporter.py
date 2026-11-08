"""Durable reports and subscription-scoped penalties from the custom collector."""

import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from ipaddress import ip_address
from math import ceil
from time import monotonic
from typing import Annotated, Any, Literal, get_args

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiohttp import web
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.keyboards.inline.user_keyboards import get_user_banned_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.panel_api_service import PanelApiService, PanelUnavailableError, extract_panel_user_ref
from bot.utils.date_utils import as_utc
from config.settings import Settings
from db.dal import subscription_dal
from db.models import Subscription, TorrentReport, TorrentReportSettings, User

TorrentLevel = Literal["low", "medium", "high", "highest"]
TORRENT_LEVELS = get_args(TorrentLevel)
WARNING_WINDOW = timedelta(days=5)
REPORT_RETRY_DELAY = timedelta(seconds=30)
REPORT_BATCH_SIZE = 20
USER_NOTICE_DONE = "_notice_done"
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
Timestamp = Annotated[int, Field(ge=0, le=253402300799)]
PageNumber = Annotated[int, Field(ge=1, le=2147483647)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class TorrentAlert(BaseModel):
    inbound_tag: Label
    node_name: Label
    window_start: Timestamp
    window_end: Timestamp
    created_at: Timestamp
    user_id: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")]
    client_ip: Annotated[str, StringConstraints(max_length=45)]
    level: TorrentLevel
    details: dict[str, Any]

    @field_validator("node_name", "inbound_tag")
    @classmethod
    def valid_label(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Label contains a null character")
        value.encode("utf-8")
        return value

    @field_validator("client_ip")
    @classmethod
    def valid_ip(cls, value: str) -> str:
        if "%" in value:
            raise ValueError("IP scopes are not supported")
        return str(ip_address(value))

    @field_validator("details")
    @classmethod
    def valid_details(cls, value: dict) -> dict:
        json.dumps(value, allow_nan=False)
        return value


class TorrentPage(BaseModel):
    batch_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    node_uuid: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    page: PageNumber
    total_pages: PageNumber
    alerts: list[TorrentAlert]

    @property
    def key(self) -> str:
        return self.batch_id if self.total_pages == 1 else f"{self.batch_id}:{self.page}"


async def _store_page(session: AsyncSession, payload: TorrentPage) -> bool:
    if not payload.alerts:
        return True
    key = payload.key
    rows = [
        dict(batch_id=payload.batch_id, page=payload.page, idempotency_key=key, alert_index=index,
             node_uuid=payload.node_uuid, node_name=alert.node_name, inbound_tag=alert.inbound_tag,
             panel_user_ref=alert.user_id, client_ip=alert.client_ip, level=alert.level, details=alert.details,
             window_start=UNIX_EPOCH + timedelta(seconds=alert.window_start),
             window_end=UNIX_EPOCH + timedelta(seconds=alert.window_end),
             created_at=UNIX_EPOCH + timedelta(seconds=alert.created_at))
        for index, alert in enumerate(payload.alerts)
    ]
    # The first alert claims the entire page. A concurrent replay waits on this
    # key, so it cannot append new positions or acknowledge an uncommitted page.
    first_id = await session.scalar(
        pg_insert(TorrentReport).values(rows[0])
        .on_conflict_do_nothing(index_elements=["batch_id", "page", "alert_index"])
        .returning(TorrentReport.id)
    )
    if first_id is None:
        return False
    if len(rows) > 1:
        await session.execute(insert(TorrentReport), rows[1:])
    return True


async def torrent_webhook_route(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if not settings.custom_torrent_reporter_active:
        return web.json_response({"status": "disabled"}, status=503)
    if request.content_type != "application/json":
        return web.json_response({"status": "invalid_content_type"}, status=415)
    try:
        raw = json.loads(await request.read())
        auth = raw.pop("auth", None) if isinstance(raw, dict) else None
        secret = settings.CUSTOM_TORRENT_REPORTER_SECRET.get_secret_value()
        if not isinstance(auth, str) or not hmac.compare_digest(auth.encode(), secret.encode()):
            return web.json_response({"status": "invalid_auth"}, status=403)
        payload = TorrentPage.model_validate(raw, strict=True)
        if payload.page > payload.total_pages or request.headers.get("Idempotency-Key", payload.key) != payload.key:
            return web.json_response({"status": "invalid_page"}, status=400)
    except (ValueError, RecursionError):
        return web.json_response({"status": "invalid_payload"}, status=400)
    try:
        async with request.app["async_session_factory"].begin() as session:
            accepted = await _store_page(session, payload)
    except Exception as exc:
        logging.error("Custom torrent inbox unavailable (%s)", type(exc).__name__)
        return web.json_response({"status": "retry"}, status=503)
    return web.json_response({"status": "accepted" if accepted else "duplicate"})


async def _latest_warnings(session: AsyncSession, panel_ref: str, limit: int = 3,
                           *, before: TorrentReport | None = None):
    statement = (
        select(TorrentReport.warning_number, TorrentReport.received_at,
               TorrentReport.decision, TorrentReport.processing_stage)
        .where(TorrentReport.panel_user_ref == panel_ref, TorrentReport.warning_number.is_not(None))
    )
    if before is not None:
        statement = statement.where(or_(
            TorrentReport.received_at < before.received_at,
            (TorrentReport.received_at == before.received_at) & (TorrentReport.id < before.id),
        ))
    return (await session.execute(
        statement.order_by(TorrentReport.received_at.desc(), TorrentReport.id.desc()).limit(limit)
    )).all()


def _policy_rejection(policy: TorrentReportSettings | None, level: str) -> str | None:
    if policy is None or not policy.auto_response_enabled:
        return "auto_off"
    if TORRENT_LEVELS.index(level) < TORRENT_LEVELS.index(policy.minimum_level):
        return "below_threshold"
    return None


def _previous_warning_pending(history) -> bool:
    return any(entry.decision == "warn" and entry.processing_stage != "done" for entry in history)


def _cancel_disable(report: TorrentReport, reason: str) -> str:
    report.warning_number = None
    report.decision = reason
    return reason


class CustomTorrentReporter:
    def __init__(self, settings: Settings, panel: PanelApiService, bot: Bot, i18n: JsonI18n):
        self.settings = settings
        self.panel = panel
        self.bot = bot
        self.i18n = i18n
        self.cooldown = timedelta(minutes=settings.CUSTOM_TORRENT_REPORTER_COOLDOWN_MINUTES)
        self.telegram_resume_at = 0.0
        self.report_retry_at: dict[int, datetime] = {}
        self.last_report_id = 0

    async def run_checks(self, factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None) -> None:
        if not self.settings.custom_torrent_reporter_active or monotonic() < self.telegram_resume_at:
            return
        current_time = now or datetime.now(timezone.utc)
        self.report_retry_at = {report_id: retry_at for report_id, retry_at in self.report_retry_at.items()
                                if retry_at > current_time}
        statement = select(TorrentReport.id).where(text("processing_stage != 'done'"))
        if self.report_retry_at:
            statement = statement.where(TorrentReport.id.not_in(self.report_retry_at))
        statement = statement.order_by(TorrentReport.id)
        async with factory() as session:
            ids = list(await session.scalars(
                statement.where(TorrentReport.id > self.last_report_id).limit(REPORT_BATCH_SIZE)
            ))
            if self.last_report_id and len(ids) < REPORT_BATCH_SIZE:
                # Wrap so a large backlog of old failures cannot monopolize the batch.
                ids.extend(await session.scalars(
                    statement.where(TorrentReport.id <= self.last_report_id).limit(REPORT_BATCH_SIZE - len(ids))
                ))
        if ids:
            self.last_report_id = ids[-1]
        retry_ids = []
        # Within a wrapped batch, finish earlier warnings before later actions.
        for report_id in sorted(ids):
            try:
                complete = await self._process(factory, report_id, now or datetime.now(timezone.utc))
            except TelegramRetryAfter as exc:
                self.telegram_resume_at = monotonic() + exc.retry_after
                break
            except Exception as exc:
                logging.warning("Custom torrent report %s will retry (%s)", report_id, type(exc).__name__)
                complete = False
            if not complete:
                retry_ids.append(report_id)
        retry_at = (now or datetime.now(timezone.utc)) + REPORT_RETRY_DELAY
        self.report_retry_at.update(dict.fromkeys(retry_ids, retry_at))

    async def _process(self, factory: async_sessionmaker[AsyncSession], report_id: int, now: datetime) -> bool:
        async with factory.begin() as session:
            report = await session.get(TorrentReport, report_id)
            if report is None or report.processing_stage == "done":
                return True
            if report.decision is None:
                await self._decide(session, report)

        if report.decision == "disable_pending":
            try:
                report.decision = await self._disable(factory, report, now)
            except PanelUnavailableError:
                logging.warning("Custom torrent report %s is waiting for the panel", report.id)

        retry = report.decision == "disable_pending"
        if report.decision in ("warn", "disabled"):
            try:
                retry = not await self._notify_user(factory, report)
            except TelegramRetryAfter:
                raise
            except Exception as exc:
                logging.warning("Custom torrent report %s: personal notice will retry (%s)",
                                report.id, type(exc).__name__)
                retry = True
            if not retry and report.processing_stage == "pending":
                # Checkpoint personal delivery before trying the admin topic.
                # The suffix is only needed while the other delivery is pending.
                report.decision += USER_NOTICE_DONE
                async with factory.begin() as session:
                    await session.execute(update(TorrentReport).where(TorrentReport.id == report.id)
                                          .values(decision=report.decision))

        if report.processing_stage == "pending":
            await self._notify_admin(factory, report, now)
        elif retry:
            return False
        async with factory.begin() as session:
            await session.execute(update(TorrentReport).where(TorrentReport.id == report.id).values(
                processing_stage="admin_sent" if retry else "done",
                decision=report.decision.removesuffix(USER_NOTICE_DONE),
            ))
        return not retry

    async def _decide(self, session: AsyncSession, report: TorrentReport) -> None:
        sub = await subscription_dal.get_subscription_for_panel_ref(session, report.panel_user_ref)
        if sub is None:
            report.decision = "user_not_found"
            return
        report.telegram_user_id = sub.user_id
        report.subscription_id = sub.subscription_id
        rejection = _policy_rejection(await session.get(TorrentReportSettings, 1), report.level)
        if rejection:
            report.decision = rejection
            return
        received = as_utc(report.received_at)
        history = await _latest_warnings(session, report.panel_user_ref)
        number = 1
        if history:
            last = history[0]
            if received < as_utc(last.received_at) + self.cooldown:
                report.decision = "cooldown"
                return
            first = next(entry for entry in history if entry.warning_number == 1)
            if received < as_utc(first.received_at) + WARNING_WINDOW:
                number = last.warning_number + 1
        if number > 3:
            report.decision = "window_limit"
        elif not sub.is_active or as_utc(sub.end_date) <= received:
            report.decision = "inactive_subscription"
        else:
            report.warning_number = number
            report.decision = "disable_pending" if number == 3 else "warn"

    async def _disable(self, factory: async_sessionmaker[AsyncSession], report: TorrentReport, now: datetime) -> str:
        async with factory.begin() as session:
            saved = await session.get(TorrentReport, report.id)
            rejection = _policy_rejection(await session.get(TorrentReportSettings, 1), report.level)
            if rejection:
                return _cancel_disable(saved, rejection)
            if as_utc(report.received_at) + WARNING_WINDOW <= now:
                return _cancel_disable(saved, "window_expired")
            history = await _latest_warnings(session, report.panel_user_ref, limit=2, before=report)
            if _previous_warning_pending(history):
                return "disable_pending"

            user = await session.get(User, report.telegram_user_id, with_for_update=True)
            sub = await subscription_dal.get_subscription_for_panel_ref(session, report.panel_user_ref)
            if user is None or sub is None or sub.user_id != report.telegram_user_id:
                saved.decision = "subscription_changed"
                return saved.decision

            panel_user = await self.panel.get_user_by_ref(report.panel_user_ref, log_response=False)
            if (extract_panel_user_ref(panel_user) != report.panel_user_ref
                    or panel_user.get("telegramId") not in (None, 0, report.telegram_user_id)):
                saved.decision = "subscription_changed"
                return saved.decision
            status = panel_user.get("status")
            if status != "DISABLED":
                if status != "ACTIVE":
                    saved.decision = "inactive_subscription"
                    return saved.decision
                policy = await session.get(TorrentReportSettings, 1, populate_existing=True)
                if rejection := _policy_rejection(policy, report.level):
                    return _cancel_disable(saved, rejection)
                if not await self.panel.update_user_status_on_panel(report.panel_user_ref, False, log_response=False):
                    raise PanelUnavailableError("Custom torrent disable was not confirmed")
            await session.execute(update(Subscription).where(
                Subscription.panel_user_uuid == report.panel_user_ref,
                Subscription.user_id == report.telegram_user_id,
                (Subscription.is_active.is_(True) | (Subscription.subscription_id == sub.subscription_id)),
            ).values(is_active=False, status_from_panel="DISABLED"))
            saved.decision = "disabled"
            return saved.decision

    async def _notify_admin(self, factory: async_sessionmaker[AsyncSession],
                            report: TorrentReport, now: datetime) -> None:
        lang = self.settings.DEFAULT_LANGUAGE
        minutes = 0
        if report.decision == "cooldown":
            async with factory() as session:
                latest = await _latest_warnings(session, report.panel_user_ref, limit=1)
            if latest:
                minutes = max(0, ceil((as_utc(latest[0].received_at) + self.cooldown - now).total_seconds() / 60))
        decision = report.decision.removesuffix(USER_NOTICE_DONE)
        reason = self.i18n.gettext(lang, f"custom_torrent_result_{decision}",
                                   minutes=minutes, number=report.warning_number)
        details = json.dumps(report.details)
        message = self.i18n.gettext(
            lang, "custom_torrent_admin_notice", result=reason, panel_id=report.panel_user_ref,
            user_id=report.telegram_user_id or "—", ip=report.client_ip, level=report.level,
            node=escape(report.node_name), node_uuid=report.node_uuid, inbound=escape(report.inbound_tag),
            score=escape(json.dumps(report.details["score"])[:80]) if "score" in report.details else "—",
            details=escape(details[:1000] + ("…" if len(details) > 1000 else "")),
            window_start=as_utc(report.window_start).strftime("%Y-%m-%d %H:%M:%S"),
            window_end=as_utc(report.window_end).strftime("%Y-%m-%d %H:%M:%S"),
            created_at=as_utc(report.created_at).strftime("%Y-%m-%d %H:%M:%S"),
        )
        await self.bot.send_message(self.settings.LOG_CHAT_ID, message,
                                    message_thread_id=self.settings.CUSTOM_TORRENT_REPORTER_THREAD_ID,
                                    parse_mode="HTML", request_timeout=8)

    async def _notify_user(self, factory: async_sessionmaker[AsyncSession], report: TorrentReport) -> bool:
        async with factory() as session:
            if report.decision == "warn" and report.warning_number == 2:
                history = await _latest_warnings(session, report.panel_user_ref, limit=1, before=report)
                if _previous_warning_pending(history):
                    return False
            user = await session.get(User, report.telegram_user_id)
            sub = await subscription_dal.get_subscription_for_panel_ref(session, report.panel_user_ref)
            if (user is None or sub is None or sub.user_id != report.telegram_user_id
                    or (report.decision == "disabled" and sub.status_from_panel != "DISABLED")):
                return True
            lang = user.language_code or self.settings.DEFAULT_LANGUAGE
        key = "custom_torrent_user_disabled" if report.decision == "disabled" else "custom_torrent_user_warning"
        try:
            await self.bot.send_message(
                report.telegram_user_id, self.i18n.gettext(lang, key, number=report.warning_number),
                reply_markup=get_user_banned_keyboard(self.settings.SUPPORT_LINK, lang, self.i18n),
                parse_mode="HTML", request_timeout=8,
            )
        except TelegramForbiddenError:
            logging.info("Custom torrent report %s: user notifications are blocked", report.id)
        except TelegramBadRequest as exc:
            if "chat not found" not in exc.message.lower():
                raise
        return True
