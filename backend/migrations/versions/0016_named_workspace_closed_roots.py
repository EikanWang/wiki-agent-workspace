"""named_workspaces.closed_roots: config.toml folders closed in the UI stay closed

Revision ID: 0016_named_workspace_closed_roots
Revises: 0015_github_pr_drafts_subject
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_named_workspace_closed_roots"
down_revision = "0015_github_pr_drafts_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("named_workspaces") as batch:
        batch.add_column(sa.Column("closed_roots", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("named_workspaces") as batch:
        batch.drop_column("closed_roots")
