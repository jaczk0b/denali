"""Sprawdzenia z LAN: ping i HTTP wyliczane z adresów i linków urządzeń.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "check_state",
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target", sa.String(500), nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("summary", sa.String(300), nullable=False, server_default=""),
        sa.Column("ms", sa.Float),
        sa.Column("cert_expires_at", sa.Float),
        sa.Column("first_fail_at", sa.Float),
        sa.Column("last_ok_at", sa.Float),
        sa.Column("checked_at", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("device_id", "kind", "target", name="pk_check_state"),
    )


def downgrade():
    op.drop_table("check_state")
