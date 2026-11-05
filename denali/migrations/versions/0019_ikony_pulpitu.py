"""Ikony pulpitu: własne kategorie i kafle (też bez urządzenia) zamiast sztywnego device.pulpit_group.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

STARTER = [("codzien", "Na co dzień"), ("zarzadzanie", "Zarządzanie")]


def upgrade():
    op.create_table(
        "desk_category",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "desk_tile",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("desk_category.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), unique=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("url", sa.String(500), nullable=False, server_default=""),
        sa.Column("icon", sa.String(64), nullable=False, server_default=""),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_desk_tile_category_id", "desk_tile", ["category_id"])
    conn = op.get_bind()
    for pos, (group, name) in enumerate(STARTER):
        cat = conn.execute(sa.text("INSERT INTO desk_category (name, position) VALUES (:n, :p)"),
                           {"n": name, "p": pos}).lastrowid
        rows = conn.execute(sa.text("SELECT id FROM device WHERE pulpit_group = :g ORDER BY name"), {"g": group}).all()
        for i, r in enumerate(rows):
            conn.execute(sa.text("INSERT INTO desk_tile (category_id, device_id, position) VALUES (:c, :d, :p)"),
                         {"c": cat, "d": r.id, "p": i})
    # Zwykłe DROP COLUMN, nie batch: przebudowa tabeli device przy włączonych kluczach obcych skasowałaby jej dzieci.
    op.execute("ALTER TABLE device DROP COLUMN pulpit_group")


def downgrade():
    op.add_column("device", sa.Column("pulpit_group", sa.String(16), nullable=False, server_default="brak"))
    conn = op.get_bind()
    first = conn.execute(sa.text("SELECT id FROM desk_category ORDER BY position, id LIMIT 1")).scalar()
    for r in conn.execute(sa.text("SELECT device_id, category_id FROM desk_tile WHERE device_id IS NOT NULL")).all():
        conn.execute(sa.text("UPDATE device SET pulpit_group = :g WHERE id = :d"),
                     {"g": "codzien" if r.category_id == first else "zarzadzanie", "d": r.device_id})
    op.drop_table("desk_tile")
    op.drop_table("desk_category")
