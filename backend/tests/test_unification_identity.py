"""Persistent identity decision behavior."""
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import Base
from app.models.entities import Project, Workspace
from app.models.unification import IdentityChangeRecord, SourceRecord, SourceRevision, UnifiedEntity
from app.services.unification.identity import (
    DecisionInput,
    IdentityConflict,
    apply_decision,
    merge_entities,
    resolve_entity_id,
    split_entity,
    undo_identity_change,
)
from app.services.unification.schemes import MatchScheme
from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing, start_span


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Workspace(id="workspace", name="Workspace", slug="workspace"))
    session.flush()
    session.add(Project(id="project", workspace_id="workspace", name="Project", slug="project"))
    session.flush()
    first = UnifiedEntity(project_id="project", entity_type="supplier")
    second = UnifiedEntity(project_id="project", entity_type="supplier")
    session.add_all([first, second])
    session.flush()
    session.add_all([
        SourceRecord(project_id="project", source_system="erp", dataset="suppliers", source_record_id="1", current_entity_id=first.id),
        SourceRecord(project_id="project", source_system="crm", dataset="suppliers", source_record_id="2", current_entity_id=second.id),
    ])
    session.commit()
    try:
        yield session, first.id, second.id
    finally:
        session.close()
        engine.dispose()


def test_human_approve_persists_merge_and_reuses_decision(db):
    session, target, source = db
    decision = DecisionInput(project_id="project", left_entity_id=target, right_entity_id=source, action="MERGE", reason="verified tax registration")
    first = apply_decision(session, decision, expected_revision=1, actor_id="reviewer")
    retry = apply_decision(session, decision, expected_revision=1, actor_id="reviewer")
    assert first.id == retry.id
    assert resolve_entity_id(session, source) == target
    assert session.scalar(select(SourceRecord).where(SourceRecord.source_record_id == "2")).current_entity_id == target


def test_identity_audit_keeps_trace_reference_after_trace_data_is_cleared(db):
    session, target, source = db
    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    try:
        with start_span("api.identity_decision"):
            change = merge_entities(session, project_id="project", target_id=target, source_ids=[source], actor_id="reviewer", expected_revision=1)
        trace_id = session.get(IdentityChangeRecord, change.id).trace_id
        assert trace_id is not None
        assert provider.force_flush()
        assert exporter.get_finished_spans()
        exporter.clear()
        assert not exporter.get_finished_spans()
        # The trace backend can discard its span; the durable identity audit still retains the reference.
        assert session.get(IdentityChangeRecord, change.id).trace_id == trace_id
    finally:
        shutdown_tracing()


def test_human_reject_persists_cannot_link_and_blocks_merge(db):
    session, target, source = db
    decision = DecisionInput(project_id="project", left_entity_id=target, right_entity_id=source, action="KEEP_DISTINCT", reason="separate tax IDs")
    first = apply_decision(session, decision, expected_revision=1, actor_id="reviewer")
    retry = apply_decision(session, decision, expected_revision=1, actor_id="reviewer")
    assert first.id == retry.id
    with pytest.raises(IdentityConflict, match="cannot-link"):
        merge_entities(session, project_id="project", target_id=target, source_ids=[source], actor_id="reviewer", expected_revision=2)


def test_human_reject_can_be_undone_with_audited_revision(db):
    session, target, source = db
    decision = apply_decision(session, DecisionInput(project_id="project", left_entity_id=target, right_entity_id=source, action="KEEP_DISTINCT", reason="verified separate"), expected_revision=1, actor_id="reviewer")
    undone = undo_identity_change(session, decision.id, expected_revision=decision.revision, actor_id="reviewer")
    merged = merge_entities(session, project_id="project", target_id=target, source_ids=[source], actor_id="reviewer", expected_revision=undone.revision)
    assert undone.change_type == "UNDO"
    assert merged.change_type == "MERGE"


def test_undo_merge_restores_retired_entity_id(db):
    session, target, source = db
    change = merge_entities(session, project_id="project", target_id=target, source_ids=[source], actor_id="reviewer", expected_revision=1)
    undone = undo_identity_change(session, change.id, expected_revision=change.revision, actor_id="reviewer")
    assert undone.change_type == "UNDO"
    assert resolve_entity_id(session, source) == source
    assert session.get(UnifiedEntity, source).status == "ACTIVE"


def test_stale_identity_revision_is_rejected(db):
    session, target, source = db
    merge_entities(session, project_id="project", target_id=target, source_ids=[source], actor_id="reviewer", expected_revision=1)
    with pytest.raises(IdentityConflict, match="stale"):
        merge_entities(session, project_id="project", target_id=target, source_ids=[source], actor_id="reviewer", expected_revision=1)


