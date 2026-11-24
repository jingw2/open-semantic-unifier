"""Atomic, auditable changes to project scoped entity identity."""
from dataclasses import dataclass
from itertools import combinations

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.unification import (
    IdentityChangeRecord,
    IdentityConflictRecord,
    IdentityDecisionRecord,
    SourceRecord,
    UnifiedEntity,
    SourceRevision,
)
from app.services.unification.matching import _normalize
from app.services.unification.schemes import MatchScheme
from app.services.unification.locking import lock_project_identity_state
from app.services.unification.telemetry import current_trace_id


class IdentityConflict(RuntimeError):
    """The requested identity change conflicts with current state or evidence."""


class DecisionInput(BaseModel):
    project_id: str
    left_entity_id: str
    right_entity_id: str
    action: str
    reason: str


@dataclass(frozen=True)
class IdentityChange:
    id: str
    revision: int
    change_type: str


@dataclass(frozen=True)
class CurrentMemberConflict:
    entity_id: str
    left_revision_id: str
    right_revision_id: str
    evidence: tuple[dict, ...]


def _current_revision(db: Session, project_id: str) -> int:
    return db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
        IdentityChangeRecord.project_id == project_id
    )) or 1


def _record_change(db: Session, project_id: str, action: str, actor_id: str, reason: str, payload: dict, expected_revision: int) -> IdentityChangeRecord:
    lock_project_identity_state(db, project_id)
    current = _current_revision(db, project_id)
    if current != expected_revision:
        raise IdentityConflict(f"stale identity revision: expected {expected_revision}, current {current}")
    change = IdentityChangeRecord(
        project_id=project_id, revision=current + 1, change_type=action,
        actor_id=actor_id, reason=reason, payload=payload, trace_id=current_trace_id(),
    )
    db.add(change)
    try:
        db.flush()
    except IntegrityError as exc:
        raise IdentityConflict("identity revision changed concurrently") from exc
    return change


def set_attribute_override(db: Session, *, project_id: str, entity_id: str, field: str,
                           value: object, reason: str, actor_id: str, expected_revision: int,
                           expected_member_revisions: list[list[str]]) -> IdentityChange:
    lock_project_identity_state(db, project_id)
    entity = db.get(UnifiedEntity, entity_id)
    if entity is None or entity.project_id != project_id or entity.status != "ACTIVE":
        raise IdentityConflict("attribute overrides require an active project entity")
    try:
        members = db.execute(select(SourceRecord.id, SourceRecord.current_revision_id).where(
            SourceRecord.project_id == project_id, SourceRecord.current_entity_id == entity_id,
        ).order_by(SourceRecord.id)).all()
        if not members:
            raise IdentityConflict("attribute overrides require an entity with source members")
        member_revisions = [[member.id, member.current_revision_id] for member in members]
        if member_revisions != expected_member_revisions:
            raise IdentityConflict("source evidence changed; refresh the attribute override preview")
        change = _record_change(db, project_id, "ATTRIBUTE_OVERRIDE", actor_id, reason,
                                {"entity_id": entity_id, "field": field, "value": value, "member_revisions": member_revisions}, expected_revision)
        db.commit()
        return IdentityChange(change.id, change.revision, change.change_type)
    except Exception:
        db.rollback()
        raise


