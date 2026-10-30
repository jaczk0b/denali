"""Początek: tabela ustawień.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "setting",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )


def downgrade():
    op.drop_table("setting")
