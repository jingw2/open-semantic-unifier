"""Project-scoped HTTP contracts for the unification workbench."""
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.deps import get_current_user, verify_serving_api_key
from app.core.db import Base, get_db
from app.main import app
from app.models.entities import Project, Workspace
from app.models.unification import IdentityChangeRecord, ProjectSnapshotState, SourceRevision, UnificationSchemeVersion, UnificationSnapshot
from app.services.unification.contracts import IngestMode, SourceRecordInput
from app.services.unification.ingest import ingest_batch
from app.services.unification.identity import DecisionInput, apply_decision
from app.services.unification.releases import complete_run, start_run, submit_run
from app.services.unification.schemes import MatchScheme, save_scheme


@pytest.fixture
def public_api(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'public-api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add_all([
            Workspace(id="ws-one", name="Workspace One", slug="ws-one"),
            Workspace(id="ws-two", name="Workspace Two", slug="ws-two"),
        ])
        db.flush()
        db.add_all([
            Project(id="project-one", workspace_id="ws-one", name="Project One", slug="project-one"),
            Project(id="project-two", workspace_id="ws-two", name="Project Two", slug="project-two"),
        ])
        db.commit()
    identity = {"workspace_id": "ws-one", "role": "editor", "user_id": "editor-one"}

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: identity.copy()
    try:
        yield TestClient(app), maker, identity
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_foreign_project_resources_are_hidden(public_api):
    client, _maker, _identity = public_api
    response = client.get("/api/v1/projects/project-two/unification/schemes")
    assert response.status_code == 404


def test_viewer_cannot_mutate_unification_scheme(public_api):
    client, _maker, identity = public_api
    identity["role"] = "viewer"
    response = client.put("/api/v1/projects/project-one/unification/schemes/org", json={
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name"},
    })
    assert response.status_code == 403


def test_entity_export_is_derived_from_the_requested_snapshot(public_api):
    client, maker, _identity = public_api
    snapshot_json = {
        "entities": {"entity-one": {"entity_type": "organization", "status": "ACTIVE"}},
        "memberships": {'["project-one","erp","vendor","17"]': "entity-one"},
        "source_revisions": {'["project-one","erp","vendor","17"]': "revision-one"},
        "attributes": {"entity-one": {"name": "Acme Ltd"}},
        "conflicts": {"entity-one": []},
        "provenance": {"entity-one": {"selected": {}, "requires_review": []}},
        "pairs": [{
            "left_source_revision_id": "revision-left", "right_source_revision_id": "revision-right",
            "candidate": True, "action": "REVIEW", "evidence_codes": ["EXACT_TAX_ID"],
            "evidence": [{"rule": "exact", "field": "tax_id"}], "conflict_id": None,
        }],
    }
    with maker() as db:
        snapshot = UnificationSnapshot(
            project_id="project-one", version=3, snapshot_json=snapshot_json, actor_id="publisher",
        )
        db.add(snapshot)
        db.flush()
        db.add(ProjectSnapshotState(project_id="project-one", current_snapshot_id=snapshot.id, current_version=3))
        db.add(IdentityChangeRecord(
            project_id="project-one", revision=4, change_type="MERGE", actor_id="reviewer",
            reason="later decision", payload={"later": True},
        ))
        db.commit()
        snapshot_id = snapshot.id
    response = client.get(f"/api/v1/projects/project-one/unification/snapshots/{snapshot_id}/export?kind=entities")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "snapshot_id": snapshot_id,
        "version": 3,
        "entities": [{
            "entity_id": "entity-one", "entity_type": "organization", "status": "ACTIVE",
            "attributes": {"name": "Acme Ltd"},
            "members": [{
                "source_key": ["project-one", "erp", "vendor", "17"],
                "source_revision_id": "revision-one",
            }],
            "provenance": {"selected": {}, "requires_review": []},
            "conflicts": [],
        }],
    }


