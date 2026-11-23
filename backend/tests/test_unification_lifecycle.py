"""Golden lifecycle contracts exercised through the real project API."""
from pathlib import Path
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Event
from threading import Thread
import pytest

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.unification import SourceRecord, SourceRevision, UnifiedEntity
from test_unification_public_api import public_api


def test_l18_serving_reads_complete_snapshots_across_publication(public_api, monkeypatch):
    import app.api.v1.unification as api
    import app.services.unification.releases as releases
    from app.api.deps import verify_serving_api_key
    from app.main import app
    from app.models.entities import Project
    from app.models.unification import UnificationSnapshot

    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"

    def run_inline(run_id):
        with maker() as db:
            releases.execute_unification_run(db, run_id)

    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    assert client.put(prefix + "/schemes/supplier", json={"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name"}}).status_code == 200

    def submit(version, records):
        batch = client.post(prefix + "/batches", json={"input_version": version, "records": records})
        assert batch.status_code == 201, batch.text
        run = client.post(prefix + "/runs", headers={"Idempotency-Key": version}, json={"scheme_key": "supplier", "input_version": version})
        assert run.status_code == 202
        return run.json()["run_id"], batch.json()["identities"]

    def record(source_id, name):
        return {"source_system": "erp", "dataset": "supplier", "source_record_id": source_id, "attributes": {"name": name}}

    first_run, first_identities = submit("atomic-v1", [record("1", "Old")])
    first = client.post(prefix + "/snapshots", json={"run_id": first_run, "expected_current_version": 0})
    assert first.status_code == 201
    old_id = first.json()["snapshot_id"]
    entity_one = first_identities[0]["entity_id"]
    old_revisions = {item["source_key"][-1]: item["source_revision_id"] for item in first_identities}
    next_run, next_identities = submit("atomic-v2", [record("1", "New"), record("2", "Second")])
    entity_two = next(item["entity_id"] for item in next_identities if item["source_record_id"] != first_identities[0]["source_record_id"])
    new_revisions = {item["source_key"][-1]: item["source_revision_id"] for item in next_identities}
    assert entity_one != entity_two
    with maker() as db:
        project = db.get(Project, "project-one")
    app.dependency_overrides[verify_serving_api_key] = lambda: project
    original_pointer = releases._publish_pointer

    def abort_pointer(db, *args, **kwargs):
        original_pointer(db, *args, **kwargs)
        db.flush()
        raise releases.RunConflict("injected publication failure")

    monkeypatch.setattr(releases, "_publish_pointer", abort_pointer)
    rejected = client.post(prefix + "/snapshots", json={"run_id": next_run, "expected_current_version": 1})
    assert rejected.status_code == 409
    assert client.get(prefix + "/snapshots").json()["current_snapshot_id"] == old_id
    with maker() as db:
        snapshots = db.scalars(select(UnificationSnapshot)).all()
        assert [(snapshot.id, snapshot.version) for snapshot in snapshots] == [(old_id, 1)]

    entered, proceed, saw_old, saw_new = Event(), Event(), Event(), Event()
    def pause_pointer(db, *args, **kwargs):
        original_pointer(db, *args, **kwargs)
        db.flush()
        entered.set()
        assert proceed.wait(timeout=10)

    monkeypatch.setattr(releases, "_publish_pointer", pause_pointer)

    def read_until_new():
        observed = []
        deadline = time.monotonic() + 15
        while not saw_new.is_set() and time.monotonic() < deadline:
            current = client.get(prefix + "/snapshots").json()["current_snapshot_id"]
            results = []
            for source_id in ("1", "2"):
                response = client.post("/api/v1/serve/project-one/unification/resolve", json={"snapshot_id": current, "source_system": "erp", "dataset": "supplier", "source_record_id": source_id})
                assert response.status_code == 200, response.text
                results.append(response.json())
            exported = client.get(prefix + "/snapshots/" + current + "/export?kind=entities")
            assert exported.status_code == 200
            mappings = client.get(prefix + "/snapshots/" + current + "/export?kind=mappings")
            assert mappings.status_code == 200
            observed.append((current, results, exported.json(), mappings.json()))
            if current == old_id:
                saw_old.set()
            else:
                saw_new.set()
        assert saw_new.is_set(), "reader did not observe the new snapshot"
        return observed

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(read_until_new)
        assert saw_old.wait(timeout=10)
        future = executor.submit(lambda: client.post(prefix + "/snapshots", json={"run_id": next_run, "expected_current_version": 1}))
        try:
            assert entered.wait(timeout=10)
            # Start this entire read after the pointer flush, while commit is blocked.
            current = client.get(prefix + "/snapshots").json()["current_snapshot_id"]
            assert current == old_id
            paused_results = []
            for source_id in ("1", "2"):
                result = client.post("/api/v1/serve/project-one/unification/resolve", json={"snapshot_id": current, "source_system": "erp", "dataset": "supplier", "source_record_id": source_id})
                assert result.status_code == 200
                paused_results.append(result.json())
            paused_entities = client.get(prefix + "/snapshots/" + current + "/export?kind=entities")
            paused_mappings = client.get(prefix + "/snapshots/" + current + "/export?kind=mappings")
            assert paused_entities.status_code == paused_mappings.status_code == 200
        finally:
            proceed.set()
        response = future.result(timeout=10)
        observed = reader.result(timeout=20)
        observed.append((current, paused_results, paused_entities.json(), paused_mappings.json()))
    assert response.status_code == 201, response.text
    new_id = response.json()["snapshot_id"]
    assert {item[0] for item in observed} == {old_id, new_id}
    for snapshot_id, results, entities, mappings in observed:
        version = 1 if snapshot_id == old_id else 2
        expected = {"1": (entity_one, "Old" if version == 1 else "New")}
        if version == 2:
            expected["2"] = (entity_two, "Second")
        assert entities["snapshot_id"] == mappings["snapshot_id"] == snapshot_id
        assert entities["version"] == mappings["version"] == version
        revisions = old_revisions if version == 1 else new_revisions
        assert {row["source_key"][-1]: row["source_revision_id"] for row in mappings["mappings"]} == revisions
        assert {member["source_key"][-1]: member["source_revision_id"] for row in entities["entities"] for member in row["members"]} == revisions
        assert {row["source_key"][-1]: row["entity_id"] for row in mappings["mappings"]} == {key: value[0] for key, value in expected.items()}
        assert {member["source_key"][-1]: row["entity_id"] for row in entities["entities"] for member in row["members"]} == {key: value[0] for key, value in expected.items()}
        assert {row["entity_id"]: row["attributes"]["name"] for row in entities["entities"]} == {entity: name for entity, name in expected.values()}
        for source_id, result in zip(("1", "2"), results):
            assert (result["snapshot_id"], result["version"]) == (snapshot_id, version)
            assert result["matched"] == (source_id in expected)
            if source_id in expected:
                entity, name = expected[source_id]
                assert result["entity_id"] == entity and result["attributes"]["name"] == name
            else:
                assert result["entity_id"] is result["entity"] is result["attributes"] is None


