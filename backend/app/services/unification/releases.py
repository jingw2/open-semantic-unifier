"""Idempotent unification runs and immutable published snapshots."""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload
from app.models.entities import LLMProvider, Project
from app.core.config import settings
from app.core.security import decrypt_secret
from app.services.llm_client import JudgeRequest, JudgeResult, ModelError, ProviderConfig, judge_pair

from app.models.unification import (
    IdentityChangeRecord,
    IdentityDecisionRecord,
    ProjectSnapshotState,
    SourceRecord,
    SourceRevision,
    UnifiedEntity,
    UnificationSchemeVersion,
    UnificationRun,
    UnificationSnapshot,
)
from app.services.unification.identity import (
    DecisionInput,
    IdentityConflict as IdentityOperationConflict,
    _record_change,
    apply_decision,
    find_current_member_conflicts,
    record_identity_conflict,
    resolve_entity_id,
)
from app.services.unification.matching import CannotLink, MatchAction, match_records
from app.services.unification.locking import lock_project_identity_state
from app.services.unification.schemes import AttributeOverride, MatchScheme, select_attributes
from app.services.unification.telemetry import current_trace_id, start_span


class RunConflict(RuntimeError):
    """A run or publication precondition conflicts with current state."""


class ModelFailure(RuntimeError):
    """Configured model failure policy requires this run to stop."""


@dataclass(frozen=True)
class SnapshotDelta:
    added_entities: tuple[str, ...]
    removed_entities: tuple[str, ...]
    changed_memberships: dict[str, tuple[str | None, str | None]]
    changed_attributes: dict[str, tuple[dict | None, dict | None]]
    changed_conflicts: dict[str, tuple[list | None, list | None]]


def _model_judge_for_run(db: Session, project_id: str, run: UnificationRun, scheme: MatchScheme):
    project = db.get(Project, project_id)
    provider = None
    if project is not None and scheme.model_provider_id:
        provider = db.scalar(select(LLMProvider).where(
            LLMProvider.id == scheme.model_provider_id,
            LLMProvider.workspace_id == project.workspace_id,
        ))
    if provider is None:
        def missing_provider(_left, _right, _scheme):
            return JudgeResult(
                status="ERROR", error=ModelError("provider_missing", "model provider is unavailable in this workspace", False),
            )
        return missing_provider
    try:
        api_key = decrypt_secret(provider.encrypted_api_key) if provider.encrypted_api_key else ""
    except Exception:
        def unavailable_credential(_left, _right, _scheme):
            return JudgeResult(
                status="ERROR", error=ModelError("credential_unavailable", "provider credentials cannot be decrypted", False),
                model=provider.default_model,
            )
        return unavailable_credential
    config = ProviderConfig(
        provider_key=provider.provider_key,
        model=provider.default_model,
        base_url=provider.base_url,
        api_key=api_key,
        name=provider.name,
        timeout_seconds=settings.llm_timeout_seconds,
    )

    def judge(left: SourceRevision, right: SourceRevision, active_scheme: MatchScheme) -> JudgeResult:
        def canonical_attributes(revision: SourceRevision) -> dict[str, Any]:
            return {canonical: revision.attributes.get(source) for canonical, source in active_scheme.field_map.items()}
        return judge_pair(JudgeRequest(
            project_id=project_id,
            run_id=run.id,
            input_version=run.input_version,
            scheme_revision=run.scheme_revision,
            prompt_version=active_scheme.prompt_version,
            entity_type=active_scheme.entity_type,
            left=canonical_attributes(left),
            right=canonical_attributes(right),
        ), config)
    return judge


def _stable_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_input_manifest(db: Session, project_id: str) -> list[dict[str, str | None]]:
    rows = db.execute(select(
        SourceRecord.id, SourceRecord.current_revision_id, SourceRecord.current_entity_id,
    ).where(
        SourceRecord.project_id == project_id,
        SourceRecord.current_revision_id.is_not(None),
    ).order_by(SourceRecord.id)).all()
    return [{"source_record_id": row.id, "source_revision_id": row.current_revision_id, "entity_id": row.current_entity_id} for row in rows]