def test_all_snapshot_exports_are_bound_to_the_requested_version(public_api):
    client, maker, _identity = public_api
    snapshot_json = {
        "entities": {"entity-one": {"entity_type": "organization", "status": "ACTIVE"}},
        "memberships": {}, "source_revisions": {}, "attributes": {}, "conflicts": {}, "provenance": {},
        "pairs": [{
            "left_source_revision_id": "left-r1", "right_source_revision_id": "right-r1",
            "candidate": True, "action": "REVIEW", "evidence_codes": ["NAME_SIMILAR"],
            "evidence": [{"score": 0.9}], "conflict_id": None,
        }],
        "identity_revision": 8,
        "identity_changes": [{
            "change_id": "decision-at-v8", "revision": 8, "change_type": "MERGE",
            "actor_id": "reviewer-one", "reason": "reviewed same entity", "payload": {"target_id": "entity-one"},
            "trace_id": "trace-one", "created_at": "2026-11-22T12:00:00+00:00",
        }],
    }
    with maker() as db:
        snapshot = UnificationSnapshot(project_id="project-one", version=7, snapshot_json=snapshot_json, actor_id="publisher")
        db.add(snapshot)
        db.add(IdentityChangeRecord(
            project_id="project-one", revision=9, change_type="MERGE", actor_id="later-reviewer",
            reason="a decision made after this snapshot", payload={"later": True},
        ))
        db.commit()
        snapshot_id = snapshot.id

    outputs = {}
    for kind in ("entities", "mappings", "decisions", "delta"):
        response = client.get(f"/api/v1/projects/project-one/unification/snapshots/{snapshot_id}/export?kind={kind}")
        assert response.status_code == 200, response.text
        outputs[kind] = response.json()
        assert outputs[kind]["snapshot_id"] == snapshot_id
        assert outputs[kind]["version"] == 7
    assert outputs["decisions"] == {
        "snapshot_id": snapshot_id, "version": 7, "identity_revision": 8,
        "decisions": [{
            "change_id": "decision-at-v8", "revision": 8, "change_type": "MERGE",
            "actor_id": "reviewer-one", "reason": "reviewed same entity", "payload": {"target_id": "entity-one"},
            "trace_id": "trace-one", "created_at": "2026-11-22T12:00:00+00:00",
        }],
    }
    assert outputs["delta"]["delta"]["added_entities"] == ["entity-one"]
    assert client.get("/api/v1/projects/project-one/unification/entities").status_code == 422


def test_serving_resolves_only_against_an_explicit_project_snapshot(public_api):
    client, maker, _identity = public_api
    source_key = '["project-one","erp","vendor","17"]'
    with maker() as db:
        snapshot = UnificationSnapshot(
            project_id="project-one", version=5,
            snapshot_json={
                "memberships": {source_key: "entity-one"},
                "entities": {"entity-one": {"entity_type": "organization", "status": "ACTIVE"}},
                "attributes": {"entity-one": {"name": "Acme Ltd"}},
            }, actor_id="publisher",
        )
        db.add(snapshot)
        db.commit()
        snapshot_id = snapshot.id
        project = db.get(Project, "project-one")
    app.dependency_overrides[verify_serving_api_key] = lambda: project
    try:
        response = client.post("/api/v1/serve/project-one/unification/resolve", json={
            "snapshot_id": snapshot_id, "source_system": "ERP", "dataset": "Vendor", "source_record_id": "17",
        })
        assert response.status_code == 200, response.text
        assert response.json() == {
            "project": "project-one", "snapshot_id": snapshot_id, "version": 5,
            "matched": True, "entity_id": "entity-one",
            "entity": {"entity_type": "organization", "status": "ACTIVE"},
            "attributes": {"name": "Acme Ltd"},
        }
        missing_version = client.post("/api/v1/serve/project-one/unification/resolve", json={
            "source_system": "erp", "dataset": "vendor", "source_record_id": "17",
        })
        assert missing_version.status_code == 422
    finally:
        app.dependency_overrides.pop(verify_serving_api_key, None)


def test_exploration_batch_generates_a_temporary_source_id(public_api):
    client, _maker, _identity = public_api
    response = client.post("/api/v1/projects/project-one/unification/batches", json={
        "input_version": "exploration-v1", "mode": "exploration",
        "records": [{"source_system": "erp", "dataset": "vendor", "attributes": {"name": "Acme"}}],
    })
    assert response.status_code == 201, response.text
    identity = response.json()["identities"][0]
    assert identity["source_key"][3].startswith("explore:")


def test_invalid_batch_reports_validation_as_422(public_api):
    client, _maker, _identity = public_api
    response = client.post("/api/v1/projects/project-one/unification/batches", json={
        "input_version": "invalid-v1", "mode": "sync",
        "records": [{"source_system": "", "dataset": "vendor", "source_record_id": "17", "attributes": {}}],
    })
    assert response.status_code == 422
    assert response.json()["detail"][0]["code"] == "SOURCE_SYSTEM_REQUIRED"


