"""locks: user-declared read-only paths (files or folders)

Revision ID: 0005_locks
Revises: 0004_threads_diff
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_locks"
down_revision = "0004_threads_diff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("is_dir", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "path", name="uq_locks_workspace_path"),
    )


def downgrade() -> None:
    op.drop_table("locks")
