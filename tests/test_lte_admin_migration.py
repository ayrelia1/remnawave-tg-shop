"""The signed ledger migration preserves data and rejects lossy downgrades."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def load_migration(name):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signed_adjustments_and_low_flags_preserve_legacy_data():
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE users (user_id BIGINT PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE payments (payment_id INTEGER PRIMARY KEY, amount FLOAT)"))
            connection.execute(sa.text("INSERT INTO users VALUES (1), (2)"))
            connection.execute(sa.text("INSERT INTO payments VALUES (7, 799)"))
            with Operations.context(MigrationContext.configure(connection)):
                load_migration("0010_catalog_lte").upgrade()
                connection.execute(sa.text("UPDATE lte_accounts SET credited_bytes=100 WHERE user_id=1"))
                connection.execute(sa.text("INSERT INTO lte_grants (user_id,source,payment_id,traffic_bytes) "
                                           "VALUES (1,'payment:7',7,100)"))
                migration = load_migration("0011_lte_admin_alerts")
                migration.upgrade()
                row = connection.execute(sa.text("SELECT * FROM lte_accounts WHERE user_id=1")).mappings().one()
                assert not row["managed"]
                assert row["credited_bytes"] == 100
                assert not row["low_notified"] and not row["low_notification_pending"]
                connection.execute(sa.text("DELETE FROM lte_accounts WHERE user_id=2"))
                connection.execute(sa.text("INSERT INTO lte_accounts (user_id) VALUES (2)"))
                assert connection.execute(sa.text("SELECT managed FROM lte_accounts WHERE user_id=2")).scalar()
                for source, payment_id in [("payment:bad", None), ("admin:777:bad", 7)]:
                    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
                        connection.execute(sa.text("INSERT INTO lte_grants (user_id,source,payment_id,traffic_bytes) "
                                                   "VALUES (1,:source,:payment_id,-10)"),
                                           {"source": source, "payment_id": payment_id})
                connection.execute(sa.text("INSERT INTO lte_grants (user_id,source,traffic_bytes) "
                                           "VALUES (1,'admin:777:debit',-10)"))
                with pytest.raises(RuntimeError, match="Cannot downgrade"):
                    migration.downgrade()
                assert connection.execute(sa.text("SELECT traffic_bytes FROM lte_grants "
                                                   "WHERE source='admin:777:debit'")).scalar() == -10
                assert connection.execute(sa.text("SELECT amount FROM payments WHERE payment_id=7")).scalar() == 799
                assert "low_notified" in {c["name"] for c in sa.inspect(connection).get_columns("lte_accounts")}
    finally:
        engine.dispose()
