"""Project-scoped scheme management and reusable entity type catalog."""
from dataclasses import asdict, replace
import csv
import io
import hashlib
import json
from typing import Any
import uuid

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user, require_resource_access, require_role
from app.core.db import get_db
from app.models.unification import DecisionModelCall, IdentityChangeRecord, IdentityConflictRecord, IdentityDecisionRecord, ProjectSnapshotState, SourceRecord, SourceRevision, UnifiedEntity, UnificationRun, UnificationSchemeVersion, UnificationSnapshot
from app.services.unification.identity import DecisionInput, IdentityConflict, apply_decision, find_current_member_conflicts, record_identity_conflict, resolve_entity_id, set_attribute_override, split_entity, undo_identity_change
from app.services.unification.schemes import (
    CatalogEntity,
    EntityDefinition,
    MatchScheme,
    get_entity_definition,
    get_catalog_entity,
    get_entity_type_catalog,
    get_scheme,
    list_catalog_entities,
    list_entity_definitions,
    list_schemes,
    save_entity_definition,
    save_catalog_entity,
    save_scheme,
)
from app.services.unification.contracts import IngestMode, SourceRecordInput
from app.services.unification.ingest import ingest_batch
from app.services.unification.matching import CannotLink, MatchAction, match_records
from app.services.unification.locking import lock_project_identity_state
from app.services.unification.releases import RunConflict, diff_snapshots, get_current_snapshot, publish_snapshot, rollback_snapshot, submit_run as submit_run_service
from app.services.unification.telemetry import start_span
from app.workers.tasks import dispatch_unification_run

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_resource_access)],
    tags=["Unification Schemes"],
)


class MatchInputRecord(BaseModel):
    source_system: str
    dataset: str
    source_record_id: str | None = None
    attributes: dict[str, Any]


class CannotLinkInput(BaseModel):
    left_source_revision_id: str
    right_source_revision_id: str
    decision_id: str


class MatchBatchRequest(BaseModel):
    scheme_key: str
    scheme_revision: int | None = Field(default=None, ge=1)
    input_version: str
    records: list[MatchInputRecord]
    constraints: list[CannotLinkInput] = Field(default_factory=list)


class IdentityDecisionRequest(BaseModel):
    scheme_key: str
    scheme_revision: int | None = Field(default=None, ge=1)
    left_entity_id: str
    right_entity_id: str
    action: str
    reason: str
    expected_revision: int = Field(ge=1)


class SplitInput(BaseModel):
    source_record_ids: list[str] = Field(min_length=1)
    reason: str
    expected_revision: int = Field(ge=1)


class RunSubmissionRequest(BaseModel):
    scheme_key: str
    scheme_revision: int | None = Field(default=None, ge=1)
    input_version: str = Field(min_length=1, max_length=160)


class BatchIngestRequest(BaseModel):
    input_version: str = Field(min_length=1, max_length=160)
    mode: IngestMode = IngestMode.SYNC
    records: list[MatchInputRecord] = Field(min_length=1)


class IdentityReviewRequest(BaseModel):
    run_id: str
    left_source_revision_id: str
    right_source_revision_id: str
    action: str
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=1)


class IdentityUndoRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class AttributeOverrideRequest(BaseModel):
    snapshot_id: str
    field: str = Field(min_length=1, max_length=160)
    value: Any
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=1)
    member_revisions: list[tuple[str, str]]


class SnapshotPublishRequest(BaseModel):
    run_id: str
    expected_current_version: int | None = Field(default=None, ge=0)


class SnapshotRollbackRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/unification/catalog")
def entity_type_catalog(ctx: dict[str, str] = Depends(get_current_user)) -> list[dict[str, str]]:
    return [asdict(item) for item in get_entity_type_catalog()]