def submit_run(
    db: Session, *, project_id: str, input_version: str, scheme_revision: str,
    idempotency_key: str, payload_hash: str,
) -> UnificationRun:
    if not all(isinstance(value, str) and value.strip() for value in (project_id, input_version, scheme_revision, idempotency_key, payload_hash)):
        raise ValueError("project, input, scheme, idempotency key, and payload hash are required")
    existing = db.scalar(select(UnificationRun).where(
        UnificationRun.project_id == project_id,
        UnificationRun.idempotency_key == idempotency_key,
    ))
    if existing:
        if existing.payload_hash != payload_hash:
            raise RunConflict("idempotency key was already used with a different payload")
        return existing
    input_manifest = _current_input_manifest(db, project_id)
    identity_revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
        IdentityChangeRecord.project_id == project_id,
    )) or 1
    run = UnificationRun(
        project_id=project_id, input_version=input_version, scheme_revision=scheme_revision,
        input_manifest_json=input_manifest, identity_revision=identity_revision,
        idempotency_key=idempotency_key, payload_hash=payload_hash, status="PENDING", attempt_count=0,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(UnificationRun).where(
            UnificationRun.project_id == project_id,
            UnificationRun.idempotency_key == idempotency_key,
        ))
        if existing and existing.payload_hash == payload_hash:
            return existing
        if existing:
            raise RunConflict("idempotency key was already used with a different payload")
        raise RunConflict("run submission conflicted with a concurrent update")
    db.refresh(run)
    return run


def start_run(db: Session, run_id: str, *, max_attempts: int = 3) -> UnificationRun:
    run = db.scalar(select(UnificationRun).where(UnificationRun.id == run_id).with_for_update())
    if run is None:
        raise RunConflict("run not found")
    if run.status == "SUCCEEDED":
        return run
    if run.status != "PENDING":
        raise RunConflict(f"run cannot start from status {run.status}")
    if run.attempt_count >= max_attempts:
        run.status = "FAILED"
        run.error_message = run.error_message or "maximum attempts exceeded"
        db.commit()
        raise RunConflict("run exhausted its retry limit")
    run.status = "RUNNING"
    run.attempt_count += 1
    run.error_message = None
    db.commit()
    db.refresh(run)
    return run


def complete_run(db: Session, run_id: str, result: dict, *, expected_attempt: int | None = None) -> UnificationRun:
    run = db.scalar(select(UnificationRun).where(UnificationRun.id == run_id))
    if run is None or run.status != "RUNNING":
        raise RunConflict("only a running run can stage a complete result")
    attempt = run.attempt_count if expected_attempt is None else expected_attempt
    _stable_hash(result)
    updated = db.execute(update(UnificationRun).where(
        UnificationRun.id == run_id,
        UnificationRun.status == "RUNNING",
        UnificationRun.attempt_count == attempt,
    ).values(status="SUCCEEDED", result_json=result, error_message=None))
    if updated.rowcount != 1:
        db.rollback()
        raise RunConflict("worker attempt lost its fencing token; staged changes were rolled back")
    db.commit()
    return db.get(UnificationRun, run_id)


def fail_run(db: Session, run_id: str, error: str, *, max_attempts: int = 3, expected_attempt: int | None = None) -> UnificationRun:
    state = db.execute(select(UnificationRun.status, UnificationRun.attempt_count).where(
        UnificationRun.id == run_id,
    )).one_or_none()
    if state is None:
        raise RunConflict("run not found")
    status, current_attempt = state
    if status == "SUCCEEDED":
        raise RunConflict("a succeeded run cannot be marked failed")
    if status != "RUNNING":
        raise RunConflict(f"only a running run can be marked failed, got {status}")
    attempt = current_attempt if expected_attempt is None else expected_attempt
    if current_attempt != attempt:
        db.rollback()
        raise RunConflict("worker attempt lost its fencing token; failure state was not changed")
    next_status = "FAILED" if attempt >= max_attempts else "PENDING"
    updated = db.execute(update(UnificationRun).where(
        UnificationRun.id == run_id,
        UnificationRun.status == "RUNNING",
        UnificationRun.attempt_count == attempt,
    ).values(status=next_status, error_message=error[:2000]))
    if updated.rowcount != 1:
        db.rollback()
        raise RunConflict("worker attempt lost its fencing token; failure state was not changed")
    db.commit()
    db.expire_all()
    return db.get(UnificationRun, run_id)


