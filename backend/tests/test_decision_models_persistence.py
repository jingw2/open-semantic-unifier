"""Decision model calls are persisted with their run and exposed project-scoped."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.models.unification import DecisionModelCall
from app.services.decision_models.contracts import DecisionError, DecisionSuggestion, DecisionVerdict
from test_unification_public_api import public_api

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "golden" / "v1" / "customer"


def _typed_suggestion():
    return DecisionSuggestion(
        verdict=DecisionVerdict.SAME,
        confidence=0.85,
        distribution={DecisionVerdict.SAME: 0.85, DecisionVerdict.DIFFERENT: 0.15},
        explanation="shared phone suggests same household member",
        provider_kind="openai_compatible",
        provider_key="controlled",
        model_version="controlled",
        prompt_version="entity-pair-choice-v1",
        latency_ms=7.5,
        usage={"input_tokens": 42, "output_tokens": 9},
        cost_usd=0.0004,
        trace_id="trace-fixed",
    )


def _typed_error():
    return DecisionError(
        code="timeout", message="provider request timed out", retryable=True,
        provider_kind="openai_compatible", provider_key="controlled",
        model_version="controlled", prompt_version="entity-pair-choice-v1",
        latency_ms=25.0, trace_id="trace-error",
    )


def _install_judge(monkeypatch, outcome):
    import app.services.unification.releases as releases

    def fake_judge_for_run(db, project_id, run, scheme):
        def judge(left, right, active_scheme):
            return outcome() if callable(outcome) else outcome
        return judge

    monkeypatch.setattr(releases, "_model_judge_for_run", fake_judge_for_run)


def _run_with_judge(client, maker, monkeypatch, outcome, *, api_key="supersecret-key", input_version="typed-v1", failure_policy="REVIEW", swallow_model_failure=False):
    import app.api.v1.unification as api
    from app.services.unification.releases import ModelFailure, execute_unification_run

    def run_inline(run_id):
        with maker() as db:
            try:
                execute_unification_run(db, run_id)
            except ModelFailure:
                if not swallow_model_failure:
                    raise

    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    _install_judge(monkeypatch, outcome)
    provider = client.post("/api/v1/providers", json={
        "provider_key": "controlled", "name": "Controlled model",
        "base_url": "https://controlled.example/v1", "default_model": "controlled",
        "api_key": api_key,
    })
    assert provider.status_code == 200, provider.text
    prefix = "/api/v1/projects/project-one/unification"
    scheme = json.loads((GOLDEN / "scheme.json").read_text())
    scheme.update(model_judgment_enabled=True, model_provider_id=provider.json()["id"], model_failure_policy=failure_policy)
    assert client.put(prefix + "/schemes/customer", json=scheme).status_code == 200
    records = [
        {key: row[key] for key in ("source_system", "dataset", "source_record_id", "attributes")}
        for row in json.loads((GOLDEN / "records.json").read_text())
        if row["record_ref"] in {"customer:erp:04", "customer:secondary:04"}
    ]
    batch = client.post(prefix + "/batches", json={"input_version": input_version, "records": records})
    assert batch.status_code == 201, batch.text
    run = client.post(prefix + "/runs", headers={"Idempotency-Key": input_version}, json={"scheme_key": "customer", "input_version": input_version})
    assert run.status_code == 202, run.text
    return run.json()["run_id"], batch.json()["identities"], provider.json()


def test_suggestion_run_persists_expected_rows(public_api, monkeypatch):
    client, maker, _ = public_api
    run_id, identities, _ = _run_with_judge(client, maker, monkeypatch, _typed_suggestion())
    run = client.get("/api/v1/projects/project-one/unification/runs/" + run_id).json()
    assert run["status"] == "SUCCEEDED"
    with maker() as db:
        rows = db.scalars(select(DecisionModelCall).where(DecisionModelCall.run_id == run_id)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.project_id == "project-one"
        assert {row.left_source_revision_id, row.right_source_revision_id} == {item["source_revision_id"] for item in identities}
        assert row.verdict == "SAME" and row.confidence == 0.85
        assert row.distribution_json == {"SAME": 0.85, "DIFFERENT": 0.15}
        assert row.explanation == "shared phone suggests same household member"
        assert row.provider_kind == "openai_compatible" and row.provider_key == "controlled"
        assert row.model_version == "controlled" and row.prompt_version == "entity-pair-choice-v1"
        assert row.latency_ms == 7.5
        assert row.usage_json == {"input_tokens": 42, "output_tokens": 9}
        assert row.cost_usd == 0.0004
        assert row.trace_id == "trace-fixed"
        assert row.error_code is None and row.error_message is None and row.retryable is None


def test_error_run_persists_error_row(public_api, monkeypatch):
    client, maker, _ = public_api
    run_id, _, _ = _run_with_judge(client, maker, monkeypatch, _typed_error())
    run = client.get("/api/v1/projects/project-one/unification/runs/" + run_id).json()
    assert run["status"] == "SUCCEEDED"
    assert "timeout" in run["result"]["pairs"][0]["model_error"]
    with maker() as db:
        rows = db.scalars(select(DecisionModelCall).where(DecisionModelCall.run_id == run_id)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.error_code == "timeout"
        assert row.error_message == "provider request timed out"
        assert row.retryable is True
        assert row.latency_ms == 25.0
        assert row.trace_id == "trace-error"
        assert row.verdict is None and row.confidence is None and row.distribution_json is None


def test_legacy_judge_result_writes_no_rows(public_api, monkeypatch):
    from app.services.llm_client import JudgeResult

    def legacy_judge():
        return JudgeResult(status="SUGGESTION", suggestion=True, confidence=0.9, reason="legacy", model="legacy-model")

    client, maker, _ = public_api
    run_id, _, _ = _run_with_judge(client, maker, monkeypatch, legacy_judge)
    run = client.get("/api/v1/projects/project-one/unification/runs/" + run_id).json()
    assert run["result"]["pairs"][0]["recommendation"]["is_same_entity"] is True
    with maker() as db:
        assert db.scalars(select(DecisionModelCall)).all() == []


def test_failed_run_rolls_back_staged_model_call_rows(public_api, monkeypatch):
    client, maker, _ = public_api
    run_id, _, _ = _run_with_judge(
        client, maker, monkeypatch, _typed_error(),
        failure_policy="FAIL", swallow_model_failure=True,
    )
    run = client.get("/api/v1/projects/project-one/unification/runs/" + run_id).json()
    assert run["status"] == "FAILED"
    assert "timeout" in run["error_message"]
    with maker() as db:
        assert db.scalars(select(DecisionModelCall)).all() == []


def test_model_calls_api_is_project_scoped_viewer_readable_and_redacted(public_api, monkeypatch):
    client, maker, identity = public_api
    first_run, _, _ = _run_with_judge(client, maker, monkeypatch, _typed_suggestion(), api_key="supersecret-key", input_version="typed-v1")
    second_run, _, _ = _run_with_judge(client, maker, monkeypatch, _typed_error(), input_version="typed-v2")
    response = client.get("/api/v1/projects/project-one/unification/model-calls")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2
    assert [item["run_id"] for item in payload["items"]] == [second_run, first_run]  # newest first
    item = payload["items"][0]
    assert item["project_id"] == "project-one"
    assert item["error_code"] == "timeout" and item["retryable"] is True
    suggestion = payload["items"][1]
    assert suggestion["verdict"] == "SAME" and suggestion["confidence"] == 0.85
    assert suggestion["distribution"] == {"SAME": 0.85, "DIFFERENT": 0.15}
    assert suggestion["usage"] == {"input_tokens": 42, "output_tokens": 9}
    assert suggestion["explanation"] == "shared phone suggests same household member"
    assert suggestion["model_version"] == "controlled"
    assert suggestion["prompt_version"] == "entity-pair-choice-v1"
    assert suggestion["provider_kind"] == "openai_compatible" and suggestion["provider_key"] == "controlled"
    assert suggestion["latency_ms"] == 7.5
    # Credentials and raw record values never leave the database.
    assert "supersecret-key" not in response.text
    assert "测试家庭成员甲" not in response.text
    assert "TEST-HOME-01" not in response.text
    # Run filter narrows to the requested run.
    filtered = client.get("/api/v1/projects/project-one/unification/model-calls", params={"run_id": first_run})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["run_id"] == first_run
    missing = client.get("/api/v1/projects/project-one/unification/model-calls", params={"run_id": "missing-run"})
    assert missing.json()["total"] == 0
    # Pagination is honored.
    paged = client.get("/api/v1/projects/project-one/unification/model-calls", params={"limit": 1, "offset": 1})
    assert paged.json()["total"] == 2 and [item["run_id"] for item in paged.json()["items"]] == [first_run]
    # Viewers may read model calls.
    identity["role"] = "viewer"
    assert client.get("/api/v1/projects/project-one/unification/model-calls").status_code == 200
    identity["role"] = "editor"
    # A foreign project stays hidden.
    assert client.get("/api/v1/projects/project-two/unification/model-calls").status_code == 404


def test_model_call_rows_survive_trace_shutdown(public_api, monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    configure_tracing(TraceConfig(enabled=True), span_exporter=InMemorySpanExporter())
    client, maker, _ = public_api
    try:
        run_id, _, _ = _run_with_judge(client, maker, monkeypatch, _typed_suggestion())
    finally:
        shutdown_tracing()
    # Tracing is gone; business audit rows remain queryable.
    payload = client.get("/api/v1/projects/project-one/unification/model-calls", params={"run_id": run_id}).json()
    assert payload["total"] == 1
    with maker() as db:
        assert db.scalars(select(DecisionModelCall).where(DecisionModelCall.run_id == run_id)).all()