def test_review_queue_closes_after_decision_and_rejects_stale_source_evidence(public_api):
    client, maker, _identity = public_api
    with maker() as db:
        save_scheme(db, project_id="project-one", scheme_key="review", scheme=MatchScheme(
            entity_type="organization", identity_granularity="legal_entity",
            field_map={"name": "name"}, candidate_rules=[{"kind": "exact", "fields": ["name"]}],
        ))
        version = db.query(UnificationSchemeVersion).filter_by(project_id="project-one", scheme_key="review").first()
        batch = ingest_batch(db, project_id="project-one", records=[
            SourceRecordInput("project-one", "erp", "vendors", "A", {"name": "Acme"}),
            SourceRecordInput("project-one", "crm", "vendors", "B", {"name": "Acme"}),
        ], mode=IngestMode.SYNC, input_version="review-input")
        db.commit()
        revisions = db.query(SourceRevision).filter(SourceRevision.id.in_(batch.source_revision_ids)).order_by(SourceRevision.id).all()
        run = submit_run(
            db, project_id="project-one", input_version="review-input", scheme_revision=version.id,
            idempotency_key="review-api-run", payload_hash="review-api-run-hash",
        )
        start_run(db, run.id)
        pair = {
            "left_source_revision_id": revisions[0].id, "right_source_revision_id": revisions[1].id,
            "candidate": True, "action": "REVIEW", "evidence_codes": ["CANDIDATE"], "evidence": [],
        }
        complete_run(db, run.id, {"pairs": [pair]})
        run_id = run.id
        revision_ids = (revisions[0].id, revisions[1].id)

    queue = client.get(f"/api/v1/projects/project-one/unification/reviews?run_id={run_id}")
    assert queue.status_code == 200
    assert queue.json()["total"] == 1
    decision = client.post("/api/v1/projects/project-one/unification/reviews", json={
        "run_id": run_id, "left_source_revision_id": revision_ids[0],
        "right_source_revision_id": revision_ids[1], "action": "MERGE",
        "reason": "review confirmed", "expected_revision": 1,
    })
    assert decision.status_code == 201, decision.text
    assert client.get(f"/api/v1/projects/project-one/unification/reviews?run_id={run_id}").json()["total"] == 0

    with maker() as db:
        changed = ingest_batch(db, project_id="project-one", records=[
            SourceRecordInput("project-one", "crm", "vendors", "B", {"name": "Acme New"}),
        ], mode=IngestMode.SYNC, input_version="review-input-v2")
        assert changed.accepted
        db.commit()
    stale = client.post("/api/v1/projects/project-one/unification/reviews", json={
        "run_id": run_id, "left_source_revision_id": revision_ids[0],
        "right_source_revision_id": revision_ids[1], "action": "KEEP_DISTINCT",
        "reason": "stale review must not apply", "expected_revision": 2,
    })
    assert stale.status_code == 409


def test_review_queue_keeps_transitive_cannot_link_after_entity_redirect(public_api):
    client, maker, _identity = public_api
    scheme = MatchScheme(
        entity_type="organization", identity_granularity="legal_entity",
        field_map={"name": "name"}, candidate_rules=[],
    )
    with maker() as db:
        save_scheme(db, project_id="project-one", scheme_key="redirects", scheme=scheme)
        version = db.query(UnificationSchemeVersion).filter_by(project_id="project-one", scheme_key="redirects").first()
        batch = ingest_batch(db, project_id="project-one", records=[
            SourceRecordInput("project-one", "erp", "vendors", "A", {"name": "Acme"}),
            SourceRecordInput("project-one", "crm", "vendors", "B", {"name": "Acme"}),
            SourceRecordInput("project-one", "mdm", "vendors", "C", {"name": "Acme"}),
        ], mode=IngestMode.SYNC, input_version="redirect-input")
        db.commit()
        revisions = db.query(SourceRevision).filter(SourceRevision.id.in_(batch.source_revision_ids)).all()
        revision_by_source_id = {item.source_record.source_record_id: item for item in revisions}
        run = submit_run(
            db, project_id="project-one", input_version="redirect-input", scheme_revision=version.id,
            idempotency_key="redirect-review-run", payload_hash="redirect-review-run-hash",
        )
        start_run(db, run.id)
        complete_run(db, run.id, {"pairs": [{
            "left_source_revision_id": revision_by_source_id["B"].id,
            "right_source_revision_id": revision_by_source_id["C"].id,
            "candidate": True, "action": "REVIEW", "evidence_codes": ["CANDIDATE"], "evidence": [],
        }]})
        entity_by_source = {
            key: item.source_record.current_entity_id for key, item in revision_by_source_id.items()
        }
        distinct = apply_decision(db, DecisionInput(
            project_id="project-one", left_entity_id=entity_by_source["A"],
            right_entity_id=entity_by_source["C"], action="KEEP_DISTINCT", reason="A and C are separate",
        ), expected_revision=1, actor_id="reviewer")
        apply_decision(db, DecisionInput(
            project_id="project-one", left_entity_id=entity_by_source["B"],
            right_entity_id=entity_by_source["A"], action="MERGE", reason="A belongs to B",
        ), expected_revision=distinct.revision, actor_id="reviewer", scheme=scheme)
        run_id = run.id

    queue = client.get(f"/api/v1/projects/project-one/unification/reviews?run_id={run_id}")
    assert queue.status_code == 200
    assert queue.json()["total"] == 0