def record_identity_conflict(
    db: Session, *, project_id: str, entity_id: str, left_source_revision_id: str,
    right_source_revision_id: str, evidence: list[dict], actor_id: str, commit: bool = True,
) -> str:
    lock_project_identity_state(db, project_id)
    if left_source_revision_id > right_source_revision_id:
        left_source_revision_id, right_source_revision_id = right_source_revision_id, left_source_revision_id
        evidence = [
            ({**item, "left": item["right"], "right": item["left"]} if "left" in item and "right" in item else item)
            for item in evidence
        ]
    existing = db.scalar(select(IdentityConflictRecord).where(
        IdentityConflictRecord.project_id == project_id,
        IdentityConflictRecord.entity_id == entity_id,
        IdentityConflictRecord.left_source_revision_id == left_source_revision_id,
        IdentityConflictRecord.right_source_revision_id == right_source_revision_id,
        IdentityConflictRecord.status == "OPEN",
    ))
    if existing:
        return existing.id
    change = _record_change(
        db, project_id, "CONFLICT", actor_id, "source revision violates current entity identity",
        {"entity_id": entity_id, "left_source_revision_id": left_source_revision_id,
         "right_source_revision_id": right_source_revision_id, "evidence": evidence},
        _current_revision(db, project_id),
    )
    conflict = IdentityConflictRecord(
        project_id=project_id, entity_id=entity_id,
        left_source_revision_id=left_source_revision_id,
        right_source_revision_id=right_source_revision_id,
        evidence=evidence, status="OPEN", change_id=change.id,
    )
    db.add(conflict)
    db.flush()
    if commit:
        db.commit()
    return conflict.id


def find_current_member_conflicts(
    db: Session, *, project_id: str, entity_ids: set[str], scheme: MatchScheme,
) -> list[CurrentMemberConflict]:
    """Check all current persisted members, including records outside this input batch."""
    if not entity_ids:
        return []
    revisions = db.scalars(select(SourceRevision).join(
        SourceRecord, SourceRecord.current_revision_id == SourceRevision.id,
    ).options(joinedload(SourceRevision.source_record)).where(
        SourceRecord.project_id == project_id,
        SourceRecord.current_entity_id.in_(entity_ids),
    )).all()
    by_entity: dict[str, list[SourceRevision]] = {}
    for revision in revisions:
        by_entity.setdefault(revision.source_record.current_entity_id, []).append(revision)
    fields = set(scheme.strong_identifiers)
    fields.update(item.get("field") for item in scheme.conflict_constraints)
    conflicts: dict[tuple[str, str, str], list[dict]] = {}
    for entity_id, members in by_entity.items():
        for left, right in combinations(members, 2):
            evidence = []
            for field in fields:
                source_field = scheme.field_map[field]
                left_value = _normalize(left.attributes.get(source_field), scheme.normalizers)
                right_value = _normalize(right.attributes.get(source_field), scheme.normalizers)
                if left_value is not None and right_value is not None and left_value != right_value:
                    evidence.append({"rule": "current_member_conflict", "field": field, "left": left_value, "right": right_value})
            if evidence:
                pair = (left.id, right.id)
                conflicts[(entity_id, pair[0], pair[1])] = evidence
    return [
        CurrentMemberConflict(entity_id, left_id, right_id, tuple(evidence))
        for (entity_id, left_id, right_id), evidence in sorted(conflicts.items())
    ]


def resolve_entity_id(db: Session, entity_id: str) -> str:
    seen: set[str] = set()
    current = entity_id
    while True:
        if current in seen:
            raise IdentityConflict("redirect cycle rejected")
        seen.add(current)
        entity = db.get(UnifiedEntity, current)
        if entity is None:
            raise IdentityConflict("entity not found")
        if entity.redirect_to_id is None:
            if entity.status != "ACTIVE":
                raise IdentityConflict("retired entity has no redirect")
            return current
        current = entity.redirect_to_id


def _assert_no_cannot_link(db: Session, project_id: str, members: set[str]) -> None:
    for link in db.scalars(select(IdentityDecisionRecord).where(
        IdentityDecisionRecord.project_id == project_id,
        IdentityDecisionRecord.action == "KEEP_DISTINCT",
    )):
        left = resolve_entity_id(db, link.left_entity_id)
        right = resolve_entity_id(db, link.right_entity_id)
        if left in members and right in members:
            raise IdentityConflict(f"cannot-link constraint blocks merge; decision change_id={link.change_id}")


