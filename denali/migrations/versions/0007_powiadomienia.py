"""Powiadomienia: kanały (Discord) i znaczniki wysłania przy incydentach.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_channel",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("config_ct", sa.Text, nullable=False),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="ostrzezenie"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_sent_at", sa.Float),
        sa.Column("last_error", sa.String(300), nullable=False, server_default=""),
    )
    with op.batch_alter_table("incident") as b:
        b.add_column(sa.Column("notified_at", sa.Float))
        b.add_column(sa.Column("closed_notified_at", sa.Float))


def downgrade():
    with op.batch_alter_table("incident") as b:
        b.drop_column("closed_notified_at")
        b.drop_column("notified_at")
    op.drop_table("notification_channel")
