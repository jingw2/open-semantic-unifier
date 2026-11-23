"""Validate and persist repeatable source record imports."""
import hashlib
import json
import uuid
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.unification import SourceRecord, SourceRevision, UnifiedEntity
from .contracts import IngestIdentity, IngestIssue, IngestMode, IngestResult, SourceKey, SourceRecordInput
from .locking import lock_project_identity_state


def _content_hash(attributes: dict[str, Any]) -> str:
    payload = json.dumps(attributes, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ingest_batch(
    db: Session,
    *,
    project_id: str,
    records: list[SourceRecordInput],
    mode: IngestMode,
    input_version: str,
) -> IngestResult:
    if not records:
        return _rejected(input_version, IngestMode(mode), [IngestIssue(0, "EMPTY_BATCH", "batch must contain at least one record")])

    mode = IngestMode(mode)
    issues: list[IngestIssue] = []
    seen: dict[SourceKey, str] = {}
    normalized: list[tuple[SourceKey, dict[str, Any], str]] = []
    exploration_token = uuid.uuid4().hex
    for index, item in enumerate(records):
        if item.project_id != project_id:
            issues.append(IngestIssue(index, "PROJECT_MISMATCH", "source key belongs to another project"))
            continue
        if not item.source_system or not item.source_system.strip():
            issues.append(IngestIssue(index, "SOURCE_SYSTEM_REQUIRED", "source system is required"))
            continue
        if not item.dataset or not item.dataset.strip():
            issues.append(IngestIssue(index, "DATASET_REQUIRED", "dataset is required"))
            continue
        source_record_id = item.source_record_id
        if mode == IngestMode.EXPLORATION:
            source_record_id = f"explore:{exploration_token}:{index}"
        elif not source_record_id or not source_record_id.strip():
            if mode == IngestMode.SYNC:
                issues.append(IngestIssue(index, "SOURCE_ID_REQUIRED", "continuous imports require a source record ID"))
                continue
        try:
            key = SourceKey(project_id, item.source_system, item.dataset, source_record_id)
        except ValueError as exc:
            issues.append(IngestIssue(index, "SOURCE_KEY_INVALID", str(exc)))
            continue
        if not isinstance(item.attributes, Mapping):
            issues.append(IngestIssue(index, "ATTRIBUTES_INVALID", "attributes must be an object"))
            continue
        attrs = dict(item.attributes)
        try:
            digest = _content_hash(attrs)
        except (TypeError, ValueError) as exc:
            issues.append(IngestIssue(index, "ATTRIBUTES_INVALID", f"attributes must contain JSON values: {exc}"))
            continue
        if key in seen:
            detail = "the same source key has conflicting content in this batch" if seen[key] != digest else "duplicate source key in this batch"
            issues.append(IngestIssue(index, "DUPLICATE_SOURCE_KEY", detail))
            continue
        seen[key] = digest
        normalized.append((key, attrs, digest))
    if issues:
        return _rejected(input_version, mode, issues)

    identities: list[IngestIdentity] = []
    created_records = created_entities = created_revisions = 0
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        raw_connection = connection.connection.driver_connection
        # SQLite's legacy transaction mode does not start a DB transaction for SELECT;
        # a savepoint released without an outer BEGIN would otherwise commit the batch.
        if not raw_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
    lock_project_identity_state(db, project_id)
    with db.begin_nested():
        for key, attrs, digest in normalized:
            source = db.scalar(select(SourceRecord).where(
                SourceRecord.project_id == project_id,
                SourceRecord.source_system == key.source_system,
                SourceRecord.dataset == key.dataset,
                SourceRecord.source_record_id == key.source_record_id,
            ))
            if source is None:
                entity = UnifiedEntity(project_id=project_id)
                db.add(entity)
                db.flush()
                created_entities += 1
                source = SourceRecord(
                    project_id=project_id,
                    source_system=key.source_system,
                    dataset=key.dataset,
                    source_record_id=key.source_record_id,
                    current_entity_id=entity.id,
                    current_revision_id=None,
                )
                db.add(source)
                db.flush()
                created_records += 1
            latest = db.scalar(select(SourceRevision).where(
                SourceRevision.source_record_id == source.id
            ).order_by(SourceRevision.revision_no.desc()).limit(1))
            if latest is not None and latest.content_hash == digest:
                identities.append(IngestIdentity(key, source.id, source.current_entity_id, latest.id))
                continue
            revision_no = 1 if latest is None else latest.revision_no + 1
            revision = SourceRevision(
                source_record_id=source.id,
                revision_no=revision_no,
                input_version=input_version,
                attributes=attrs,
                content_hash=digest,
            )
            db.add(revision)
            db.flush()
            source.current_revision_id = revision.id
            created_revisions += 1
            identities.append(IngestIdentity(key, source.id, source.current_entity_id, revision.id))

    guarantee = "stable_source_key" if mode == IngestMode.SYNC else "single_import"
    limitations = () if mode == IngestMode.SYNC else ("generated source IDs are valid only for this import; repeated imports create new identities",)
    revision_ids = tuple(identity.source_revision_id for identity in identities)
    return IngestResult(True, input_version, tuple(identities), revision_ids, guarantee, limitations, created_records, created_entities, created_revisions, ())


def _rejected(input_version: str, mode: IngestMode, errors: list[IngestIssue]) -> IngestResult:
    guarantee = "stable_source_key" if mode == IngestMode.SYNC else "single_import"
    limitations = () if mode == IngestMode.SYNC else ("generated source IDs are valid only for this import",)
    return IngestResult(False, input_version, (), (), guarantee, limitations, 0, 0, 0, tuple(errors))