def merge_entities(
    db: Session, *, project_id: str, target_id: str, source_ids: list[str],
    actor_id: str, expected_revision: int, reason: str = "manual merge",
    decision_pair: tuple[str, str] | None = None,
    commit: bool = True,
    scheme: MatchScheme | None = None,
) -> IdentityChange:
    try:
        lock_project_identity_state(db, project_id)
        if _current_revision(db, project_id) != expected_revision:
            raise IdentityConflict(f"stale identity revision: expected {expected_revision}, current {_current_revision(db, project_id)}")
        target_id = resolve_entity_id(db, target_id)
        sources = sorted({resolve_entity_id(db, entity_id) for entity_id in source_ids} - {target_id})
        if not sources:
            raise IdentityConflict("merge requires at least one distinct source entity")
        entities = [db.get(UnifiedEntity, entity_id) for entity_id in [target_id, *sources]]
        if any(entity is None or entity.project_id != project_id for entity in entities):
            raise IdentityConflict("entities must belong to the project")
        if len({entity.entity_type for entity in entities}) != 1:
            raise IdentityConflict("entities of different types cannot be merged")
        members = {target_id, *sources}
        _assert_no_cannot_link(db, project_id, members)
        current_revisions = db.scalars(select(SourceRevision).join(
            SourceRecord, SourceRecord.current_revision_id == SourceRevision.id,
        ).where(
            SourceRecord.project_id == project_id,
            SourceRecord.current_entity_id.in_(members),
        )).all()
        if current_revisions and scheme is None:
            raise IdentityConflict("scheme is required to validate entity members before merge")
        if scheme is not None:
            constrained_fields = set(scheme.strong_identifiers)
            constrained_fields.update(rule.get("field") for rule in scheme.conflict_constraints)
            for field in constrained_fields:
                source_field = scheme.field_map.get(field)
                values = {
                    value for revision in current_revisions
                    if (value := _normalize(revision.attributes.get(source_field), scheme.normalizers)) is not None
                }
                if len(values) > 1:
                    raise IdentityConflict(f"scheme conflict blocks merge for field {field}")
        records = db.scalars(select(SourceRecord).where(
            SourceRecord.project_id == project_id,
            SourceRecord.current_entity_id.in_(sources),
        ).with_for_update()).all()
        target_record_ids = db.scalars(select(SourceRecord.id).where(
            SourceRecord.project_id == project_id,
            SourceRecord.current_entity_id == target_id,
        )).all()
        previous = {
            "source_entity_ids": sources,
            "target_id": target_id,
            "record_ids": [item.id for item in records],
            "record_entity_map": {item.id: item.current_entity_id for item in records},
            "record_revision_map": {item.id: item.current_revision_id for item in records},
            "target_record_ids": target_record_ids,
        }
        change = _record_change(db, project_id, "MERGE", actor_id, reason, previous, expected_revision)
        for item in records:
            item.current_entity_id = target_id
        for entity in entities[1:]:
            entity.status = "MERGED"
            entity.redirect_to_id = target_id
        if decision_pair:
            left, right = sorted(decision_pair)
            decision = db.scalar(select(IdentityDecisionRecord).where(
                IdentityDecisionRecord.project_id == project_id,
                IdentityDecisionRecord.left_entity_id == left,
                IdentityDecisionRecord.right_entity_id == right,
            ))
            if decision:
                decision.action = "MERGE"
                decision.actor_id = actor_id
                decision.reason = reason
                decision.change_id = change.id
            else:
                db.add(IdentityDecisionRecord(
                    project_id=project_id, left_entity_id=left, right_entity_id=right,
                    action="MERGE", actor_id=actor_id, reason=reason, change_id=change.id,
                ))
        if commit:
            db.commit()
        else:
            db.flush()
        return IdentityChange(change.id, change.revision, change.change_type)
    except Exception:
        db.rollback()
        raise


