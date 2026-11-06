"""reviews + review_comments: stash diff comments, submit them as one review

Revision ID: 0008_reviews
Revises: 0007_file_reviews
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_reviews"
down_revision = "0007_file_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("base", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("agent_sessions.id"), nullable=True),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "review_comments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("review_id", sa.String(length=36), sa.ForeignKey("reviews.id"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("line_text", sa.Text(), nullable=False),
        sa.Column("hunk", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(length=36), sa.ForeignKey("threads.id"), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_comments_review", "review_comments", ["review_id"])


def downgrade() -> None:
    op.drop_index("ix_review_comments_review", table_name="review_comments")
    op.drop_table("review_comments")
    op.drop_table("reviews")
