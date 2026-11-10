"""github_repo_wikis: which wiki folder the agent uses for a GitHub repository's pull requests

Revision ID: 0013_github_repo_wikis
Revises: 0012_thread_target
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_github_repo_wikis"
down_revision = "0012_thread_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_repo_wikis",
        sa.Column("repo", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", name="fk_github_repo_wikis_workspace"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("github_repo_wikis")
