"""machine recipes (setup steps) and their state per machine; dev_workspaces.clone_url

Revision ID: 0024_machine_recipes
Revises: 0023_machine_setup
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_machine_recipes"
down_revision = "0023_machine_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machine_recipes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("run", sa.Text(), nullable=False),
        sa.Column("check", sa.Text(), nullable=True),
        sa.Column("env_out", sa.Text(), nullable=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "machine_recipe_state",
        sa.Column("machine", sa.Text(), primary_key=True),
        sa.Column("recipe_id", sa.String(36), sa.ForeignKey("machine_recipes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="not_run"),
        sa.Column("version_ok", sa.String(16), nullable=True),
        sa.Column("env_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("last_job_id", sa.String(36), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    with op.batch_alter_table("dev_workspaces") as b:
        b.add_column(sa.Column("clone_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dev_workspaces") as b:
        b.drop_column("clone_url")
    op.drop_table("machine_recipe_state")
    op.drop_table("machine_recipes")
