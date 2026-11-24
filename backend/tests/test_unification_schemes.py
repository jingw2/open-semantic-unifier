"""Configuration contracts for reusable unification schemes."""
import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker, joinedload

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.db import Base, get_db
from app.api.deps import get_current_user
from app.main import app
from app.models.entities import LLMProvider, Project, ProjectApiKey, Workspace
from app.models.unification import IdentityChangeRecord, IdentityConflictRecord, SourceRecord, SourceRevision, UnificationSchemeVersion
from app.services.unification.contracts import IngestMode, SourceRecordInput
from app.services.unification.ingest import ingest_batch
from app.services.unification.identity import record_identity_conflict
from app.services.unification.schemes import (
    AttributeOverride,
    EntityDefinition,
    MatchScheme,
    get_entity_definition,
    get_entity_type_catalog,
    list_schemes,
    save_entity_definition,
    save_scheme,
    select_attributes,
)
from app.demo.catalogs import MASTER_SUPPLIERS, supplier_catalog_for_sources
from app.core.security import hash_api_key
from fastapi.testclient import TestClient


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schemes.db'}")
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    session = maker()
    session.add(Workspace(id="workspace-1", name="Workspace", slug="workspace-1"))
    session.flush()
    session.add(Project(id="project-1", workspace_id="workspace-1", name="Project", slug="project-1"))
    session.commit()
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def test_three_domains_use_only_configuration():
    golden_root = Path(__file__).resolve().parents[2] / "tests" / "golden" / "v1"
    schemes = [MatchScheme.model_validate(json.loads((golden_root / domain / "scheme.json").read_text())) for domain in ("supplier", "customer", "product")]
    assert {scheme.entity_type for scheme in schemes} == {"supplier", "customer", "product"}
    assert len({scheme.identity_granularity for scheme in schemes}) == 3
    assert [scheme.strong_identifiers for scheme in schemes] == [["tax_id"], ["person_ref"], ["gtin"]]


def test_model_judging_requires_a_provider_and_explicit_prompt_version(db):
    base = {
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name"}, "model_judgment_enabled": True,
    }
    with pytest.raises(ValueError, match="model_provider_id"):
        MatchScheme.model_validate(base)
    scheme = MatchScheme.model_validate({
        **base, "model_provider_id": "provider-1", "model_failure_policy": "FAIL",
        "prompt_version": "pair-v1",
    })
    assert scheme.model_failure_policy == "FAIL"
    assert scheme.prompt_version == "pair-v1"


def test_scheme_cannot_reference_a_model_provider_from_another_workspace(db):
    db.add(Workspace(id="other-workspace", name="Other", slug="other-workspace"))
    db.flush()
    db.add(LLMProvider(
        id="foreign-provider", workspace_id="other-workspace", provider_key="ollama",
        name="Private model", base_url="http://127.0.0.1:11434/v1", default_model="test-model",
    ))
    db.commit()
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name"}, "model_judgment_enabled": True,
        "model_provider_id": "foreign-provider",
    })
    with pytest.raises(ValueError, match="provider is not available"):
        save_scheme(db, project_id="project-1", scheme_key="orgs", scheme=scheme)


def test_scheme_api_rejects_model_provider_outside_workspace_without_leaking_provider_id(db):
    db.add(Workspace(id="other-workspace", name="Other", slug="other-workspace"))
    db.flush()
    db.add(LLMProvider(
        id="foreign-provider", workspace_id="other-workspace", provider_key="ollama",
        name="Private model", base_url="http://127.0.0.1:11434/v1", default_model="test-model",
    ))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1", "workspace_id": "workspace-1", "role": "admin"}
    response = TestClient(app).put(
        "/api/v1/projects/project-1/unification/schemes/orgs",
        headers={"Authorization": "Bearer test"},
        json={
            "entity_type": "organization", "identity_granularity": "legal_entity",
            "field_map": {"name": "name"}, "model_judgment_enabled": True,
            "model_provider_id": "foreign-provider",
        },
    )
    assert response.status_code == 422
    assert "foreign-provider" not in response.text


