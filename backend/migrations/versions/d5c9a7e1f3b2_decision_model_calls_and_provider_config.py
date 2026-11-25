"""Persist decision model calls and provider configuration fields."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d5c9a7e1f3b2"
down_revision: Union[str, None] = "c17dbecd1a30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_providers", sa.Column("provider_kind", sa.String(length=32), nullable=False, server_default=sa.text("'openai_compatible'")))
    op.add_column("llm_providers", sa.Column("send_fields_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("llm_providers", sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default=sa.text("30.0")))
    op.add_column("llm_providers", sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("llm_providers", sa.Column("prompt_version", sa.String(length=80), nullable=False, server_default=sa.text("'entity-pair-choice-v1'")))

    op.create_table(
        "decision_model_calls",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("left_source_revision_id", sa.String(length=32), nullable=False),
        sa.Column("right_source_revision_id", sa.String(length=32), nullable=False),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.String(length=140), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("distribution_json", sa.JSON(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["unification_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["left_source_revision_id"], ["unified_source_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["right_source_revision_id"], ["unified_source_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "left_source_revision_id", "right_source_revision_id", name="uq_decision_model_call_pair"),
    )
    op.create_index(op.f("ix_decision_model_calls_project_id"), "decision_model_calls", ["project_id"], unique=False)
    op.create_index(op.f("ix_decision_model_calls_run_id"), "decision_model_calls", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_decision_model_calls_run_id"), table_name="decision_model_calls")
    op.drop_index(op.f("ix_decision_model_calls_project_id"), table_name="decision_model_calls")
    op.drop_table("decision_model_calls")
    op.drop_column("llm_providers", "prompt_version")
    op.drop_column("llm_providers", "max_retries")
    op.drop_column("llm_providers", "timeout_seconds")
    op.drop_column("llm_providers", "send_fields_json")
    op.drop_column("llm_providers", "provider_kind")
