import asyncio
import logging
from typing import Optional

from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from bot.services.panel_api_service import PanelApiService
from bot.services.notification_service import NotificationService
from bot.middlewares.i18n import JsonI18n


class AutoSyncService:
    """Periodic panel synchronization at a configurable interval."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        panel_service: PanelApiService,
        notification_service: NotificationService,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
    ):
        self.bot = bot
        self.settings = settings
        self.panel_service = panel_service
        self.notification_service = notification_service
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self._task: Optional[asyncio.Task] = None

    async def _sync_loop(self):
        from bot.handlers.admin.sync_admin import perform_sync

        interval_seconds = self.settings.AUTO_SYNC_INTERVAL_HOURS * 3600
        logging.info(
            "Auto-sync scheduler started: every %d hour(s)",
            self.settings.AUTO_SYNC_INTERVAL_HOURS,
        )

        while True:
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                logging.info("Auto-sync scheduler: task cancelled")
                return

            logging.info("Auto-sync scheduler: starting periodic panel sync")
            try:
                async with self.async_session_factory() as session:
                    sync_result = await perform_sync(
                        panel_service=self.panel_service,
                        session=session,
                        settings=self.settings,
                        i18n_instance=self.i18n,
                    )

                status = sync_result.get("status", "unknown")
                details = sync_result.get("details", "")
                logging.info("Auto-sync completed with status: %s", status)

                try:
                    await self.notification_service.notify_panel_sync(
                        status,
                        details,
                        sync_result.get("users_processed", 0),
                        sync_result.get("subs_synced", 0),
                    )
                except Exception as e_notif:
                    logging.error("Auto-sync: failed to send notification: %s", e_notif)

            except Exception as e:
                logging.error("Auto-sync scheduler: error during sync: %s", e, exc_info=True)

    def start(self):
        """Start the background auto-sync scheduler task."""
        if not self.settings.AUTO_SYNC_ENABLED:
            logging.info("Auto-sync is disabled (AUTO_SYNC_ENABLED=False)")
            return
        self._task = asyncio.create_task(self._sync_loop(), name="AutoSyncSchedulerTask")

    def stop(self):
        """Cancel the background scheduler task."""
        if self._task and not self._task.done():
            self._task.cancel()