def apply_decision(
    db: Session, decision: DecisionInput, *, expected_revision: int, actor_id: str, commit: bool = True,
    scheme: MatchScheme | None = None,
) -> IdentityChange:
    lock_project_identity_state(db, decision.project_id)
    if decision.action not in {"MERGE", "KEEP_DISTINCT"}:
        raise IdentityConflict("action must be MERGE or KEEP_DISTINCT")
    if decision.left_entity_id == decision.right_entity_id:
        raise IdentityConflict("decision requires two distinct entities")
    left, right = sorted((decision.left_entity_id, decision.right_entity_id))
    existing = db.scalar(select(IdentityDecisionRecord).where(
        IdentityDecisionRecord.project_id == decision.project_id,
        IdentityDecisionRecord.left_entity_id == left,
        IdentityDecisionRecord.right_entity_id == right,
    ))
    if existing and existing.action == decision.action:
        change = db.get(IdentityChangeRecord, existing.change_id)
        return IdentityChange(change.id, change.revision, change.change_type)
    entities = [db.get(UnifiedEntity, decision.left_entity_id), db.get(UnifiedEntity, decision.right_entity_id)]
    if any(entity is None or entity.project_id != decision.project_id for entity in entities):
        raise IdentityConflict("decision entities must belong to the project")
    if resolve_entity_id(db, decision.left_entity_id) == resolve_entity_id(db, decision.right_entity_id):
        raise IdentityConflict("decision requires two distinct active entities")
    if decision.action == "MERGE":
        change = merge_entities(
            db, project_id=decision.project_id, target_id=decision.left_entity_id,
            source_ids=[decision.right_entity_id],
            actor_id=actor_id, expected_revision=expected_revision, reason=decision.reason,
            decision_pair=(left, right), commit=commit, scheme=scheme,
        )
        return change
    try:
        change = _record_change(db, decision.project_id, "DECISION", actor_id, decision.reason,
                                {"left_entity_id": left, "right_entity_id": right, "action": "KEEP_DISTINCT"}, expected_revision)
        existing = db.scalar(select(IdentityDecisionRecord).where(
            IdentityDecisionRecord.project_id == decision.project_id,
            IdentityDecisionRecord.left_entity_id == left,
            IdentityDecisionRecord.right_entity_id == right,
        ))
        if existing:
            existing.action = "KEEP_DISTINCT"
            existing.actor_id = actor_id
            existing.reason = decision.reason
            existing.change_id = change.id
        else:
            db.add(IdentityDecisionRecord(
                project_id=decision.project_id, left_entity_id=left, right_entity_id=right,
                action="KEEP_DISTINCT", actor_id=actor_id, reason=decision.reason, change_id=change.id,
            ))
        if commit:
            db.commit()
        else:
            db.flush()
        return IdentityChange(change.id, change.revision, change.change_type)
    except Exception:
        db.rollback()
        raise


