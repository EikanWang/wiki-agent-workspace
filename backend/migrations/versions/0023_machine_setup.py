"""machine setup: what a machine gets from this server (machines.env_json), machine_jobs

Revision ID: 0023_machine_setup
Revises: 0022_remote_tmux
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_machine_setup"
down_revision = "0022_remote_tmux"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("machines") as b:
        b.add_column(sa.Column("env_json", sa.Text(), nullable=False, server_default="{}"))
    op.create_table(
        "machine_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("machine", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_machine_jobs_machine", "machine_jobs", ["machine", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_machine_jobs_machine", table_name="machine_jobs")
    op.drop_table("machine_jobs")
    with op.batch_alter_table("machines") as b:
        b.drop_column("env_json")
