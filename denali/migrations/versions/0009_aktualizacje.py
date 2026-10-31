"""Dostępne aktualizacje ze wszystkich źródeł (Proxmox, PBS, Komodo, agent) — jedna lista.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "update_item",
        sa.Column("integration_id", sa.Integer, sa.ForeignKey("integration.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("component", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="pakiet"),
        sa.Column("current", sa.String(100), nullable=False, server_default=""),
        sa.Column("available", sa.String(100), nullable=False, server_default=""),
        sa.Column("security", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("detected_at", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("integration_id", "external_id", "component", name="pk_update_item"),
    )


def downgrade():
    op.drop_table("update_item")
