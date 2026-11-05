"""Nowy pulpit: wyciszanie incydentu, widżety Statystyk i miejsce na logo aplikacji.

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

STARTER = ("proxmox", "pula", "kopie", "kontenery", "siec", "certyfikaty")


def upgrade():
    op.add_column("incident", sa.Column("muted_until", sa.Float))
    op.add_column("desk_tile", sa.Column("logo", sa.String(200), nullable=False, server_default=""))
    op.create_table(
        "desk_widget",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(100), nullable=False, server_default=""),
        sa.Column("size", sa.Integer, nullable=False, server_default="1"),
        sa.Column("config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    )
    conn = op.get_bind()
    for pos, kind in enumerate(STARTER):
        conn.execute(sa.text("INSERT INTO desk_widget (kind, position) VALUES (:k, :p)"), {"k": kind, "p": pos})


def downgrade():
    op.drop_table("desk_widget")
    op.execute("ALTER TABLE desk_tile DROP COLUMN logo")
    op.execute("ALTER TABLE incident DROP COLUMN muted_until")