def test_l22_rollback_serving_and_audit_preserve_history(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.api.deps import verify_serving_api_key
    from app.main import app
    from app.models.entities import Project
    from app.services.unification.releases import execute_unification_run
    client, maker, identity = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    assert client.put(prefix + "/schemes/supplier", json={"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name"}}).status_code == 200
    def publish(version, name, current):
        record = {"source_system": "erp", "dataset": "supplier", "source_record_id": "1", "attributes": {"name": name}}
        assert client.post(prefix + "/batches", json={"input_version": version, "records": [record]}).status_code == 201
        run = client.post(prefix + "/runs", headers={"Idempotency-Key": version}, json={"scheme_key": "supplier", "input_version": version})
        assert run.status_code == 202
        snapshot = client.post(prefix + "/snapshots", json={"run_id": run.json()["run_id"], "expected_current_version": current})
        assert snapshot.status_code == 201
        return snapshot.json()["snapshot_id"]
    v1 = publish("rollback-v1", "Original", 0)
    v2 = publish("rollback-v2", "Updated", 1)
    with maker() as db:
        project = db.get(Project, "project-one")
    app.dependency_overrides[verify_serving_api_key] = lambda: project
    def serve(snapshot_id):
        response = client.post("/api/v1/serve/project-one/unification/resolve", json={"snapshot_id": snapshot_id, "source_system": "erp", "dataset": "supplier", "source_record_id": "1"})
        assert response.status_code == 200
        return response.json()
    old, newer = serve(v1), serve(v2)
    assert old["attributes"]["name"] == "Original" and newer["attributes"]["name"] == "Updated"
    assert client.post(prefix + "/snapshots/" + v1 + "/rollback", json={"reason": ""}).status_code == 422
    assert client.get(prefix + "/snapshots").json()["current_snapshot_id"] == v2
    rollback = client.post(prefix + "/snapshots/" + v1 + "/rollback", json={"reason": "Restore verified version"})
    assert rollback.status_code == 201, rollback.text
    current = client.get(prefix + "/snapshots").json()["current_snapshot_id"]
    assert current == rollback.json()["snapshot_id"]
    assert serve(current)["attributes"] == old["attributes"]
    assert serve(current)["entity_id"] == old["entity_id"] and serve(current)["entity"] == old["entity"]
    old_mapping = client.get(prefix + "/snapshots/" + v1 + "/export?kind=mappings").json()["mappings"]
    restored_mapping = client.get(prefix + "/snapshots/" + current + "/export?kind=mappings").json()["mappings"]
    assert restored_mapping == old_mapping
    assert serve(v2) == newer
    audit = client.get(prefix + "/identity-changes").json()["items"]
    assert any(row["change_type"] == "ROLLBACK" and row["actor_id"] == identity["user_id"] and row["reason"] == "Restore verified version" and row["created_at"] and row["payload"]["from_snapshot_id"] == v2 and row["payload"]["target_snapshot_id"] == v1 and row["payload"]["new_snapshot_id"] == current and row["payload"]["from_version"] == 2 and row["payload"]["target_version"] == 1 and row["payload"]["new_version"] == 3 for row in audit)


def test_l17_concurrent_reviews_one_wins_and_audit_is_durable(public_api, monkeypatch, request):
    import app.api.v1.unification as api
    from fastapi import Request
    from app.api.deps import get_current_user
    from app.main import app
    from app.services.unification.releases import execute_unification_run
    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    client, maker, identity = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    root = Path(__file__).resolve().parents[2] / "tests/golden/v1/supplier"
    assert client.put(prefix + "/schemes/supplier", json=json.loads((root / "scheme.json").read_text())).status_code == 200
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text()) if row["record_ref"] in {"supplier:erp:02", "supplier:secondary:02"}]
    batch = client.post(prefix + "/batches", json={"input_version": "concurrent", "records": records})
    assert batch.status_code == 201
    run = client.post(prefix + "/runs", headers={"Idempotency-Key": "concurrent"}, json={"scheme_key": "supplier", "input_version": "concurrent"})
    assert run.status_code == 202
    run_id = run.json()["run_id"]
    pair = client.get(prefix + "/reviews", params={"run_id": run_id}).json()["items"][0]
    def user_from_header(request: Request):
        return {**identity, "user_id": request.headers.get("X-Test-Actor", "editor-one")}
    app.dependency_overrides[get_current_user] = user_from_header
    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    request.addfinalizer(shutdown_tracing)
    barrier = Barrier(2)
    def review(item):
        action, actor = item
        barrier.wait(timeout=10)
        return actor, client.post(prefix + "/reviews", headers={"X-Test-Actor": actor}, json={"run_id": run_id, "left_source_revision_id": pair["left_source_revision_id"], "right_source_revision_id": pair["right_source_revision_id"], "action": action, "reason": "Concurrent decision", "expected_revision": 1})
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(review, [("MERGE", "reviewer-a"), ("KEEP_DISTINCT", "reviewer-b")]))
    assert sorted(response.status_code for _, response in responses) == [201, 409]
    winner_actor, winner_response = next((actor, response) for actor, response in responses if response.status_code == 201)
    loser_response = next(response for _, response in responses if response.status_code == 409)
    assert any(word in loser_response.json()["detail"] for word in ("stale", "distinct", "revision"))
    winner = winner_response.json()
    expected_action = {"reviewer-a": "MERGE", "reviewer-b": "KEEP_DISTINCT"}[winner_actor]
    assert winner["change_type"] == ("MERGE" if expected_action == "MERGE" else "DECISION")
    audit = client.get(prefix + "/identity-changes").json()["items"]
    assert len(audit) == 1 and audit[0]["change_id"] == winner["change_id"] and audit[0]["revision"] == 2
    assert audit[0]["actor_id"] == winner_actor and audit[0]["created_at"] and audit[0]["reason"] == "Concurrent decision"
    with maker() as db:
        owners = [db.get(SourceRecord, row["source_record_id"]).current_entity_id for row in batch.json()["identities"]]
        assert (owners[0] == owners[1]) == (expected_action == "MERGE")
        from app.models.unification import IdentityDecisionRecord
        decision = db.scalar(select(IdentityDecisionRecord).where(IdentityDecisionRecord.change_id == winner["change_id"]))
        assert decision.action == expected_action
        assert {decision.left_entity_id, decision.right_entity_id} == {row["entity_id"] for row in batch.json()["identities"]}
    assert provider.force_flush()
    spans = [span for span in exporter.get_finished_spans() if span.name == "unification.api_review_decision"]
    assert {span.attributes["actor_id"] for span in spans} == {"reviewer-a", "reviewer-b"}
    assert len({span.context.trace_id for span in spans}) == 2
    assert sum(span.status.status_code.name == "ERROR" for span in spans) == 1