def execute_unification_run(db: Session, run_id: str, *, max_attempts: int = 3) -> UnificationRun:
    run = db.get(UnificationRun, run_id)
    attributes = {"run_id": run_id}
    if run is not None:
        attributes.update({
            "project_id": run.project_id,
            "input_version": run.input_version,
            "scheme_revision": run.scheme_revision,
        })
        scheme_version = db.get(UnificationSchemeVersion, run.scheme_revision)
        if scheme_version is not None:
            attributes["prompt_version"] = MatchScheme.model_validate(scheme_version.scheme_json).prompt_version
    with start_span("unification.run", attributes) as span:
        if run is not None:
            run.trace_id = current_trace_id()
        return _execute_unification_run(db, run_id, max_attempts=max_attempts)


def _execute_unification_run(db: Session, run_id: str, *, max_attempts: int = 3) -> UnificationRun:
    """Execute deterministic matching and stage a complete result in one transaction."""
    run = start_run(db, run_id, max_attempts=max_attempts)
    if run.status == "SUCCEEDED":
        return run
    worker_attempt = run.attempt_count
    try:
        with start_span("unification.read_input", {
            "project_id": run.project_id, "run_id": run.id, "input_version": run.input_version,
            "scheme_revision": run.scheme_revision,
        }):
            current_identity_revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
                IdentityChangeRecord.project_id == run.project_id,
            )) or 1
            current_manifest = _current_input_manifest(db, run.project_id)
            if current_manifest != run.input_manifest_json or current_identity_revision != run.identity_revision:
                raise RunConflict("run input or identity revision changed after submission; submit a new run")
            scheme_version = db.get(UnificationSchemeVersion, run.scheme_revision)
            if scheme_version is None or scheme_version.project_id != run.project_id:
                raise RunConflict("run scheme revision not found in this project")
            scheme = MatchScheme.model_validate(scheme_version.scheme_json)
            revision_ids = [item["source_revision_id"] for item in run.input_manifest_json]
            revisions = db.scalars(select(SourceRevision).join(
                SourceRecord, SourceRecord.id == SourceRevision.source_record_id,
            ).options(joinedload(SourceRevision.source_record)).where(
                SourceRecord.project_id == run.project_id,
                SourceRevision.id.in_(revision_ids),
            ).order_by(SourceRevision.id)).all() if revision_ids else []

        explicit_constraints = []
        decisions = db.scalars(select(IdentityDecisionRecord).where(
            IdentityDecisionRecord.project_id == run.project_id,
            IdentityDecisionRecord.action == "KEEP_DISTINCT",
        )).all()
        if decisions:
            by_entity: dict[str, list[SourceRevision]] = {}
            for revision in revisions:
                canonical = resolve_entity_id(db, revision.source_record.current_entity_id)
                by_entity.setdefault(canonical, []).append(revision)
            for decision in decisions:
                left, right = resolve_entity_id(db, decision.left_entity_id), resolve_entity_id(db, decision.right_entity_id)
                for left_revision in by_entity.get(left, []):
                    for right_revision in by_entity.get(right, []):
                        explicit_constraints.append(CannotLink(left_revision.id, right_revision.id, decision.id))

        judge = _model_judge_for_run(db, run.project_id, run, scheme) if scheme.model_judgment_enabled else None
        with start_span("unification.standardize_and_match", {
            "project_id": run.project_id, "run_id": run.id,
            "input_version": run.input_version, "scheme_revision": run.scheme_revision,
            "prompt_version": scheme.prompt_version,
        }) as match_span:
            result = match_records(revisions, scheme, explicit_constraints, judge=judge)
            match_span.set_attribute("unification.input_record_count", len(revisions))
            match_span.set_attribute("unification.pair_count", len(result.pairs))
        model_errors = [pair.model_error for pair in result.pairs if pair.model_error]
        if model_errors and scheme.model_failure_policy == "FAIL":
            raise ModelFailure(f"configured model judgment failed: {model_errors[0]}")
        current_conflicts = find_current_member_conflicts(
            db, project_id=run.project_id,
            entity_ids={item.source_record.current_entity_id for item in revisions}, scheme=scheme,
        )
        conflict_ids = []
        worker_actor_id = f"unification-worker:{run.id}"
        for conflict in current_conflicts:
            conflict_ids.append(record_identity_conflict(
                db, project_id=run.project_id, entity_id=conflict.entity_id,
                left_source_revision_id=conflict.left_revision_id,
                right_source_revision_id=conflict.right_revision_id,
                evidence=list(conflict.evidence), actor_id=worker_actor_id, commit=False,
            ))
        conflicted_entities = {item.entity_id for item in current_conflicts}
        if conflicted_entities:
            result = replace(result, pairs=tuple(
                replace(
                    pair, action=MatchAction.DISTINCT,
                    evidence_codes=pair.evidence_codes + ("TRANSITIVE_CONFLICT",),
                    evidence=pair.evidence + ({"rule": "transitive_merge_veto", "reason": "open_member_conflict"},),
                ) if pair.action == MatchAction.AUTO_MATCH and {
                    next(item for item in revisions if item.id == pair.left_source_revision_id).source_record.current_entity_id,
                    next(item for item in revisions if item.id == pair.right_source_revision_id).source_record.current_entity_id,
                } & conflicted_entities else pair
                for pair in result.pairs
            ))
        if any(revision.source_record.current_entity.entity_type == "unclassified" for revision in revisions):
            for revision in revisions:
                if revision.source_record.current_entity.entity_type == "unclassified":
                    revision.source_record.current_entity.entity_type = scheme.entity_type
        for pair in result.pairs:
            if pair.action != MatchAction.AUTO_MATCH:
                continue
            left = next(item for item in revisions if item.id == pair.left_source_revision_id)
            right = next(item for item in revisions if item.id == pair.right_source_revision_id)
            left_entity, right_entity = left.source_record.current_entity_id, right.source_record.current_entity_id
            if left_entity == right_entity or {left_entity, right_entity} & conflicted_entities:
                continue
            revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
                IdentityChangeRecord.project_id == run.project_id,
            )) or 1
            apply_decision(db, DecisionInput(
                project_id=run.project_id, left_entity_id=left_entity, right_entity_id=right_entity,
                action="MERGE", reason="configured matching rules allowed auto match",
            ), expected_revision=revision, actor_id=worker_actor_id, commit=False, scheme=scheme)

        memberships: dict[str, str] = {}
        source_revisions: dict[str, str] = {}
        groups: dict[str, list[SourceRevision]] = {}
        entities = {}
        for revision in revisions:
            source = revision.source_record
            entity_id = source.current_entity_id
            source_key = json.dumps([run.project_id, source.source_system, source.dataset, source.source_record_id], ensure_ascii=False, separators=(",", ":"))
            memberships[source_key] = entity_id
            source_revisions[source_key] = revision.id
            groups.setdefault(entity_id, []).append(revision)
            entity = db.get(UnifiedEntity, entity_id)
            entities[entity_id] = {"entity_type": entity.entity_type, "status": entity.status}
        attributes = {}
        provenance = {}
        conflicts_by_entity = {}
        for entity_id, members in groups.items():
            selected = select_attributes(members, scheme)
            attributes[entity_id] = selected.values
            provenance[entity_id] = {
                "selected": {key: value.__dict__ if hasattr(value, "__dict__") else {
                    "source_system": value.source_system, "source_revision_id": value.source_revision_id,
                    "value": value.value, "actor_id": value.actor_id, "reason": value.reason,
                } for key, value in selected.selected.items()},
                "conflicts": [{"field": conflict.field, "values": [
                    {"source_system": value.source_system, "source_revision_id": value.source_revision_id, "value": value.value}
                    for value in conflict.values
                ]} for conflict in selected.conflicts],
                "requires_review": list(selected.requires_review),
            }
            conflicts_by_entity[entity_id] = [{
                "field": conflict.field,
                "values": [{"source_system": value.source_system, "source_revision_id": value.source_revision_id, "value": value.value} for value in conflict.values],
            } for conflict in selected.conflicts]
        staged_result = {
            "run_id": run.id,
            "project_id": run.project_id,
            "input_version": run.input_version,
            "scheme_revision": run.scheme_revision,
            "prompt_version": scheme.prompt_version,
            "entities": entities,
            "memberships": memberships,
            "source_revisions": source_revisions,
            "attributes": attributes,
            "conflicts": conflicts_by_entity,
            "provenance": provenance,
            "pairs": [{"left_source_revision_id": item.left_source_revision_id,
                       "right_source_revision_id": item.right_source_revision_id,
                       "candidate": item.candidate, "action": item.action.value,
                       "evidence_codes": list(item.evidence_codes), "evidence": list(item.evidence),
                       "recommendation": item.recommendation, "model_error": item.model_error}
                      for item in result.pairs],
            "identity_conflict_ids": sorted(set(conflict_ids)),
        }
        own_changes = db.scalar(select(func.count()).select_from(IdentityChangeRecord).where(
            IdentityChangeRecord.project_id == run.project_id,
            IdentityChangeRecord.actor_id == worker_actor_id,
        )) or 0
        current_identity_revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
            IdentityChangeRecord.project_id == run.project_id,
        )) or 1
        if current_identity_revision != run.identity_revision + own_changes:
            raise RunConflict("identity state changed during run; staged changes were rolled back")
        final_manifest = _current_input_manifest(db, run.project_id)
        frozen_revisions = [(item["source_record_id"], item["source_revision_id"]) for item in run.input_manifest_json]
        current_revisions = [(item["source_record_id"], item["source_revision_id"]) for item in final_manifest]
        if current_revisions != frozen_revisions:
            raise RunConflict("source input changed during run; staged changes were rolled back")
        with start_span("unification.write_result", {
            "project_id": run.project_id, "run_id": run.id, "input_version": run.input_version,
            "scheme_revision": run.scheme_revision, "prompt_version": scheme.prompt_version,
        }):
            _stable_hash(staged_result)
            return complete_run(db, run.id, staged_result, expected_attempt=worker_attempt)
    except RunConflict:
        db.rollback()
        try:
            fail_run(db, run_id, "run execution precondition failed; submit a new run", max_attempts=worker_attempt, expected_attempt=worker_attempt)
        except RunConflict:
            pass
        raise
    except ModelFailure as exc:
        db.rollback()
        fail_run(db, run_id, f"{type(exc).__name__}: {exc}", max_attempts=worker_attempt, expected_attempt=worker_attempt)
        raise
    except Exception as exc:
        db.rollback()
        fail_run(db, run_id, f"{type(exc).__name__}: {exc}", max_attempts=max_attempts, expected_attempt=worker_attempt)
        raise