@router.get("/projects/{project_id}/unification/schemes")
def schemes_for_project(
    project_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return list_schemes(db, project_id=project_id)


@router.get("/projects/{project_id}/unification/catalog")
def project_entity_catalog(
    project_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return list_entity_definitions(db, project_id=project_id)


@router.get("/projects/{project_id}/unification/catalog/entities")
def project_standard_entities(
    project_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return list_catalog_entities(db, project_id=project_id)


@router.put("/projects/{project_id}/unification/catalog/entities/{catalog_key}")
def save_standard_entity(
    project_id: str,
    catalog_key: str,
    entity: CatalogEntity,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        versioned = save_catalog_entity(db, project_id=project_id, catalog_key=catalog_key, entity=entity)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return versioned.model_dump(mode="json")


@router.get("/projects/{project_id}/unification/catalog/entities/{catalog_key}")
def get_standard_entity(
    project_id: str,
    catalog_key: str,
    revision: int | None = Query(default=None, ge=1),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    entity = get_catalog_entity(db, project_id=project_id, catalog_key=catalog_key, revision=revision)
    if entity is None:
        raise HTTPException(status_code=404, detail="standard entity version not found")
    return entity.model_dump(mode="json")


@router.post("/projects/{project_id}/unification/match")
def match_project_records(
    project_id: str,
    request: MatchBatchRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    scheme = get_scheme(db, project_id=project_id, scheme_key=request.scheme_key, revision=request.scheme_revision)
    if scheme is None:
        raise HTTPException(status_code=404, detail="scheme version not found")
    batch = ingest_batch(
        db,
        project_id=project_id,
        records=[SourceRecordInput(project_id, item.source_system, item.dataset, item.source_record_id, item.attributes) for item in request.records],
        mode=IngestMode.SYNC,
        input_version=request.input_version,
    )
    if not batch.accepted:
        raise HTTPException(status_code=422, detail={"errors": [asdict(issue) for issue in batch.errors]})
    revisions = db.scalars(select(SourceRevision).options(joinedload(SourceRevision.source_record)).where(
        SourceRevision.id.in_(batch.source_revision_ids)
    )).all()
    explicit_constraints = [CannotLink(item.left_source_revision_id, item.right_source_revision_id, item.decision_id) for item in request.constraints]
    decisions = db.scalars(select(IdentityDecisionRecord).where(
        IdentityDecisionRecord.project_id == project_id,
        IdentityDecisionRecord.action == "KEEP_DISTINCT",
    )).all()
    if decisions:
        records_by_entity: dict[str, list[SourceRevision]] = {}
        for revision in revisions:
            canonical_id = resolve_entity_id(db, revision.source_record.current_entity_id)
            records_by_entity.setdefault(canonical_id, []).append(revision)
        for decision in decisions:
            left_entity_id = resolve_entity_id(db, decision.left_entity_id)
            right_entity_id = resolve_entity_id(db, decision.right_entity_id)
            for left_revision in records_by_entity.get(left_entity_id, []):
                for right_revision in records_by_entity.get(right_entity_id, []):
                    explicit_constraints.append(CannotLink(left_revision.id, right_revision.id, decision.id))
    result = match_records(
        revisions,
        scheme,
        explicit_constraints,
    )
    current_conflicts = find_current_member_conflicts(
        db, project_id=project_id,
        entity_ids={revision.source_record.current_entity_id for revision in revisions},
        scheme=scheme,
    )
    conflict_ids: dict[frozenset[str], str] = {}
    for conflict in current_conflicts:
        conflict_id = record_identity_conflict(
            db, project_id=project_id, entity_id=conflict.entity_id,
            left_source_revision_id=conflict.left_revision_id,
            right_source_revision_id=conflict.right_revision_id,
            evidence=list(conflict.evidence), actor_id=ctx["user_id"], commit=False,
        )
        conflict_ids[frozenset((conflict.left_revision_id, conflict.right_revision_id))] = conflict_id
    conflicted_entities = {item.entity_id for item in current_conflicts}
    if conflicted_entities:
        revised_pairs = []
        for pair in result.pairs:
            left = next(item for item in revisions if item.id == pair.left_source_revision_id)
            right = next(item for item in revisions if item.id == pair.right_source_revision_id)
            if pair.action.value == "AUTO_MATCH" and {
                left.source_record.current_entity_id, right.source_record.current_entity_id,
            } & conflicted_entities:
                revised_pairs.append(replace(
                    pair, action=MatchAction.DISTINCT,
                    evidence_codes=pair.evidence_codes + ("TRANSITIVE_CONFLICT",),
                    evidence=pair.evidence + ({"rule": "transitive_merge_veto", "reason": "open_member_conflict"},),
                ))
            else:
                revised_pairs.append(pair)
        result = replace(result, pairs=tuple(revised_pairs))
    for revision in revisions:
        entity = revision.source_record.current_entity
        if entity.entity_type == "unclassified":
            entity.entity_type = scheme.entity_type
    for pair in result.pairs:
        if pair.action.value != "AUTO_MATCH":
            continue
        left_revision = next(item for item in revisions if item.id == pair.left_source_revision_id)
        right_revision = next(item for item in revisions if item.id == pair.right_source_revision_id)
        left_entity = left_revision.source_record.current_entity_id
        right_entity = right_revision.source_record.current_entity_id
        if left_entity == right_entity:
            continue
        current_revision = db.query(func.max(IdentityChangeRecord.revision)).filter(
            IdentityChangeRecord.project_id == project_id
        ).scalar() or 1
        try:
            apply_decision(db, DecisionInput(
                project_id=project_id, left_entity_id=left_entity, right_entity_id=right_entity,
                action="MERGE", reason="configured matching rules allowed auto match",
            ), expected_revision=current_revision, actor_id=ctx["user_id"], commit=False, scheme=scheme)
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    for pair in result.pairs:
        if pair.action.value != "DISTINCT":
            continue
        left_revision = next(item for item in revisions if item.id == pair.left_source_revision_id)
        right_revision = next(item for item in revisions if item.id == pair.right_source_revision_id)
        left_entity = left_revision.source_record.current_entity_id
        right_entity = right_revision.source_record.current_entity_id
        if left_entity != right_entity:
            continue
        conflict_id = record_identity_conflict(
            db, project_id=project_id, entity_id=left_entity,
            left_source_revision_id=left_revision.id, right_source_revision_id=right_revision.id,
            evidence=list(pair.evidence), actor_id=ctx["user_id"], commit=False,
        )
        conflict_ids[frozenset((pair.left_source_revision_id, pair.right_source_revision_id))] = conflict_id
    db.commit()
    return {
        "execution_id": uuid.uuid4().hex,
        "input_version": request.input_version,
        "scheme_key": request.scheme_key,
        "scheme_revision": scheme.revision,
        "identities": [
            {
                "source_system": identity.source_key.source_system,
                "dataset": identity.source_key.dataset,
                "source_record_id": identity.source_key.source_record_id,
                "source_revision_id": identity.source_revision_id,
                "entity_id": db.get(SourceRecord, identity.source_record_id).current_entity_id,
            }
            for identity in batch.identities
        ],
        "considered_pair_count": result.considered_pair_count,
        "candidate_count": result.candidate_count,
        "identity_conflict_ids": sorted(set(conflict_ids.values())),
        "pairs": [
            {
                "left_source_revision_id": pair.left_source_revision_id,
                "right_source_revision_id": pair.right_source_revision_id,
                "candidate": pair.candidate,
                "action": pair.action.value,
                "score": pair.score,
                "evidence_codes": pair.evidence_codes,
                "evidence": pair.evidence,
                "recommendation": pair.recommendation,
                "model_error": pair.model_error,
                "conflict_id": conflict_ids.get(frozenset((pair.left_source_revision_id, pair.right_source_revision_id))),
            }
            for pair in result.pairs
        ],
    }


@router.get("/projects/{project_id}/unification/conflicts")
def list_identity_conflicts(
    project_id: str,
    status: str = "OPEN",
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    conflicts = db.scalars(select(IdentityConflictRecord).where(
        IdentityConflictRecord.project_id == project_id,
        IdentityConflictRecord.status == status,
    ).order_by(IdentityConflictRecord.created_at, IdentityConflictRecord.id)).all()
    return [{
        "conflict_id": item.id,
        "entity_id": item.entity_id,
        "left_source_revision_id": item.left_source_revision_id,
        "right_source_revision_id": item.right_source_revision_id,
        "evidence": item.evidence,
        "status": item.status,
        "change_id": item.change_id,
        "created_at": item.created_at.isoformat(),
    } for item in conflicts]


@router.post("/projects/{project_id}/unification/decisions")
def create_identity_decision(
    project_id: str,
    request: IdentityDecisionRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    scheme = get_scheme(db, project_id=project_id, scheme_key=request.scheme_key, revision=request.scheme_revision)
    if scheme is None:
        raise HTTPException(status_code=404, detail="scheme version not found")
    try:
        change = apply_decision(db, DecisionInput(
            project_id=project_id, left_entity_id=request.left_entity_id,
            right_entity_id=request.right_entity_id, action=request.action, reason=request.reason,
        ), expected_revision=request.expected_revision, actor_id=ctx["user_id"], scheme=scheme)
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_id": change.id, "revision": change.revision, "change_type": change.change_type}


@router.post("/projects/{project_id}/unification/identity-changes/{change_id}/undo")
def undo_identity_decision(
    project_id: str,
    change_id: str,
    request: IdentityUndoRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    change_record = db.get(IdentityChangeRecord, change_id)
    if change_record is None or change_record.project_id != project_id:
        raise HTTPException(status_code=404, detail="identity change not found")
    try:
        with start_span("unification.api_identity_undo", {
            "project_id": project_id, "change_id": change_id,
            "expected_identity_revision": request.expected_revision,
        }):
            change = undo_identity_change(
                db, change_id, expected_revision=request.expected_revision,
                actor_id=ctx["user_id"], reason=request.reason,
            )
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_id": change.id, "revision": change.revision, "change_type": change.change_type}


@router.post("/projects/{project_id}/unification/entities/{entity_id}/attribute-overrides", status_code=status.HTTP_201_CREATED)
def override_entity_attribute(project_id: str, entity_id: str, request: AttributeOverrideRequest,
                              ctx: dict[str, str] = Depends(require_role("admin", "editor")),
                              db: Session = Depends(get_db)) -> dict[str, Any]:
    lock_project_identity_state(db, project_id)
    state = db.get(ProjectSnapshotState, project_id)
    if state is None or state.current_snapshot_id != request.snapshot_id:
        raise HTTPException(status_code=409, detail="refresh the current snapshot before overriding attributes")
    snapshot = db.get(UnificationSnapshot, request.snapshot_id)
    scheme = _snapshot_scheme(db, snapshot)
    if scheme is None or entity_id not in snapshot.snapshot_json.get("entities", {}):
        raise HTTPException(status_code=404, detail="entity or scheme not found in snapshot")
    if request.field not in scheme.field_map:
        raise HTTPException(status_code=422, detail="field must be mapped by the scheme")
    entity = db.scalar(select(UnifiedEntity).where(UnifiedEntity.id == entity_id, UnifiedEntity.project_id == project_id))
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    if entity.entity_type != scheme.entity_type:
        raise HTTPException(status_code=422, detail="scheme entity type does not match entity")
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason must be non-empty")
    try:
        change = set_attribute_override(db, project_id=project_id, entity_id=entity_id, field=request.field,
                                        value=request.value, reason=request.reason, actor_id=ctx["user_id"],
                                        expected_revision=request.expected_revision,
                                        expected_member_revisions=[list(item) for item in request.member_revisions])
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_id": change.id, "revision": change.revision, "change_type": change.change_type}


@router.get("/projects/{project_id}/unification/entities/{entity_id}/attribute-overrides/preview")
def preview_attribute_override(project_id: str, entity_id: str, snapshot_id: str, field: str,
                               ctx: dict[str, str] = Depends(get_current_user),
                               db: Session = Depends(get_db)) -> dict[str, Any]:
    lock_project_identity_state(db, project_id)
    state = db.get(ProjectSnapshotState, project_id)
    if state is None or state.current_snapshot_id != snapshot_id:
        raise HTTPException(status_code=409, detail="refresh the current snapshot")
    snapshot = db.get(UnificationSnapshot, snapshot_id)
    scheme = _snapshot_scheme(db, snapshot)
    entity = db.get(UnifiedEntity, entity_id)
    if not snapshot or not scheme or entity_id not in snapshot.snapshot_json.get("entities", {}) or not entity or entity.project_id != project_id or entity.status != "ACTIVE":
        raise HTTPException(status_code=404, detail="active entity or scheme not found")
    if field not in scheme.field_map or entity.entity_type != scheme.entity_type:
        raise HTTPException(status_code=422, detail="field or entity type does not match the snapshot scheme")
    rows = db.execute(select(SourceRecord, SourceRevision).join(
        SourceRevision, SourceRevision.id == SourceRecord.current_revision_id,
    ).where(SourceRecord.project_id == project_id, SourceRecord.current_entity_id == entity_id).order_by(SourceRecord.id)).all()
    revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(IdentityChangeRecord.project_id == project_id)) or 1
    return {"snapshot_id": snapshot_id, "field": field, "identity_revision": revision,
            "member_revisions": [[source.id, source_revision.id] for source, source_revision in rows],
            "members": [{"source_system": source.source_system, "dataset": source.dataset,
                         "source_record_id": source.source_record_id, "source_revision_id": source_revision.id,
                         "attributes": source_revision.attributes} for source, source_revision in rows]}


def _snapshot_scheme(db: Session, snapshot: UnificationSnapshot | None) -> MatchScheme | None:
    visited: set[str] = set()
    while snapshot is not None and snapshot.run_id is None and snapshot.rollback_of_id:
        if snapshot.id in visited:
            return None
        visited.add(snapshot.id)
        snapshot = db.get(UnificationSnapshot, snapshot.rollback_of_id)
    run = db.get(UnificationRun, snapshot.run_id) if snapshot and snapshot.run_id else None
    version = db.get(UnificationSchemeVersion, run.scheme_revision) if run else None
    return MatchScheme.model_validate(version.scheme_json) if version else None


@router.post("/projects/{project_id}/unification/entities/{entity_id}/split")
def split_unified_entity(
    project_id: str,
    entity_id: str,
    request: SplitInput,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        change = split_entity(
            db, project_id=project_id, entity_id=entity_id,
            source_record_ids=request.source_record_ids, reason=request.reason,
            actor_id=ctx["user_id"], expected_revision=request.expected_revision,
        )
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_id": change.id, "revision": change.revision, "change_type": change.change_type}


@router.put("/projects/{project_id}/unification/catalog/{catalog_key}")
def save_project_entity_definition(
    project_id: str,
    catalog_key: str,
    definition: EntityDefinition,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        versioned = save_entity_definition(db, project_id=project_id, catalog_key=catalog_key, definition=definition)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return versioned.model_dump(mode="json")


@router.get("/projects/{project_id}/unification/catalog/{catalog_key}")
def get_project_entity_definition(
    project_id: str,
    catalog_key: str,
    revision: int | None = Query(default=None, ge=1),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    definition = get_entity_definition(db, project_id=project_id, catalog_key=catalog_key, revision=revision)
    if definition is None:
        raise HTTPException(status_code=404, detail="entity catalog version not found")
    return definition.model_dump(mode="json")


@router.put("/projects/{project_id}/unification/schemes/{scheme_key}")
def create_scheme_revision(
    project_id: str,
    scheme_key: str,
    scheme: MatchScheme,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not scheme_key.strip():
        raise HTTPException(status_code=422, detail="scheme_key must be non-empty")
    try:
        versioned = save_scheme(db, project_id=project_id, scheme_key=scheme_key, scheme=scheme)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    return versioned.model_dump(mode="json")


@router.get("/projects/{project_id}/unification/schemes/{scheme_key}")
def scheme_revision(
    project_id: str,
    scheme_key: str,
    revision: int | None = Query(default=None, ge=1),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    scheme = get_scheme(db, project_id=project_id, scheme_key=scheme_key, revision=revision)
    if scheme is None:
        raise HTTPException(status_code=404, detail="scheme version not found")
    return scheme.model_dump(mode="json")


@router.post("/projects/{project_id}/unification/runs", status_code=status.HTTP_202_ACCEPTED)
def submit_unification_run(
    project_id: str,
    request: RunSubmissionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = select(UnificationSchemeVersion).where(
        UnificationSchemeVersion.project_id == project_id,
        UnificationSchemeVersion.scheme_key == request.scheme_key,
    )
    if request.scheme_revision is not None:
        statement = statement.where(UnificationSchemeVersion.revision == request.scheme_revision)
    version = db.scalar(statement.order_by(UnificationSchemeVersion.revision.desc()).limit(1))
    if version is None:
        raise HTTPException(status_code=404, detail="scheme version not found")
    scheme = MatchScheme.model_validate(version.scheme_json)
    payload_hash = hashlib.sha256(json.dumps({
        "project_id": project_id,
        "input_version": request.input_version,
        "scheme_revision": version.id,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    attributes = {
        "project_id": project_id,
        "input_version": request.input_version,
        "scheme_revision": version.id,
        "prompt_version": scheme.prompt_version,
    }
    with start_span("unification.api_submit", attributes):
        try:
            run = submit_run_service(
                db,
                project_id=project_id,
                input_version=request.input_version,
                scheme_revision=version.id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        dispatch_unification_run(run.id)
    return {"run_id": run.id, "status": run.status, "input_version": run.input_version}


@router.post("/projects/{project_id}/unification/batches", status_code=status.HTTP_201_CREATED)
def ingest_unification_batch(
    project_id: str,
    request: BatchIngestRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = ingest_batch(
        db,
        project_id=project_id,
        records=[SourceRecordInput(
            project_id=project_id,
            source_system=item.source_system,
            dataset=item.dataset,
            source_record_id=item.source_record_id,
            attributes=item.attributes,
        ) for item in request.records],
        mode=request.mode,
        input_version=request.input_version,
    )
    if not result.accepted:
        raise HTTPException(status_code=422, detail=[asdict(issue) for issue in result.errors])
    db.commit()
    return {
        "accepted": result.accepted,
        "input_version": result.input_version,
        "identity_guarantee": result.identity_guarantee,
        "limitations": list(result.limitations),
        "created_records": result.created_records,
        "created_entities": result.created_entities,
        "created_revisions": result.created_revisions,
        "identities": [{
            "source_key": [item.source_key.project_id, item.source_key.source_system, item.source_key.dataset, item.source_key.source_record_id],
            "source_record_id": item.source_record_id,
            "source_revision_id": item.source_revision_id,
            "entity_id": item.entity_id,
        } for item in result.identities],
    }


FILE_IMPORT_MAX_BYTES = 10 * 1024 * 1024
FILE_IMPORT_MAX_ROWS = 10_000
FILE_IMPORT_MAX_COLUMNS = 256
FILE_IMPORT_MAX_EXPANDED_BYTES = 50 * 1024 * 1024


@router.post("/projects/{project_id}/unification/batches/file", status_code=status.HTTP_201_CREATED)
def ingest_unification_file(
    project_id: str,
    file: UploadFile = File(...),
    input_version: str = Form(..., min_length=1, max_length=160),
    source_system: str = Form(...),
    dataset: str = Form(...),
    source_key_column: str = Form(...),
    mode: IngestMode = Form(IngestMode.SYNC),
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Import CSV or Parquet rows; the named column holds the textual source ID.

    The remaining columns become attributes. Parquet IDs must use a string type;
    CSV is UTF-8 with a header and is read as text to preserve leading zeroes.
    """
    filename = (file.filename or "").casefold()
    if not filename.endswith((".csv", ".parquet")):
        raise HTTPException(status_code=415, detail="only CSV and Parquet files are supported")
    content = file.file.read(FILE_IMPORT_MAX_BYTES + 1)
    if len(content) > FILE_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds the 10 MiB limit")
    try:
        if filename.endswith(".csv"):
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            columns = reader.fieldnames or []
            if len(columns) != len(set(columns)) or any(not column for column in columns):
                raise ValueError("column names must be unique and nonempty")
            if len(columns) > FILE_IMPORT_MAX_COLUMNS:
                raise HTTPException(status_code=413, detail="file exceeds the 256 column limit")
            rows = []
            for row in reader:
                if len(rows) >= FILE_IMPORT_MAX_ROWS:
                    raise HTTPException(status_code=413, detail="file exceeds the 10000 row limit")
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("each CSV row must have the same number of fields as its header")
                rows.append(row)
        else:
            import pyarrow as pa
            import pyarrow.parquet as pq

            parquet_file = pq.ParquetFile(io.BytesIO(content))
            metadata = parquet_file.metadata
            if metadata.num_rows > FILE_IMPORT_MAX_ROWS:
                raise HTTPException(status_code=413, detail="file exceeds the 10000 row limit")
            if metadata.num_columns > FILE_IMPORT_MAX_COLUMNS:
                raise HTTPException(status_code=413, detail="file exceeds the 256 column limit")
            expanded_size = sum(metadata.row_group(i).column(j).total_uncompressed_size for i in range(metadata.num_row_groups) for j in range(metadata.num_columns))
            if expanded_size > FILE_IMPORT_MAX_EXPANDED_BYTES:
                raise HTTPException(status_code=413, detail="Parquet data exceeds the 50 MiB expanded limit")
            schema = parquet_file.schema_arrow
            columns = schema.names
            if len(columns) != len(set(columns)) or any(not column for column in columns):
                raise ValueError("column names must be unique and nonempty")
            if source_key_column in columns and not (pa.types.is_string(schema.field(source_key_column).type) or pa.types.is_large_string(schema.field(source_key_column).type)):
                raise ValueError("Parquet source key column must have a string type")
            rows = []
            for batch in parquet_file.iter_batches(batch_size=512):
                rows.extend(batch.to_pylist())
    except (ValueError, OSError, UnicodeError, csv.Error) as exc:
        raise HTTPException(status_code=422, detail="invalid import file or unsupported column values") from exc
    if source_key_column not in columns:
        raise HTTPException(status_code=422, detail="source key column not found")
    if not rows:
        raise HTTPException(status_code=422, detail="batch must contain at least one record")
    records = [MatchInputRecord(
        source_system=source_system, dataset=dataset, source_record_id=row[source_key_column],
        attributes={key: value for key, value in row.items() if key != source_key_column},
    ) for row in rows]
    return ingest_unification_batch(project_id, BatchIngestRequest(input_version=input_version, mode=mode, records=records), ctx, db)


def _run_payload(run: UnificationRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "input_version": run.input_version,
        "scheme_revision": run.scheme_revision,
        "status": run.status,
        "attempt_count": run.attempt_count,
        "error_message": run.error_message,
        "last_interruption": run.last_interruption,
        "trace_id": run.trace_id,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "result": run.result_json,
    }


@router.get("/projects/{project_id}/unification/model-calls")
def list_decision_model_calls(
    project_id: str,
    run_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = select(DecisionModelCall).where(DecisionModelCall.project_id == project_id)
    if run_id:
        statement = statement.where(DecisionModelCall.run_id == run_id)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(statement.order_by(
        DecisionModelCall.created_at.desc(), DecisionModelCall.id.desc(),
    ).offset(offset).limit(limit)).all()
    return {
        "items": [{
            "id": item.id,
            "project_id": item.project_id,
            "run_id": item.run_id,
            "left_source_revision_id": item.left_source_revision_id,
            "right_source_revision_id": item.right_source_revision_id,
            "provider_kind": item.provider_kind,
            "provider_key": item.provider_key,
            "model_version": item.model_version,
            "prompt_version": item.prompt_version,
            "verdict": item.verdict,
            "confidence": item.confidence,
            "distribution": item.distribution_json,
            "explanation": item.explanation,
            "latency_ms": item.latency_ms,
            "usage": item.usage_json,
            "cost_usd": item.cost_usd,
            "error_code": item.error_code,
            "error_message": item.error_message,
            "retryable": item.retryable,
            "trace_id": item.trace_id,
            "created_at": item.created_at.isoformat(),
        } for item in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
    }


@router.get("/projects/{project_id}/unification/runs")
def list_unification_runs(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(UnificationRun).where(UnificationRun.project_id == project_id)) or 0
    rows = db.scalars(select(UnificationRun).where(
        UnificationRun.project_id == project_id,
    ).order_by(UnificationRun.created_at.desc(), UnificationRun.id.desc()).offset(offset).limit(limit)).all()
    return {"items": [_run_payload(run) for run in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/projects/{project_id}/unification/runs/{run_id}")
def get_unification_run(
    project_id: str,
    run_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(select(UnificationRun).where(
        UnificationRun.id == run_id, UnificationRun.project_id == project_id,
    ))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_payload(run)


@router.get("/projects/{project_id}/unification/reviews")
def list_unification_reviews(
    project_id: str,
    run_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(select(UnificationRun).where(
        UnificationRun.id == run_id, UnificationRun.project_id == project_id,
    ))
    if run is None or run.result_json is None:
        raise HTTPException(status_code=404, detail="completed run not found")
    pairs = [item for item in run.result_json.get("pairs", []) if item.get("action") == "REVIEW"]
    pairs.sort(key=lambda item: (item.get("left_source_revision_id", ""), item.get("right_source_revision_id", "")))
    revision_ids = {revision_id for pair in pairs for revision_id in (
        pair.get("left_source_revision_id"), pair.get("right_source_revision_id"),
    ) if revision_id}
    revisions = db.scalars(select(SourceRevision).join(
        SourceRecord, SourceRecord.id == SourceRevision.source_record_id,
    ).where(
        SourceRecord.project_id == project_id, SourceRevision.id.in_(revision_ids),
    )).all() if revision_ids else []
    revisions_by_id = {item.id: item for item in revisions}
    active_decisions = db.scalars(select(IdentityDecisionRecord).where(
        IdentityDecisionRecord.project_id == project_id,
        IdentityDecisionRecord.action.in_(("MERGE", "KEEP_DISTINCT")),
    )).all()
    decision_pairs = {
        tuple(sorted((resolve_entity_id(db, item.left_entity_id), resolve_entity_id(db, item.right_entity_id))))
        for item in active_decisions
    }
    pending = []
    for pair in pairs:
        left = revisions_by_id.get(pair.get("left_source_revision_id"))
        right = revisions_by_id.get(pair.get("right_source_revision_id"))
        if left is None or right is None or left.source_record.current_revision_id != left.id or right.source_record.current_revision_id != right.id:
            continue
        left_entity = resolve_entity_id(db, left.source_record.current_entity_id)
        right_entity = resolve_entity_id(db, right.source_record.current_entity_id)
        if left_entity == right_entity:
            continue
        if tuple(sorted((left_entity, right_entity))) not in decision_pairs:
            pending.append(pair)
    return {"items": pending[offset:offset + limit], "limit": limit, "offset": offset, "total": len(pending), "run_id": run.id}


@router.post("/projects/{project_id}/unification/reviews", status_code=status.HTTP_201_CREATED)
def decide_unification_review(
    project_id: str,
    request: IdentityReviewRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(select(UnificationRun).where(
        UnificationRun.id == request.run_id, UnificationRun.project_id == project_id,
    ))
    revision_ids = {item.get("source_revision_id") for item in (run.input_manifest_json if run else [])}
    pair_ids = {request.left_source_revision_id, request.right_source_revision_id}
    recorded_review_pair = any(
        item.get("action") == "REVIEW"
        and {item.get("left_source_revision_id"), item.get("right_source_revision_id")} == pair_ids
        for item in (run.result_json.get("pairs", []) if run and run.result_json else [])
    )
    if run is None or run.status != "SUCCEEDED" or not pair_ids <= revision_ids or not recorded_review_pair:
        raise HTTPException(status_code=404, detail="review pair not found in this project run")
    lock_project_identity_state(db, project_id)
    revisions = db.scalars(select(SourceRevision).join(
        SourceRecord, SourceRecord.id == SourceRevision.source_record_id,
    ).where(
        SourceRecord.project_id == project_id,
        SourceRevision.id.in_([request.left_source_revision_id, request.right_source_revision_id]),
        SourceRecord.current_revision_id == SourceRevision.id,
    )).all()
    if len(revisions) != 2:
        raise HTTPException(status_code=409, detail="review evidence is stale; submit a new run before deciding")
    revisions.sort(key=lambda item: item.id)
    scheme_version = db.get(UnificationSchemeVersion, run.scheme_revision)
    scheme = MatchScheme.model_validate(scheme_version.scheme_json) if scheme_version else None
    try:
        with start_span("unification.api_review_decision", {
            "project_id": project_id, "run_id": run.id, "action": request.action,
            "actor_id": ctx["user_id"],
            "expected_identity_revision": request.expected_revision,
        }):
            change = apply_decision(db, DecisionInput(
                project_id=project_id,
                left_entity_id=revisions[0].source_record.current_entity_id,
                right_entity_id=revisions[1].source_record.current_entity_id,
                action=request.action,
                reason=request.reason,
            ), expected_revision=request.expected_revision, actor_id=ctx["user_id"], scheme=scheme)
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_id": change.id, "revision": change.revision, "change_type": change.change_type}


@router.get("/projects/{project_id}/unification/entities")
def list_snapshot_entities(
    project_id: str,
    snapshot_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    snapshot = db.scalar(select(UnificationSnapshot).where(
        UnificationSnapshot.id == snapshot_id, UnificationSnapshot.project_id == project_id,
    ))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    payload = _snapshot_export_payload(snapshot, "entities", db)
    entities = payload["entities"]
    scheme = _snapshot_scheme(db, snapshot)
    return {"snapshot_id": snapshot.id, "version": snapshot.version, "fields": list(scheme.field_map) if scheme else [], "items": entities[offset:offset + limit], "limit": limit, "offset": offset, "total": len(entities)}


@router.get("/projects/{project_id}/unification/identity-changes")
def list_identity_changes(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(IdentityChangeRecord).where(IdentityChangeRecord.project_id == project_id)) or 0
    rows = db.scalars(select(IdentityChangeRecord).where(
        IdentityChangeRecord.project_id == project_id,
    ).order_by(IdentityChangeRecord.revision.desc(), IdentityChangeRecord.id.desc()).offset(offset).limit(limit)).all()
    return {"items": [{
        "change_id": item.id, "revision": item.revision, "change_type": item.change_type,
        "actor_id": item.actor_id, "reason": item.reason, "payload": item.payload,
        "trace_id": item.trace_id, "created_at": item.created_at.isoformat(),
    } for item in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/projects/{project_id}/unification/snapshots")
def list_unification_snapshots(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    state = db.get(ProjectSnapshotState, project_id)
    total = db.scalar(select(func.count()).select_from(UnificationSnapshot).where(UnificationSnapshot.project_id == project_id)) or 0
    rows = db.scalars(select(UnificationSnapshot).where(
        UnificationSnapshot.project_id == project_id,
    ).order_by(UnificationSnapshot.version.desc(), UnificationSnapshot.id.desc()).offset(offset).limit(limit)).all()
    return {"items": [{
        "snapshot_id": item.id, "version": item.version, "run_id": item.run_id,
        "parent_snapshot_id": item.parent_snapshot_id, "rollback_of_id": item.rollback_of_id,
        "actor_id": item.actor_id, "trace_id": item.trace_id, "created_at": item.created_at.isoformat(),
        "is_current": bool(state and state.current_snapshot_id == item.id),
    } for item in rows], "limit": limit, "offset": offset, "total": total,
        "current_snapshot_id": state.current_snapshot_id if state else None,
        "current_version": state.current_version if state else 0}


@router.post("/projects/{project_id}/unification/snapshots", status_code=status.HTTP_201_CREATED)
def publish_unification_snapshot(
    project_id: str,
    request: SnapshotPublishRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(select(UnificationRun).where(
        UnificationRun.id == request.run_id, UnificationRun.project_id == project_id,
    ))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        with start_span("unification.api_publish_snapshot", {
            "project_id": project_id, "run_id": run.id,
            "expected_current_version": request.expected_current_version if request.expected_current_version is not None else 0,
        }):
            snapshot = publish_snapshot(
                db, run_id=run.id, expected_current_version=request.expected_current_version,
                actor_id=ctx["user_id"],
            )
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"snapshot_id": snapshot.id, "version": snapshot.version, "run_id": snapshot.run_id, "trace_id": snapshot.trace_id}


@router.post("/projects/{project_id}/unification/snapshots/{snapshot_id}/rollback", status_code=status.HTTP_201_CREATED)
def rollback_unification_snapshot(
    project_id: str,
    snapshot_id: str,
    request: SnapshotRollbackRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.scalar(select(UnificationSnapshot.id).where(
        UnificationSnapshot.id == snapshot_id, UnificationSnapshot.project_id == project_id,
    )) is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    try:
        with start_span("unification.api_rollback_snapshot", {
            "project_id": project_id, "snapshot_id": snapshot_id,
        }):
            snapshot = rollback_snapshot(db, project_id=project_id, snapshot_id=snapshot_id, actor_id=ctx["user_id"], reason=request.reason)
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"snapshot_id": snapshot.id, "version": snapshot.version, "rollback_of_id": snapshot.rollback_of_id}


def _snapshot_export_payload(snapshot: UnificationSnapshot, kind: str, db: Session) -> dict[str, Any]:
    data = snapshot.snapshot_json
    if kind == "entities":
        memberships = data.get("memberships", {})
        revisions = data.get("source_revisions", {})
        entities = []
        for entity_id, details in sorted(data.get("entities", {}).items()):
            member_keys = sorted(key for key, target in memberships.items() if target == entity_id)
            entities.append({
                "entity_id": entity_id, **details,
                "attributes": data.get("attributes", {}).get(entity_id, {}),
                "members": [{"source_key": json.loads(key), "source_revision_id": revisions.get(key)} for key in member_keys],
                "provenance": data.get("provenance", {}).get(entity_id, {}),
                "conflicts": data.get("conflicts", {}).get(entity_id, []),
            })
        return {"snapshot_id": snapshot.id, "version": snapshot.version, "entities": entities}
    if kind == "mappings":
        return {"snapshot_id": snapshot.id, "version": snapshot.version, "mappings": [
            {"source_key": json.loads(key), "source_revision_id": revision_id,
             "entity_id": data.get("memberships", {}).get(key)}
            for key, revision_id in sorted(data.get("source_revisions", {}).items())
        ]}
    if kind == "decisions":
        decision_types = {"MERGE", "DECISION", "SPLIT", "UNDO"}
        decisions = [change for change in data.get("identity_changes", [])
                     if change.get("change_type") in decision_types]
        return {"snapshot_id": snapshot.id, "version": snapshot.version,
                "identity_revision": data.get("identity_revision"), "decisions": decisions}
    if kind == "delta":
        parent = db.get(UnificationSnapshot, snapshot.parent_snapshot_id) if snapshot.parent_snapshot_id else None
        delta = diff_snapshots(parent.snapshot_json if parent else {}, data)
        return {"snapshot_id": snapshot.id, "version": snapshot.version, "delta": {
            "added_entities": list(delta.added_entities), "removed_entities": list(delta.removed_entities),
            "changed_memberships": delta.changed_memberships,
            "changed_attributes": delta.changed_attributes,
            "changed_conflicts": delta.changed_conflicts,
        }}
    raise HTTPException(status_code=422, detail="kind must be entities, mappings, decisions, or delta")


@router.get("/projects/{project_id}/unification/snapshots/{snapshot_id}/export")
def export_unification_snapshot(
    project_id: str,
    snapshot_id: str,
    kind: str = Query(pattern="^(entities|mappings|decisions|delta)$"),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    snapshot = db.scalar(select(UnificationSnapshot).where(
        UnificationSnapshot.id == snapshot_id, UnificationSnapshot.project_id == project_id,
    ))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    payload = _snapshot_export_payload(snapshot, kind, db)
    filename = f"{project_id}-snapshot-{snapshot.version}-{kind}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