class SameModelHandler(BaseHTTPRequestHandler):
    calls = 0
    def do_POST(self):
        type(self).calls += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        content = json.dumps({"is_same_entity": True, "confidence": 1.0, "reason": "suggest same"})
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "missing_credentials"])
@pytest.mark.parametrize("policy", ["REVIEW", "FAIL"])
def test_l12_model_failures_follow_configured_policy(public_api, monkeypatch, failure, policy):
    import app.api.v1.unification as api
    from app.core.config import settings
    from app.services.unification.releases import ModelFailure, execute_unification_run

    class FailureHandler(BaseHTTPRequestHandler):
        calls = 0
        def do_POST(self):
            type(self).calls += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            if failure == "timeout":
                time.sleep(0.3)
            body = json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    try:
        monkeypatch.setattr(settings, "llm_timeout_seconds", 0.05 if failure == "timeout" else 2.0)
        def run_inline(run_id):
            with maker() as db:
                try:
                    execute_unification_run(db, run_id)
                except ModelFailure:
                    pass
        monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
        provider = client.post("/api/v1/providers", json={"provider_key": "controlled", "name": "Controlled model", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "default_model": "controlled", "api_key": "" if failure == "missing_credentials" else "test-only"})
        assert provider.status_code == 200
        root = Path(__file__).resolve().parents[2] / "tests/golden/v1/customer"
        scheme = json.loads((root / "scheme.json").read_text())
        scheme.update(model_judgment_enabled=True, model_provider_id=provider.json()["id"], model_failure_policy=policy)
        assert client.put(prefix + "/schemes/customer", json=scheme).status_code == 200
        records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text()) if row["record_ref"] in {"customer:erp:04", "customer:secondary:04"}]
        batch = client.post(prefix + "/batches", json={"input_version": "model-failure", "records": records})
        assert batch.status_code == 201
        response = client.post(prefix + "/runs", headers={"Idempotency-Key": "model-failure"}, json={"scheme_key": "customer", "input_version": "model-failure"})
        assert response.status_code == 202
        run = client.get(prefix + "/runs/" + response.json()["run_id"]).json()
        assert run["attempt_count"] == 1
        if policy == "REVIEW":
            assert run["status"] == "SUCCEEDED"
            pair = run["result"]["pairs"][0]
            assert pair["action"] == "REVIEW" and pair["recommendation"] is None
            assert failure in pair["model_error"]
            reviews = client.get(prefix + "/reviews", params={"run_id": run["run_id"]}).json()["items"]
            revisions = {row["source_revision_id"] for row in batch.json()["identities"]}
            assert any({row["left_source_revision_id"], row["right_source_revision_id"]} == revisions for row in reviews)
            entity_ids = {row["entity_id"] for row in batch.json()["identities"]}
            from app.models.unification import IdentityDecisionRecord
            with maker() as db:
                assert len(entity_ids) == 2 and all(db.get(UnifiedEntity, entity_id).status == "ACTIVE" for entity_id in entity_ids)
                assert db.scalar(select(func.count()).select_from(IdentityDecisionRecord)) == 0
        else:
            assert run["status"] == "FAILED" and failure in run["error_message"]
            assert run["result"] is None
        assert FailureHandler.calls == (0 if failure == "missing_credentials" else 1)
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_l19_product_spec_veto_overrides_real_model_suggestion(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.releases import execute_unification_run
    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    SameModelHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), SameModelHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    exporter = InMemorySpanExporter()
    trace_provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    try:
        def run_inline(run_id):
            with maker() as db:
                execute_unification_run(db, run_id)
        monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
        provider = client.post("/api/v1/providers", json={"provider_key": "controlled", "name": "Controlled model", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "default_model": "controlled", "api_key": "test-only"})
        assert provider.status_code == 200, provider.text
        root = Path(__file__).resolve().parents[2] / "tests/golden/v1/product"
        scheme = json.loads((root / "scheme.json").read_text())
        scheme.update(model_judgment_enabled=True, model_provider_id=provider.json()["id"])
        assert client.put(prefix + "/schemes/product", json=scheme).status_code == 200
        records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text()) if row["record_ref"] in {"product:erp:07", "product:secondary:07"}]
        assert len(records) == 2
        batch = client.post(prefix + "/batches", json={"input_version": "product-veto", "records": records})
        assert batch.status_code == 201
        run = client.post(prefix + "/runs", headers={"Idempotency-Key": "veto"}, json={"scheme_key": "product", "input_version": "product-veto"})
        assert run.status_code == 202
        result = client.get(prefix + "/runs/" + run.json()["run_id"]).json()
        assert result["status"] == "SUCCEEDED"
        pair = result["result"]["pairs"][0]
        assert pair["action"] == "DISTINCT" and pair["recommendation"]["is_same_entity"] is True
        assert pair["recommendation"]["confidence"] == 1.0 and pair["recommendation"]["model"] == "controlled"
        assert "CONFLICT_CONSTRAINT" in pair["evidence_codes"] and SameModelHandler.calls == 1
        conflict = next(item for item in pair["evidence"] if item.get("field") == "voltage" and item.get("rule") == "veto_auto_merge")
        assert {conflict["left"], conflict["right"]} == {"110V", "220V"}
        assert trace_provider.force_flush()
        assert any(span.name == "unification.model_judge" for span in exporter.get_finished_spans())
        snapshot = client.post(prefix + "/snapshots", json={"run_id": run.json()["run_id"], "expected_current_version": 0})
        assert snapshot.status_code == 201
        snapshot_id = snapshot.json()["snapshot_id"]
        mappings = client.get(prefix + "/snapshots/" + snapshot_id + "/export?kind=mappings").json()["mappings"]
        assert len(mappings) == 2 and len({row["entity_id"] for row in mappings}) == 2
        assert {tuple(row["source_key"]): row["entity_id"] for row in mappings} == {tuple(row["source_key"]): row["entity_id"] for row in batch.json()["identities"]}
        exporter.clear()
        assert client.get(prefix + "/runs/" + run.json()["run_id"]).json()["result"]["pairs"][0] == pair
    finally:
        shutdown_tracing()
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_l05_rejection_survives_model_same_suggestion_and_rerun(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.releases import execute_unification_run

    SameModelHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), SameModelHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client, maker, identity = public_api
    prefix = "/api/v1/projects/project-one/unification"
    try:
        def run_inline(run_id):
            with maker() as db:
                execute_unification_run(db, run_id)
        monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
        provider = client.post("/api/v1/providers", json={"provider_key": "controlled", "name": "Controlled model", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "default_model": "controlled", "api_key": "test-only"})
        assert provider.status_code == 200, provider.text
        root = Path(__file__).resolve().parents[2] / "tests/golden/v1/customer"
        scheme = json.loads((root / "scheme.json").read_text())
        scheme.update(model_judgment_enabled=True, model_provider_id=provider.json()["id"])
        assert client.put(prefix + "/schemes/customer", json=scheme).status_code == 200
        records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text()) if row["record_ref"] in {"customer:erp:04", "customer:secondary:04"}]
        assert len(records) == 2
        batch = client.post(prefix + "/batches", json={"input_version": "customer-reject", "records": records})
        assert batch.status_code == 201, batch.text
        def run(key):
            response = client.post(prefix + "/runs", headers={"Idempotency-Key": key}, json={"scheme_key": "customer", "input_version": "customer-reject"})
            assert response.status_code == 202, response.text
            result = client.get(prefix + "/runs/" + response.json()["run_id"]).json()
            assert result["status"] == "SUCCEEDED", result
            return result
        first = run("reject-first")
        pairs = client.get(prefix + "/reviews", params={"run_id": first["run_id"]}).json()["items"]
        assert len(pairs) == 1
        pair = pairs[0]
        decision = client.post(prefix + "/reviews", json={"run_id": first["run_id"], "left_source_revision_id": pair["left_source_revision_id"], "right_source_revision_id": pair["right_source_revision_id"], "action": "KEEP_DISTINCT", "reason": "Different people", "expected_revision": 1})
        assert decision.status_code == 201, decision.text
        second = run("reject-second")
        assert SameModelHandler.calls >= 2
        result_pair = second["result"]["pairs"][0]
        assert result_pair["action"] == "DISTINCT" and result_pair["recommendation"]["is_same_entity"] is True
        snapshot = client.post(prefix + "/snapshots", json={"run_id": second["run_id"], "expected_current_version": 0})
        assert snapshot.status_code == 201, snapshot.text
        mappings = client.get(prefix + "/snapshots/" + snapshot.json()["snapshot_id"] + "/export?kind=mappings").json()["mappings"]
        assert len(mappings) == 2 and len({row["entity_id"] for row in mappings}) == 2
        expected = {tuple(row["source_key"]): row["entity_id"] for row in batch.json()["identities"]}
        assert {tuple(row["source_key"]): row["entity_id"] for row in mappings} == expected
        from app.models.unification import IdentityDecisionRecord, IdentityChangeRecord
        with maker() as db:
            link = db.scalar(select(IdentityDecisionRecord).where(IdentityDecisionRecord.change_id == decision.json()["change_id"]))
            assert link is not None and link.action == "KEEP_DISTINCT"
            assert {link.left_entity_id, link.right_entity_id} == set(expected.values())
            assert all(db.get(UnifiedEntity, entity_id).status == "ACTIVE" for entity_id in expected.values())
            audit = db.get(IdentityChangeRecord, decision.json()["change_id"])
            assert audit.actor_id == identity["user_id"] and audit.reason == "Different people"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_l09_attribute_override_is_audited_and_snapshot_versioned(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.releases import execute_unification_run
    client, maker, identity = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    scheme = {"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name", "tax_id": "tax_id", "address": "address"},
        "strong_identifiers": ["tax_id"], "candidate_rules": [{"kind": "exact", "fields": ["tax_id"]}],
        "attribute_policies": {"address": {"source_priority": ["erp", "secondary"], "on_conflict": "retain_provenance"}}}
    assert client.put(prefix + "/schemes/supplier", json=scheme).status_code == 200
    records = [{"source_system": source, "dataset": "supplier", "source_record_id": "1", "attributes": {"name": "Acme", "tax_id": "TEST-1", "address": address}}
               for source, address in [("erp", "Old address"), ("secondary", "New address")]]
    batch = client.post(prefix + "/batches", json={"input_version": "attributes-v1", "records": records})
    assert batch.status_code == 201
    def run(key):
        result = client.post(prefix + "/runs", headers={"Idempotency-Key": key}, json={"scheme_key": "supplier", "input_version": "attributes-v1"})
        assert result.status_code == 202
        run_id = result.json()["run_id"]
        assert client.get(prefix + "/runs/" + run_id).json()["status"] == "SUCCEEDED"
        return run_id
    def publish(run_id, version):
        result = client.post(prefix + "/snapshots", json={"run_id": run_id, "expected_current_version": version})
        assert result.status_code == 201, result.text
        return result.json()["snapshot_id"]
    first = publish(run("attributes-first"), 0)
    old = client.get(prefix + "/snapshots/" + first + "/export?kind=entities").json()
    assert len(old["entities"]) == 1
    entity = old["entities"][0]
    assert entity["attributes"]["address"] == "Old address"
    assert {(value["source_system"], value["value"]) for value in entity["provenance"]["conflicts"][0]["values"]} == {("erp", "Old address"), ("secondary", "New address")}
    changes = client.get(prefix + "/identity-changes").json()["items"]
    revision = max((row["revision"] for row in changes), default=1)
    endpoint = prefix + "/entities/" + entity["entity_id"] + "/attribute-overrides"
    preview = client.get(endpoint + "/preview", params={"snapshot_id": first, "field": "address"})
    assert preview.status_code == 200, preview.text
    assert {(item["source_system"], item["attributes"]["address"]) for item in preview.json()["members"]} == {("erp", "Old address"), ("secondary", "New address")}
    request = {"snapshot_id": first, "field": "address", "value": "Verified address", "reason": "Verified document", "expected_revision": revision, "member_revisions": preview.json()["member_revisions"]}
    identity["role"] = "viewer"
    assert client.post(endpoint, json=request).status_code == 403
    identity["role"] = "editor"
    assert client.post(endpoint, json={**request, "field": "missing"}).status_code == 422
    assert client.post(endpoint, json={**request, "expected_revision": revision + 1}).status_code == 409
    assert client.get(prefix + "/identity-changes").json()["items"] == changes
    override = client.post(endpoint, json=request)
    assert override.status_code == 201, override.text
    second = publish(run("attributes-second"), 1)
    current = client.get(prefix + "/snapshots/" + second + "/export?kind=entities").json()["entities"][0]
    assert current["attributes"]["address"] == "Verified address"
    assert current["provenance"]["selected"]["address"]["actor_id"] == identity["user_id"]
    assert current["provenance"]["selected"]["address"]["reason"] == "Verified document"
    assert {(value["source_system"], value["value"]) for value in current["provenance"]["conflicts"][0]["values"]} == {("erp", "Old address"), ("secondary", "New address")}
    assert client.get(prefix + "/snapshots/" + first + "/export?kind=entities").json() == old
    audit = client.get(prefix + "/identity-changes").json()["items"]
    change = next(row for row in audit if row["change_id"] == override.json()["change_id"])
    assert change["actor_id"] == identity["user_id"] and change["reason"] == "Verified document"
    assert {key: change["payload"][key] for key in ("entity_id", "field", "value")} == {"entity_id": entity["entity_id"], "field": "address", "value": "Verified address"}
    delta = client.get(prefix + "/snapshots/" + second + "/export?kind=delta").json()["delta"]
    before, after = delta["changed_attributes"][entity["entity_id"]]
    assert before["address"] == "Old address" and after["address"] == "Verified address"
    third = client.post(prefix + "/batches", json={"input_version": "third", "records": [{"source_system": "other", "dataset": "supplier", "source_record_id": "3", "attributes": {"name": "Other"}}]})
    assert third.status_code == 201
    run("classify-third")
    merged = client.post(prefix + "/decisions", json={"scheme_key": "supplier", "left_entity_id": entity["entity_id"], "right_entity_id": third.json()["identities"][0]["entity_id"], "action": "MERGE", "reason": "Merge reviewed", "expected_revision": override.json()["revision"]})
    assert merged.status_code == 200, merged.text
    rerun = run("attributes-after-merge")
    rejected = client.post(prefix + "/snapshots", json={"run_id": rerun, "expected_current_version": 2})
    assert rejected.status_code == 409 and "attribute override" in rejected.json()["detail"]
    preview_after_merge = client.get(endpoint + "/preview", params={"snapshot_id": second, "field": "address"})
    assert preview_after_merge.status_code == 200 and len(preview_after_merge.json()["members"]) == 3
    stale_preview = client.post(endpoint, json={**request, "snapshot_id": second, "expected_revision": merged.json()["revision"]})
    assert stale_preview.status_code == 409
    refreshed = client.post(endpoint, json={**request, "snapshot_id": second, "expected_revision": merged.json()["revision"], "member_revisions": preview_after_merge.json()["member_revisions"]})
    assert refreshed.status_code == 201, refreshed.text
    final = publish(run("attributes-reconfirmed"), 2)
    final_entity = client.get(prefix + "/snapshots/" + final + "/export?kind=entities").json()["entities"][0]
    assert final_entity["attributes"]["address"] == "Verified address"
    before_change = client.get(endpoint + "/preview", params={"snapshot_id": final, "field": "address"}).json()
    changed = {**records[0], "attributes": {**records[0]["attributes"], "address": "Changed upstream"}}
    assert client.post(prefix + "/batches", json={"input_version": "changed-source", "records": [changed]}).status_code == 201
    stale = client.post(endpoint, json={**request, "snapshot_id": final, "expected_revision": before_change["identity_revision"], "member_revisions": before_change["member_revisions"]})
    assert stale.status_code == 409 and "source evidence changed" in stale.json()["detail"]
    stale_run = run("changed-source-run")
    rejected = client.post(prefix + "/snapshots", json={"run_id": stale_run, "expected_current_version": 3})
    assert rejected.status_code == 409 and "attribute override" in rejected.json()["detail"]
    reduced = {**scheme, "field_map": {"name": "name", "tax_id": "tax_id"}, "attribute_policies": {}}
    assert client.put(prefix + "/schemes/supplier", json=reduced).status_code == 200
    assert publish(run("scheme-without-address"), 3)