def recover_stale_runs(db: Session, *, older_than: datetime, max_attempts: int = 3) -> tuple[str, ...]:
    """Release abandoned RUNNING leases and terminally fail exhausted attempts."""
    stale = db.scalars(select(UnificationRun).where(
        UnificationRun.status == "RUNNING",
        UnificationRun.updated_at < older_than,
    ).with_for_update()).all()
    recovered = []
    for run in stale:
        run.status = "PENDING" if run.attempt_count < max_attempts else "FAILED"
        if run.status == "FAILED":
            run.error_message = run.error_message or "worker lease expired after maximum attempts"
        recovered.append(run.id)
    db.commit()
    return tuple(recovered)


def recover_interrupted_run(db: Session, run_id: str, *, max_attempts: int = 3) -> UnificationRun:
    """Reclaim a Celery message explicitly redelivered after its worker was lost."""
    run = db.scalar(select(UnificationRun).where(UnificationRun.id == run_id).with_for_update())
    if run is None:
        raise RunConflict("run not found")
    if run.status == "RUNNING":
        if run.attempt_count < max_attempts:
            run.status = "PENDING"
            run.error_message = "previous worker was lost; run reclaimed for retry"
            run.last_interruption = run.error_message
        else:
            run.status = "FAILED"
            run.error_message = "maximum attempts exhausted after worker loss"
            run.last_interruption = run.error_message
        db.commit()
        db.refresh(run)
    return run


