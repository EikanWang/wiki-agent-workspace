"""agent_sessions.model / variant: per-session model override

Revision ID: 0006_session_model
Revises: 0005_locks
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_session_model"
down_revision = "0005_locks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_sessions") as batch:
        batch.add_column(sa.Column("model", sa.Text(), nullable=True))
        batch.add_column(sa.Column("variant", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_column("variant")
        batch.drop_column("model")
