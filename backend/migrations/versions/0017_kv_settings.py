"""kv_settings: review hub preferences, extra agent context folders

Revision ID: 0017_kv_settings
Revises: 0016_named_workspace_closed_roots
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_kv_settings"
down_revision = "0016_named_workspace_closed_roots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("kv_settings", sa.Column("key", sa.Text(), primary_key=True), sa.Column("value", sa.Text(), nullable=False),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_table("kv_settings")
