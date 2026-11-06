"""Sposób sprawdzania zamiast „monitoruj tak/nie”: True → auto, False → brak (profile i wyjątki na urządzeniach).

Revision ID: 0023
Revises: 0022
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

MAP = {True: "auto", False: "brak"}


def upgrade():
    conn = op.get_bind()
    for pid, raw in conn.execute(sa.text("SELECT id, settings FROM profile")).all():
        s = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if isinstance(s.get("monitor"), bool):
            s["monitor"] = MAP[s["monitor"]]
            conn.execute(sa.text("UPDATE profile SET settings = :s WHERE id = :id"), {"s": json.dumps(s), "id": pid})
    for did, raw in conn.execute(sa.text("SELECT device_id, value FROM device_override WHERE key = 'monitor'")).all():
        v = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(v, bool):
            conn.execute(sa.text("UPDATE device_override SET value = :v WHERE device_id = :d AND key = 'monitor'"),
                         {"v": json.dumps(MAP[v]), "d": did})


def downgrade():
    pass
