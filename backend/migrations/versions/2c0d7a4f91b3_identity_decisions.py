"""persist identity decisions and merge history"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "2c0d7a4f91b3"
down_revision: Union[str, Sequence[str], None] = ("e072c1d2a362", "8cf931ace431")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("unified_entities", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("redirect_to_id", sa.String(length=32), nullable=True))
            batch_op.create_foreign_key("fk_unified_entity_redirect", "unified_entities", ["redirect_to_id"], ["id"])
    else:
        op.add_column("unified_entities", sa.Column("redirect_to_id", sa.String(length=32), nullable=True))
        op.create_foreign_key("fk_unified_entity_redirect", "unified_entities", "unified_entities", ["redirect_to_id"], ["id"])
    op.create_table(
        "unification_identity_changes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "revision", name="uq_identity_change_project_revision"),
    )
    op.create_index("ix_unification_identity_changes_project_id", "unification_identity_changes", ["project_id"])
    op.create_table(
        "unification_identity_decisions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("left_entity_id", sa.String(length=32), sa.ForeignKey("unified_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("right_entity_id", sa.String(length=32), sa.ForeignKey("unified_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("change_id", sa.String(length=32), sa.ForeignKey("unification_identity_changes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "left_entity_id", "right_entity_id", name="uq_identity_decision_pair"),
    )
    op.create_index("ix_unification_identity_decisions_project_id", "unification_identity_decisions", ["project_id"])
    op.create_table(
        "unification_identity_conflicts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.String(length=32), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.String(length=32), sa.ForeignKey("unified_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("left_source_revision_id", sa.String(length=32), sa.ForeignKey("unified_source_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("right_source_revision_id", sa.String(length=32), sa.ForeignKey("unified_source_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("change_id", sa.String(length=32), sa.ForeignKey("unification_identity_changes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_unification_identity_conflicts_project_id", "unification_identity_conflicts", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_unification_identity_conflicts_project_id", table_name="unification_identity_conflicts")
    op.drop_table("unification_identity_conflicts")
    op.drop_index("ix_unification_identity_decisions_project_id", table_name="unification_identity_decisions")
    op.drop_table("unification_identity_decisions")
    op.drop_index("ix_unification_identity_changes_project_id", table_name="unification_identity_changes")
    op.drop_table("unification_identity_changes")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("unified_entities", recreate="always") as batch_op:
            batch_op.drop_constraint("fk_unified_entity_redirect", type_="foreignkey")
            batch_op.drop_column("redirect_to_id")
    else:
        op.drop_constraint("fk_unified_entity_redirect", "unified_entities", type_="foreignkey")
        op.drop_column("unified_entities", "redirect_to_id")
