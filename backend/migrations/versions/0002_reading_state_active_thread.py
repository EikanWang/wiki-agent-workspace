"""reading_state.active_thread_id: remember which discussion was open

Revision ID: 0002_active_thread
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_active_thread"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reading_state") as batch:
        batch.add_column(sa.Column("active_thread_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reading_state") as batch:
        batch.drop_column("active_thread_id")