def test_l08_transitive_merge_reports_blocking_decision(public_api):
    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    assert client.put(prefix + "/schemes/supplier", json={"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name"}}).status_code == 200
    records = [{"source_system": system, "dataset": "supplier", "source_record_id": "001", "attributes": {"name": system}} for system in ("a", "b", "c")]
    batch = client.post(prefix + "/batches", json={"input_version": "bridge", "records": records})
    assert batch.status_code == 201
    a, b, c = [row["entity_id"] for row in batch.json()["identities"]]
    def decide(left, right, action, revision):
        return client.post(prefix + "/decisions", json={"scheme_key": "supplier", "left_entity_id": left, "right_entity_id": right,
            "action": action, "reason": "Human review", "expected_revision": revision})
    rejection = decide(a, c, "KEEP_DISTINCT", 1)
    assert rejection.status_code == 200
    merge = decide(a, b, "MERGE", rejection.json()["revision"])
    assert merge.status_code == 200
    audit_before = client.get(prefix + "/identity-changes").json()["items"]
    blocked = decide(b, c, "MERGE", merge.json()["revision"])
    assert blocked.status_code == 409
    assert rejection.json()["change_id"] in blocked.json()["detail"]
    assert client.get(prefix + "/identity-changes").json()["items"] == audit_before
    with maker() as db:
        rows = db.scalars(select(SourceRecord).where(SourceRecord.project_id == "project-one")).all()
        owners = {row.source_system: row.current_entity_id for row in rows}
        assert owners["a"] == owners["b"] and owners["a"] != owners["c"]


