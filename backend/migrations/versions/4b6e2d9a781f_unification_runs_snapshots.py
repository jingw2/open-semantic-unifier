"""add idempotent runs and immutable snapshots"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4b6e2d9a781f"
down_revision: Union[str, Sequence[str], None] = "2c0d7a4f91b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "unification_runs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_version", sa.String(length=160), nullable=False),
        sa.Column("input_manifest_json", sa.JSON(), nullable=False),
        sa.Column("identity_revision", sa.Integer(), nullable=False),
        sa.Column("scheme_revision", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_unification_run_idempotency"),
    )
    op.create_index("ix_unification_runs_project_id", "unification_runs", ["project_id"])
    op.create_table(
        "unification_snapshots",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("unification_runs.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("parent_snapshot_id", sa.String(length=32), sa.ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("rollback_of_id", sa.String(length=32), sa.ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "version", name="uq_unification_snapshot_project_version"),
        sa.UniqueConstraint("run_id", name="uq_unification_snapshot_run"),
    )
    op.create_index("ix_unification_snapshots_project_id", "unification_snapshots", ["project_id"])
    op.create_table(
        "unification_project_snapshot_state",
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("current_snapshot_id", sa.String(length=32), sa.ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("unification_project_snapshot_state")
    op.drop_index("ix_unification_snapshots_project_id", table_name="unification_snapshots")
    op.drop_table("unification_snapshots")
    op.drop_index("ix_unification_runs_project_id", table_name="unification_runs")
    op.drop_table("unification_runs")
