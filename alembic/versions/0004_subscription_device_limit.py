"""add hwid_device_limit to subscriptions and payments

Revision ID: 0004_subscription_device_limit
Revises: 0003_promo_curr_act_not_null
Create Date: 2026-06-07 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_subscription_device_limit"
down_revision: Union[str, Sequence[str], None] = "0003_promo_curr_act_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "subscriptions",
            sa.Column("hwid_device_limit", sa.Integer(), nullable=True),
        )
        op.add_column(
            "payments",
            sa.Column("hwid_device_limit", sa.Integer(), nullable=True),
        )
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("subscriptions"):
        columns = {c["name"] for c in inspector.get_columns("subscriptions")}
        if "hwid_device_limit" not in columns:
            op.add_column(
                "subscriptions",
                sa.Column("hwid_device_limit", sa.Integer(), nullable=True),
            )

    if inspector.has_table("payments"):
        columns = {c["name"] for c in inspector.get_columns("payments")}
        if "hwid_device_limit" not in columns:
            op.add_column(
                "payments",
                sa.Column("hwid_device_limit", sa.Integer(), nullable=True),
            )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_column("payments", "hwid_device_limit")
        op.drop_column("subscriptions", "hwid_device_limit")
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("payments"):
        columns = {c["name"] for c in inspector.get_columns("payments")}
        if "hwid_device_limit" in columns:
            op.drop_column("payments", "hwid_device_limit")

    if inspector.has_table("subscriptions"):
        columns = {c["name"] for c in inspector.get_columns("subscriptions")}
        if "hwid_device_limit" in columns:
            op.drop_column("subscriptions", "hwid_device_limit")
