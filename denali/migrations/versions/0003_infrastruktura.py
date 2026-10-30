"""Infrastruktura: urządzenia, adresy, zależności, kable, profile, wyjątki, integracje i kolejka do zatwierdzenia.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Profile wbudowane: urządzenie dostaje profil po typie, bez ustawiania czegokolwiek.
BUILTIN_PROFILES = [
    {"name": "Domyślny", "kinds": [], "settings": {}},
    {"name": "Sieć", "kinds": ["router", "switch", "ap", "firewall", "modem"], "settings": {"down_after_s": 60}},
    {"name": "Serwer fizyczny", "kinds": ["host", "nas"], "settings": {}},
    {"name": "Maszyna wirtualna", "kinds": ["vm", "lxc"], "settings": {}},
    {"name": "Stack Dockera", "kinds": ["kontener"], "settings": {}},
    {"name": "Usługa", "kinds": ["usluga"], "settings": {}},
]


def upgrade():
    profile = op.create_table(
        "profile",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("kinds", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("settings", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("builtin", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.bulk_insert(profile, [dict(p, builtin=True) for p in BUILTIN_PROFILES])

    op.create_table(
        "device",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("role", sa.String(300), nullable=False, server_default=""),
        sa.Column("criticality", sa.Integer, nullable=False, server_default="3"),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("device.id", ondelete="SET NULL")),
        sa.Column("profile_id", sa.Integer, sa.ForeignKey("profile.id", ondelete="SET NULL")),
        sa.Column("place", sa.String(200), nullable=False, server_default=""),
        sa.Column("serial", sa.String(100), nullable=False, server_default=""),
        sa.Column("notes_md", sa.Text, nullable=False, server_default=""),
        sa.Column("icon", sa.String(64), nullable=False, server_default=""),
        sa.Column("app_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("pulpit_group", sa.String(16), nullable=False, server_default="brak"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("ix_device_parent_id", "device", ["parent_id"])

    op.create_table(
        "device_address",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("device_id", "kind", "value", name="uq_device_address_device_id"),
    )
    op.create_index("ix_device_address_device_id", "device_address", ["device_id"])

    op.create_table(
        "dependency",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("depends_on_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("hard", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.UniqueConstraint("device_id", "depends_on_id", "type", name="uq_dependency_device_id"),
    )
    op.create_index("ix_dependency_device_id", "dependency", ["device_id"])
    op.create_index("ix_dependency_depends_on_id", "dependency", ["depends_on_id"])

    op.create_table(
        "cable",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("a_device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("a_port", sa.String(40), nullable=False, server_default=""),
        sa.Column("b_device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("b_port", sa.String(40), nullable=False, server_default=""),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
    )
    op.create_index("ix_cable_a_device_id", "cable", ["a_device_id"])
    op.create_index("ix_cable_b_device_id", "cable", ["b_device_id"])

    op.create_table(
        "device_override",
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
    )

    op.create_table(
        "integration",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("secret_ct", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("poll_seconds", sa.Integer, nullable=False, server_default="60"),
        sa.Column("last_ok_at", sa.Float),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
    )

    op.create_table(
        "external_ref",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("integration_id", sa.Integer, sa.ForeignKey("integration.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="nowy"),
        sa.Column("proposed", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("first_seen", sa.Float, nullable=False),
        sa.Column("last_seen", sa.Float, nullable=False),
        sa.UniqueConstraint("integration_id", "external_id", name="uq_external_ref_integration_id"),
    )
    op.create_index("ix_external_ref_device_id", "external_ref", ["device_id"])


def downgrade():
    for t in ("external_ref", "integration", "device_override", "cable", "dependency", "device_address", "device", "profile"):
        op.drop_table(t)
