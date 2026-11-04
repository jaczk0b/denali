"""Kopie konfiguracji routerów OpenWrt — archiwum zaszyfrowane w bazie (zawiera hasła Wi-Fi).

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "config_backup",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("source", sa.String(100), nullable=False, server_default=""),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("taken_at", sa.Float, nullable=False),
        sa.Column("checked_at", sa.Float, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("data_ct", sa.Text, nullable=False),
        sa.Column("taken_by", sa.String(64), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_table("config_backup")