def test_user_catalog_has_no_demo_dependency(db):
    assert {item.key for item in get_entity_type_catalog()} >= {"organization", "person", "product", "custom"}
    assert list_schemes(db, project_id="project-1") == []

    headers = {"Authorization": "Bearer test"}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1", "workspace_id": "workspace-1", "role": "admin"}
    response = TestClient(app).get("/api/v1/unification/catalog", headers=headers)
    assert response.status_code == 200
    assert "星云智造科技有限公司" not in response.text


def test_attribute_priority_and_override_keep_provenance(db):
    records = [
        SourceRecordInput("project-1", "secondary", "vendors", "s1", {"name": "Secondary name", "address": "B"}),
        SourceRecordInput("project-1", "erp", "vendors", "e1", {"name": "ERP name", "address": "A"}),
    ]
    result = ingest_batch(db, project_id="project-1", records=records, mode=IngestMode.SYNC, input_version="v1")
    db.commit()
    revision_ids = [identity.source_revision_id for identity in result.identities]
    revisions = db.scalars(select(SourceRevision).options(joinedload(SourceRevision.source_record)).where(SourceRevision.id.in_(revision_ids))).all()
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "address": "address"},
        "strong_identifiers": [], "attribute_policies": {
            "name": {"source_priority": ["erp", "secondary"], "on_conflict": "retain_provenance"},
            "other_attributes": {"on_conflict": "REVIEW"},
        },
    })
    canonical = select_attributes(revisions, scheme)
    assert canonical.values["name"] == "ERP name"
    assert {value.value for value in canonical.provenance["name"]} == {"ERP name", "Secondary name"}
    review_scheme = MatchScheme.model_validate({
        **scheme.model_dump(),
        "attribute_policies": {"name": {"source_priority": ["erp"], "on_conflict": "REVIEW"}},
    })
    review_result = select_attributes(revisions, review_scheme)
    assert "address" in canonical.requires_review
    override = AttributeOverride(field="name", value="Human choice", actor_id="user-1", reason="verified by contract")
    revised = select_attributes(revisions, scheme, overrides=[override])
    assert revised.values["name"] == "Human choice"
    assert revised.selected["name"].actor_id == "user-1"
    assert revised.selected["name"].reason == "verified by contract"


def test_unmapped_attributes_cannot_override_mapped_canonical_fields(db):
    inputs = [
        SourceRecordInput("project-1", "erp", "vendors", "e1", {"legal_name": "Official name", "name": "Alias"}),
        SourceRecordInput("project-1", "secondary", "vendors", "s1", {"legal_name": "Official name", "name": "Other alias"}),
    ]
    result = ingest_batch(db, project_id="project-1", records=inputs, mode=IngestMode.SYNC, input_version="v1")
    db.commit()
    revisions = db.scalars(select(SourceRevision).options(joinedload(SourceRevision.source_record)).where(
        SourceRevision.id.in_([identity.source_revision_id for identity in result.identities])
    )).all()
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "legal_name"}, "attribute_policies": {
            "name": {"source_priority": ["erp"], "on_conflict": "retain_provenance"},
            "other_attributes": {"on_conflict": "REVIEW"},
        },
    })
    selected = select_attributes(revisions, scheme)
    assert selected.values["name"] == "Official name"
    assert [item.value for item in selected.provenance["name"]] == ["Official name", "Official name"]
    assert "name" not in selected.requires_review


def test_invalid_field_map_is_rejected():
    with pytest.raises(ValueError):
        MatchScheme.model_validate({
            "entity_type": "organization", "identity_granularity": "legal_entity",
            "field_map": {"name": "  "}, "strong_identifiers": ["tax_id"],
        })
    with pytest.raises(ValueError):
        MatchScheme.model_validate({
            "entity_type": "organization", "identity_granularity": "legal_entity",
            "field_map": {"name": "name"}, "candidate_rules": [{"kind": "name_similarity", "field": "unknown", "threshold": 3}],
        })


