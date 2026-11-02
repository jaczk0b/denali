"""Sprzątanie: adresy z sieci wewnętrznej Home Assistanta (interfejs hassio, 172.30.32.0/23), które adapter Proxmoxa
dopisał do urządzeń, zanim pomijał ten interfejs. Usuwa tylko adresy dopisane przez integrację, nie wpisane ręcznie.

Revision ID: 0013
Revises: 0012
"""
import ipaddress

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

HASSIO = ipaddress.ip_network("172.30.32.0/23")


def upgrade():
    conn = op.get_bind()
    addr = sa.table("device_address", sa.column("id", sa.Integer), sa.column("kind", sa.String),
                    sa.column("value", sa.String), sa.column("source", sa.String))
    bad = []
    for r in conn.execute(sa.select(addr.c.id, addr.c.value).where(addr.c.kind == "ip", addr.c.source == "integracja")):
        try:
            if ipaddress.ip_address(r.value) in HASSIO:
                bad.append(r.id)
        except ValueError:
            continue
    if bad:
        conn.execute(addr.delete().where(addr.c.id.in_(bad)))


def downgrade():
    pass
