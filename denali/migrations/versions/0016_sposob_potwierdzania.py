"""Którym sposobem konto potwierdza najpierw: hasłem i kodem czy przez Pocket ID.

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("app_user") as b:
        b.add_column(sa.Column("reauth_method", sa.String(16), nullable=False, server_default="haslo"))


def downgrade():
    with op.batch_alter_table("app_user") as b:
        b.drop_column("reauth_method")
