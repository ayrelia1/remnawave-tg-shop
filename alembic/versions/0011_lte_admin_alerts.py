"""Signed manual LTE adjustments and durable low-balance notifications.

Revision ID: 0011_lte_admin_alerts
Revises: 0010_catalog_lte
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_lte_admin_alerts"
down_revision = "0010_catalog_lte"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("lte_grants") as batch:
        batch.drop_constraint("ck_lte_grant_bytes", type_="check")
        batch.create_check_constraint(
            "ck_lte_grant_bytes", "traffic_bytes >= 0 OR (payment_id IS NULL AND source LIKE 'admin:%')")
    with op.batch_alter_table("lte_accounts") as batch:
        # Existing legacy exemptions stay unchanged until backfill_lte.py runs.
        batch.alter_column("managed", existing_type=sa.Boolean(), server_default=sa.true())
        batch.add_column(sa.Column("low_notified", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("low_notification_pending", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    # Never delete ledger history to satisfy the old unsigned constraint.
    if op.get_bind().execute(sa.text("SELECT 1 FROM lte_grants WHERE traffic_bytes < 0 LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade after manual LTE debits; preserve the signed ledger")
    with op.batch_alter_table("lte_accounts") as batch:
        batch.drop_column("low_notification_pending")
        batch.drop_column("low_notified")
        batch.alter_column("managed", existing_type=sa.Boolean(), server_default=None)
    with op.batch_alter_table("lte_grants") as batch:
        batch.drop_constraint("ck_lte_grant_bytes", type_="check")
        batch.create_check_constraint("ck_lte_grant_bytes", "traffic_bytes >= 0")
