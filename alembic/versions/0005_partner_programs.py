"""split ad campaigns into ad/partner types and add partner payouts

Revision ID: 0005_partner_programs
Revises: 0004_subscription_device_limit
Create Date: 2026-08-27 00:00:00.000000

Existing rows are backfilled as campaign_type='ad', which is exactly what they
were before, so nothing about the current ad statistics changes.
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_partner_programs"
down_revision: Union[str, Sequence[str], None] = "0004_subscription_device_limit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CAMPAIGN_COLUMNS = (
    ("campaign_type", lambda: sa.Column(
        "campaign_type",
        sa.String(length=16),
        nullable=False,
        server_default="ad",
    )),
    ("partner_user_id", lambda: sa.Column(
        "partner_user_id", sa.BigInteger(), nullable=True
    )),
    ("partner_percent", lambda: sa.Column(
        "partner_percent", sa.Float(), nullable=True
    )),
)


def _create_payouts_table() -> None:
    op.create_table(
        "partner_payouts",
        sa.Column("payout_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ad_campaign_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["ad_campaign_id"],
            ["ad_campaigns.ad_campaign_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("payout_id"),
    )
    op.create_index(
        "ix_partner_payouts_ad_campaign_id",
        "partner_payouts",
        ["ad_campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_partner_payouts_created_at",
        "partner_payouts",
        ["created_at"],
        unique=False,
    )


def upgrade() -> None:
    if context.is_offline_mode():
        for _name, factory in CAMPAIGN_COLUMNS:
            op.add_column("ad_campaigns", factory())
        op.create_foreign_key(
            "fk_ad_campaigns_partner_user_id_users",
            "ad_campaigns",
            "users",
            ["partner_user_id"],
            ["user_id"],
        )
        op.create_index(
            "ix_ad_campaigns_campaign_type", "ad_campaigns", ["campaign_type"]
        )
        op.create_index(
            "ix_ad_campaigns_partner_user_id", "ad_campaigns", ["partner_user_id"]
        )
        _create_payouts_table()
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("ad_campaigns"):
        columns = {c["name"] for c in inspector.get_columns("ad_campaigns")}
        added = []
        for name, factory in CAMPAIGN_COLUMNS:
            if name not in columns:
                op.add_column("ad_campaigns", factory())
                added.append(name)

        if "campaign_type" in added:
            # Backfill is redundant next to the server_default, but keeps the
            # column consistent if the default is ever dropped.
            op.execute(
                "UPDATE ad_campaigns SET campaign_type = 'ad' "
                "WHERE campaign_type IS NULL"
            )

        if "partner_user_id" in added:
            fk_names = {
                fk.get("name") for fk in inspector.get_foreign_keys("ad_campaigns")
            }
            if "fk_ad_campaigns_partner_user_id_users" not in fk_names:
                op.create_foreign_key(
                    "fk_ad_campaigns_partner_user_id_users",
                    "ad_campaigns",
                    "users",
                    ["partner_user_id"],
                    ["user_id"],
                )

        index_names = {ix["name"] for ix in inspector.get_indexes("ad_campaigns")}
        if "ix_ad_campaigns_campaign_type" not in index_names:
            op.create_index(
                "ix_ad_campaigns_campaign_type", "ad_campaigns", ["campaign_type"]
            )
        if "ix_ad_campaigns_partner_user_id" not in index_names:
            op.create_index(
                "ix_ad_campaigns_partner_user_id",
                "ad_campaigns",
                ["partner_user_id"],
            )

    if not inspector.has_table("partner_payouts"):
        _create_payouts_table()


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_index("ix_partner_payouts_created_at", table_name="partner_payouts")
        op.drop_index("ix_partner_payouts_ad_campaign_id", table_name="partner_payouts")
        op.drop_table("partner_payouts")
        op.drop_index("ix_ad_campaigns_partner_user_id", table_name="ad_campaigns")
        op.drop_index("ix_ad_campaigns_campaign_type", table_name="ad_campaigns")
        op.drop_constraint(
            "fk_ad_campaigns_partner_user_id_users", "ad_campaigns", type_="foreignkey"
        )
        for name, _factory in reversed(CAMPAIGN_COLUMNS):
            op.drop_column("ad_campaigns", name)
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("partner_payouts"):
        index_names = {ix["name"] for ix in inspector.get_indexes("partner_payouts")}
        if "ix_partner_payouts_created_at" in index_names:
            op.drop_index(
                "ix_partner_payouts_created_at", table_name="partner_payouts"
            )
        if "ix_partner_payouts_ad_campaign_id" in index_names:
            op.drop_index(
                "ix_partner_payouts_ad_campaign_id", table_name="partner_payouts"
            )
        op.drop_table("partner_payouts")

    if inspector.has_table("ad_campaigns"):
        index_names = {ix["name"] for ix in inspector.get_indexes("ad_campaigns")}
        if "ix_ad_campaigns_partner_user_id" in index_names:
            op.drop_index("ix_ad_campaigns_partner_user_id", table_name="ad_campaigns")
        if "ix_ad_campaigns_campaign_type" in index_names:
            op.drop_index("ix_ad_campaigns_campaign_type", table_name="ad_campaigns")

        fk_names = {
            fk.get("name") for fk in inspector.get_foreign_keys("ad_campaigns")
        }
        if "fk_ad_campaigns_partner_user_id_users" in fk_names:
            op.drop_constraint(
                "fk_ad_campaigns_partner_user_id_users",
                "ad_campaigns",
                type_="foreignkey",
            )

        columns = {c["name"] for c in inspector.get_columns("ad_campaigns")}
        for name, _factory in reversed(CAMPAIGN_COLUMNS):
            if name in columns:
                op.drop_column("ad_campaigns", name)
