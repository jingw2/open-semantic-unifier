"""Immutable contract model versions, publication pointer, audit, rollback, and version-bound exports."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sys

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.contracts import ContractAuditRecord, ContractModelVersion, ContractReleaseState, ContractSource
from app.models.entities import DataSource
from app.models.unification import UnificationSnapshot
from app.services.contracts import compiler
from app.services.storage import storage
from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing
from test_unification_public_api import public_api  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"

_MISSING_COMMANDS = sorted(name for name in compiler.COMMAND_NAMES if shutil.which(name) is None)

requires_commands = pytest.mark.skipif(
    bool(_MISSING_COMMANDS),
    reason=f"compiler commands missing from PATH: {', '.join(_MISSING_COMMANDS)}",
)

VERSIONS_URL = "/api/v1/projects/{project}/contracts/versions"


def _add_data_source(maker, *, project_id, name, object_key, content):
    storage.put_bytes(object_key, content)
    with maker() as db:
        source = DataSource(project_id=project_id, name=name, source_type="file", object_key=object_key)
        db.add(source)
        db.commit()
        return source.id


def _select_source(client, project_id, data_source_id):
    return client.post(f"/api/v1/projects/{project_id}/contracts/sources", json={"data_source_id": data_source_id})


def _add_snapshot(maker, project_id, *, entity_type="Task", version=1):
    with maker() as db:
        snapshot = UnificationSnapshot(
            project_id=project_id, version=version,
            snapshot_json={"entities": {"entity-one": {"entity_type": entity_type, "status": "ACTIVE"}}},
            actor_id="publisher",
        )
        db.add(snapshot)
        db.commit()
        return snapshot.id


def _source_and_snapshot(client, maker, project_id, *, content=None, entity_type="Task"):
    content = (FIXTURES / "minimal_schema.yaml").read_bytes() if content is None else content
    data_source_id = _add_data_source(
        maker, project_id=project_id, name="minimal_schema.yaml",
        object_key=f"projects/{project_id}/uploads/minimal_schema.yaml", content=content,
    )
    selected = _select_source(client, project_id, data_source_id)
    assert selected.status_code == 201, selected.text
    snapshot_id = _add_snapshot(maker, project_id, entity_type=entity_type)
    return selected.json()["id"], snapshot_id


def _artifacts(prefix):
    return {
        "linkml_yaml": f"{prefix}-linkml",
        "sql_ddl": f"{prefix}-ddl",
        "json_schema": f"{prefix}-jsonschema",
        "er_diagram": f"{prefix}-er",
    }


def _add_version_row(maker, *, project_id, source_id, snapshot_id, version, canonical, artifacts):
    with maker() as db:
        row = ContractModelVersion(
            project_id=project_id, source_id=source_id, version=version, status="DRAFT",
            source_sha256="ab" * 32, canonical_model_json=canonical, artifacts_json=artifacts,
            compiler_versions_json={"linkml": "9.9.9"},
            validation_json={"counts": {"classes": 1, "issues": 0}, "issues": []},
            entity_snapshot_id=snapshot_id, snapshot_version=1, created_by="editor-one",
        )
        db.add(row)
        db.commit()
        return row.id


def _create_version(client, project_id, source_id, snapshot_id, allowed_new=None):
    body = {"source_id": source_id, "snapshot_id": snapshot_id}
    if allowed_new is not None:
        body["allowed_new_entity_types"] = allowed_new
    return client.post(VERSIONS_URL.format(project=project_id), json=body)


def _publish(client, project_id, version_id, expected, reason):
    return client.post(
        f"{VERSIONS_URL.format(project=project_id)}/{version_id}/publish",
        json={"expected_current_version": expected, "reason": reason},
    )


def _rollback(client, project_id, version_id, expected, reason):
    return client.post(
        f"{VERSIONS_URL.format(project=project_id)}/{version_id}/rollback",
        json={"expected_current_version": expected, "reason": reason},
    )


@requires_commands
def test_create_version_compiles_and_validates_against_snapshot(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    response = _create_version(client, "project-one", source_id, snapshot_id)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert len(payload["id"]) == 32
    assert payload["version"] == 1
    assert payload["status"] == "DRAFT"
    assert payload["source_id"] == source_id
    assert payload["source_sha256"] == hashlib.sha256((FIXTURES / "minimal_schema.yaml").read_bytes()).hexdigest()
    assert payload["entity_snapshot_id"] == snapshot_id
    assert payload["snapshot_version"] == 1
    assert payload["created_by"] == "editor-one"
    datetime.fromisoformat(payload["created_at"])
    assert payload["counts"]["classes"] == 1
    assert payload["counts"]["attributes"] == 3
    assert payload["counts"]["issues"] == 0
    assert set(payload["compiler_versions"]) == {
        "linkml", "schemasheets", "sheets2linkml", "gen-sqlddl", "gen-json-schema", "gen-erdiagram",
    }
    with maker() as db:
        row = db.query(ContractModelVersion).one()
        assert row.status == "DRAFT"
        assert row.canonical_model_json.startswith("{")
        assert row.artifacts_json["sql_ddl"]
        assert row.artifacts_json["json_schema"]
        assert row.artifacts_json["er_diagram"]


@requires_commands
def test_unknown_entity_type_returns_422_and_writes_no_version(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one", entity_type="Other")
    response = _create_version(client, "project-one", source_id, snapshot_id)
    assert response.status_code == 422
    assert any(item["code"] == "UNKNOWN_ENTITY_TYPE" for item in response.json()["detail"])
    with maker() as db:
        assert db.query(ContractModelVersion).count() == 0


@requires_commands
def test_allowed_new_entity_types_permit_unlisted_classes(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one", entity_type="Other")
    response = _create_version(client, "project-one", source_id, snapshot_id, allowed_new=["Task"])
    assert response.status_code == 201, response.text
    assert response.json()["version"] == 1


def test_source_hash_mismatch_returns_409_and_writes_no_version(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    with maker() as db:
        data_source = db.query(DataSource).filter(DataSource.project_id == "project-one").one()
        object_key = data_source.object_key
    storage.put_bytes(object_key, b"changed: true\n")
    response = _create_version(client, "project-one", source_id, snapshot_id)
    assert response.status_code == 409
    with maker() as db:
        assert db.query(ContractModelVersion).count() == 0


def test_missing_or_foreign_source_or_snapshot_is_404(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    foreign_snapshot_id = _add_snapshot(maker, "project-two")
    with maker() as db:
        data_source = DataSource(
            project_id="project-two", name="foreign.yaml", source_type="file",
            object_key="projects/project-two/uploads/foreign.yaml",
        )
        db.add(data_source)
        db.flush()
        source = ContractSource(
            project_id="project-two", data_source_id=data_source.id, filename="foreign.yaml",
            format="linkml_yaml", sha256="cd" * 32, size_bytes=4, created_by="editor-two",
        )
        db.add(source)
        db.commit()
        foreign_source_id = source.id
    assert _create_version(client, "project-one", "no-such-source", snapshot_id).status_code == 404
    assert _create_version(client, "project-one", source_id, "no-such-snapshot").status_code == 404
    assert _create_version(client, "project-one", source_id, foreign_snapshot_id).status_code == 404
    assert _create_version(client, "project-one", foreign_source_id, snapshot_id).status_code == 404
    with maker() as db:
        assert db.query(ContractModelVersion).count() == 0


def test_publish_moves_pointer_writes_audit_and_is_idempotent(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    first = _publish(client, "project-one", v1_id, None, "initial publication")
    assert first.status_code == 201, first.text
    assert first.json() == {
        "project_id": "project-one", "current_version": 1,
        "current_version_id": v1_id, "status": "PUBLISHED",
    }
    with maker() as db:
        assert db.get(ContractModelVersion, v1_id).status == "PUBLISHED"
        audit = db.query(ContractAuditRecord).one()
        assert audit.action == "PUBLISH"
        assert audit.version_id == v1_id
        assert audit.actor_id == "editor-one"
        assert audit.reason == "initial publication"
        assert audit.from_version is None
        assert audit.to_version == 1
    repeated = _publish(client, "project-one", v1_id, 1, "republish")
    assert repeated.status_code == 201, repeated.text
    with maker() as db:
        assert db.query(ContractAuditRecord).count() == 1

    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    second = _publish(client, "project-one", v2_id, 1, "second publication")
    assert second.status_code == 201, second.text
    assert second.json()["current_version"] == 2
    with maker() as db:
        assert db.get(ContractModelVersion, v1_id).status == "ARCHIVED"
        assert db.get(ContractModelVersion, v2_id).status == "PUBLISHED"
        state = db.get(ContractReleaseState, "project-one")
        assert state.current_version == 2
        assert state.current_version_id == v2_id

    v3_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=3, canonical='{"v":3}', artifacts=_artifacts("v3"))
    stale = _publish(client, "project-one", v3_id, 0, "stale expectation")
    assert stale.status_code == 409
    with maker() as db:
        state = db.get(ContractReleaseState, "project-one")
        assert state.current_version == 2
        assert state.current_version_id == v2_id
        assert db.get(ContractModelVersion, v3_id).status == "DRAFT"


def test_rollback_moves_pointer_to_a_historical_version_and_is_idempotent(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201
    assert _publish(client, "project-one", v2_id, 1, "second").status_code == 201

    rollback = _rollback(client, "project-one", v1_id, 2, "v2 was wrong")
    assert rollback.status_code == 201, rollback.text
    assert rollback.json() == {
        "project_id": "project-one", "current_version": 1,
        "current_version_id": v1_id, "status": "PUBLISHED",
    }
    with maker() as db:
        assert db.get(ContractModelVersion, v1_id).status == "PUBLISHED"
        assert db.get(ContractModelVersion, v2_id).status == "ARCHIVED"
        state = db.get(ContractReleaseState, "project-one")
        assert state.current_version == 1
        assert state.current_version_id == v1_id
        audits = db.query(ContractAuditRecord).filter_by(action="ROLLBACK").all()
        assert len(audits) == 1
        assert audits[0].version_id == v1_id
        assert audits[0].from_version == 2
        assert audits[0].to_version == 1
        assert audits[0].reason == "v2 was wrong"

    repeated = _rollback(client, "project-one", v1_id, 1, "already current")
    assert repeated.status_code == 201, repeated.text
    with maker() as db:
        assert db.query(ContractAuditRecord).filter_by(action="ROLLBACK").count() == 1


def test_rollback_rejects_stale_expectations_and_draft_targets(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201

    stale = _rollback(client, "project-one", v2_id, 0, "stale expectation")
    assert stale.status_code == 409
    draft = _rollback(client, "project-one", v2_id, 1, "draft target")
    assert draft.status_code == 409
    with maker() as db:
        assert db.get(ContractReleaseState, "project-one").current_version == 1
        assert db.get(ContractModelVersion, v2_id).status == "DRAFT"
        assert db.query(ContractAuditRecord).filter_by(action="ROLLBACK").count() == 0


def test_historical_version_content_is_unchanged_by_publish_and_rollback(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_artifacts = _artifacts("v1")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1,"model":"original"}', artifacts=v1_artifacts)
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201
    assert _publish(client, "project-one", v2_id, 1, "second").status_code == 201
    assert _rollback(client, "project-one", v1_id, 2, "back to v1").status_code == 201

    with maker() as db:
        row = db.get(ContractModelVersion, v1_id)
        assert row.version == 1
        assert row.canonical_model_json == '{"v":1,"model":"original"}'
        assert row.artifacts_json == v1_artifacts
        assert row.source_sha256 == "ab" * 32
        assert row.compiler_versions_json == {"linkml": "9.9.9"}
        assert row.entity_snapshot_id == snapshot_id
        assert row.snapshot_version == 1
    export = client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=model")
    assert export.status_code == 200, export.text
    assert export.text == '{"v":1,"model":"original"}'


def test_exports_are_bound_to_the_requested_version(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v2_id, None, "v2 is current").status_code == 201

    model = client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=model")
    assert model.status_code == 200, model.text
    assert model.text == '{"v":1}'
    assert model.headers["content-type"].startswith("application/json")
    json_schema = client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=json_schema")
    assert json_schema.status_code == 200, json_schema.text
    assert json_schema.text == "v1-jsonschema"
    assert json_schema.headers["content-type"].startswith("application/json")
    sql_ddl = client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=sql_ddl")
    assert sql_ddl.status_code == 200, sql_ddl.text
    assert sql_ddl.text == "v1-ddl"
    assert sql_ddl.headers["content-type"].startswith("text/plain")
    er = client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=er_diagram")
    assert er.status_code == 200, er.text
    assert er.text == "v1-er"
    assert er.headers["content-type"].startswith("text/plain")

    unknown = client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=diagram")
    assert unknown.status_code == 422
    foreign = client.get(f"{VERSIONS_URL.format(project='project-two')}/{v1_id}/export?kind=model")
    assert foreign.status_code == 404
    missing = client.get(f"{VERSIONS_URL.format(project='project-one')}/no-such-version/export?kind=model")
    assert missing.status_code == 404


def test_version_listing_is_paginated_newest_first_and_artifact_free(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    ids = [
        _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                         version=number, canonical=f'{{"v":{number}}}', artifacts=_artifacts(f"v{number}"))
        for number in (1, 2, 3)
    ]
    listing = client.get(f"{VERSIONS_URL.format(project='project-one')}?limit=2&offset=0")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == [ids[2], ids[1]]
    for item in body["items"]:
        assert "canonical_model_json" not in item
        assert "artifacts_json" not in item
    page = client.get(f"{VERSIONS_URL.format(project='project-one')}?limit=2&offset=2")
    assert [item["id"] for item in page.json()["items"]] == [ids[0]]

    detail = client.get(f"{VERSIONS_URL.format(project='project-one')}/{ids[1]}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["version"] == 2
    assert payload["status"] == "DRAFT"
    assert payload["counts"] == {"classes": 1, "issues": 0}
    assert payload["compiler_versions"] == {"linkml": "9.9.9"}
    assert "canonical_model_json" not in payload
    assert "artifacts_json" not in payload


def test_empty_version_list_reports_zero_total(public_api):
    client, _maker, _identity = public_api
    listing = client.get(f"{VERSIONS_URL.format(project='project-one')}?limit=10")
    assert listing.status_code == 200
    assert listing.json() == {"items": [], "limit": 10, "offset": 0, "total": 0}


def test_version_lists_are_project_scoped(public_api):
    client, maker, identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    identity["workspace_id"] = "ws-two"
    foreign_source_id, foreign_snapshot_id = _source_and_snapshot(client, maker, "project-two")
    foreign_version_id = _add_version_row(maker, project_id="project-two", source_id=foreign_source_id,
                                          snapshot_id=foreign_snapshot_id, version=1,
                                          canonical='{"v":99}', artifacts=_artifacts("v99"))
    listing = client.get(f"{VERSIONS_URL.format(project='project-two')}?limit=10")
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()["items"]] == [foreign_version_id]
    assert client.get(f"{VERSIONS_URL.format(project='project-one')}?limit=10").status_code == 404
    identity["workspace_id"] = "ws-one"
    listing = client.get(f"{VERSIONS_URL.format(project='project-one')}?limit=10")
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()["items"]] == [v1_id]


def test_release_endpoint_reports_the_pointer_or_an_empty_state(public_api):
    client, maker, _identity = public_api
    empty = client.get("/api/v1/projects/project-one/contracts/release")
    assert empty.status_code == 200
    assert empty.json() == {"project_id": "project-one", "current_version": 0, "current_version_id": None}

    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201
    release = client.get("/api/v1/projects/project-one/contracts/release")
    assert release.status_code == 200
    assert release.json() == {"project_id": "project-one", "current_version": 1, "current_version_id": v1_id}


def test_audit_endpoint_lists_history_newest_first_with_pagination(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201
    assert _publish(client, "project-one", v2_id, 1, "second").status_code == 201
    assert _rollback(client, "project-one", v1_id, 2, "back").status_code == 201
    with maker() as db:
        publishes = db.query(ContractAuditRecord).filter_by(action="PUBLISH").order_by(ContractAuditRecord.to_version).all()
        rollback = db.query(ContractAuditRecord).filter_by(action="ROLLBACK").one()
        publishes[0].created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        publishes[1].created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        rollback.created_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
        db.commit()

    audit = client.get("/api/v1/projects/project-one/contracts/audit?limit=10")
    assert audit.status_code == 200, audit.text
    body = audit.json()
    assert body["total"] == 3
    items = body["items"]
    assert [item["action"] for item in items] == ["ROLLBACK", "PUBLISH", "PUBLISH"]
    assert items[0]["from_version"] == 2 and items[0]["to_version"] == 1
    assert items[0]["version_id"] == v1_id
    assert items[0]["reason"] == "back"
    assert items[0]["actor_id"] == "editor-one"
    assert "trace_id" in items[0]
    assert items[1]["from_version"] == 1 and items[1]["to_version"] == 2
    assert items[2]["from_version"] is None and items[2]["to_version"] == 1
    paged = client.get("/api/v1/projects/project-one/contracts/audit?limit=2&offset=1")
    assert paged.status_code == 200
    assert paged.json()["total"] == 3
    assert [item["action"] for item in paged.json()["items"]] == ["PUBLISH", "PUBLISH"]


def test_viewer_cannot_mutate_but_can_read(public_api):
    client, maker, identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    identity["role"] = "viewer"
    assert _create_version(client, "project-one", source_id, snapshot_id).status_code == 403
    assert _publish(client, "project-one", v1_id, None, "no").status_code == 403
    assert _rollback(client, "project-one", v1_id, 0, "no").status_code == 403
    assert client.get(f"{VERSIONS_URL.format(project='project-one')}").status_code == 200
    assert client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}").status_code == 200
    assert client.get(f"{VERSIONS_URL.format(project='project-one')}/{v1_id}/export?kind=model").status_code == 200
    assert client.get("/api/v1/projects/project-one/contracts/release").status_code == 200
    assert client.get("/api/v1/projects/project-one/contracts/audit").status_code == 200
    assert client.get(f"{VERSIONS_URL.format(project='project-two')}").status_code == 404
    with maker() as db:
        assert db.query(ContractAuditRecord).count() == 0
        assert db.get(ContractReleaseState, "project-one") is None


def test_publish_with_null_expected_after_current_is_rejected(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201

    response = _publish(client, "project-one", v2_id, None, "null must mean no current version")
    assert response.status_code == 409
    with maker() as db:
        state = db.get(ContractReleaseState, "project-one")
        assert state.current_version == 1
        assert state.current_version_id == v1_id
        assert db.get(ContractModelVersion, v2_id).status == "DRAFT"
        assert db.query(ContractAuditRecord).count() == 1


def test_publish_rejects_archived_version(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    v2_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=2, canonical='{"v":2}', artifacts=_artifacts("v2"))
    assert _publish(client, "project-one", v1_id, None, "first").status_code == 201
    assert _publish(client, "project-one", v2_id, 1, "second").status_code == 201

    response = _publish(client, "project-one", v1_id, 2, "republish archived")
    assert response.status_code == 409
    with maker() as db:
        state = db.get(ContractReleaseState, "project-one")
        assert state.current_version == 2
        assert db.get(ContractModelVersion, v1_id).status == "ARCHIVED"
        assert db.query(ContractAuditRecord).count() == 2


def test_publish_audit_records_trace_when_tracing_enabled(public_api):
    client, maker, _identity = public_api
    source_id, snapshot_id = _source_and_snapshot(client, maker, "project-one")
    v1_id = _add_version_row(maker, project_id="project-one", source_id=source_id, snapshot_id=snapshot_id,
                             version=1, canonical='{"v":1}', artifacts=_artifacts("v1"))
    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    try:
        response = _publish(client, "project-one", v1_id, None, "traced publication")
        assert response.status_code == 201, response.text
        with maker() as db:
            audit = db.query(ContractAuditRecord).one()
            assert audit.trace_id is not None
            assert len(audit.trace_id) == 32
        assert provider.force_flush()
        assert any(span.name == "contracts.publish_version" for span in exporter.get_finished_spans())
    finally:
        shutdown_tracing()