def undo_identity_change(
    db: Session, change_id: str, *, expected_revision: int, actor_id: str, reason: str | None = None,
) -> IdentityChange:
    original = db.get(IdentityChangeRecord, change_id)
    if original is None:
        raise IdentityConflict("identity change not found")
    lock_project_identity_state(db, original.project_id)
    try:
        current = _current_revision(db, original.project_id)
        if current != expected_revision:
            raise IdentityConflict(f"stale identity revision: expected {expected_revision}, current {current}")
        if original.change_type == "DECISION":
            decision = db.scalar(select(IdentityDecisionRecord).where(IdentityDecisionRecord.change_id == original.id))
            if decision is None or decision.action != "KEEP_DISTINCT":
                raise IdentityConflict("decision is no longer active")
            change = _record_change(db, original.project_id, "UNDO", actor_id, reason or f"undo {original.id}",
                                    {"undoes": original.id}, expected_revision)
            decision.action = "UNDONE"
            decision.change_id = change.id
            db.commit()
            return IdentityChange(change.id, change.revision, change.change_type)
        if original.change_type == "SPLIT":
            source_entity_id = original.payload["source_entity_id"]
            new_entity_id = original.payload["new_entity_id"]
            source_ids = original.payload["source_record_ids"]
            records = [db.get(SourceRecord, record_id) for record_id in source_ids]
            if any(item is None or item.current_entity_id != new_entity_id for item in records):
                raise IdentityConflict("split dependencies changed; explicit re-review is required")
            if any(item.current_revision_id != original.payload["record_revision_map"].get(item.id) for item in records):
                raise IdentityConflict("source revisions changed; re-review is required before undo")
            if db.scalar(select(SourceRecord.id).where(
                SourceRecord.project_id == original.project_id,
                SourceRecord.current_entity_id == new_entity_id,
                SourceRecord.id.not_in(source_ids),
            )):
                raise IdentityConflict("new split entity has later members; explicit re-review is required")
            pair_decision = db.scalar(select(IdentityDecisionRecord).where(IdentityDecisionRecord.change_id == original.id))
            if pair_decision is None:
                raise IdentityConflict("split cannot-link record is missing")
            for later in db.scalars(select(IdentityDecisionRecord).where(
                IdentityDecisionRecord.project_id == original.project_id,
                IdentityDecisionRecord.action.in_(["MERGE", "KEEP_DISTINCT"]),
            )):
                later_change = db.get(IdentityChangeRecord, later.change_id)
                if later_change and later_change.revision > original.revision and {source_entity_id, new_entity_id}.intersection({later.left_entity_id, later.right_entity_id}):
                    raise IdentityConflict("later identity decisions depend on this split")
            change = _record_change(db, original.project_id, "UNDO", actor_id, reason or f"undo {original.id}",
                                    {"undoes": original.id, "split_new_entity_id": new_entity_id}, expected_revision)
            for item in records:
                item.current_entity_id = source_entity_id
            new_entity = db.get(UnifiedEntity, new_entity_id)
            new_entity.status = "MERGED"
            new_entity.redirect_to_id = source_entity_id
            pair_decision.action = "UNDONE"
            pair_decision.change_id = change.id
            for conflict in db.scalars(select(IdentityConflictRecord).where(
                IdentityConflictRecord.project_id == original.project_id,
                IdentityConflictRecord.entity_id == source_entity_id,
                IdentityConflictRecord.status == "RESOLVED",
            )):
                left_source = db.scalar(select(SourceRevision.source_record_id).where(SourceRevision.id == conflict.left_source_revision_id))
                right_source = db.scalar(select(SourceRevision.source_record_id).where(SourceRevision.id == conflict.right_source_revision_id))
                left_record = db.get(SourceRecord, left_source) if left_source else None
                right_record = db.get(SourceRecord, right_source) if right_source else None
                if left_record and right_record and left_record.current_entity_id == right_record.current_entity_id:
                    conflict.status = "OPEN"
            db.commit()
            return IdentityChange(change.id, change.revision, change.change_type)
        if original.change_type != "MERGE":
            raise IdentityConflict("this identity change cannot be undone")
        target = original.payload["target_id"]
        sources = original.payload["source_entity_ids"]
        if resolve_entity_id(db, target) != target or any(resolve_entity_id(db, entity_id) != target for entity_id in sources):
            raise IdentityConflict("merge dependencies changed; explicit split is required")
        expected_record_ids = set(original.payload["record_ids"]) | set(original.payload.get("target_record_ids", []))
        present_record_ids = set(db.scalars(select(SourceRecord.id).where(
            SourceRecord.project_id == original.project_id,
            SourceRecord.current_entity_id == target,
        )).all())
        if expected_record_ids != present_record_ids:
            raise IdentityConflict("merge dependencies changed; explicit split is required")
        for record_id, revision_id in original.payload.get("record_revision_map", {}).items():
            item = db.get(SourceRecord, record_id)
            if item is None or item.current_revision_id != revision_id:
                raise IdentityConflict("source revisions changed; re-review is required before undo")
        affected = {target, *sources}
        later_decisions = db.scalars(select(IdentityDecisionRecord).where(
            IdentityDecisionRecord.project_id == original.project_id,
            IdentityDecisionRecord.action.in_(["MERGE", "KEEP_DISTINCT"]),
        )).all()
        for decision in later_decisions:
            change_record = db.get(IdentityChangeRecord, decision.change_id)
            if change_record and change_record.revision > original.revision and affected.intersection({decision.left_entity_id, decision.right_entity_id}):
                raise IdentityConflict("later identity decisions depend on this merge; explicit split is required")
        for record_id in original.payload["record_ids"]:
            item = db.get(SourceRecord, record_id)
            if item is None or item.current_entity_id != target:
                raise IdentityConflict("merge dependencies changed; explicit split is required")
        restored = {"undoes": original.id, "target_id": target, "source_entity_ids": sources}
        change = _record_change(db, original.project_id, "UNDO", actor_id, reason or f"undo {original.id}", restored, expected_revision)
        mapping = original.payload.get("record_entity_map", {})
        for record_id, entity_id in mapping.items():
            db.get(SourceRecord, record_id).current_entity_id = entity_id
        for entity_id in sources:
            entity = db.get(UnifiedEntity, entity_id)
            entity.status = "ACTIVE"
            entity.redirect_to_id = None
        decision = db.scalar(select(IdentityDecisionRecord).where(
            IdentityDecisionRecord.change_id == original.id,
        ))
        if decision:
            decision.action = "UNDONE"
            decision.change_id = change.id
        db.commit()
        return IdentityChange(change.id, change.revision, change.change_type)
    except Exception:
        db.rollback()
        raise


