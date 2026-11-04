"""Karta Terminale: sesje konsoli maszyn (kto, na czym, kiedy, jak się skończyła).

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "console_session",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.Float, nullable=False, index=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="SET NULL"), index=True),
        sa.Column("integration_id", sa.Integer, sa.ForeignKey("integration.id", ondelete="SET NULL")),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("target", sa.String(200), nullable=False, server_default=""),
        sa.Column("user", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("opened_at", sa.Float),
        sa.Column("closed_at", sa.Float),
        sa.Column("close_reason", sa.String(200), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_table("console_session")
