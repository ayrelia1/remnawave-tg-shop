"""Notify subscription owners about IP blocks reported by the panel."""

import hashlib
import json
from datetime import timezone
from typing import Annotated
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.utils.text_decorations import html_decoration as hd
from pydantic import AwareDatetime, BaseModel, Field, IPvAnyAddress, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.keyboards.inline.user_keyboards import get_user_banned_keyboard
from bot.middlewares.i18n import JsonI18n
from config.settings import Settings
from db.dal import message_log_dal, subscription_dal
from db.models import User
from .panel_api_service import PanelApiService

TORRENT_REPORT_EVENT = "torrent_blocker.report"
NOTIFICATION_LOG_EVENT = "torrent_blocker_notification"
PanelID = Annotated[StrictInt, Field(gt=0, le=2**63 - 1)]


class ReportUser(BaseModel):
    id: PanelID
    telegramId: PanelID | None = None


class ReportInbound(BaseModel):
    uuid: UUID
    profileUuid: UUID
    tag: str


class ReportConfigProfile(BaseModel):
    activeConfigProfileUuid: UUID | None = None
    activeInbounds: list[ReportInbound] = Field(default_factory=list)


class ReportNode(BaseModel):
    uuid: UUID
    name: Annotated[str, Field(min_length=1, max_length=512)]
    configProfile: ReportConfigProfile | None = None


class BlockAction(BaseModel):
    blocked: StrictBool
    ip: IPvAnyAddress
    blockDuration: Annotated[StrictInt, Field(ge=0)]
    processedAt: AwareDatetime


class XrayReport(BaseModel):
    inboundTag: str | None = None


class BlockReport(BaseModel):
    actionReport: BlockAction
    xrayReport: XrayReport


class TorrentReport(BaseModel):
    user: ReportUser
    node: ReportNode
    report: BlockReport

    def notification_marker(self) -> str:
        """Identify the action, independent of envelope timestamps or JSON formatting."""
        action = self.report.actionReport
        identity = [self.user.id, str(self.node.uuid), str(action.ip),
                    action.processedAt.astimezone(timezone.utc).isoformat()]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def resolve_host_name(event: TorrentReport, hosts: list[dict]) -> str:
    """Use a host remark only when node/profile/inbound identify one host."""
    profile = event.node.configProfile
    tag = event.report.xrayReport.inboundTag
    if profile is None or not profile.activeConfigProfileUuid or not tag:
        return event.node.name
    inbounds = [inbound for inbound in profile.activeInbounds
                if inbound.tag == tag and inbound.profileUuid == profile.activeConfigProfileUuid]
    if len(inbounds) != 1:
        return event.node.name
    matches = []
    for host in hosts:
        inbound = host.get("inbound") or {}
        if (inbound.get("configProfileUuid") != str(profile.activeConfigProfileUuid)
                or inbound.get("configProfileInboundUuid") != str(inbounds[0].uuid)):
            continue
        nodes = host.get("nodes") or []
        if nodes and str(event.node.uuid) not in nodes:
            continue
        matches.append(host)
    if len(matches) == 1 and isinstance(matches[0].get("remark"), str) and matches[0]["remark"].strip():
        # Bound escaped HTML as well as plain text to Telegram's message limit.
        return matches[0]["remark"].strip()[:512]
    return event.node.name


class TorrentBlockerService:
    def __init__(self, bot: Bot, settings: Settings, i18n: JsonI18n,
                 async_session_factory: async_sessionmaker[AsyncSession], panel_service: PanelApiService):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.panel = panel_service

    async def handle_event(self, data: object) -> str:
        if not self.settings.TORRENT_BLOCKER_NOTIFICATIONS_ENABLED:
            return "disabled"
        event = TorrentReport.model_validate(data)
        if not event.report.actionReport.blocked:
            return "ignored"
        panel_ref = str(event.user.id)
        marker = event.notification_marker()
        async with self.async_session_factory() as session:
            sub = await subscription_dal.get_subscription_for_panel_ref(session, panel_ref)
            if sub is None or event.user.telegramId not in (None, sub.user_id):
                return "ignored"
            owner_id = sub.user_id
            if await message_log_dal.has_event_marker(session, owner_id, NOTIFICATION_LOG_EVENT, marker):
                return "duplicate"

        # No owner lock is held during the panel call. The signed report carries
        # the node's active profile, so ordinary delivery needs only GET /hosts.
        hosts = await self.panel.get_hosts()
        if hosts is None:
            raise RuntimeError("Host catalog unavailable")
        server_name = resolve_host_name(event, hosts)

        async with self.async_session_factory() as session:
            # PostgreSQL serializes concurrent deliveries, including in another
            # bot process. Recheck both ownership and the marker under this lock.
            user = await session.scalar(select(User).where(User.user_id == owner_id).with_for_update())
            sub = await subscription_dal.get_subscription_for_panel_ref(session, panel_ref)
            if user is None or sub is None or sub.user_id != owner_id:
                return "ignored"
            if await message_log_dal.has_event_marker(session, owner_id, NOTIFICATION_LOG_EVENT, marker):
                return "duplicate"
            lang = user.language_code or self.settings.DEFAULT_LANGUAGE
            action = event.report.actionReport
            key = "torrent_blocked_notification" if action.blockDuration else "torrent_blocked_indefinite_notification"
            text = self.i18n.gettext(
                lang, key, ip=hd.quote(str(action.ip)), server_name=hd.quote(server_name),
                seconds=action.blockDuration,
            )
            outcome = "Torrent block notification delivered"
            try:
                await self.bot.send_message(
                    owner_id, text, parse_mode="HTML",
                    reply_markup=get_user_banned_keyboard(self.settings.SUPPORT_LINK, lang, self.i18n),
                )
            except TelegramForbiddenError:
                # A blocked bot cannot deliver. Acknowledge this permanent error
                # instead of making the panel retry the same report forever.
                outcome = "Torrent block notification: recipient unavailable"
            await message_log_dal.create_message_log_no_commit(session, {
                "user_id": owner_id, "event_type": NOTIFICATION_LOG_EVENT,
                "content": outcome, "raw_update_preview": marker,
            })
            await session.commit()
        return "notified"
