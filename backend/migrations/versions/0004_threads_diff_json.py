"""threads.diff_json: diff context for comments made in the diff review

Revision ID: 0004_threads_diff
Revises: 0003_documents_hidden
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_threads_diff"
down_revision = "0003_documents_hidden"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.add_column(sa.Column("diff_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.drop_column("diff_json")
