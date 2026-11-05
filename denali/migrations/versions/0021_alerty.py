"""Alerty, oś czasu zmian, terminy i własne sprawdzenia (funkcje przywrócone z HLC).

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("fingerprint", sa.String(400), nullable=False, index=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), index=True),
        sa.Column("integration_id", sa.Integer),
        sa.Column("external_id", sa.String(200)),
        sa.Column("rule", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("first_seen", sa.Float, nullable=False),
        sa.Column("last_seen", sa.Float, nullable=False),
        sa.Column("resolved_at", sa.Float),
        sa.Column("notified_at", sa.Float),
        sa.Column("resolved_notified_at", sa.Float),
        sa.Column("silenced_until", sa.Float),
    )
    op.create_table(
        "change_event",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("text", sa.String(300), nullable=False),
        sa.Index("ix_change_event_device_ts", "device_id", "ts"),
    )
    op.create_table(
        "device_term",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("due_on", sa.String(10), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
    )
    op.create_table(
        "custom_check",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target", sa.String(500), nullable=False),
        sa.Column("expect", sa.String(200), nullable=False, server_default=""),
        sa.UniqueConstraint("device_id", "kind", "target"),
    )
    op.add_column("incident", sa.Column("reminded_at", sa.Float))


def downgrade():
    with op.batch_alter_table("incident") as b:
        b.drop_column("reminded_at")
    op.drop_table("custom_check")
    op.drop_table("device_term")
    op.drop_table("change_event")
    op.drop_table("alert")
