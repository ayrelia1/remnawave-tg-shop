"""Custom torrent reporter inbox and admin policy.

Revision ID: 0013_custom_torrent_reports
Revises: 0012_limiter_violations
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_custom_torrent_reports"
down_revision = "0012_limiter_violations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "torrent_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(140), nullable=False),
        sa.Column("batch_id", sa.String(128), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("alert_index", sa.Integer(), nullable=False),
        sa.Column("panel_user_ref", sa.String(19), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.subscription_id", ondelete="SET NULL")),
        sa.Column("node_uuid", sa.String(128), nullable=False),
        sa.Column("node_name", sa.String(255), nullable=False),
        sa.Column("inbound_tag", sa.String(255), nullable=False),
        sa.Column("client_ip", sa.String(45), nullable=False),
        sa.Column("level", sa.String(7), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decision", sa.String(32), nullable=True),
        sa.Column("warning_number", sa.Integer(), nullable=True),
        sa.Column("processing_stage", sa.String(10), nullable=False, server_default="pending"),
        sa.UniqueConstraint("batch_id", "page", "alert_index", name="uq_torrent_report_page_alert"),
        sa.CheckConstraint("level IN ('low', 'medium', 'high', 'highest')", name="ck_torrent_report_level"),
        sa.CheckConstraint("warning_number BETWEEN 1 AND 3", name="ck_torrent_report_warning"),
        sa.CheckConstraint("processing_stage IN ('pending', 'admin_sent', 'done')", name="ck_torrent_report_stage"),
    )
    counted = sa.text("warning_number IS NOT NULL")
    pending = sa.text("processing_stage != 'done'")
    op.create_index("ix_torrent_report_warnings", "torrent_reports", ["panel_user_ref", "received_at", "id"],
                    postgresql_where=counted, sqlite_where=counted)
    op.create_index("ix_torrent_report_pending", "torrent_reports", ["id"],
                    postgresql_where=pending, sqlite_where=pending)
    settings = op.create_table(
        "torrent_report_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("auto_response_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("minimum_level", sa.String(7), nullable=False, server_default="medium"),
        sa.CheckConstraint("id = 1", name="ck_torrent_report_settings_singleton"),
        sa.CheckConstraint("minimum_level IN ('low', 'medium', 'high', 'highest')", name="ck_torrent_report_min_level"),
    )
    op.bulk_insert(settings, [{"id": 1}])


def downgrade():
    op.drop_table("torrent_report_settings")
    op.drop_table("torrent_reports")
