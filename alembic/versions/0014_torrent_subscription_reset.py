"""Persist the torrent reset checkpoint and optional IP block metadata.

Revision ID: 0014_torrent_subscription_reset
Revises: 0013_custom_torrent_reports
"""

from alembic import op
import sqlalchemy as sa

revision = "0014_torrent_subscription_reset"
down_revision = "0013_custom_torrent_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("torrent_reports", sa.Column("reset_marker", sa.String(64)))
    op.add_column("torrent_reports", sa.Column("block_mode", sa.String(8)))
    op.add_column("torrent_reports", sa.Column("block_seconds", sa.Integer()))


def downgrade():
    op.drop_column("torrent_reports", "block_seconds")
    op.drop_column("torrent_reports", "block_mode")
    op.drop_column("torrent_reports", "reset_marker")
