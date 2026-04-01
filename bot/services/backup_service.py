import asyncio
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from config.settings import Settings
from bot.services.notification_service import NotificationService


class BackupService:
    """Daily PostgreSQL backup: pg_dump → 7z with password → send to Telegram topic."""

    def __init__(self, bot: Bot, settings: Settings, notification_service: NotificationService):
        self.bot = bot
        self.settings = settings
        self.notification_service = notification_service
        self._task: Optional[asyncio.Task] = None

    async def perform_backup(self) -> bool:
        """Create a password-protected 7z backup of the database and send to Telegram."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        db_name = self.settings.POSTGRES_DB
        archive_name = f"backup_{db_name}_{timestamp}.7z"

        tmpdir = tempfile.mkdtemp(prefix="pg_backup_")
        dump_path = os.path.join(tmpdir, f"{db_name}_{timestamp}.sql")
        archive_path = os.path.join(tmpdir, archive_name)

        try:
            # Step 1: pg_dump
            env = os.environ.copy()
            env["PGPASSWORD"] = self.settings.POSTGRES_PASSWORD

            pg_cmd = [
                "pg_dump",
                "-h", self.settings.POSTGRES_HOST,
                "-p", str(self.settings.POSTGRES_PORT),
                "-U", self.settings.POSTGRES_USER,
                "-d", db_name,
                "-f", dump_path,
                "--no-password",
            ]

            logging.info("Backup: running pg_dump for database '%s'", db_name)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    pg_cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                ),
            )

            if result.returncode != 0:
                err_msg = result.stderr.strip() or "pg_dump failed"
                logging.error("Backup: pg_dump error: %s", err_msg)
                await self.notification_service.notify_backup_complete(
                    archive_name, 0, False, err_msg[:300]
                )
                return False

            # Step 2: compress with 7za (AES-256 encryption, headers encrypted too)
            archive_cmd = [
                "7za", "a",
                "-t7z",
                f"-p{self.settings.BACKUP_PASSWORD}",
                "-mhe=on",
                "-mx=5",
                archive_path,
                dump_path,
            ]

            logging.info("Backup: compressing with 7za to '%s'", archive_name)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    archive_cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                ),
            )

            if result.returncode != 0:
                err_msg = result.stderr.strip() or "7za compression failed"
                logging.error("Backup: 7za error: %s", err_msg)
                await self.notification_service.notify_backup_complete(
                    archive_name, 0, False, err_msg[:300]
                )
                return False

            archive_size = os.path.getsize(archive_path)
            logging.info("Backup: archive ready, size=%d bytes", archive_size)

            # Step 3: send to Telegram backup topic
            thread_id = self.notification_service._thread_id_for("backups")
            chat_id = self.settings.LOG_CHAT_ID
            if not chat_id:
                logging.warning("Backup: LOG_CHAT_ID not set, cannot send backup to Telegram")
                await self.notification_service.notify_backup_complete(
                    archive_name, archive_size, True
                )
                return True

            with open(archive_path, "rb") as f:
                file_data = f.read()

            caption = (
                f"💾 <b>Резервная копия БД</b>\n"
                f"📁 {archive_name}\n"
                f"📦 {archive_size / (1024 * 1024):.2f} МБ\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )

            send_kwargs = {
                "chat_id": chat_id,
                "document": BufferedInputFile(file_data, filename=archive_name),
                "caption": caption,
                "parse_mode": "HTML",
            }
            if thread_id:
                send_kwargs["message_thread_id"] = thread_id

            try:
                await self.bot.send_document(**send_kwargs)
                logging.info("Backup: sent successfully to chat %s", chat_id)
            except Exception as e:
                logging.error("Backup: failed to send to Telegram: %s", e)
                await self.notification_service.notify_backup_complete(
                    archive_name, archive_size, False, str(e)[:300]
                )
                return False

            await self.notification_service.notify_backup_complete(archive_name, archive_size, True)
            return True

        except Exception as e:
            logging.error("Backup: unexpected error: %s", e, exc_info=True)
            await self.notification_service.notify_backup_complete(archive_name, 0, False, str(e)[:300])
            return False
        finally:
            # Clean up temp files
            try:
                if os.path.exists(dump_path):
                    os.remove(dump_path)
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                os.rmdir(tmpdir)
            except Exception:
                pass

    async def _scheduler_loop(self):
        """Run backup daily at the configured hour (server local time)."""
        logging.info(
            "Backup scheduler started: daily at %02d:00 (local time)",
            self.settings.BACKUP_HOUR,
        )
        while True:
            now = datetime.now()
            # Calculate seconds until next scheduled hour
            target_hour = self.settings.BACKUP_HOUR
            if now.hour < target_hour:
                next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            elif now.hour == target_hour and now.minute == 0 and now.second < 5:
                # Just started at the right hour — run immediately
                next_run = now
            else:
                from datetime import timedelta
                next_run = (now + timedelta(days=1)).replace(
                    hour=target_hour, minute=0, second=0, microsecond=0
                )

            delay = (next_run - now).total_seconds()
            if delay > 1:
                logging.info("Backup scheduler: next backup in %.0f seconds (at %s)", delay, next_run.strftime("%Y-%m-%d %H:%M"))
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    logging.info("Backup scheduler: task cancelled")
                    return

            logging.info("Backup scheduler: starting scheduled backup")
            try:
                await self.perform_backup()
            except Exception as e:
                logging.error("Backup scheduler: error during backup: %s", e, exc_info=True)

            # Sleep a bit to avoid double-triggering at the same minute
            await asyncio.sleep(60)

    def start_scheduler(self):
        """Start the background backup scheduler task."""
        self._task = asyncio.create_task(self._scheduler_loop(), name="BackupSchedulerTask")

    def stop(self):
        """Cancel the background scheduler task."""
        if self._task and not self._task.done():
            self._task.cancel()