def _current_state(db: Session, project_id: str, *, lock: bool = False) -> ProjectSnapshotState | None:
    statement = select(ProjectSnapshotState).where(ProjectSnapshotState.project_id == project_id)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _publish_pointer(db: Session, project_id: str, snapshot: UnificationSnapshot, expected_current_version: int | None) -> None:
    state = _current_state(db, project_id, lock=True)
    current_version = state.current_version if state else 0
    expected = 0 if expected_current_version is None else expected_current_version
    if current_version != expected:
        raise RunConflict(f"current snapshot version is {current_version}, expected {expected}")
    if state is None:
        state = ProjectSnapshotState(project_id=project_id, current_snapshot_id=snapshot.id, current_version=current_version + 1)
        db.add(state)
    else:
        state.current_snapshot_id = snapshot.id
        state.current_version = current_version + 1


def _snapshot_result_at_publication(db: Session, run: UnificationRun) -> dict[str, Any]:
    """Freeze the run's sources against current reviewed identities at publish time."""
    result = deepcopy(run.result_json or {})
    manifest = run.input_manifest_json or []
    if manifest:
        current_manifest = _current_input_manifest(db, run.project_id)
        frozen_revisions = sorted((item["source_record_id"], item["source_revision_id"]) for item in manifest)
        current_revisions = sorted((item["source_record_id"], item["source_revision_id"]) for item in current_manifest)
        if frozen_revisions != current_revisions:
            raise RunConflict("run sources changed after completion; submit a new run before publishing")

        revision_ids = [item["source_revision_id"] for item in manifest]
        revisions = db.scalars(select(SourceRevision).join(
            SourceRecord, SourceRecord.id == SourceRevision.source_record_id,
        ).options(joinedload(SourceRevision.source_record)).where(
            SourceRecord.project_id == run.project_id,
            SourceRevision.id.in_(revision_ids),
        ).order_by(SourceRevision.id)).all() if revision_ids else []
        if len(revisions) != len(revision_ids) or any(item.source_record.current_revision_id != item.id for item in revisions):
            raise RunConflict("run source revisions are no longer current; submit a new run before publishing")
        scheme_version = db.get(UnificationSchemeVersion, run.scheme_revision)
        if scheme_version is None or scheme_version.project_id != run.project_id:
            raise RunConflict("run scheme revision is no longer available in this project")
        scheme = MatchScheme.model_validate(scheme_version.scheme_json)

        groups: dict[str, list[SourceRevision]] = {}
        memberships: dict[str, str] = {}
        source_revisions: dict[str, str] = {}
        entities: dict[str, dict[str, str]] = {}
        for revision in revisions:
            source = revision.source_record
            entity_id = source.current_entity_id
            source_key = json.dumps(
                [run.project_id, source.source_system, source.dataset, source.source_record_id],
                ensure_ascii=False, separators=(",", ":"),
            )
            memberships[source_key] = entity_id
            source_revisions[source_key] = revision.id
            groups.setdefault(entity_id, []).append(revision)
            entity = db.get(UnifiedEntity, entity_id)
            if entity is None:
                raise RunConflict("a source record points to a missing unified entity")
            entities[entity_id] = {"entity_type": entity.entity_type, "status": entity.status}

        attributes: dict[str, dict] = {}
        provenance: dict[str, dict] = {}
        conflicts: dict[str, list] = {}
        overrides_by_entity: dict[str, dict[str, IdentityChangeRecord]] = {}
        for change in db.scalars(select(IdentityChangeRecord).where(
            IdentityChangeRecord.project_id == run.project_id,
            IdentityChangeRecord.change_type == "ATTRIBUTE_OVERRIDE",
        ).order_by(IdentityChangeRecord.revision)):
            entity_id = resolve_entity_id(db, change.payload["entity_id"])
            field = change.payload["field"]
            if field not in scheme.field_map:
                continue
            overrides_by_entity.setdefault(entity_id, {})[field] = change
        for entity_id, members in groups.items():
            overrides = []
            for field, change in overrides_by_entity.get(entity_id, {}).items():
                current = sorted([[member.source_record_id, member.id] for member in members])
                if current != change.payload.get("member_revisions"):
                    raise RunConflict(f"attribute override {change.id} requires review after entity membership or source revisions changed")
                overrides.append(AttributeOverride(field=field, value=change.payload["value"], actor_id=change.actor_id, reason=change.reason))
            selected = select_attributes(members, scheme, overrides=overrides)
            attributes[entity_id] = selected.values
            provenance[entity_id] = {
                "selected": {
                    field: {
                        "source_system": source.source_system,
                        "source_revision_id": source.source_revision_id,
                        "value": source.value,
                        "actor_id": source.actor_id,
                        "reason": source.reason,
                    }
                    for field, source in selected.selected.items()
                },
                "conflicts": [{
                    "field": conflict.field,
                    "values": [{
                        "source_system": source.source_system,
                        "source_revision_id": source.source_revision_id,
                        "value": source.value,
                    } for source in conflict.values],
                } for conflict in selected.conflicts],
                "requires_review": list(selected.requires_review),
            }
            conflicts[entity_id] = [{
                "field": conflict.field,
                "values": [{
                    "source_system": source.source_system,
                    "source_revision_id": source.source_revision_id,
                    "value": source.value,
                } for source in conflict.values],
            } for conflict in selected.conflicts]

        result.update({
            "entities": entities,
            "memberships": memberships,
            "source_revisions": source_revisions,
            "attributes": attributes,
            "conflicts": conflicts,
            "provenance": provenance,
        })

    identity_revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
        IdentityChangeRecord.project_id == run.project_id,
    )) or 1
    result["identity_revision"] = identity_revision
    result["identity_changes"] = [{
        "change_id": change.id,
        "revision": change.revision,
        "change_type": change.change_type,
        "actor_id": change.actor_id,
        "reason": change.reason,
        "payload": change.payload,
        "trace_id": change.trace_id,
        "created_at": change.created_at.isoformat(),
    } for change in db.scalars(select(IdentityChangeRecord).where(
        IdentityChangeRecord.project_id == run.project_id,
        IdentityChangeRecord.revision <= identity_revision,
    ).order_by(IdentityChangeRecord.revision, IdentityChangeRecord.id)).all()]
    return result


