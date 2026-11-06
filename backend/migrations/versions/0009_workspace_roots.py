"""workspaces: name / position / active for multi-root windows ("add folder to workspace")

Revision ID: 0009_workspace_roots
Revises: 0008_reviews
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_workspace_roots"
down_revision = "0008_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("name", sa.Text(), nullable=True))
        batch.add_column(sa.Column("position", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("active")
        batch.drop_column("position")
        batch.drop_column("name")