def test_cannot_link_blocks_transitive_merge(db):
    session, first, second = db
    third = UnifiedEntity(project_id="project", entity_type="supplier")
    session.add(third)
    session.flush()
    session.add(SourceRecord(project_id="project", source_system="other", dataset="suppliers", source_record_id="3", current_entity_id=third.id))
    session.commit()
    apply_decision(session, DecisionInput(project_id="project", left_entity_id=first, right_entity_id=third.id, action="KEEP_DISTINCT", reason="different legal entities"), expected_revision=1, actor_id="reviewer")
    merge_entities(session, project_id="project", target_id=first, source_ids=[second], actor_id="reviewer", expected_revision=2)
    with pytest.raises(IdentityConflict, match="cannot-link"):
        merge_entities(session, project_id="project", target_id=first, source_ids=[third.id], actor_id="reviewer", expected_revision=3)


def test_explicit_split_creates_new_identity_and_cannot_link(db):
    session, first, _second = db
    retained = SourceRecord(project_id="project", source_system="other", dataset="suppliers", source_record_id="3", current_entity_id=first)
    selected = SourceRecord(project_id="project", source_system="other", dataset="suppliers", source_record_id="3", current_entity_id=first)
    selected.source_record_id = "4"
    session.add_all([retained, selected])
    session.commit()
    change = split_entity(session, project_id="project", entity_id=first, source_record_ids=[selected.id], actor_id="reviewer", reason="verified separate legal entities", expected_revision=1)
    moved = session.get(SourceRecord, selected.id).current_entity_id
    assert change.change_type == "SPLIT"
    assert moved != first
    with pytest.raises(IdentityConflict, match="cannot-link"):
        merge_entities(session, project_id="project", target_id=first, source_ids=[moved], actor_id="reviewer", expected_revision=2)
    undone = undo_identity_change(session, change.id, expected_revision=change.revision, actor_id="reviewer")
    assert undone.change_type == "UNDO"
    assert session.get(SourceRecord, selected.id).current_entity_id == first
    assert resolve_entity_id(session, moved) == first


def test_redirect_cycle_is_rejected(db):
    session, first, _second = db
    entity = session.get(UnifiedEntity, first)
    entity.status = "MERGED"
    entity.redirect_to_id = first
    session.commit()
    with pytest.raises(IdentityConflict, match="cycle"):
        resolve_entity_id(session, first)


def test_hard_conflict_across_existing_members_blocks_transitive_merge(db):
    session, first, second = db
    third = UnifiedEntity(project_id="project", entity_type="supplier")
    session.add(third)
    session.flush()
    third_record = SourceRecord(project_id="project", source_system="other", dataset="suppliers", source_record_id="3", current_entity_id=third.id)
    session.add(third_record)
    session.flush()
    source_records = session.scalars(select(SourceRecord).where(SourceRecord.project_id == "project").order_by(SourceRecord.source_record_id)).all()
    attrs = [
        {"tax_id": "T-1", "voltage": "110"},
        {"tax_id": "T-1"},
        {"tax_id": "T-1", "voltage": "220"},
    ]
    for item, values in zip(source_records, attrs):
        revision = SourceRevision(source_record_id=item.id, revision_no=1, input_version="v1", attributes=values, content_hash="x" * 64)
        session.add(revision)
        session.flush()
        item.current_revision_id = revision.id
    session.commit()
    scheme = MatchScheme.model_validate({
        "entity_type": "supplier", "identity_granularity": "legal_entity",
        "field_map": {"tax_id": "tax_id", "voltage": "voltage"},
        "strong_identifiers": ["tax_id"], "conflict_constraints": [{"field": "voltage"}],
    })
    merge_entities(session, project_id="project", target_id=first, source_ids=[second], actor_id="reviewer", expected_revision=1, scheme=scheme)
    with pytest.raises(IdentityConflict, match="scheme conflict"):
        merge_entities(session, project_id="project", target_id=first, source_ids=[third.id], actor_id="reviewer", expected_revision=2, scheme=scheme)


def test_undo_merge_rejected_when_later_cannot_link_depends_on_it(db):
    session, first, second = db
    third = UnifiedEntity(project_id="project", entity_type="supplier")
    session.add(third)
    session.flush()
    session.add(SourceRecord(project_id="project", source_system="other", dataset="suppliers", source_record_id="3", current_entity_id=third.id))
    session.commit()
    merged = merge_entities(session, project_id="project", target_id=first, source_ids=[second], actor_id="reviewer", expected_revision=1)
    decision = apply_decision(session, DecisionInput(project_id="project", left_entity_id=first, right_entity_id=third.id, action="KEEP_DISTINCT", reason="verified distinct"), expected_revision=merged.revision, actor_id="reviewer")
    with pytest.raises(IdentityConflict, match="later identity decisions"):
        undo_identity_change(session, merged.id, expected_revision=decision.revision, actor_id="reviewer")