def test_l07_dependent_merge_requires_explicit_split(public_api):
    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    scheme = {"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name"}}
    assert client.put(prefix + "/schemes/supplier", json=scheme).status_code == 200
    records = [{"source_system": "erp", "dataset": "supplier", "source_record_id": str(i), "attributes": {"name": str(i)}} for i in range(3)]
    batch = client.post(prefix + "/batches", json={"input_version": "dependency-v1", "records": records})
    assert batch.status_code == 201, batch.text
    ids = batch.json()["identities"]
    def decide(left, right, action, revision):
        return client.post(prefix + "/decisions", json={"scheme_key": "supplier", "left_entity_id": left, "right_entity_id": right,
            "action": action, "reason": "Verified decision", "expected_revision": revision})
    merge = decide(ids[0]["entity_id"], ids[1]["entity_id"], "MERGE", 1)
    assert merge.status_code == 200, merge.text
    cannot = decide(ids[0]["entity_id"], ids[2]["entity_id"], "KEEP_DISTINCT", merge.json()["revision"])
    assert cannot.status_code == 200, cannot.text
    blocked = client.post(prefix + "/identity-changes/" + merge.json()["change_id"] + "/undo", json={"expected_revision": cannot.json()["revision"], "reason": "Undo dependent merge"})
    assert blocked.status_code == 409
    with maker() as db:
        assert db.get(SourceRecord, ids[1]["source_record_id"]).current_entity_id == ids[0]["entity_id"]
    split = client.post(prefix + "/entities/" + ids[0]["entity_id"] + "/split", json={"source_record_ids": [ids[1]["source_record_id"]], "reason": "Explicit separation", "expected_revision": cannot.json()["revision"]})
    assert split.status_code == 200, split.text
    changes = client.get(prefix + "/identity-changes").json()["items"]
    lineage = next(row for row in changes if row["change_id"] == split.json()["change_id"])
    new_id = lineage["payload"]["new_entity_id"]
    assert new_id not in {item["entity_id"] for item in ids}
    assert lineage["payload"]["source_entity_id"] == ids[0]["entity_id"]
    assert lineage["payload"]["source_record_ids"] == [ids[1]["source_record_id"]]
    with maker() as db:
        assert db.get(SourceRecord, ids[0]["source_record_id"]).current_entity_id == ids[0]["entity_id"]
        assert db.get(SourceRecord, ids[1]["source_record_id"]).current_entity_id == new_id
        assert db.get(SourceRecord, ids[2]["source_record_id"]).current_entity_id == ids[2]["entity_id"]
        assert db.get(UnifiedEntity, new_id).status == "ACTIVE"
        assert db.get(UnifiedEntity, ids[1]["entity_id"]).status == "MERGED"
        assert db.get(UnifiedEntity, ids[1]["entity_id"]).redirect_to_id == ids[0]["entity_id"]
        from app.models.unification import IdentityDecisionRecord
        assert db.scalar(select(func.count()).select_from(IdentityDecisionRecord).where(
            IdentityDecisionRecord.change_id == cannot.json()["change_id"], IdentityDecisionRecord.action == "KEEP_DISTINCT")) == 1


