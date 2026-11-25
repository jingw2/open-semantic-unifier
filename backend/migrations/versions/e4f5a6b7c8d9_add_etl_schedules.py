"""Add persisted ETL schedules."""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "etl_schedules",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("stage", sa.String(length=8), nullable=False),
        sa.Column("datasource_ids", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("target_datasource_id", sa.String(length=32), nullable=False),
        sa.Column("target_table", sa.String(length=160), nullable=False),
        sa.Column("write_mode", sa.String(length=16), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_etl_schedules_project_id"), "etl_schedules", ["project_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_etl_schedules_project_id"), table_name="etl_schedules")
    op.drop_table("etl_schedules")
