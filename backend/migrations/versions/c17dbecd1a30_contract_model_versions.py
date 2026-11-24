"""Add immutable contract model versions, the release pointer, and audit records."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c17dbecd1a30"
down_revision: Union[str, None] = "9952d7172b37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contract_model_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_model_json", sa.Text(), nullable=False),
        sa.Column("artifacts_json", sa.JSON(), nullable=False),
        sa.Column("compiler_versions_json", sa.JSON(), nullable=False),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("entity_snapshot_id", sa.String(length=32), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["contract_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entity_snapshot_id"], ["unification_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version", name="uq_contract_model_version_project"),
    )
    op.create_index(op.f("ix_contract_model_versions_project_id"), "contract_model_versions", ["project_id"], unique=False)
    op.create_index(op.f("ix_contract_model_versions_source_id"), "contract_model_versions", ["source_id"], unique=False)
    op.create_index(op.f("ix_contract_model_versions_entity_snapshot_id"), "contract_model_versions", ["entity_snapshot_id"], unique=False)

    op.create_table(
        "contract_release_state",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.String(length=32), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_version_id"], ["contract_model_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id"),
    )

    op.create_table(
        "contract_audit_records",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("version_id", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=True),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["contract_model_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_contract_audit_records_project_id"), "contract_audit_records", ["project_id"], unique=False)
    op.create_index(op.f("ix_contract_audit_records_version_id"), "contract_audit_records", ["version_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_contract_audit_records_version_id"), table_name="contract_audit_records")
    op.drop_index(op.f("ix_contract_audit_records_project_id"), table_name="contract_audit_records")
    op.drop_table("contract_audit_records")
    op.drop_table("contract_release_state")
    op.drop_index(op.f("ix_contract_model_versions_entity_snapshot_id"), table_name="contract_model_versions")
    op.drop_index(op.f("ix_contract_model_versions_source_id"), table_name="contract_model_versions")
    op.drop_index(op.f("ix_contract_model_versions_project_id"), table_name="contract_model_versions")
    op.drop_table("contract_model_versions")