def publish_snapshot(
    db: Session, *, run_id: str, expected_current_version: int | None, actor_id: str,
) -> UnificationSnapshot:
    run = db.scalar(select(UnificationRun).where(UnificationRun.id == run_id).with_for_update())
    if run is None or run.status != "SUCCEEDED" or run.result_json is None:
        raise RunConflict("only a succeeded run with a complete result can be published")
    lock_project_identity_state(db, run.project_id)
    state = _current_state(db, run.project_id, lock=True)
    current_version = state.current_version if state else 0
    expected = 0 if expected_current_version is None else expected_current_version
    if current_version != expected:
        raise RunConflict(f"current snapshot version is {current_version}, expected {expected}")
    snapshot_result = _snapshot_result_at_publication(db, run)
    snapshot = UnificationSnapshot(
        project_id=run.project_id, version=current_version + 1, run_id=run.id,
        parent_snapshot_id=state.current_snapshot_id if state else None,
        snapshot_json=snapshot_result, actor_id=actor_id, trace_id=current_trace_id(),
    )
    db.add(snapshot)
    try:
        db.flush()
        _publish_pointer(db, run.project_id, snapshot, expected_current_version)
        db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        raise RunConflict("snapshot publication conflicted with a concurrent publish") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(snapshot)
    return snapshot


