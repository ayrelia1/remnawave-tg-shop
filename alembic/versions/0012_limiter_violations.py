"""Subscription-scoped limiter inbox and resumable warnings.

Revision ID: 0012_limiter_violations
Revises: 0011_lte_admin_alerts
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_limiter_violations"
down_revision = "0011_lte_admin_alerts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "limiter_violations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.subscription_id", ondelete="SET NULL"), nullable=True),
        sa.Column("panel_user_ref", sa.String(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warning_number", sa.Integer(), nullable=True),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("effective_limit", sa.Integer(), nullable=False),
        sa.Column("ip_count", sa.Integer(), nullable=False),
        sa.Column("device_count", sa.Integer(), nullable=False),
        sa.Column("grouping_mode", sa.String(8), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reset_marker", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_hash", name="uq_limiter_violation_event_hash"),
        sa.CheckConstraint("warning_number IS NULL OR warning_number BETWEEN 1 AND 3", name="ck_limiter_warning_number"),
        sa.CheckConstraint("device_limit > 0 AND effective_limit >= device_limit AND ip_count > 0 "
                           "AND device_count > 0 AND device_count <= ip_count", name="ck_limiter_counts"),
    )
    op.create_index("ix_limiter_panel_history", "limiter_violations", ["panel_user_ref", "id"])
    op.create_index("ix_limiter_pending", "limiter_violations", ["stage", "next_attempt_at", "id"])
    op.create_index("ix_limiter_received_at", "limiter_violations", ["received_at"])


def downgrade():
    op.drop_table("limiter_violations")
