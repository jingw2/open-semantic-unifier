"""Contract sources selected from stored file data sources for L3 model work."""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.entities import _uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ContractSource(Base):
    __tablename__ = "contract_sources"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uq_contract_source_project_sha256"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    data_source_id: Mapped[str] = mapped_column(String(32), ForeignKey("data_sources.id", ondelete="RESTRICT"), index=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(240), nullable=False)
    format: Mapped[str] = mapped_column(String(24), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ContractModelVersion(Base):
    """An immutable compiled and validated contract model version.

    canonical_model_json, artifacts_json, source_sha256, compiler_versions_json,
    entity_snapshot_id, and snapshot_version are written once at creation and
    never updated afterwards. Corrections produce a new version row.
    """
    __tablename__ = "contract_model_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_contract_model_version_project"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), ForeignKey("contract_sources.id", ondelete="RESTRICT"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_model_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    compiler_versions_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    validation_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    entity_snapshot_id: Mapped[str] = mapped_column(String(32), ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), index=True, nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ContractReleaseState(Base):
    """The published pointer: which contract model version is current per project."""
    __tablename__ = "contract_release_state"
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    current_version_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("contract_model_versions.id", ondelete="RESTRICT"), nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ContractAuditRecord(Base):
    """One row per publication or rollback of the contract release pointer."""
    __tablename__ = "contract_audit_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    version_id: Mapped[str] = mapped_column(String(32), ForeignKey("contract_model_versions.id", ondelete="RESTRICT"), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
