"""Logowanie przez OIDC (Pocket ID): powiązanie konta z tożsamością u dostawcy i sposób logowania sesji.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("app_user") as b:
        b.add_column(sa.Column("oidc_sub", sa.String(300), nullable=True))
        b.create_unique_constraint("uq_app_user_oidc_sub", ["oidc_sub"])
    with op.batch_alter_table("session") as b:
        b.add_column(sa.Column("method", sa.String(16), nullable=False, server_default="haslo"))


def downgrade():
    with op.batch_alter_table("session") as b:
        b.drop_column("method")
    with op.batch_alter_table("app_user") as b:
        b.drop_constraint("uq_app_user_oidc_sub", type_="unique")
        b.drop_column("oidc_sub")
