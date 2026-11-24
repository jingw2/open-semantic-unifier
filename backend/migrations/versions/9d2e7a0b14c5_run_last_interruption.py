"""Preserve the most recent worker interruption across successful retries."""
from alembic import op
import sqlalchemy as sa

revision = "9d2e7a0b14c5"
down_revision = "6c7d8e9f102a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("unification_runs", sa.Column("last_interruption", sa.String(length=2000), nullable=True))


def downgrade():
    op.drop_column("unification_runs", "last_interruption")
