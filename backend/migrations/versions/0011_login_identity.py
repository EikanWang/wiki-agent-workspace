"""login_sessions.identity: who logged in ("token" or "github:<login>")

Revision ID: 0011_login_identity
Revises: 0010_named_workspaces
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_login_identity"
down_revision = "0010_named_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("login_sessions") as batch:
        batch.add_column(sa.Column("identity", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("login_sessions") as batch:
        batch.drop_column("identity")
