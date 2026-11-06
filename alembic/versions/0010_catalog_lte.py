"""Database catalog, immutable payment terms and cumulative LTE credit.

Revision ID: 0010_catalog_lte
Revises: 0009_name_bonus_claims
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_catalog_lte"
down_revision = "0009_name_bonus_claims"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("price_rub", sa.Float(), nullable=True),
        sa.Column("price_stars", sa.Integer(), nullable=True),
        sa.Column("lte_traffic_bytes", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("months", "device_limit", name="uq_subscription_plan_period_devices"),
        sa.CheckConstraint("months > 0 AND device_limit >= 0 AND lte_traffic_bytes >= 0", name="ck_subscription_plan_limits"),
        sa.CheckConstraint("price_rub IS NULL OR price_rub > 0", name="ck_subscription_plan_price_rub"),
        sa.CheckConstraint("price_stars IS NULL OR price_stars > 0", name="ck_subscription_plan_price_stars"),
    )
    op.create_table(
        "traffic_packages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("traffic_bytes", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("price_rub", sa.Float(), nullable=True),
        sa.Column("price_stars", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("traffic_bytes > 0", name="ck_traffic_package_bytes"),
        sa.CheckConstraint("price_rub IS NULL OR price_rub > 0", name="ck_traffic_package_price_rub"),
        sa.CheckConstraint("price_stars IS NULL OR price_stars > 0", name="ck_traffic_package_price_stars"),
    )
    op.create_table(
        "trial_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("lte_traffic_bytes", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1 AND days > 0 AND device_limit >= 0 AND lte_traffic_bytes >= 0", name="ck_trial_settings"),
    )
    # Nullable snapshot columns preserve already issued invoices. Their existing
    # subscription duration/device fields continue to describe historical terms.
    op.add_column("payments", sa.Column("product_type", sa.String(24), nullable=False, server_default="subscription"))
    op.add_column("payments", sa.Column("subscription_plan_id", sa.Integer(), nullable=True))
    op.add_column("payments", sa.Column("traffic_package_id", sa.Integer(), nullable=True))
    op.add_column("payments", sa.Column("lte_traffic_bytes", sa.BigInteger(), nullable=True))
    with op.batch_alter_table("payments") as batch:
        batch.create_foreign_key("fk_payments_subscription_plan", "subscription_plans", ["subscription_plan_id"], ["id"])
        batch.create_foreign_key("fk_payments_traffic_package", "traffic_packages", ["traffic_package_id"], ["id"])
    op.create_index("ix_payments_product_type", "payments", ["product_type"])
    op.create_table(
        "lte_accounts",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("managed", sa.Boolean(), nullable=False),
        sa.Column("credited_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_panel_counter", sa.BigInteger(), nullable=True),
        sa.Column("panel_user_ref", sa.String(), nullable=True),
        sa.Column("access_sync_pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("access_enabled", sa.Boolean(), nullable=True),
        sa.Column("depleted_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notification_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("credited_bytes >= 0 AND used_bytes >= 0", name="ck_lte_account_bytes"),
    )
    op.execute(sa.text("INSERT INTO lte_accounts (user_id, managed) SELECT user_id, false FROM users"))
    op.create_index("ix_lte_accounts_managed_user", "lte_accounts", ["managed", "user_id"])
    op.create_table(
        "lte_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(80), nullable=False, unique=True),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.payment_id", ondelete="SET NULL"), nullable=True, unique=True),
        sa.Column("traffic_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("traffic_bytes >= 0", name="ck_lte_grant_bytes"),
    )
    op.create_index("ix_lte_grants_user_id", "lte_grants", ["user_id"])


def downgrade():
    op.drop_table("lte_grants")
    op.drop_table("lte_accounts")
    op.drop_index("ix_payments_product_type", table_name="payments")
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("fk_payments_subscription_plan", type_="foreignkey")
        batch.drop_constraint("fk_payments_traffic_package", type_="foreignkey")
        for column in ("product_type", "subscription_plan_id", "traffic_package_id", "lte_traffic_bytes"):
            batch.drop_column(column)
    op.drop_table("trial_settings")
    op.drop_table("traffic_packages")
    op.drop_table("subscription_plans")
