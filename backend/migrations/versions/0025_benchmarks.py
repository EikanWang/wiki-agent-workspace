"""benchmarks: definitions kept in the app and runs with their results

Revision ID: 0025_benchmarks
Revises: 0024_machine_recipes
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_benchmarks"
down_revision = "0024_machine_recipes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bench_defs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("scope", "scope_key", "name", name="uq_bench_defs_scope_name"),
    )
    op.create_table(
        "bench_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dev_id", sa.String(36), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("def_name", sa.Text(), nullable=False),
        sa.Column("def_source", sa.String(8), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("machine", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(4), nullable=False, server_default="run"),
        sa.Column("variant", sa.String(1), nullable=True),
        sa.Column("ab_id", sa.String(36), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("dirty_hash", sa.String(16), nullable=True),
        sa.Column("env_hash", sa.String(16), nullable=True),
        sa.Column("caps_json", sa.Text(), nullable=True),
        sa.Column("samples_json", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.Column("lock_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("task_run_id", sa.String(36), nullable=True),
        sa.Column("baseline_id", sa.String(36), nullable=True),
        sa.Column("worktrees_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bench_runs_dev_id", "bench_runs", ["dev_id"])
    op.create_index("ix_bench_runs_ab_id", "bench_runs", ["ab_id"])
    op.create_index("ix_bench_runs_task_run_id", "bench_runs", ["task_run_id"])


def downgrade() -> None:
    op.drop_table("bench_runs")
    op.drop_table("bench_defs")