def test_scheme_api_creates_immutable_revisions(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1", "workspace_id": "workspace-1", "role": "admin"}
    client = TestClient(app)
    headers = {"Authorization": "Bearer test"}
    payload = {
        "entity_type": "custom", "identity_granularity": "subscription",
        "field_map": {"account": "account_no"}, "strong_identifiers": ["account"],
        "attribute_policies": {"account": {"source_priority": ["crm"], "on_conflict": "retain_provenance"}},
    }
    first = client.put("/api/v1/projects/project-1/unification/schemes/subscriptions", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    retry = client.put("/api/v1/projects/project-1/unification/schemes/subscriptions", json=payload, headers=headers)
    assert retry.status_code == 200
    assert retry.json()["revision"] == 1
    second = client.put("/api/v1/projects/project-1/unification/schemes/subscriptions", json={**payload, "identity_granularity": "household"}, headers=headers)
    assert second.status_code == 200, second.text
    assert (first.json()["revision"], second.json()["revision"]) == (1, 2)
    history = client.get("/api/v1/projects/project-1/unification/schemes/subscriptions?revision=1", headers=headers)
    assert history.json()["identity_granularity"] == "subscription"
    listed = client.get("/api/v1/projects/project-1/unification/schemes", headers=headers)
    assert listed.json()[0]["scheme_key"] == "subscriptions"
    catalog = client.get("/api/v1/projects/project-1/unification/catalog", headers=headers)
    assert catalog.json() == []
    standard_entities = client.get("/api/v1/projects/project-1/unification/catalog/entities", headers=headers)
    assert standard_entities.json() == []
    supplier = {
        "entity_type": "organization", "canonical_id": "ORG-001", "canonical_name": "Acme Ltd",
        "aliases": ["Acme", "ACME"], "attributes": {"country": "US", "tier": "gold"},
    }
    entity_v1 = client.put("/api/v1/projects/project-1/unification/catalog/entities/acme", json=supplier, headers=headers)
    assert entity_v1.status_code == 200, entity_v1.text
    assert entity_v1.json()["revision"] == 1
    entity_retry = client.put("/api/v1/projects/project-1/unification/catalog/entities/acme", json=supplier, headers=headers)
    assert entity_retry.json()["revision"] == 1
    entity_v2 = client.put("/api/v1/projects/project-1/unification/catalog/entities/acme", json={**supplier, "attributes": {"country": "US", "tier": "platinum"}}, headers=headers)
    assert entity_v2.json()["revision"] == 2
    entity_history = client.get("/api/v1/projects/project-1/unification/catalog/entities/acme?revision=1", headers=headers)
    assert entity_history.json()["canonical_name"] == "Acme Ltd"
    assert entity_history.json()["attributes"]["tier"] == "gold"
    definition = {
        "label": "Customer account", "description": "A billing account",
        "identity_granularity": "account", "attributes": {"account_no": {"type": "string", "required": True}},
    }
    created_catalog = client.put("/api/v1/projects/project-1/unification/catalog/billing_account", json=definition, headers=headers)
    assert created_catalog.status_code == 200, created_catalog.text
    assert created_catalog.json()["revision"] == 1
    catalog_retry = client.put("/api/v1/projects/project-1/unification/catalog/billing_account", json=definition, headers=headers)
    assert catalog_retry.json()["revision"] == 1
    updated_catalog = client.put("/api/v1/projects/project-1/unification/catalog/billing_account", json={**definition, "description": "Verified account"}, headers=headers)
    assert updated_catalog.json()["revision"] == 2
    catalog_history = client.get("/api/v1/projects/project-1/unification/catalog/billing_account?revision=1", headers=headers)
    assert catalog_history.json()["description"] == "A billing account"
    invalid = client.put("/api/v1/projects/project-1/unification/schemes/invalid", json={**payload, "field_map": {}}, headers=headers)
    assert invalid.status_code == 422


def test_concurrent_identical_scheme_updates_reuse_one_revision(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheme-race.db'}", connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    with maker.begin() as session:
        session.add(Workspace(id="workspace-race", name="Workspace", slug="workspace-race"))
        session.flush()
        session.add(Project(id="project-race", workspace_id="workspace-race", name="Project", slug="project-race"))
    barrier = Barrier(2)
    thread_state = local()

    @event.listens_for(engine, "before_cursor_execute")
    def synchronize_initial_reads(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM unification_scheme_versions" in statement and getattr(thread_state, "wait_for_peer", False):
            thread_state.wait_for_peer = False
            barrier.wait(timeout=5)

    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name"},
    })

    def save_once():
        thread_state.wait_for_peer = True
        with maker() as session:
            return save_scheme(session, project_id="project-race", scheme_key="organizations", scheme=scheme).revision

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            revisions = list(workers.map(lambda _: save_once(), range(2)))
        with maker() as session:
            count = session.scalar(select(func.count()).select_from(UnificationSchemeVersion))
        assert revisions == [1, 1]
        assert count == 1
    finally:
        engine.dispose()


def test_demo_supplier_catalog_is_used_only_for_explicit_demo_sources():
    real_source = type("Source", (), {"routing_json": {}})()
    demo_source = type("Source", (), {"routing_json": {"demo_catalog_key": "supplier_master_demo_v1"}})()
    assert supplier_catalog_for_sources([real_source]) is None
    assert supplier_catalog_for_sources([demo_source]) is MASTER_SUPPLIERS


def test_serving_does_not_search_the_demo_supplier_catalog(db):
    api_key = "project-key-1"
    db.add(ProjectApiKey(
        project_id="project-1", name="test", key_prefix="project-", key_hash=hash_api_key(api_key), is_active=True,
    ))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    response = TestClient(app).post(
        "/api/v1/serve/project-1/reconcile",
        json={"query": "星云智造科技有限公司"},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    assert response.json()["matched"] is False
    assert response.json()["source"] == "NONE"


def test_match_api_returns_actual_revision_and_pair_evidence(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1", "workspace_id": "workspace-1", "role": "admin"}
    client = TestClient(app)
    headers = {"Authorization": "Bearer test"}
    scheme = {
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
        "normalizers": ["unicode_nfkc", "trim", "casefold"],
        "candidate_rules": [{"kind": "exact", "fields": ["name"]}],
    }
    saved = client.put("/api/v1/projects/project-1/unification/schemes/orgs", json=scheme, headers=headers)
    assert saved.status_code == 200
    response = client.post("/api/v1/projects/project-1/unification/match", headers=headers, json={
        "scheme_key": "orgs", "input_version": "upload-1", "records": [
            {"source_system": "erp", "dataset": "vendors", "source_record_id": "A", "attributes": {"name": "Acme", "tax_id": "T-1"}},
            {"source_system": "secondary", "dataset": "vendors", "source_record_id": "B", "attributes": {"name": "Acme", "tax_id": "T-1"}},
        ],
    })
    assert response.status_code == 200, response.text
    assert response.json()["scheme_revision"] == 1
    assert len(response.json()["identities"]) == 2
    assert response.json()["pairs"][0]["action"] == "AUTO_MATCH"
    assert "STRONG_IDENTIFIER_MATCH" in response.json()["pairs"][0]["evidence_codes"]
    identities = response.json()["identities"]
    assert identities[0]["entity_id"] == identities[1]["entity_id"]


def test_match_api_rolls_back_ingest_when_candidate_generation_fails(db, monkeypatch):
    import app.api.v1.unification as unification_api

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1", "workspace_id": "workspace-1", "role": "admin"}
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test"}
    scheme = {"entity_type": "custom", "identity_granularity": "account", "field_map": {"name": "name"}}
    assert client.put("/api/v1/projects/project-1/unification/schemes/accounts", json=scheme, headers=headers).status_code == 200

    def fail_matching(*_args, **_kwargs):
        raise RuntimeError("candidate generation failed")
    monkeypatch.setattr(unification_api, "match_records", fail_matching)
    response = client.post("/api/v1/projects/project-1/unification/match", headers=headers, json={
        "scheme_key": "accounts", "input_version": "failed-run", "records": [
            {"source_system": "crm", "dataset": "accounts", "source_record_id": "A", "attributes": {"name": "Acme"}},
        ],
    })
    assert response.status_code == 500
    db.rollback()
    assert db.query(SourceRecord).count() == 0


def test_source_update_after_merge_creates_persistent_identity_conflict(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1", "workspace_id": "workspace-1", "role": "admin"}
    client = TestClient(app)
    headers = {"Authorization": "Bearer test"}
    scheme = {
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
        "candidate_rules": [{"kind": "exact", "fields": ["name"]}],
    }
    assert client.put("/api/v1/projects/project-1/unification/schemes/orgs", json=scheme, headers=headers).status_code == 200
    records = [
        {"source_system": "erp", "dataset": "vendors", "source_record_id": "A", "attributes": {"name": "Acme", "tax_id": "T-1"}},
        {"source_system": "crm", "dataset": "vendors", "source_record_id": "B", "attributes": {"name": "Acme", "tax_id": "T-1"}},
    ]
    first = client.post("/api/v1/projects/project-1/unification/match", headers=headers, json={"scheme_key": "orgs", "input_version": "before-change", "records": records})
    assert first.status_code == 200, first.text
    assert first.json()["identities"][0]["entity_id"] == first.json()["identities"][1]["entity_id"]
    changed = [{**records[1], "attributes": {"name": "Acme renamed", "tax_id": "T-2"}}]
    second = client.post("/api/v1/projects/project-1/unification/match", headers=headers, json={"scheme_key": "orgs", "input_version": "after-change", "records": changed})
    assert second.status_code == 200, second.text
    assert second.json()["pairs"] == []
    assert len(second.json()["identity_conflict_ids"]) == 1
    conflicts = client.get("/api/v1/projects/project-1/unification/conflicts", headers=headers)
    assert conflicts.status_code == 200
    conflict_id = second.json()["identity_conflict_ids"][0]
    assert conflicts.json()[0]["conflict_id"] == conflict_id
    stored_conflict = db.get(IdentityConflictRecord, conflict_id)
    reversed_evidence = [
        ({**item, "left": item["right"], "right": item["left"]} if "left" in item and "right" in item else item)
        for item in stored_conflict.evidence
    ]
    retry_conflict = record_identity_conflict(
        db, project_id="project-1", entity_id=stored_conflict.entity_id,
        left_source_revision_id=stored_conflict.right_source_revision_id,
        right_source_revision_id=stored_conflict.left_source_revision_id,
        evidence=reversed_evidence, actor_id="user-1",
    )
    assert retry_conflict == conflict_id
    entity_id = first.json()["identities"][0]["entity_id"]
    source_record_id = db.scalar(select(SourceRecord.id).where(SourceRecord.source_record_id == "B"))
    split = client.post(f"/api/v1/projects/project-1/unification/entities/{entity_id}/split", headers=headers, json={
        "source_record_ids": [source_record_id], "reason": "verified separate registration", "expected_revision": 3,
    })
    assert split.status_code == 200, split.text
    assert client.get("/api/v1/projects/project-1/unification/conflicts?status=OPEN", headers=headers).json() == []
    undo = client.post(f"/api/v1/projects/project-1/unification/identity-changes/{split.json()['change_id']}/undo", headers=headers, json={"expected_revision": 4, "reason": "undo mistaken split"})
    assert undo.status_code == 200, undo.text
    assert db.get(IdentityChangeRecord, undo.json()["change_id"]).reason == "undo mistaken split"
    assert client.get("/api/v1/projects/project-1/unification/conflicts?status=OPEN", headers=headers).json()[0]["conflict_id"] == second.json()["identity_conflict_ids"][0]