def rollback_snapshot(db: Session, *, project_id: str, snapshot_id: str, actor_id: str, reason: str) -> UnificationSnapshot:
    if not reason.strip():
        raise RunConflict("rollback reason is required")
    lock_project_identity_state(db, project_id)
    target = db.scalar(select(UnificationSnapshot).where(
        UnificationSnapshot.id == snapshot_id,
        UnificationSnapshot.project_id == project_id,
    ))
    if target is None:
        raise RunConflict("snapshot not found in this project")
    state = _current_state(db, project_id, lock=True)
    if state is None or state.current_snapshot_id is None:
        raise RunConflict("project has no current snapshot")
    snapshot = UnificationSnapshot(
        project_id=project_id, version=state.current_version + 1, run_id=None,
        parent_snapshot_id=state.current_snapshot_id, rollback_of_id=target.id,
        snapshot_json=target.snapshot_json, actor_id=actor_id, trace_id=current_trace_id(),
    )
    db.add(snapshot)
    try:
        db.flush()
        revision = db.scalar(select(func.max(IdentityChangeRecord.revision)).where(IdentityChangeRecord.project_id == project_id)) or 1
        _record_change(db, project_id, "ROLLBACK", actor_id, reason.strip(), {
            "from_snapshot_id": state.current_snapshot_id, "target_snapshot_id": target.id, "new_snapshot_id": snapshot.id,
            "from_version": state.current_version, "target_version": target.version, "new_version": snapshot.version,
        }, revision)
        state.current_snapshot_id = snapshot.id
        state.current_version += 1
        db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        raise RunConflict("snapshot rollback conflicted with a concurrent publish") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(snapshot)
    return snapshot


