"""dev_workspaces, dev_task_runs: PR Development

Revision ID: 0019_dev_workspaces
Revises: 0018_agent_review_drafts
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_dev_workspaces"
down_revision = "0018_agent_review_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dev_workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False, server_default="local"),
        sa.Column("source_repo", sa.Text(), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=True),
        sa.Column("github_repo", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("push_remote", sa.Text(), nullable=True),
        sa.Column("push_branch", sa.Text(), nullable=True),
        sa.Column("wiki_workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "dev_task_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dev_id", sa.String(36), sa.ForeignKey("dev_workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dev_task_runs_dev_id", "dev_task_runs", ["dev_id"])


def downgrade() -> None:
    op.drop_index("ix_dev_task_runs_dev_id", "dev_task_runs")
    op.drop_table("dev_task_runs")
    op.drop_table("dev_workspaces")
