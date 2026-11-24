"""Frozen baseline for the application schema that predates Alembic."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3a5d3f7c8e11"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

metadata = sa.MetaData()
users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("email", sa.String(180), nullable=False),
    sa.Column("password_hash", sa.String(256), nullable=False),
    sa.Column("display_name", sa.String(120), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_users_email", users.c.email, unique=True)
workspaces = sa.Table(
    "workspaces", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("name", sa.String(160), nullable=False),
    sa.Column("slug", sa.String(80), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_workspaces_slug", workspaces.c.slug, unique=True)
workspace_members = sa.Table(
    "workspace_members", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
    sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
    sa.Column("role", sa.String(20), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_workspace_members_workspace_id", workspace_members.c.workspace_id)
sa.Index("ix_workspace_members_user_id", workspace_members.c.user_id)
projects = sa.Table(
    "projects", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
    sa.Column("name", sa.String(160), nullable=False),
    sa.Column("slug", sa.String(100), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_projects_workspace_id", projects.c.workspace_id)
sa.Index("ix_projects_slug", projects.c.slug, unique=True)
data_sources = sa.Table(
    "data_sources", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("name", sa.String(180), nullable=False),
    sa.Column("source_type", sa.String(32), nullable=False),
    sa.Column("object_key", sa.String(320), nullable=False),
    sa.Column("encrypted_config", sa.Text(), nullable=False),
    sa.Column("table_name", sa.String(160), nullable=False),
    sa.Column("custom_sql", sa.Text(), nullable=False),
    sa.Column("profile_json", sa.JSON(), nullable=False),
    sa.Column("routing_json", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_data_sources_project_id", data_sources.c.project_id)
llm_providers = sa.Table(
    "llm_providers", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("workspace_id", sa.String(32), sa.ForeignKey("workspaces.id"), nullable=False),
    sa.Column("provider_key", sa.String(64), nullable=False),
    sa.Column("name", sa.String(140), nullable=False),
    sa.Column("base_url", sa.String(320), nullable=False),
    sa.Column("default_model", sa.String(140), nullable=False),
    sa.Column("encrypted_api_key", sa.Text(), nullable=False),
    sa.Column("is_custom", sa.Boolean(), nullable=False),
    sa.Column("is_default", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_llm_providers_workspace_id", llm_providers.c.workspace_id)
jobs = sa.Table(
    "jobs", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("stage", sa.String(16), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("progress", sa.Integer(), nullable=False),
    sa.Column("stage_message", sa.String(240), nullable=False),
    sa.Column("params_json", sa.JSON(), nullable=False),
    sa.Column("result_json", sa.JSON(), nullable=False),
    sa.Column("error_message", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_jobs_project_id", jobs.c.project_id)
canonical_mappings = sa.Table(
    "canonical_mappings", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("raw_alias", sa.String(300), nullable=False),
    sa.Column("canonical_id", sa.String(80), nullable=False),
    sa.Column("canonical_name", sa.String(240), nullable=False),
    sa.Column("source", sa.String(24), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_canonical_mappings_project_id", canonical_mappings.c.project_id)
sa.Index("ix_canonical_mappings_raw_alias", canonical_mappings.c.raw_alias)
project_api_keys = sa.Table(
    "project_api_keys", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("key_prefix", sa.String(24), nullable=False),
    sa.Column("key_hash", sa.String(64), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_project_api_keys_project_id", project_api_keys.c.project_id)
sa.Index("ix_project_api_keys_key_hash", project_api_keys.c.key_hash, unique=True)
artifact_versions = sa.Table(
    "artifact_versions", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
    sa.Column("version_no", sa.Integer(), nullable=False),
    sa.Column("artifact_type", sa.String(48), nullable=False),
    sa.Column("release_status", sa.String(20), nullable=False),
    sa.Column("filename", sa.String(180), nullable=False),
    sa.Column("object_key", sa.String(360), nullable=False),
    sa.Column("size_bytes", sa.Integer(), nullable=False),
    sa.Column("meta_json", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_artifact_versions_project_id", artifact_versions.c.project_id)
sa.Index("ix_artifact_versions_job_id", artifact_versions.c.job_id)
review_items = sa.Table(
    "review_items", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
    sa.Column("stage", sa.String(16), nullable=False),
    sa.Column("case_type", sa.String(80), nullable=False),
    sa.Column("left_text", sa.Text(), nullable=False),
    sa.Column("right_text", sa.Text(), nullable=False),
    sa.Column("score", sa.Float(), nullable=False),
    sa.Column("llm_suggestion", sa.JSON(), nullable=False),
    sa.Column("decision", sa.String(20), nullable=False),
    sa.Column("resolved_canonical_id", sa.String(80), nullable=False),
    sa.Column("resolved_canonical_name", sa.String(200), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
sa.Index("ix_review_items_project_id", review_items.c.project_id)
sa.Index("ix_review_items_job_id", review_items.c.job_id)

LEGACY_TABLES = (
    users, workspaces, workspace_members, projects, data_sources, llm_providers,
    jobs, artifact_versions, review_items, canonical_mappings, project_api_keys,
)


def upgrade() -> None:
    # Existing databases created before Alembic are adopted without recreating tables.
    metadata.create_all(op.get_bind(), tables=list(LEGACY_TABLES), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(op.get_bind(), tables=list(reversed(LEGACY_TABLES)), checkfirst=True)
