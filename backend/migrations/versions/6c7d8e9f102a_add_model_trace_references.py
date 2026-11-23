"""add durable trace references to runs, identity audit, and snapshots"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "6c7d8e9f102a"
down_revision: Union[str, Sequence[str], None] = "4b6e2d9a781f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("unification_runs", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.add_column("unification_identity_changes", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.add_column("unification_snapshots", sa.Column("trace_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("unification_snapshots", "trace_id")
    op.drop_column("unification_identity_changes", "trace_id")
    op.drop_column("unification_runs", "trace_id")
