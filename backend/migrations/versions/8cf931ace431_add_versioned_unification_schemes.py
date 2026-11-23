"""Add project-owned immutable scheme revisions."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8cf931ace431"
down_revision: Union[str, None] = "e072c1d2a362"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "unification_scheme_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("scheme_key", sa.String(length=120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scheme_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "scheme_key", "revision", name="uq_scheme_project_key_revision"),
    )
    op.create_index(op.f("ix_unification_scheme_versions_project_id"), "unification_scheme_versions", ["project_id"], unique=False)
    op.create_table(
        "unification_entity_catalog_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("catalog_key", sa.String(length=120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "catalog_key", "revision", name="uq_entity_catalog_project_key_revision"),
    )
    op.create_index(op.f("ix_unification_entity_catalog_versions_project_id"), "unification_entity_catalog_versions", ["project_id"], unique=False)
    op.create_table(
        "unification_catalog_entity_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("catalog_key", sa.String(length=120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("canonical_id", sa.String(length=160), nullable=False),
        sa.Column("canonical_name", sa.String(length=240), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "catalog_key", "revision", name="uq_catalog_entity_project_key_revision"),
    )
    op.create_index(op.f("ix_unification_catalog_entity_versions_project_id"), "unification_catalog_entity_versions", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_unification_catalog_entity_versions_project_id"), table_name="unification_catalog_entity_versions")
    op.drop_table("unification_catalog_entity_versions")
    op.drop_index(op.f("ix_unification_entity_catalog_versions_project_id"), table_name="unification_entity_catalog_versions")
    op.drop_table("unification_entity_catalog_versions")
    op.drop_index(op.f("ix_unification_scheme_versions_project_id"), table_name="unification_scheme_versions")
    op.drop_table("unification_scheme_versions")
