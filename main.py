import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from bot.main_bot import run_bot
from config.settings import get_settings
from config.logging_config import setup_logging
from db.database_setup import init_db, init_db_connection


async def main():
    load_dotenv()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    settings = get_settings()

    session_factory = init_db_connection(settings)
    if not session_factory:
        logging.critical(
            "Failed to initialize DB connection and session factory. Exiting.")
        return

    await init_db(settings, session_factory)

    # Re-apply logging config: Alembic's env.py calls fileConfig() which
    # overwrites the root logger with alembic.ini's handlers/level.
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    await run_bot(settings)


if __name__ == "__main__":
    load_dotenv()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped manually")
    except Exception as e_global:
        logging.critical(f"Global unhandled exception in main: {e_global}",
                         exc_info=True)
        sys.exit(1)
