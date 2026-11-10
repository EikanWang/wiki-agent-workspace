"""threads.target_json: discussions about a page, slide or area of a laid-out document (with attached images)

Revision ID: 0012_thread_target
Revises: 0011_login_identity
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_thread_target"
down_revision = "0011_login_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.add_column(sa.Column("target_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.drop_column("target_json")
