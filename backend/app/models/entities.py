"""SQLAlchemy 2.0 生产级 10 张核心业务表定义"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="editor")  # admin | editor | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # file | duckdb | sqlite | mysql | postgresql | mongodb
    object_key: Mapped[str] = mapped_column(String(320), default="")      # 文件在 MinIO / Local 中的路径
    encrypted_config: Mapped[str] = mapped_column(Text, default="")       # Fernet 加密的数据库连接 JSON
    table_name: Mapped[str] = mapped_column(String(160), default="")
    custom_sql: Mapped[str] = mapped_column(Text, default="")
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    routing_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LLMProvider(Base):
    __tablename__ = "llm_providers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), index=True, nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    base_url: Mapped[str] = mapped_column(String(320), default="")
    default_model: Mapped[str] = mapped_column(String(140), default="")
    encrypted_api_key: Mapped[str] = mapped_column(Text, default="")
    is_custom: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # L1 | L2 | L3 | L4
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING | RUNNING | SUCCESS | FAILED
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage_message: Mapped[str] = mapped_column(String(240), default="任务已入队")
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"), index=True, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    artifact_type: Mapped[str] = mapped_column(String(48), nullable=False)  # clean_csv | rules_json | waterfall_html | l3_contract | l4_metrics | sql_ddl | json_schema
    release_status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT | PUBLISHED | ARCHIVED
    filename: Mapped[str] = mapped_column(String(180), nullable=False)
    object_key: Mapped[str] = mapped_column(String(360), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # L1 | L2
    case_type: Mapped[str] = mapped_column(String(80), default="灰色边界样本")
    left_text: Mapped[str] = mapped_column(Text, nullable=False)
    right_text: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    llm_suggestion: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING | APPROVED | REJECTED
    resolved_canonical_id: Mapped[str] = mapped_column(String(80), default="")
    resolved_canonical_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CanonicalMapping(Base):
    __tablename__ = "canonical_mappings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True, nullable=False)
    raw_alias: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    canonical_id: Mapped[str] = mapped_column(String(80), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(240), nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="HUMAN_REVIEW")  # HUMAN_REVIEW | AUTO
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProjectApiKey(Base):
    __tablename__ = "project_api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
