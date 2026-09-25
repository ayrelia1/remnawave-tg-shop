"""Track five-day Telegram name bonus claims and revocations.

Revision ID: 0009_name_bonus_claims
Revises: 0008_attribution_is_new_user
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_name_bonus_claims"
down_revision: Union[str, Sequence[str], None] = "0008_attribution_is_new_user"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "name_bonus_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("monitor_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bonus_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reclaimed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("panel_sync_pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notification_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_name_bonus_claims_user_id", "name_bonus_claims", ["user_id"])
    op.create_index("ix_name_bonus_claims_status", "name_bonus_claims", ["status"])
    op.create_index("ix_name_bonus_claims_user_granted", "name_bonus_claims", ["user_id", "granted_at"])
    op.create_index(
        "uq_name_bonus_active_user",
        "name_bonus_claims",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_name_bonus_active_user", table_name="name_bonus_claims")
    op.drop_index("ix_name_bonus_claims_user_granted", table_name="name_bonus_claims")
    op.drop_index("ix_name_bonus_claims_status", table_name="name_bonus_claims")
    op.drop_index("ix_name_bonus_claims_user_id", table_name="name_bonus_claims")
    op.drop_table("name_bonus_claims")
