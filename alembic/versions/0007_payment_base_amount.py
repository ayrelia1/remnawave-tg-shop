"""normalise payments into a base currency and move rates into the database

Revision ID: 0007_payment_base_amount
Revises: 0006_campaign_accruals
Create Date: 2026-08-27 00:00:00.000000

Conversion moves from the campaign ledger onto the payment itself: every
payment now carries `base_amount` and the `fx_rate` that produced it, frozen at
purchase time. Every money total in the bot — financial statistics, user LTV,
referral revenue, campaign revenue — becomes a SUM over that one column, so
they can no longer disagree with each other.

Rates themselves move from PARTNER_CURRENCY_RATES into `currency_rates`, which
admins edit from the panel. The env var is read once here to seed the table so
an existing configuration carries over.

Existing payments are valued 1:1 (`base_amount = amount`, `fx_rate = 1`), which
is exactly how every total in the bot treated them before this revision — the
upgrade therefore changes no historical number.
"""

import os
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007_payment_base_amount"
down_revision: Union[str, Sequence[str], None] = "0006_campaign_accruals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAYMENT_COLUMNS = (
    ("base_amount", lambda: sa.Column("base_amount", sa.Float(), nullable=True)),
    ("fx_rate", lambda: sa.Column("fx_rate", sa.Float(), nullable=True)),
)

# (table, old name, new name)
RENAMES = (
    ("campaign_accruals", "amount_rub", "base_amount"),
    ("campaign_accruals", "earned_rub", "earned_amount"),
    ("partner_payouts", "amount_rub", "base_amount"),
    ("partner_payouts", "rate", "fx_rate"),
)


def _seed_rates() -> dict:
    """Base currency at 1.0, plus whatever PARTNER_CURRENCY_RATES carried."""
    from config.currency import BASE_CURRENCY

    rates = {BASE_CURRENCY.upper(): 1.0, "XTR": 1.0}
    raw = (os.getenv("PARTNER_CURRENCY_RATES") or "").strip()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        code, _sep, value = chunk.partition("=")
        try:
            rates[code.strip().upper()] = float(value.strip().replace(",", "."))
        except ValueError:
            continue
    return rates


def _create_rates_table() -> None:
    op.create_table(
        "currency_rates",
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("currency"),
    )


def _insert_seed_rates() -> None:
    values = ", ".join(
        f"('{code}', {rate})" for code, rate in sorted(_seed_rates().items())
    )
    op.execute(
        f"INSERT INTO currency_rates (currency, rate) VALUES {values} "
        "ON CONFLICT (currency) DO NOTHING"
    )
    # Whatever else the ledger already contains gets a 1:1 row, so no existing
    # currency is left without a rate. Admins can correct any of them from the
    # panel afterwards; the correction applies to later payments only.
    op.execute(
        "INSERT INTO currency_rates (currency, rate) "
        "SELECT DISTINCT UPPER(currency), 1.0 FROM payments "
        "WHERE currency IS NOT NULL AND currency <> '' "
        "ON CONFLICT (currency) DO NOTHING"
    )


def _value_existing_payments() -> None:
    """Value history 1:1, which is exactly how every total treated it before."""
    op.execute(
        "UPDATE payments SET fx_rate = 1.0, base_amount = amount "
        "WHERE base_amount IS NULL"
    )


# Runs after the renames, so it uses the final column names. 0006 could only
# backfill the currencies it knew a rate for; now that every payment carries its
# own value, the ledger can be completed from the payments themselves.
BACKFILL_ACCRUALS_SQL = """
    INSERT INTO campaign_accruals (
        ad_campaign_id, payment_id, user_id, amount, currency, base_amount,
        percent, earned_amount, provider, subscription_duration_months,
        hwid_device_limit, paid_at
    )
    SELECT
        a.ad_campaign_id,
        p.payment_id,
        p.user_id,
        p.amount,
        UPPER(p.currency),
        p.base_amount,
        COALESCE(c.partner_percent, 0),
        p.base_amount * COALESCE(c.partner_percent, 0) / 100.0,
        p.provider,
        p.subscription_duration_months,
        p.hwid_device_limit,
        p.created_at
    FROM payments p
    JOIN ad_attributions a ON a.user_id = p.user_id
    JOIN ad_campaigns c ON c.ad_campaign_id = a.ad_campaign_id
    WHERE p.status = 'succeeded'
      AND p.created_at >= a.first_start_at
      AND p.base_amount IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM campaign_accruals ca WHERE ca.payment_id = p.payment_id
      )
"""


def upgrade() -> None:
    if context.is_offline_mode():
        for _name, factory in PAYMENT_COLUMNS:
            op.add_column("payments", factory())
        _create_rates_table()
        _insert_seed_rates()
        _value_existing_payments()
        for table, old, new in RENAMES:
            op.alter_column(table, old, new_column_name=new)
        op.drop_column("campaign_accruals", "rate")
        op.execute(BACKFILL_ACCRUALS_SQL)
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("payments"):
        columns = {c["name"] for c in inspector.get_columns("payments")}
        for name, factory in PAYMENT_COLUMNS:
            if name not in columns:
                op.add_column("payments", factory())

    if not inspector.has_table("currency_rates"):
        _create_rates_table()
    _insert_seed_rates()

    _value_existing_payments()

    for table, old, new in RENAMES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if old in columns and new not in columns:
            op.alter_column(table, old, new_column_name=new)

    if inspector.has_table("campaign_accruals"):
        columns = {c["name"] for c in inspector.get_columns("campaign_accruals")}
        if "rate" in columns:
            # The rate now lives on the payment the accrual was built from.
            op.drop_column("campaign_accruals", "rate")

        # Complete the ledger for anything 0006 had to skip.
        if inspector.has_table("payments") and inspector.has_table("ad_attributions"):
            op.execute(BACKFILL_ACCRUALS_SQL)


def downgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "campaign_accruals",
            sa.Column("rate", sa.Float(), nullable=False, server_default="1.0"),
        )
        for table, old, new in RENAMES:
            op.alter_column(table, new, new_column_name=old)
        op.drop_table("currency_rates")
        for name, _factory in reversed(PAYMENT_COLUMNS):
            op.drop_column("payments", name)
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("campaign_accruals"):
        columns = {c["name"] for c in inspector.get_columns("campaign_accruals")}
        if "rate" not in columns:
            op.add_column(
                "campaign_accruals",
                sa.Column("rate", sa.Float(), nullable=False, server_default="1.0"),
            )

    for table, old, new in RENAMES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if new in columns and old not in columns:
            op.alter_column(table, new, new_column_name=old)

    if inspector.has_table("currency_rates"):
        op.drop_table("currency_rates")

    if inspector.has_table("payments"):
        columns = {c["name"] for c in inspector.get_columns("payments")}
        for name, _factory in reversed(PAYMENT_COLUMNS):
            if name in columns:
                op.drop_column("payments", name)
