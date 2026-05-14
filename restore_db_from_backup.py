"""Restore the entire PostgreSQL database from a bot backup.

Accepts a backup file in the exact format BackupService produces and sends to
the Telegram chat:
  * an encrypted .7z archive (AES-256, password = BACKUP_PASSWORD), or
  * the plain pg_dump .sql file inside it.

The script drops and recreates the target database, then replays the dump.
This WIPES the current database — it is gated behind an explicit --apply flag.

Usage:
    python restore_db_from_backup.py            # dry run, uses ./backup.sql
    python restore_db_from_backup.py --apply    # restore from ./backup.sql
    python restore_db_from_backup.py other.7z --apply   # explicit path

By default it looks for 'backup.sql' next to this script. Connection params
(POSTGRES_*) and BACKUP_PASSWORD are read from the .env in the same directory.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
DEFAULT_BACKUP = SCRIPT_DIR / "backup.sql"


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'")


def load_config() -> dict:
    """Read config from the .env next to the script, falling back to the
    process environment (so it also works inside the Docker container, where
    .env is injected via env_file rather than present on disk)."""
    values = dict(dotenv_values(ENV_PATH)) if ENV_PATH.is_file() else {}
    if not values:
        logging.info("No .env file at %s — using process environment", ENV_PATH)

    def get(key: str) -> str:
        return _clean(values.get(key) or os.environ.get(key))

    return {
        "POSTGRES_USER": get("POSTGRES_USER") or "user",
        "POSTGRES_PASSWORD": get("POSTGRES_PASSWORD") or "password",
        "POSTGRES_HOST": get("POSTGRES_HOST") or "localhost",
        "POSTGRES_PORT": get("POSTGRES_PORT") or "5432",
        "POSTGRES_DB": get("POSTGRES_DB") or "vpn_shop_db",
        "BACKUP_PASSWORD": get("BACKUP_PASSWORD") or "changeme",
    }


def find_7z() -> str | None:
    for name in ("7za", "7z", "7zr"):
        if shutil.which(name):
            return name
    return None


def extract_archive(archive_path: Path, password: str, workdir: Path) -> Path:
    """Extract the .7z backup and return the path to the .sql file inside it."""
    seven_zip = find_7z()
    if not seven_zip:
        raise RuntimeError("7-Zip not found on PATH (need 7za / 7z / 7zr)")

    cmd = [
        seven_zip, "x",
        f"-p{password}",
        f"-o{workdir}",
        "-y",
        str(archive_path),
    ]
    logging.info("Extracting archive %s with %s", archive_path.name, seven_zip)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "7z extraction failed"
        raise RuntimeError(f"Failed to extract archive (wrong password?): {err[:300]}")

    sql_files = list(workdir.glob("*.sql"))
    if not sql_files:
        raise RuntimeError("No .sql file found inside the archive")
    if len(sql_files) > 1:
        logging.warning("Multiple .sql files in archive, using %s", sql_files[0].name)
    return sql_files[0]


def run_psql(args: list[str], env: dict, *, input_text: str | None = None,
             timeout: int = 600) -> subprocess.CompletedProcess:
    cmd = ["psql", "--no-password"] + args
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        input=input_text, timeout=timeout,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    apply = "--apply" in sys.argv
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    backup_path = (
        Path(positional[0]).expanduser().resolve()
        if positional
        else DEFAULT_BACKUP
    )
    if not backup_path.is_file():
        logging.error("Backup file not found: %s", backup_path)
        logging.error("Place the backup as 'backup.sql' next to this script, or pass a path.")
        return 2

    cfg = load_config()
    pg_user = cfg["POSTGRES_USER"]
    pg_password = cfg["POSTGRES_PASSWORD"]
    pg_host = cfg["POSTGRES_HOST"]
    pg_port = cfg["POSTGRES_PORT"]
    pg_db = cfg["POSTGRES_DB"]
    backup_password = cfg["BACKUP_PASSWORD"]

    if not shutil.which("psql"):
        logging.error("psql not found on PATH — install PostgreSQL client tools")
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="pg_restore_"))
    try:
        # Resolve the actual .sql dump (extract if we were given a .7z archive).
        if backup_path.suffix.lower() == ".7z":
            dump_path = extract_archive(backup_path, backup_password, tmpdir)
        elif backup_path.suffix.lower() == ".sql":
            dump_path = backup_path
        else:
            logging.error("Unsupported file type: %s (expected .7z or .sql)", backup_path.suffix)
            return 2

        dump_size = dump_path.stat().st_size
        logging.info(
            "Dump ready: %s (%.2f MB)", dump_path.name, dump_size / (1024 * 1024)
        )

        target = f"{pg_user}@{pg_host}:{pg_port}/{pg_db}"
        if not apply:
            logging.warning("DRY RUN — no changes made.")
            logging.warning("Would DROP and RECREATE database: %s", target)
            logging.warning("Would replay dump: %s", dump_path)
            logging.warning("Re-run with --apply to actually restore (this WIPES the DB).")
            return 0

        env = os.environ.copy()
        env["PGPASSWORD"] = pg_password

        # 1. Terminate active connections to the target DB.
        logging.info("Terminating active connections to '%s'", pg_db)
        terminate_sql = (
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{pg_db}' AND pid <> pg_backend_pid();"
        )
        base_conn = ["-h", pg_host, "-p", pg_port, "-U", pg_user]
        res = run_psql(base_conn + ["-d", "postgres", "-c", terminate_sql], env)
        if res.returncode != 0:
            logging.error("Failed to terminate connections: %s", res.stderr.strip()[:300])
            return 1

        # 2. Drop and recreate the database. DROP/CREATE DATABASE cannot run
        # inside a transaction block, and psql -c wraps multiple statements in
        # one transaction — so each statement goes in its own psql call.
        logging.info("Dropping and recreating database '%s'", pg_db)
        for stmt in (
            f'DROP DATABASE IF EXISTS "{pg_db}";',
            f'CREATE DATABASE "{pg_db}" OWNER "{pg_user}";',
        ):
            res = run_psql(
                base_conn + ["-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", stmt],
                env,
            )
            if res.returncode != 0:
                logging.error("Failed (%s): %s", stmt, res.stderr.strip()[:300])
                return 1

        # 3. Replay the dump into the fresh database.
        logging.info("Restoring dump into '%s' — this may take a while...", pg_db)
        with dump_path.open("r", encoding="utf-8") as fh:
            dump_sql = fh.read()
        res = run_psql(
            base_conn + ["-d", pg_db, "-v", "ON_ERROR_STOP=1", "--single-transaction"],
            env,
            input_text=dump_sql,
            timeout=1800,
        )
        if res.returncode != 0:
            logging.error("Restore failed: %s", res.stderr.strip()[:1000])
            return 1

        if res.stderr.strip():
            logging.info("psql notices: %s", res.stderr.strip()[:500])
        logging.info("Restore complete — database '%s' restored from %s", pg_db, backup_path.name)
        return 0

    except Exception as exc:
        logging.error("Unexpected error: %s", exc, exc_info=True)
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