def test_l06_l07_merge_redirect_undo_and_snapshot_history(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.identity import resolve_entity_id
    from app.services.unification.releases import execute_unification_run

    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    root = Path(__file__).resolve().parents[2] / "tests/golden/v1/supplier"
    assert client.put(prefix + "/schemes/supplier", json=json.loads((root / "scheme.json").read_text())).status_code == 200
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text()) if row["record_ref"] in {"supplier:erp:02", "supplier:secondary:02"}]
    batch = client.post(prefix + "/batches", json={"input_version": "merge-v1", "records": records})
    assert batch.status_code == 201, batch.text
    initial_ids = {row["entity_id"] for row in batch.json()["identities"]}
    identities = batch.json()["identities"]
    intended_survivor, intended_retired = identities[0]["entity_id"], identities[1]["entity_id"]
    run = client.post(prefix + "/runs", headers={"Idempotency-Key": "merge-first"}, json={"scheme_key": "supplier", "input_version": "merge-v1"})
    assert run.status_code == 202 and client.get(prefix + "/runs/" + run.json()["run_id"]).json()["status"] == "SUCCEEDED"
    run_id = run.json()["run_id"]
    def publish(expected):
        current_run = run_id
        if expected:
            next_run = client.post(prefix + "/runs", headers={"Idempotency-Key": f"merge-rerun-{expected}"}, json={"scheme_key": "supplier", "input_version": "merge-v1"})
            assert next_run.status_code == 202, next_run.text
            current_run = next_run.json()["run_id"]
            assert client.get(prefix + "/runs/" + current_run).json()["status"] == "SUCCEEDED"
        response = client.post(prefix + "/snapshots", json={"run_id": current_run, "expected_current_version": expected})
        assert response.status_code == 201, response.text
        return response.json()["snapshot_id"]
    def exported(snapshot, kind):
        response = client.get(prefix + "/snapshots/" + snapshot + "/export?kind=" + kind)
        assert response.status_code == 200, response.text
        return response.json()
    first = publish(0)
    first_entities = exported(first, "entities")
    first_mappings = exported(first, "mappings")
    def mapping_dict(payload):
        return {tuple(row["source_key"]): row["entity_id"] for row in payload["mappings"]}
    first_by_source = mapping_dict(first_mappings)
    assert set(first_by_source) == {("project-one", row["source_system"], row["dataset"], row["source_record_id"]) for row in records}
    assert {x["entity_id"] for x in first_mappings["mappings"]} == initial_ids
    reviews = client.get(prefix + "/reviews", params={"run_id": run_id}).json()["items"]
    assert len(reviews) == 1
    pair = reviews[0]
    merged = client.post(prefix + "/decisions", json={"scheme_key": "supplier", "left_entity_id": intended_survivor,
        "right_entity_id": intended_retired, "action": "MERGE", "reason": "Reviewed merge", "expected_revision": 1})
    assert merged.status_code == 200, merged.text
    second = publish(1)
    second_mappings = exported(second, "mappings")
    second_entities = exported(second, "entities")
    assert set(mapping_dict(second_mappings)) == set(first_by_source)
    current_ids = {x["entity_id"] for x in second_mappings["mappings"]}
    assert len(current_ids) == 1
    active_id = next(iter(current_ids))
    retired_id = next(iter(initial_ids - current_ids))
    assert active_id == intended_survivor and retired_id == intended_retired
    with maker() as db:
        assert resolve_entity_id(db, retired_id) == active_id
        assert db.get(UnifiedEntity, retired_id).status == "MERGED"
        assert db.get(UnifiedEntity, active_id).status == "ACTIVE"
    changes = client.get(prefix + "/identity-changes").json()["items"]
    merge_audit = next(row for row in changes if row["change_id"] == merged.json()["change_id"])
    assert merge_audit["reason"] == "Reviewed merge"
    assert set(merge_audit["payload"]["record_ids"]) == {identities[1]["source_record_id"]}
    assert set(merge_audit["payload"]["target_record_ids"]) == {identities[0]["source_record_id"]}
    duplicate = client.post(prefix + "/decisions", json={"scheme_key": "supplier", "left_entity_id": active_id,
        "right_entity_id": retired_id, "action": "MERGE", "reason": "Duplicate merge", "expected_revision": merged.json()["revision"]})
    assert duplicate.status_code == 200 and duplicate.json()["change_id"] == merged.json()["change_id"]
    assert client.get(prefix + "/identity-changes").json()["items"] == changes
    assert exported(first, "entities") == first_entities and exported(first, "mappings") == first_mappings
    undone = client.post(prefix + "/identity-changes/" + merged.json()["change_id"] + "/undo", json={"expected_revision": merged.json()["revision"], "reason": "Reopen identities"})
    assert undone.status_code == 200, undone.text
    third = publish(2)
    third_mappings = exported(third, "mappings")
    assert mapping_dict(third_mappings) == first_by_source
    with maker() as db:
        assert all(db.get(UnifiedEntity, entity_id).status == "ACTIVE" for entity_id in initial_ids)
    assert exported(second, "mappings") == second_mappings
    assert exported(second, "entities") == second_entities
    assert exported(first, "entities") == first_entities and exported(first, "mappings") == first_mappings
    expected_changed = {json.dumps(list(key), ensure_ascii=False, separators=(",", ":")): [mapping_dict(second_mappings)[key], value]
                        for key, value in first_by_source.items() if mapping_dict(second_mappings)[key] != value}
    assert exported(third, "delta")["delta"]["changed_memberships"] == expected_changed
    changes = client.get(prefix + "/identity-changes").json()["items"]
    assert any(x["change_id"] == undone.json()["change_id"] and x["reason"] == "Reopen identities" for x in changes)


def test_l04_human_approval_survives_rerun(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.releases import execute_unification_run

    client, maker, identity = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    root = Path(__file__).resolve().parents[2] / "tests/golden/v1/supplier"
    assert client.put(prefix + "/schemes/supplier", json=json.loads((root / "scheme.json").read_text())).status_code == 200
    raw = json.loads((root / "records.json").read_text())
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in raw if row["record_ref"] in {"supplier:erp:02", "supplier:secondary:02"}]
    batch = client.post(prefix + "/batches", json={"input_version": "approval-v1", "records": records})
    assert batch.status_code == 201, batch.text
    revision_ids = {row["source_revision_id"] for row in batch.json()["identities"]}
    def run(key):
        response = client.post(prefix + "/runs", headers={"Idempotency-Key": key}, json={"scheme_key": "supplier", "input_version": "approval-v1"})
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert client.get(prefix + "/runs/" + run_id).json()["status"] == "SUCCEEDED"
        return run_id
    first = run("approval-first")
    reviews = client.get(prefix + "/reviews", params={"run_id": first}).json()["items"]
    pair = next(pair for pair in reviews if {pair["left_source_revision_id"], pair["right_source_revision_id"]} == revision_ids)
    decision = client.post(prefix + "/reviews", json={"run_id": first, "left_source_revision_id": pair["left_source_revision_id"],
        "right_source_revision_id": pair["right_source_revision_id"], "action": "MERGE", "reason": "Supplier reviewed", "expected_revision": 1})
    assert decision.status_code == 201, decision.text
    rerun = run("approval-second")
    reviews_after = client.get(prefix + "/reviews", params={"run_id": rerun}).json()["items"]
    assert not any({row["left_source_revision_id"], row["right_source_revision_id"]} == revision_ids for row in reviews_after)
    snapshot = client.post(prefix + "/snapshots", json={"run_id": rerun, "expected_current_version": 0})
    assert snapshot.status_code == 201, snapshot.text
    mappings = client.get(prefix + "/snapshots/" + snapshot.json()["snapshot_id"] + "/export?kind=mappings").json()["mappings"]
    assert len(mappings) == 2 and len({item["entity_id"] for item in mappings}) == 1
    assert {tuple(row["source_key"]) for row in mappings} == {("project-one", item["source_system"], item["dataset"], item["source_record_id"]) for item in records}
    entities = client.get(prefix + "/snapshots/" + snapshot.json()["snapshot_id"] + "/export?kind=entities").json()["entities"]
    assert any(row["entity_id"] == mappings[0]["entity_id"] and row["status"] == "ACTIVE" for row in entities)
    changes = client.get(prefix + "/identity-changes").json()["items"]
    assert any(row["change_id"] == decision.json()["change_id"] and row["actor_id"] == identity["user_id"] and row["reason"] == "Supplier reviewed" for row in changes)


