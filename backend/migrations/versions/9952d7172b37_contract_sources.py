"""Add contract sources for L3 contract input selection."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9952d7172b37"
down_revision: Union[str, None] = "9d2e7a0b14c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contract_sources",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("data_source_id", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=240), nullable=False),
        sa.Column("format", sa.String(length=24), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "sha256", name="uq_contract_source_project_sha256"),
    )
    op.create_index(op.f("ix_contract_sources_project_id"), "contract_sources", ["project_id"], unique=False)
    op.create_index(op.f("ix_contract_sources_data_source_id"), "contract_sources", ["data_source_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_contract_sources_data_source_id"), table_name="contract_sources")
    op.drop_index(op.f("ix_contract_sources_project_id"), table_name="contract_sources")
    op.drop_table("contract_sources")
