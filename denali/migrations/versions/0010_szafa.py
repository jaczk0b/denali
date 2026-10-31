"""Szafa (rack): szafy i co na której wysokości stoi.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rack",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("units", sa.Integer, nullable=False, server_default="42"),
        sa.Column("place", sa.String(200), nullable=False, server_default=""),
        sa.Column("notes", sa.String(500), nullable=False, server_default=""),
    )
    op.create_table(
        "rack_item",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("rack_id", sa.Integer, sa.ForeignKey("rack.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), unique=True),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("u", sa.Integer, nullable=False),
        sa.Column("height", sa.Integer, nullable=False, server_default="1"),
        sa.Column("back", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_table("rack_item")
    op.drop_table("rack")
