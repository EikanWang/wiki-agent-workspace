"""github_pr_drafts: review comments written in the PR tab, published to GitHub together as one review

Revision ID: 0014_github_pr_drafts
Revises: 0013_github_repo_wikis
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_github_pr_drafts"
down_revision = "0013_github_repo_wikis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_pr_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("start_side", sa.String(5), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("line_text", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_github_pr_drafts_repo", "github_pr_drafts", ["repo"])


def downgrade() -> None:
    op.drop_index("ix_github_pr_drafts_repo", "github_pr_drafts")
    op.drop_table("github_pr_drafts")
