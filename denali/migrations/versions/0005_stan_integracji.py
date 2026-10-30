"""Stan z integracji: ostatni znany stan obiektów i czas ostatniej próby odczytu.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "observed_state",
        sa.Column("integration_id", sa.Integer, sa.ForeignKey("integration.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False, server_default=""),
        sa.Column("data", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("observed_at", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("integration_id", "external_id", name="pk_observed_state"),
    )
    with op.batch_alter_table("integration") as b:
        b.add_column(sa.Column("last_attempt_at", sa.Float))


def downgrade():
    with op.batch_alter_table("integration") as b:
        b.drop_column("last_attempt_at")
    op.drop_table("observed_state")
