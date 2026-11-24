"""Persistent source identity and entity records for unification."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.entities import _uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UnifiedEntity(Base):
    __tablename__ = "unified_entities"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, default="unclassified")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    redirect_to_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("unified_entities.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class IdentityChangeRecord(Base):
    __tablename__ = "unification_identity_changes"
    __table_args__ = (UniqueConstraint("project_id", "revision", name="uq_identity_change_project_revision"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class IdentityDecisionRecord(Base):
    __tablename__ = "unification_identity_decisions"
    __table_args__ = (
        UniqueConstraint("project_id", "left_entity_id", "right_entity_id", name="uq_identity_decision_pair"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    left_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_entities.id", ondelete="CASCADE"), nullable=False)
    right_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_entities.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    change_id: Mapped[str] = mapped_column(String(32), ForeignKey("unification_identity_changes.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class IdentityConflictRecord(Base):
    __tablename__ = "unification_identity_conflicts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_entities.id", ondelete="CASCADE"), nullable=False)
    left_source_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_source_revisions.id", ondelete="CASCADE"), nullable=False)
    right_source_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_source_revisions.id", ondelete="CASCADE"), nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    change_id: Mapped[str] = mapped_column(String(32), ForeignKey("unification_identity_changes.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class UnificationRun(Base):
    __tablename__ = "unification_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_unification_run_idempotency"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    input_version: Mapped[str] = mapped_column(String(160), nullable=False)
    input_manifest_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    identity_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scheme_revision: Mapped[str] = mapped_column(String(160), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    last_interruption: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class DecisionModelCall(Base):
    """One typed decision-model outcome for a candidate pair, committed with its run."""
    __tablename__ = "decision_model_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "left_source_revision_id", "right_source_revision_id", name="uq_decision_model_call_pair"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("unification_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    left_source_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_source_revisions.id", ondelete="CASCADE"), nullable=False)
    right_source_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_source_revisions.id", ondelete="CASCADE"), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(140), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    distribution_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class UnificationSnapshot(Base):
    __tablename__ = "unification_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_unification_snapshot_project_version"),
        UniqueConstraint("run_id", name="uq_unification_snapshot_run"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("unification_runs.id", ondelete="RESTRICT"), nullable=True)
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), nullable=True)
    rollback_of_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), nullable=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ProjectSnapshotState(Base):
    __tablename__ = "unification_project_snapshot_state"
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    current_snapshot_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("unification_snapshots.id", ondelete="RESTRICT"), nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SourceRecord(Base):
    __tablename__ = "unified_source_records"
    __table_args__ = (
        UniqueConstraint("project_id", "source_system", "dataset", "source_record_id", name="uq_source_record_identity"),
        ForeignKeyConstraint(
            ["current_revision_id", "id"],
            ["unified_source_revisions.id", "unified_source_revisions.source_record_id"],
            name="fk_current_revision_same_source",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    source_system: Mapped[str] = mapped_column(String(160), nullable=False)
    dataset: Mapped[str] = mapped_column(String(200), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(320), nullable=False)
    current_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_entities.id"), index=True, nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    current_entity: Mapped[UnifiedEntity] = relationship(foreign_keys=[current_entity_id])
    revisions: Mapped[list["SourceRevision"]] = relationship(
        back_populates="source_record", foreign_keys="SourceRevision.source_record_id"
    )


class SourceRevision(Base):
    __tablename__ = "unified_source_revisions"
    __table_args__ = (
        UniqueConstraint("source_record_id", "revision_no", name="uq_source_revision_number"),
        UniqueConstraint("id", "source_record_id", name="uq_source_revision_source_pair"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source_record_id: Mapped[str] = mapped_column(String(32), ForeignKey("unified_source_records.id", ondelete="CASCADE"), index=True, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    input_version: Mapped[str] = mapped_column(String(160), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    source_record: Mapped[SourceRecord] = relationship(back_populates="revisions", foreign_keys=[source_record_id])


class UnificationSchemeVersion(Base):
    __tablename__ = "unification_scheme_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "scheme_key", "revision", name="uq_scheme_project_key_revision"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    scheme_key: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scheme_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class EntityCatalogVersion(Base):
    __tablename__ = "unification_entity_catalog_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "catalog_key", "revision", name="uq_entity_catalog_project_key_revision"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    catalog_key: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class CatalogEntityVersion(Base):
    __tablename__ = "unification_catalog_entity_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "catalog_key", "revision", name="uq_catalog_entity_project_key_revision"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    catalog_key: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    canonical_id: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(240), nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
