"""remote tmux: terminals and command runs that live in tmux on a machine

Revision ID: 0022_remote_tmux
Revises: 0021_machines
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_remote_tmux"
down_revision = "0021_machines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("terminals") as b:
        b.add_column(sa.Column("machine", sa.Text(), nullable=True))
    with op.batch_alter_table("dev_task_runs") as b:
        b.add_column(sa.Column("machine", sa.Text(), nullable=True))
        b.add_column(sa.Column("remote_dir", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dev_task_runs") as b:
        b.drop_column("remote_dir")
        b.drop_column("machine")
    with op.batch_alter_table("terminals") as b:
        b.drop_column("machine")
