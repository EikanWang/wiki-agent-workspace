"""machines: SSH machines and bastions (Machines view), with their last check and probe

Revision ID: 0021_machines
Revises: 0020_review_learning
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_machines"
down_revision = "0020_review_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machines",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False, server_default="machine"),
        sa.Column("source", sa.String(8), nullable=False, server_default="ui"),
        sa.Column("ssh", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("user", sa.Text(), nullable=True),
        sa.Column("identity_file", sa.Text(), nullable=True),
        sa.Column("via_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("options_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("work_dir", sa.Text(), nullable=False, server_default="~/.cache/wiki-agent-workspace"),
        sa.Column("code_server", sa.Text(), nullable=False, server_default="code-server"),
        sa.Column("opencode", sa.Text(), nullable=False, server_default="opencode"),
        sa.Column("codex", sa.Text(), nullable=False, server_default="codex"),
        sa.Column("status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capabilities_json", sa.Text(), nullable=True),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("machines")
