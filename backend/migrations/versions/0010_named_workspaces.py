"""named workspaces (VS Code-style named sets of folders) and the workspace of each terminal tab

Revision ID: 0010_named_workspaces
Revises: 0009_workspace_roots
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_named_workspaces"
down_revision = "0009_workspace_roots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "named_workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("folders", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The Default workspace is created at startup from the folders open today (config + folders added in the UI).
    with op.batch_alter_table("terminals") as batch:
        batch.add_column(sa.Column("named_workspace_id", sa.String(36), sa.ForeignKey("named_workspaces.id", name="fk_terminals_named_workspace"), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("terminals") as batch:
        batch.drop_column("named_workspace_id")
    op.drop_table("named_workspaces")
