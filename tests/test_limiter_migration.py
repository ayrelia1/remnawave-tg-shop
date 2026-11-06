"""Additive limiter DDL, DB constraints and historical subscription deletion."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from db.models import LimiterViolation


def test_limiter_migration_preserves_existing_data_and_sets_subscription_fk_null():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0012_limiter_violations.py"
    spec = importlib.util.spec_from_file_location("limiter_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("CREATE TABLE subscriptions (subscription_id INTEGER PRIMARY KEY, end_date TEXT)")
            connection.exec_driver_sql("INSERT INTO subscriptions VALUES (7, '2027-01-01')")
            operations = Operations(MigrationContext.configure(connection))
            with Operations.context(operations.migration_context):
                migration.upgrade()
                columns = {column["name"] for column in sa.inspect(connection).get_columns("limiter_violations")}
                assert columns == set(LimiterViolation.__table__.columns.keys())
                assert connection.exec_driver_sql("SELECT end_date FROM subscriptions WHERE subscription_id=7").scalar() == "2027-01-01"
                now = datetime.now(timezone.utc)
                values = dict(subscription_id=7, panel_user_ref="42", telegram_id=123,
                              event_hash="a" * 64, reported_at=now, received_at=now,
                              device_limit=2, effective_limit=2, ip_count=4, device_count=4,
                              grouping_mode="ip", warning_number=1)
                connection.execute(sa.insert(LimiterViolation), values)
                for changes in ({}, {"event_hash": "b" * 64, "warning_number": 4},
                                {"event_hash": "c" * 64, "device_count": 5}):
                    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
                        connection.execute(sa.insert(LimiterViolation), values | changes)
                connection.exec_driver_sql("DELETE FROM subscriptions WHERE subscription_id=7")
                assert connection.exec_driver_sql("SELECT subscription_id FROM limiter_violations").scalar() is None
                assert connection.exec_driver_sql("SELECT event_hash FROM limiter_violations").scalar() == "a" * 64
                migration.downgrade()
                assert "limiter_violations" not in sa.inspect(connection).get_table_names()
                assert "subscriptions" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
