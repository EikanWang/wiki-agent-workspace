"""github_pr_drafts.subject: comments on a whole file (not a line)

Revision ID: 0015_github_pr_drafts_subject
Revises: 0014_github_pr_drafts
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_github_pr_drafts_subject"
down_revision = "0014_github_pr_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("github_pr_drafts") as batch:
        batch.add_column(sa.Column("subject", sa.String(8), nullable=False, server_default="line"))


def downgrade() -> None:
    with op.batch_alter_table("github_pr_drafts") as batch:
        batch.drop_column("subject")
