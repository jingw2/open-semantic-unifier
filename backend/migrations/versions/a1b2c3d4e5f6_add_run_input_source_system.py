"""Scope a unification run to one source system for the assistant demo."""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "d5c9a7e1f3b2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("unification_runs", sa.Column("input_source_system", sa.String(length=160), nullable=True))


def downgrade():
    op.drop_column("unification_runs", "input_source_system")