def get_current_snapshot(db: Session, project_id: str) -> UnificationSnapshot | None:
    state = _current_state(db, project_id)
    return db.get(UnificationSnapshot, state.current_snapshot_id) if state and state.current_snapshot_id else None


def diff_snapshots(before: dict, after: dict) -> SnapshotDelta:
    before_entities = set((before.get("entities") or {}).keys())
    after_entities = set((after.get("entities") or {}).keys())
    before_memberships = before.get("memberships") or {}
    after_memberships = after.get("memberships") or {}
    memberships = {
        key: (before_memberships.get(key), after_memberships.get(key))
        for key in before_memberships.keys() | after_memberships.keys()
        if before_memberships.get(key) != after_memberships.get(key)
    }
    before_attributes = before.get("attributes") or {}
    after_attributes = after.get("attributes") or {}
    attributes = {
        key: (before_attributes.get(key), after_attributes.get(key))
        for key in before_attributes.keys() | after_attributes.keys()
        if before_attributes.get(key) != after_attributes.get(key)
    }
    before_conflicts = before.get("conflicts") or {}
    after_conflicts = after.get("conflicts") or {}
    conflicts = {
        key: (before_conflicts.get(key), after_conflicts.get(key))
        for key in before_conflicts.keys() | after_conflicts.keys()
        if before_conflicts.get(key) != after_conflicts.get(key)
    }
    return SnapshotDelta(
        tuple(sorted(after_entities - before_entities)),
        tuple(sorted(before_entities - after_entities)),
        dict(sorted(memberships.items())),
        dict(sorted(attributes.items())),
        dict(sorted(conflicts.items())),
    )
