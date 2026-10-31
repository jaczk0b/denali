"""Akcje (Etap 5): przebieg każdej akcji i osobny, zaszyfrowany wpis z tokenem do akcji przy integracji.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("integration") as b:
        b.add_column(sa.Column("action_ct", sa.Text, nullable=True))
    op.create_table(
        "action_run",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="SET NULL"), nullable=True),
        sa.Column("integration_id", sa.Integer, sa.ForeignKey("integration.id", ondelete="SET NULL"), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("target", sa.String(200), nullable=False, server_default=""),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("after_id", sa.Integer, sa.ForeignKey("action_run.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task", sa.JSON, nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("started_at", sa.Float, nullable=True),
        sa.Column("finished_at", sa.Float, nullable=True),
        sa.Column("result", sa.Text, nullable=False, server_default=""),
    )
    op.create_index("ix_action_run_created_at", "action_run", ["created_at"])
    op.create_index("ix_action_run_device_id", "action_run", ["device_id"])
    op.create_index("ix_action_run_status", "action_run", ["status"])


def downgrade():
    op.drop_table("action_run")
    with op.batch_alter_table("integration") as b:
        b.drop_column("action_ct")
