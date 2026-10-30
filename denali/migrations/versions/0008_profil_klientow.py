"""Profil wbudowany „Komputery i telefony”: bez pingu i bez powiadomień (uśpiony telefon to nie awaria).

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

NAME = "Komputery i telefony"


def upgrade():
    profile = sa.table("profile", sa.column("name", sa.String), sa.column("kinds", sa.JSON),
                       sa.column("settings", sa.JSON), sa.column("builtin", sa.Boolean))
    conn = op.get_bind()
    if not conn.execute(sa.text("SELECT 1 FROM profile WHERE name = :n"), {"n": NAME}).first():
        op.bulk_insert(profile, [{"name": NAME, "kinds": ["komputer", "telefon"],
                                  "settings": {"monitor": False, "notify": False}, "builtin": True}])


def downgrade():
    op.get_bind().execute(sa.text("DELETE FROM profile WHERE name = :n AND builtin"), {"n": NAME})