def split_entity(
    db: Session, *, project_id: str, entity_id: str, source_record_ids: list[str],
    actor_id: str, reason: str, expected_revision: int,
) -> IdentityChange:
    """Move an explicit non-empty proper subset of source records to a new entity ID."""
    try:
        lock_project_identity_state(db, project_id)
        entity_id = resolve_entity_id(db, entity_id)
        entity = db.get(UnifiedEntity, entity_id)
        records = db.scalars(select(SourceRecord).where(
            SourceRecord.project_id == project_id,
            SourceRecord.current_entity_id == entity_id,
        ).with_for_update()).all()
        chosen = set(source_record_ids)
        available = {item.id for item in records}
        if entity is None or entity.project_id != project_id:
            raise IdentityConflict("entity must belong to the project")
        if not chosen or not chosen < available:
            raise IdentityConflict("split must select a non-empty proper subset of source records")
        new_entity = UnifiedEntity(project_id=project_id, entity_type=entity.entity_type, status="ACTIVE")
        db.add(new_entity)
        db.flush()
        change = _record_change(db, project_id, "SPLIT", actor_id, reason,
                                {"source_entity_id": entity_id, "new_entity_id": new_entity.id,
                                 "source_record_ids": sorted(chosen),
                                 "record_revision_map": {item.id: item.current_revision_id for item in records if item.id in chosen}}, expected_revision)
        for record in records:
            if record.id in chosen:
                record.current_entity_id = new_entity.id
        left, right = sorted((entity_id, new_entity.id))
        db.add(IdentityDecisionRecord(
            project_id=project_id, left_entity_id=left, right_entity_id=right,
            action="KEEP_DISTINCT", actor_id=actor_id, reason=reason, change_id=change.id,
        ))
        for conflict in db.scalars(select(IdentityConflictRecord).where(
            IdentityConflictRecord.project_id == project_id,
            IdentityConflictRecord.entity_id == entity_id,
            IdentityConflictRecord.status == "OPEN",
        )):
            left_source = db.scalar(select(SourceRevision.source_record_id).where(SourceRevision.id == conflict.left_source_revision_id))
            right_source = db.scalar(select(SourceRevision.source_record_id).where(SourceRevision.id == conflict.right_source_revision_id))
            if not left_source or not right_source:
                continue
            left_record, right_record = db.get(SourceRecord, left_source), db.get(SourceRecord, right_source)
            if left_record and right_record and left_record.current_entity_id != right_record.current_entity_id:
                conflict.status = "RESOLVED"
        db.commit()
        return IdentityChange(change.id, change.revision, change.change_type)
    except Exception:
        db.rollback()
        raise
