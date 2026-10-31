"""Szafa: półki — pozycja zajmuje kilka U, a stoją na niej obok siebie urządzenia (router, komputer, terminal).

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("rack_item") as b:
        b.add_column(sa.Column("shelf", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("shelf_id", sa.Integer, nullable=True))
        b.create_foreign_key("fk_rack_item_shelf_id", "rack_item", ["shelf_id"], ["id"], ondelete="CASCADE")
        b.create_index("ix_rack_item_shelf_id", ["shelf_id"])


def downgrade():
    with op.batch_alter_table("rack_item") as b:
        b.drop_index("ix_rack_item_shelf_id")
        b.drop_constraint("fk_rack_item_shelf_id", type_="foreignkey")
        b.drop_column("shelf_id")
        b.drop_column("shelf")
