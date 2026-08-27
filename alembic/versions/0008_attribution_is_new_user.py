"""credit campaigns only for users who registered through the label

Revision ID: 0008_attribution_is_new_user
Revises: 0007_payment_base_amount
Create Date: 2026-08-27 00:00:00.000000

Until now, an already-registered user who later opened a campaign link was
attributed to it and every payment they made afterwards counted as that
campaign's revenue. That overstates paid traffic and lets a partner earn from
customers they did not bring.

`ad_attributions.is_new_user` marks the rows where the label *was* the user's
first contact with the bot. Campaign statistics count those exclusively; the
rest are kept for reference only.

Backfill: a registration and its attribution are written in the same request,
so the two timestamps land within seconds of each other. Rows where they differ
by more than five minutes are therefore later clicks by existing users, and are
flagged False. Accruals belonging to those rows are removed, because a campaign
must not keep revenue it is no longer credited for.
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008_attribution_is_new_user"
down_revision: Union[str, Sequence[str], None] = "0007_payment_base_amount"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# How far apart the registration and its attribution may sit and still count as
# the same visit.
REGISTRATION_WINDOW_SECONDS = 300

BACKFILL_SQL = f"""
    UPDATE ad_attributions a
    SET is_new_user = TRUE
    FROM users u
    WHERE u.user_id = a.user_id
      AND a.first_start_at IS NOT NULL
      AND u.registration_date IS NOT NULL
      AND ABS(EXTRACT(EPOCH FROM (a.first_start_at - u.registration_date)))
          <= {REGISTRATION_WINDOW_SECONDS}
"""

# A campaign keeps no ledger rows for users it is not credited for.
PRUNE_ACCRUALS_SQL = """
    DELETE FROM campaign_accruals ca
    USING ad_attributions a
    WHERE a.user_id = ca.user_id
      AND a.ad_campaign_id = ca.ad_campaign_id
      AND a.is_new_user = FALSE
"""


def _add_column() -> None:
    op.add_column(
        "ad_attributions",
        sa.Column(
            "is_new_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def upgrade() -> None:
    if context.is_offline_mode():
        _add_column()
        op.execute(BACKFILL_SQL)
        op.execute(PRUNE_ACCRUALS_SQL)
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("ad_attributions"):
        return

    columns = {c["name"] for c in inspector.get_columns("ad_attributions")}
    if "is_new_user" not in columns:
        _add_column()

    if inspector.has_table("users"):
        op.execute(BACKFILL_SQL)

    if inspector.has_table("campaign_accruals"):
        op.execute(PRUNE_ACCRUALS_SQL)


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_column("ad_attributions", "is_new_user")
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("ad_attributions"):
        columns = {c["name"] for c in inspector.get_columns("ad_attributions")}
        if "is_new_user" in columns:
            op.drop_column("ad_attributions", "is_new_user")
