"""campaign accrual ledger with frozen exchange rates

Revision ID: 0006_campaign_accruals
Revises: 0005_partner_programs
Create Date: 2026-08-27 00:00:00.000000

Campaign revenue used to be recomputed from `payments` on every read, which
meant a change to PARTNER_CURRENCY_RATES retroactively revalued the whole
history. This materialises one immutable row per attributed payment instead,
carrying the rate that was applied and the resulting base-currency amount.

The backfill here converts existing history at 1:1 for RUB and XTR only — the
currencies the shipped defaults define. Anything else is deliberately left
alone rather than valued at face value; revision 0007 normalises every payment
and then completes the ledger from those values, so nothing stays behind.
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_campaign_accruals"
down_revision: Union[str, Sequence[str], None] = "0005_partner_programs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Currencies the default configuration values 1:1 against the payout currency.
BACKFILL_RATES = {"RUB": 1.0, "XTR": 1.0}

PAYOUT_COLUMNS = (
    ("rate", lambda: sa.Column("rate", sa.Float(), nullable=False, server_default="1.0")),
    ("amount_rub", lambda: sa.Column("amount_rub", sa.Float(), nullable=False, server_default="0.0")),
)


def _create_accruals_table() -> None:
    op.create_table(
        "campaign_accruals",
        sa.Column("accrual_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ad_campaign_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("amount_rub", sa.Float(), nullable=False),
        sa.Column("percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("earned_rub", sa.Float(), nullable=False, server_default="0"),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("subscription_duration_months", sa.Integer(), nullable=True),
        sa.Column("hwid_device_limit", sa.Integer(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["ad_campaign_id"], ["ad_campaigns.ad_campaign_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.payment_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("accrual_id"),
        sa.UniqueConstraint("payment_id", name="uq_campaign_accruals_payment_id"),
    )
    op.create_index(
        "ix_campaign_accruals_ad_campaign_id",
        "campaign_accruals",
        ["ad_campaign_id"],
    )
    op.create_index("ix_campaign_accruals_user_id", "campaign_accruals", ["user_id"])
    op.create_index("ix_campaign_accruals_paid_at", "campaign_accruals", ["paid_at"])


def _backfill_sql() -> str:
    branches = " ".join(
        f"WHEN '{code}' THEN {rate}" for code, rate in BACKFILL_RATES.items()
    )
    rate_expr = f"(CASE UPPER(p.currency) {branches} END)"
    supported = ", ".join(f"'{code}'" for code in BACKFILL_RATES)
    return f"""
        INSERT INTO campaign_accruals (
            ad_campaign_id, payment_id, user_id, amount, currency, rate,
            amount_rub, percent, earned_rub, provider,
            subscription_duration_months, hwid_device_limit, paid_at
        )
        SELECT
            a.ad_campaign_id,
            p.payment_id,
            p.user_id,
            p.amount,
            p.currency,
            {rate_expr},
            p.amount * {rate_expr},
            COALESCE(c.partner_percent, 0),
            p.amount * {rate_expr} * COALESCE(c.partner_percent, 0) / 100.0,
            p.provider,
            p.subscription_duration_months,
            p.hwid_device_limit,
            p.created_at
        FROM payments p
        JOIN ad_attributions a ON a.user_id = p.user_id
        JOIN ad_campaigns c ON c.ad_campaign_id = a.ad_campaign_id
        WHERE p.status = 'succeeded'
          AND p.created_at >= a.first_start_at
          AND UPPER(p.currency) IN ({supported})
          AND NOT EXISTS (
              SELECT 1 FROM campaign_accruals ca WHERE ca.payment_id = p.payment_id
          )
    """


def upgrade() -> None:
    if context.is_offline_mode():
        _create_accruals_table()
        for _name, factory in PAYOUT_COLUMNS:
            op.add_column("partner_payouts", factory())
        op.execute(_backfill_sql())
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("campaign_accruals"):
        _create_accruals_table()

    if inspector.has_table("partner_payouts"):
        columns = {c["name"] for c in inspector.get_columns("partner_payouts")}
        for name, factory in PAYOUT_COLUMNS:
            if name not in columns:
                op.add_column("partner_payouts", factory())
        # Payouts recorded before this revision were all in the payout currency.
        op.execute(
            "UPDATE partner_payouts SET rate = 1.0 WHERE rate IS NULL"
        )
        op.execute(
            "UPDATE partner_payouts SET amount_rub = amount * rate "
            "WHERE amount_rub IS NULL OR amount_rub = 0"
        )

    if inspector.has_table("payments") and inspector.has_table("ad_attributions"):
        # Skip when a later revision has already renamed these columns — the
        # ledger is then filled by that revision instead.
        accrual_columns = {c["name"] for c in inspector.get_columns("campaign_accruals")}
        if {"amount_rub", "earned_rub"} <= accrual_columns:
            op.execute(_backfill_sql())


def downgrade() -> None:
    if context.is_offline_mode():
        for name, _factory in reversed(PAYOUT_COLUMNS):
            op.drop_column("partner_payouts", name)
        op.drop_index("ix_campaign_accruals_paid_at", table_name="campaign_accruals")
        op.drop_index("ix_campaign_accruals_user_id", table_name="campaign_accruals")
        op.drop_index(
            "ix_campaign_accruals_ad_campaign_id", table_name="campaign_accruals"
        )
        op.drop_table("campaign_accruals")
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("partner_payouts"):
        columns = {c["name"] for c in inspector.get_columns("partner_payouts")}
        for name, _factory in reversed(PAYOUT_COLUMNS):
            if name in columns:
                op.drop_column("partner_payouts", name)

    if inspector.has_table("campaign_accruals"):
        index_names = {ix["name"] for ix in inspector.get_indexes("campaign_accruals")}
        for name in (
            "ix_campaign_accruals_paid_at",
            "ix_campaign_accruals_user_id",
            "ix_campaign_accruals_ad_campaign_id",
        ):
            if name in index_names:
                op.drop_index(name, table_name="campaign_accruals")
        op.drop_table("campaign_accruals")
