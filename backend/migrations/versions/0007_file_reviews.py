"""file_reviews: 'Viewed' checkbox per file in the diff review, bound to the reviewed content hash

Revision ID: 0007_file_reviews
Revises: 0006_session_model
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_file_reviews"
down_revision = "0006_session_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_reviews",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("base", sa.Text(), nullable=False),
        sa.Column("reviewed_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "path", "base", name="uq_file_reviews_ws_path_base"),
    )


def downgrade() -> None:
    op.drop_table("file_reviews")
