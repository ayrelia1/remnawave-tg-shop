"""Exercise the actual additive migration and its rollback on a local database."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_existing_payments_and_customers_survive_upgrade_and_downgrade():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0010_catalog_lte.py"
    spec = importlib.util.spec_from_file_location("catalog_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        users = sa.Table("users", metadata, sa.Column("user_id", sa.BigInteger(), primary_key=True))
        payments = sa.Table("payments", metadata,
                            sa.Column("payment_id", sa.Integer(), primary_key=True),
                            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id")),
                            sa.Column("amount", sa.Float()),
                            sa.Column("subscription_duration_months", sa.Integer()))
        metadata.create_all(connection)
        connection.execute(users.insert(), {"user_id": 42})
        connection.execute(payments.insert(), {"payment_id": 7, "user_id": 42,
                                               "amount": 799, "subscription_duration_months": 3})
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            wallet = connection.execute(sa.text("SELECT * FROM lte_accounts WHERE user_id=42")).mappings().one()
            assert not wallet["managed"]
            assert wallet["credited_bytes"] == wallet["used_bytes"] == 0
            assert wallet["last_panel_counter"] is None
            assert wallet["access_enabled"] is None
            payment = connection.execute(sa.text("SELECT * FROM payments WHERE payment_id=7")).mappings().one()
            assert payment["amount"] == 799
            assert payment["subscription_duration_months"] == 3
            assert payment["lte_traffic_bytes"] is None
            from db.models import Base
            alerts_path = path.with_name("0011_lte_admin_alerts.py")
            alerts_spec = importlib.util.spec_from_file_location("lte_alerts_migration", alerts_path)
            alerts = importlib.util.module_from_spec(alerts_spec)
            alerts_spec.loader.exec_module(alerts)
            alerts.upgrade()
            inspector = sa.inspect(connection)
            # Historical migrations remain compatible with previously deployed
            # databases; the environment-owned application no longer maps catalog tables.
            assert {"subscription_plans", "traffic_packages", "trial_settings"} <= set(inspector.get_table_names())
            assert not {"subscription_plans", "traffic_packages", "trial_settings"} & Base.metadata.tables.keys()
            for name in ("lte_accounts", "lte_grants"):
                columns = {column["name"]: column for column in inspector.get_columns(name)}
                model_columns = Base.metadata.tables[name].columns
                assert set(columns) == set(model_columns.keys())
                for column in model_columns:
                    assert columns[column.name]["nullable"] == column.nullable
            alerts.downgrade()
            migration.downgrade()
            assert connection.execute(sa.select(payments)).mappings().one()["amount"] == 799
            assert "lte_accounts" not in sa.inspect(connection).get_table_names()
    engine.dispose()
