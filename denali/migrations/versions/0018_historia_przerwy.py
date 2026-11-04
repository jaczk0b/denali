"""Historia stanów, czasów odpowiedzi i zajętości magazynów, planowane przerwy, sprawdzenia portów TCP.

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "state_change",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("why", sa.String(300), nullable=False, server_default=""),
    )
    op.create_index("ix_state_change_device_ts", "state_change", ["device_id", "ts"])
    op.create_table(
        "check_sample",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("ms", sa.Float),
    )
    op.create_index("ix_check_sample_device_ts", "check_sample", ["device_id", "ts"])
    op.create_table(
        "usage_sample",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("integration_id", sa.Integer, sa.ForeignKey("integration.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("used", sa.Float, nullable=False),
        sa.Column("total", sa.Float, nullable=False),
    )
    op.create_index("ix_usage_sample_integration_name_ts", "usage_sample", ["integration_id", "name", "ts"])
    op.create_table(
        "maintenance",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("starts_at", sa.Float, nullable=False),
        sa.Column("ends_at", sa.Float, nullable=False),
        sa.Column("reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
    )
    with op.batch_alter_table("device") as b:
        b.add_column(sa.Column("tcp_ports", sa.String(200), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("device") as b:
        b.drop_column("tcp_ports")
    op.drop_table("maintenance")
    op.drop_index("ix_usage_sample_integration_name_ts", "usage_sample")
    op.drop_table("usage_sample")
    op.drop_index("ix_check_sample_device_ts", "check_sample")
    op.drop_table("check_sample")
    op.drop_index("ix_state_change_device_ts", "state_change")
    op.drop_table("state_change")
