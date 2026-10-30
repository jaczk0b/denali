"""Konta, sesje po stronie serwera, próby logowania, audit log.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("login", sa.String(64), nullable=False, unique=True),
        sa.Column("pw_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("totp_ct", sa.Text),
        sa.Column("totp_pending_ct", sa.Text),
        sa.Column("totp_last_step", sa.Integer, nullable=False, server_default="0"),
        sa.Column("backup_codes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("pw_changed_at", sa.Float, nullable=False),
    )
    op.create_table(
        "session",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("public_id", sa.String(16), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("csrf", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_seen_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("reauth_at", sa.Float),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"])
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("login", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
    )
    op.create_index("ix_login_attempt_login", "login_attempt", ["login"])
    op.create_index("ix_login_attempt_ip", "login_attempt", ["ip"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("user", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False, server_default=""),
        sa.Column("params", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("result", sa.String(200), nullable=False, server_default="ok"),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])


def downgrade():
    op.drop_table("audit_log")
    op.drop_table("login_attempt")
    op.drop_table("session")
    op.drop_table("app_user")
