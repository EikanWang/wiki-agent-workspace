"""documents.hidden: hide an entry from Recent without deleting its history

Revision ID: 0003_documents_hidden
Revises: 0002_active_thread
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_documents_hidden"
down_revision = "0002_active_thread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("hidden")
