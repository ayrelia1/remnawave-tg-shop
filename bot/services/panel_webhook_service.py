import json
import logging
import hmac
import hashlib
from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.orm import sessionmaker
from typing import Optional
from config.settings import Settings
from .panel_api_service import PanelApiService
from .notification_service import NotificationService
from bot.middlewares.i18n import JsonI18n
from bot.keyboards.inline.user_keyboards import get_subscribe_only_markup, get_autorenew_cancel_keyboard
from db.dal import user_dal

EVENT_MAP = {
    "user.expires_in_72_hours": (3, "subscription_72h_notification"),
    "user.expires_in_48_hours": (2, "subscription_48h_notification"),
    "user.expires_in_24_hours": (1, "subscription_24h_notification"),
}

NODE_EVENTS = {"node.offline", "node.online"}

class PanelWebhookService:
    def __init__(self, bot: Bot, settings: Settings, i18n: JsonI18n, async_session_factory: sessionmaker, panel_service: PanelApiService, notification_service: NotificationService):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.panel_service = panel_service
        self.notification_service = notification_service

    async def _send_message(
        self,
        user_id: int,
        lang: str,
        message_key: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kwargs,
    ):
        _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw)
        try:
            await self.bot.send_message(
                user_id, _(message_key, **kwargs), reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Failed to send notification to {user_id}: {e}")

    async def handle_event(self, event_name: str, user_payload: dict):
        telegram_id = user_payload.get("telegramId")
        if not telegram_id:
            logging.warning("Panel webhook without telegramId received")
            return
        user_id = int(telegram_id)

        if not self.settings.SUBSCRIPTION_NOTIFICATIONS_ENABLED:
            return

        async with self.async_session_factory() as session:
            db_user = await user_dal.get_user_by_id(session, user_id)
            lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
            first_name = db_user.first_name or f"User {user_id}" if db_user else f"User {user_id}"

        markup = get_subscribe_only_markup(lang, self.i18n)

        if event_name in EVENT_MAP:
            days_left, msg_key = EVENT_MAP[event_name]
            if days_left == 1:
                # Trigger auto-renew via SubscriptionService (wired in at factory)
                try:
                    subscription_service = getattr(self, "subscription_service", None)
                    if subscription_service:
                        async with self.async_session_factory() as session:
                            from db.dal import subscription_dal
                            sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id)
                            if sub and sub.auto_renew_enabled and sub.provider == 'yookassa':
                                try:
                                    ok = await subscription_service.charge_subscription_renewal(session, sub)
                                    # If initiation succeeded, suppress the 24h reminder by returning early
                                    if ok:
                                        await session.commit()
                                        return
                                    else:
                                        await session.rollback()
                                except Exception:
                                    await session.rollback()
                                    logging.exception("Auto-renew attempt (24h) failed")
                except Exception:
                    logging.exception("Auto-renew trigger (24h) failed pre-check")
            if days_left <= self.settings.SUBSCRIPTION_NOTIFY_DAYS_BEFORE:
                # For 48h event, if auto-renew is enabled, show special notice with cancel button
                if days_left == 2:
                    async with self.async_session_factory() as session:
                        from db.dal import subscription_dal
                        sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id)
                        logging.info(
                            "48h webhook check: user_id=%s sub_found=%s auto_renew=%s provider=%s",
                            user_id,
                            bool(sub),
                            getattr(sub, 'auto_renew_enabled', None) if sub else None,
                            getattr(sub, 'provider', None) if sub else None,
                        )
                        if sub and sub.auto_renew_enabled and sub.provider == 'yookassa':
                            cancel_kb = get_autorenew_cancel_keyboard(lang, self.i18n)
                            await self._send_message(
                                user_id,
                                lang,
                                "autorenew_48h_charge_tomorrow_notice",
                                reply_markup=cancel_kb,
                                user_name=first_name,
                            )
                            return
                await self._send_message(
                    user_id,
                    lang,
                    msg_key,
                    reply_markup=markup,
                    user_name=first_name,
                    end_date=user_payload.get("expireAt", "")[:10],
                )
        elif event_name == "user.expired":
            if self.settings.SUBSCRIPTION_NOTIFY_ON_EXPIRE:
                await self._send_message(
                    user_id,
                    lang,
                    "subscription_expired_notification",
                    reply_markup=markup,
                    user_name=first_name,
                    end_date=user_payload.get("expireAt", "")[:10],
                )
        elif event_name == "user.expired_24_hours_ago" and self.settings.SUBSCRIPTION_NOTIFY_AFTER_EXPIRE:
            await self._send_message(
                user_id,
                lang,
                "subscription_expired_yesterday_notification",
                reply_markup=markup,
                user_name=first_name,
                end_date=user_payload.get("expireAt", "")[:10],
            )

    async def handle_node_event(self, event_name: str, node_payload: dict):
        """Handle node status change events from the Remnawave panel."""
        name = node_payload.get("name") or node_payload.get("uuid") or "Unknown"
        address = node_payload.get("address") or node_payload.get("host") or ""
        port = node_payload.get("port")
        if port and address:
            address = f"{address}:{port}"
        address = address or "N/A"

        try:
            if event_name == "node.offline":
                logging.warning("Panel webhook: node offline: %s (%s)", name, address)
                await self.notification_service.notify_node_down(name, address)
            elif event_name == "node.online":
                logging.info("Panel webhook: node online: %s (%s)", name, address)
                await self.notification_service.notify_node_recovered(name, address)
        except Exception as e:
            logging.error("Panel webhook: failed to send node status notification: %s", e)

    async def handle_webhook(self, raw_body: bytes, signature_header: Optional[str]) -> web.Response:
        if not self.settings.PANEL_WEBHOOK_SECRET:
            logging.critical("Panel webhook rejected: PANEL_WEBHOOK_SECRET is not configured")
            return web.Response(status=503, text="panel_webhook_secret_required")

        if not signature_header:
            return web.Response(status=403, text="no_signature")
        expected_sig = hmac.new(
            self.settings.PANEL_WEBHOOK_SECRET.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, signature_header):
            return web.Response(status=403, text="invalid_signature")

        try:
            payload = json.loads(raw_body.decode())
        except Exception:
            return web.Response(status=400, text="bad_request")

        event_name = payload.get("name") or payload.get("event")

        if not event_name:
            return web.Response(status=200, text="ok_no_event")

        if event_name in NODE_EVENTS:
            node_data = payload.get("payload") or payload.get("data") or {}
            if isinstance(node_data, dict) and "node" in node_data:
                node_data = node_data["node"]
            logging.info("Panel webhook node event received: %s", event_name)
            await self.handle_node_event(event_name, node_data if isinstance(node_data, dict) else {})
        else:
            user_data = payload.get("payload") or payload.get("data", {})
            if isinstance(user_data, dict) and "user" in user_data:
                user_data = user_data.get("user") or user_data
            telegram_id = user_data.get("telegramId") if isinstance(user_data, dict) else None
            logging.info(
                "Panel webhook event received: %s; telegramId=%s",
                event_name,
                telegram_id if telegram_id is not None else "N/A",
            )
            await self.handle_event(event_name, user_data)

        return web.Response(status=200, text="ok")

async def panel_webhook_route(request: web.Request):
    service: PanelWebhookService = request.app["panel_webhook_service"]
    raw = await request.read()
    signature_header = request.headers.get("X-Remnawave-Signature")
    return await service.handle_webhook(raw, signature_header)