def test_l03_updated_record_preserves_identity_and_old_snapshot(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.releases import execute_unification_run

    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    root = Path(__file__).resolve().parents[2] / "tests/golden/v1/supplier"
    assert client.put(prefix + "/schemes/supplier", json=json.loads((root / "scheme.json").read_text())).status_code == 200
    item = json.loads((root / "records.json").read_text())[0]
    record = {k: item[k] for k in ("source_system", "dataset", "source_record_id", "attributes")}
    def publish(version, data, current):
        result = client.post(prefix + "/batches", json={"input_version": version, "records": [data]})
        assert result.status_code == 201, result.text
        identity = result.json()["identities"][0]
        run = client.post(prefix + "/runs", headers={"Idempotency-Key": version}, json={"scheme_key": "supplier", "input_version": version})
        assert run.status_code == 202, run.text
        assert client.get(prefix + "/runs/" + run.json()["run_id"]).json()["status"] == "SUCCEEDED"
        published = client.post(prefix + "/snapshots", json={"run_id": run.json()["run_id"], "expected_current_version": current})
        assert published.status_code == 201, published.text
        return identity, published.json()["snapshot_id"]
    old_identity, old_snapshot = publish("supplier-v1", record, 0)
    old_response = client.get(prefix + "/snapshots/" + old_snapshot + "/export?kind=entities")
    assert old_response.status_code == 200
    old = old_response.json()
    assert old["entities"][0]["entity_id"] == old_identity["entity_id"]
    assert old["entities"][0]["attributes"]["name"] == record["attributes"]["name"]
    updated_record = {**record, "attributes": {**record["attributes"], "name": "Updated supplier name"}}
    new_identity, new_snapshot = publish("supplier-v2", updated_record, 1)
    assert old_identity["source_key"] == new_identity["source_key"]
    assert old_identity["entity_id"] == new_identity["entity_id"]
    assert old_identity["source_revision_id"] != new_identity["source_revision_id"]
    with maker() as db:
        before = db.get(SourceRevision, old_identity["source_revision_id"])
        after = db.get(SourceRevision, new_identity["source_revision_id"])
        assert before.source_record_id == after.source_record_id
        assert before.attributes["name"] == record["attributes"]["name"]
        assert after.attributes["name"] == "Updated supplier name"
    assert client.get(prefix + "/snapshots/" + old_snapshot + "/export?kind=entities").json() == old
    latest = client.get(prefix + "/snapshots/" + new_snapshot + "/export?kind=entities").json()
    assert latest["entities"][0]["entity_id"] == old_identity["entity_id"]
    assert latest["entities"][0]["attributes"]["name"] == "Updated supplier name"
    delta = client.get(prefix + "/snapshots/" + new_snapshot + "/export?kind=delta").json()["delta"]
    before, after = delta["changed_attributes"][new_identity["entity_id"]]
    assert before["name"] == record["attributes"]["name"]
    assert after["name"] == "Updated supplier name"


def test_l01_supplier_reruns_keep_published_identity(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.models.unification import IdentityDecisionRecord
    from app.services.unification.releases import execute_unification_run

    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    root = Path(__file__).resolve().parents[2] / "tests/golden/v1/supplier"
    assert client.put(prefix + "/schemes/supplier", json=json.loads((root / "scheme.json").read_text())).status_code == 200
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text())]
    batch = {"input_version": "supplier-v1", "records": records}
    assert client.post(prefix + "/batches", json=batch).status_code == 201
    payload = {"scheme_key": "supplier", "input_version": "supplier-v1"}
    first = client.post(prefix + "/runs", headers={"Idempotency-Key": "first"}, json=payload)
    assert first.status_code == 202, first.text
    first_id = first.json()["run_id"]
    assert client.get(prefix + "/runs/" + first_id).json()["status"] == "SUCCEEDED"
    published = client.post(prefix + "/snapshots", json={"run_id": first_id, "expected_current_version": 0})
    assert published.status_code == 201, published.text
    def mapping(snapshot):
        response = client.get(prefix + "/snapshots/" + snapshot + "/export?kind=mappings")
        assert response.status_code == 200
        return {tuple(row["source_key"]): row["entity_id"] for row in response.json()["mappings"]}
    first_mapping = mapping(published.json()["snapshot_id"])
    assert set(first_mapping) == {("project-one", row["source_system"].casefold(), row["dataset"].casefold(), row["source_record_id"]) for row in records}
    with maker() as db:
        initial = tuple(db.scalar(select(func.count()).select_from(model)) for model in (SourceRecord, UnifiedEntity, IdentityDecisionRecord))
        initial_active = db.scalar(select(func.count()).select_from(UnifiedEntity).where(UnifiedEntity.status == "ACTIVE"))
    repeat = client.post(prefix + "/runs", headers={"Idempotency-Key": "first"}, json=payload)
    assert repeat.status_code == 202 and repeat.json()["run_id"] == first_id
    assert client.post(prefix + "/batches", json=batch).status_code == 201
    new = client.post(prefix + "/runs", headers={"Idempotency-Key": "second"}, json=payload)
    assert new.status_code == 202 and new.json()["run_id"] != first_id
    assert client.get(prefix + "/runs/" + new.json()["run_id"]).json()["status"] == "SUCCEEDED"
    second = client.post(prefix + "/snapshots", json={"run_id": new.json()["run_id"], "expected_current_version": 1})
    assert second.status_code == 201, second.text
    assert mapping(second.json()["snapshot_id"]) == first_mapping
    with maker() as db:
        assert tuple(db.scalar(select(func.count()).select_from(model)) for model in (SourceRecord, UnifiedEntity, IdentityDecisionRecord)) == initial
        assert db.scalar(select(func.count()).select_from(UnifiedEntity).where(UnifiedEntity.status == "ACTIVE")) == initial_active


