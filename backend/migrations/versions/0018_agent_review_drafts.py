"""github_pr_drafts: agent review suggestions (origin, decision, thread_id)

Revision ID: 0018_agent_review_drafts
Revises: 0017_kv_settings
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_agent_review_drafts"
down_revision = "0017_kv_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("github_pr_drafts") as b:
        b.add_column(sa.Column("origin", sa.String(8), nullable=False, server_default="user"))
        b.add_column(sa.Column("decision", sa.String(10), nullable=False, server_default="accepted"))
        b.add_column(sa.Column("thread_id", sa.String(36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("github_pr_drafts") as b:
        b.drop_column("thread_id")
        b.drop_column("decision")
        b.drop_column("origin")
