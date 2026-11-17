"""review learning: agent suggestion originals and feedback, review_cases, review_lessons

Revision ID: 0020_review_learning
Revises: 0019_dev_workspaces
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_review_learning"
down_revision = "0019_dev_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("github_pr_drafts") as b:
        b.add_column(sa.Column("original_body", sa.Text(), nullable=True))
        b.add_column(sa.Column("discard_reason", sa.String(16), nullable=True))
        b.add_column(sa.Column("severity", sa.String(8), nullable=True))
        b.add_column(sa.Column("category", sa.String(24), nullable=True))
    op.create_table(
        "review_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repo", sa.Text(), nullable=False, index=True),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("line_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("agent_body", sa.Text(), nullable=True),
        sa.Column("final_body", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(16), nullable=True),
        sa.Column("severity", sa.String(8), nullable=True),
        sa.Column("category", sa.String(24), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("gh_ref", sa.Text(), nullable=True, index=True),
        sa.Column("distilled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "review_lessons",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False, index=True),
        sa.Column("paths", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("replaces", sa.String(36), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contradicted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("flagged", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("review_lessons")
    op.drop_table("review_cases")
    with op.batch_alter_table("github_pr_drafts") as b:
        for c in ("category", "severity", "discard_reason", "original_body"):
            b.drop_column(c)