def test_l10_concurrent_idempotency_and_changed_payload_conflict(public_api, monkeypatch):
    import app.api.v1.unification as api

    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    monkeypatch.setattr(api, "dispatch_unification_run", lambda _run_id: None)
    assert client.put(prefix + "/schemes/product", json={"entity_type": "product", "identity_granularity": "specification", "field_map": {"name": "name"}}).status_code == 200
    records_path = Path(__file__).resolve().parents[2] / "tests/golden/v1/product/records.json"
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads(records_path.read_text())]
    assert client.post(prefix + "/batches", json={"input_version": "product-v1", "records": records}).status_code == 201
    barrier = Barrier(2)
    def submit():
        barrier.wait(timeout=10)
        return client.post(prefix + "/runs", headers={"Idempotency-Key": "same-key"}, json={"scheme_key": "product", "input_version": "product-v1"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
    changed = client.post(prefix + "/runs", headers={"Idempotency-Key": "same-key"}, json={"scheme_key": "product", "input_version": "product-v2"})
    assert changed.status_code == 409
    from app.models.unification import UnificationRun, UnificationSnapshot
    with maker() as db:
        assert db.scalar(select(func.count()).select_from(UnificationRun)) == 1
        assert db.scalar(select(func.count()).select_from(UnificationSnapshot)) == 0


def test_l02_reordered_customer_import_keeps_published_mapping(public_api, monkeypatch):
    from app.services.unification.releases import execute_unification_run
    import app.api.v1.unification as api

    client, maker, _ = public_api
    prefix = "/api/v1/projects/project-one/unification"
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    scheme_path = Path(__file__).resolve().parents[2] / "tests/golden/v1/customer/scheme.json"
    scheme = json.loads(scheme_path.read_text())
    assert client.put(prefix + "/schemes/customer", json=scheme).status_code == 200
    fixture = Path(__file__).resolve().parents[2] / "tests/golden/v1/customer/records.json"
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads(fixture.read_text())]

    def execute(input_version, data, key, expected):
        batch = client.post(prefix + "/batches", json={"input_version": input_version, "records": data})
        assert batch.status_code == 201, batch.text
        submitted = client.post(prefix + "/runs", headers={"Idempotency-Key": key}, json={"scheme_key": "customer", "input_version": input_version})
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["run_id"]
        result = client.get(prefix + "/runs/" + run_id)
        assert result.json()["status"] == "SUCCEEDED", result.text
        published = client.post(prefix + "/snapshots", json={"run_id": run_id, "expected_current_version": expected})
        assert published.status_code == 201, published.text
        snapshot = published.json()["snapshot_id"]
        mappings = client.get(prefix + "/snapshots/" + snapshot + "/export?kind=mappings").json()["mappings"]
        return {tuple(item["source_key"]): item["entity_id"] for item in mappings}

    first = execute("customer-v1", records, "customer-first", 0)
    second = execute("customer-v2", list(reversed(records)), "customer-second", 1)
    assert len(first) == len(records)
    assert first == second


def test_l16_invalid_batches_are_atomic_and_exploration_is_explicit(public_api):
    client, maker, _ = public_api
    endpoint = "/api/v1/projects/project-one/unification/batches"
    record = {"source_system": "crm", "dataset": "customers", "source_record_id": "001", "attributes": {"name": "A"}}
    missing = {**record, "source_record_id": None}
    response = client.post(endpoint, json={"input_version": "missing", "records": [record, missing]})
    assert response.status_code == 422
    assert any(row["index"] == 1 and row["code"] == "SOURCE_ID_REQUIRED" for row in response.json()["detail"])
    with maker() as db:
        assert all(db.scalar(select(func.count()).select_from(model)) == 0 for model in (SourceRecord, SourceRevision, UnifiedEntity))
    conflict = {**record, "attributes": {"name": "B"}}
    response = client.post(endpoint, json={"input_version": "conflict", "records": [record, conflict]})
    assert response.status_code == 422
    assert any(row["index"] == 1 and row["code"] == "DUPLICATE_SOURCE_KEY" for row in response.json()["detail"])
    with maker() as db:
        assert all(db.scalar(select(func.count()).select_from(model)) == 0 for model in (SourceRecord, SourceRevision, UnifiedEntity))
    response = client.post(endpoint, json={"input_version": "explore", "mode": "exploration", "records": [missing]})
    assert response.status_code == 201, response.text
    assert response.json()["identity_guarantee"] == "single_import"
    assert "repeated imports create new identities" in response.json()["limitations"][0]


def test_l21_equal_ids_are_namespaced_by_system_and_dataset(public_api, monkeypatch):
    from app.services.unification.releases import execute_unification_run
    import app.api.v1.unification as api

    client, maker, _ = public_api
    records = [
        {"source_system": system, "dataset": dataset, "source_record_id": "001", "attributes": {"name": name, "origin": f"{system}/{dataset}"}}
        for system, dataset, name in [("erp", "supplier", "A"), ("crm", "supplier", "A"), ("erp", "customer", "C")]
    ]
    response = client.post("/api/v1/projects/project-one/unification/batches", json={"input_version": "collision", "records": records})
    assert response.status_code == 201, response.text
    assert len({item["entity_id"] for item in response.json()["identities"]}) == 3
    with maker() as db:
        rows = db.scalars(select(SourceRecord).where(SourceRecord.project_id == "project-one")).all()
        assert {(r.source_system, r.dataset, r.source_record_id) for r in rows} == {
            ("erp", "supplier", "001"), ("crm", "supplier", "001"), ("erp", "customer", "001")
        }
        attributes = {(r.source_system, r.dataset): db.get(SourceRevision, r.current_revision_id).attributes for r in rows}
        assert attributes == {
            ("erp", "supplier"): {"name": "A", "origin": "erp/supplier"},
            ("crm", "supplier"): {"name": "A", "origin": "crm/supplier"},
            ("erp", "customer"): {"name": "C", "origin": "erp/customer"},
        }
    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)
    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    prefix = "/api/v1/projects/project-one/unification"
    scheme = {"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name"},
              "strong_identifiers": ["name"], "candidate_rules": [{"kind": "exact", "fields": ["name"]}]}
    assert client.put(prefix + "/schemes/collision", json=scheme).status_code == 200
    run = client.post(prefix + "/runs", headers={"Idempotency-Key": "collision"}, json={"scheme_key": "collision", "input_version": "collision"})
    assert run.status_code == 202, run.text
    result = client.get(prefix + "/runs/" + run.json()["run_id"])
    assert result.json()["status"] == "SUCCEEDED", result.text
    snapshot = client.post(prefix + "/snapshots", json={"run_id": run.json()["run_id"], "expected_current_version": 0})
    assert snapshot.status_code == 201, snapshot.text
    mappings = client.get(prefix + "/snapshots/" + snapshot.json()["snapshot_id"] + "/export?kind=mappings").json()["mappings"]
    by_key = {tuple(row["source_key"][1:3]): row["entity_id"] for row in mappings}
    assert by_key[("erp", "supplier")] == by_key[("crm", "supplier")]
    assert by_key[("erp", "customer")] != by_key[("erp", "supplier")]
