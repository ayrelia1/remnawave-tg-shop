"""
Standalone scheduler process for background tasks.

Run separately from the bot so that multiple bot workers
can coexist while background jobs execute exactly once.

Uses APScheduler (AsyncIOScheduler) to manage:
  - Database backups (daily at BACKUP_HOUR)
  - Panel auto-sync (every AUTO_SYNC_INTERVAL_HOURS)
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import get_settings
from db.database_setup import init_db, init_db_connection
from bot.middlewares.i18n import get_i18n_instance
from bot.services.panel_api_service import PanelApiService
from bot.services.notification_service import NotificationService
from bot.services.backup_service import BackupService
from bot.utils.message_queue import init_queue_manager
from config.logging_config import setup_logging


async def run_scheduler():
    load_dotenv()
    settings = get_settings()

    session_factory = init_db_connection(settings)
    if not session_factory:
        logging.critical("Failed to initialise DB connection. Exiting scheduler.")
        return

    await init_db(settings, session_factory)

    # Re-apply logging config: Alembic's env.py calls fileConfig() which
    # overwrites the root logger with alembic.ini's handlers/level.
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    init_queue_manager(bot)
    i18n = get_i18n_instance(default=settings.DEFAULT_LANGUAGE)
    notification_service = NotificationService(bot, settings, i18n)
    panel_service = PanelApiService(settings)

    backup_service = BackupService(bot, settings, notification_service)

    scheduler = AsyncIOScheduler()

    # --- Backup: daily at BACKUP_HOUR ---
    async def job_backup():
        logging.info("Scheduler: starting daily backup")
        try:
            await backup_service.perform_backup()
        except Exception as e:
            logging.error("Scheduler: backup error: %s", e, exc_info=True)

    scheduler.add_job(
        job_backup,
        CronTrigger(hour=settings.BACKUP_HOUR, minute=0),
        id="backup",
        name="Daily DB backup",
        replace_existing=True,
    )
    logging.info("Scheduler: backup job registered (daily at %02d:00)", settings.BACKUP_HOUR)

    # --- Auto-sync: every N hours ---
    if settings.AUTO_SYNC_ENABLED:
        from bot.handlers.admin.sync_admin import perform_sync

        async def job_auto_sync():
            logging.info("Scheduler: starting panel auto-sync")
            try:
                async with session_factory() as session:
                    sync_result = await perform_sync(
                        panel_service=panel_service,
                        session=session,
                        settings=settings,
                        i18n_instance=i18n,
                    )
                status = sync_result.get("status", "unknown")
                details = sync_result.get("details", "")
                logging.info("Scheduler: auto-sync completed with status: %s", status)

                try:
                    await notification_service.notify_panel_sync(
                        status,
                        details,
                        sync_result.get("users_processed", 0),
                        sync_result.get("subs_synced", 0),
                    )
                except Exception as e_notif:
                    logging.error("Scheduler: failed to send sync notification: %s", e_notif)
            except Exception as e:
                logging.error("Scheduler: auto-sync error: %s", e, exc_info=True)

        scheduler.add_job(
            job_auto_sync,
            IntervalTrigger(hours=settings.AUTO_SYNC_INTERVAL_HOURS),
            id="auto_sync",
            name="Panel auto-sync",
            replace_existing=True,
        )
        logging.info(
            "Scheduler: auto-sync job registered (every %d hours)",
            settings.AUTO_SYNC_INTERVAL_HOURS,
        )

    scheduler.start()
    logging.info("Scheduler: all jobs started, running until interrupted")

    try:
        # Keep the process alive
        stop_event = asyncio.Event()
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await panel_service.close()
        if bot.session:
            await bot.session.close()
        logging.info("Scheduler: shut down.")


if __name__ == "__main__":
    load_dotenv()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(run_scheduler())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Scheduler stopped manually")
    except Exception as e:
        logging.critical("Scheduler: unhandled exception: %s", e, exc_info=True)
        sys.exit(1)
