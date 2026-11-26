"""T-DM-4 adapter selection wired into unification runs and the provider test endpoint.

Every HTTP request in these tests goes to a controlled ThreadingHTTPServer on
127.0.0.1; no test touches an external network.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import Base
from app.core.security import encrypt_secret
from app.models.entities import LLMProvider, Project, Workspace
from app.models.unification import DecisionModelCall, UnificationRun, UnificationSchemeVersion
from app.services.unification.contracts import IngestMode, SourceRecordInput
from app.services.unification.ingest import ingest_batch
from app.services.unification.releases import ModelFailure, execute_unification_run, submit_run
from app.services.unification.schemes import MatchScheme, save_scheme
from test_unification_public_api import public_api


class ChoiceProviderHandler(BaseHTTPRequestHandler):
    """Controlled choice-protocol provider (Perplexity Decisions / TypeSafe Jev shape)."""

    bodies: list[dict] = []
    calls = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        type(self).calls += 1
        body = json.dumps({
            "model": "choice-response-model",
            "answers": {"entity_match": {
                "type": "choice",
                "choice": "SAME",
                "confidence": 0.9,
                "probabilities": {"SAME": 0.8, "DIFFERENT": 0.15, "UNKNOWN": 0.05},
            }},
            "usage": {"input_tokens": 120, "output_tokens": 3},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class OpenAICompatibleHandler(BaseHTTPRequestHandler):
    """Controlled OpenAI-compatible provider returning a same-entity suggestion."""

    calls = 0

    def do_POST(self):
        type(self).calls += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        content = json.dumps({"is_same_entity": True, "confidence": 0.7, "reason": "suggest same"})
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def choice_server():
    ChoiceProviderHandler.bodies = []
    ChoiceProviderHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), ChoiceProviderHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def openai_server():
    OpenAICompatibleHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAICompatibleHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run-integration.db'}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        session.add(Workspace(id="workspace", name="Workspace", slug="workspace"))
        session.flush()
        session.add(Project(id="project", workspace_id="workspace", name="Project", slug="project"))
        session.commit()
    try:
        yield engine, maker
    finally:
        engine.dispose()


def _provider(kind, base_url, *, send_fields=(), prompt_version="pair-v9", api_key="test-secret-key"):
    return LLMProvider(
        workspace_id="workspace",
        provider_key={"openai_compatible": "local-openai", "perplexity_decisions": "perplexity", "typesafe_jev": "typesafe"}[kind],
        name=f"{kind} controlled",
        base_url=base_url,
        default_model="controlled-model",
        encrypted_api_key=encrypt_secret(api_key),
        provider_kind=kind,
        send_fields_json=list(send_fields),
        prompt_version=prompt_version,
    )


def _submit_model_run(maker, *, provider, failure_policy="REVIEW", scheme_key="model-orgs", input_version="input-v1",
                      candidate_rules=None, records=None, field_map=None):
    with maker() as session:
        session.add(provider)
        session.flush()
        scheme = MatchScheme.model_validate({
            "entity_type": "organization", "identity_granularity": "legal_entity",
            "field_map": field_map or {"name": "name", "tax_id": "tax_id", "phone": "phone"},
            "candidate_rules": candidate_rules or [{"kind": "exact", "fields": ["name"]}],
            "model_judgment_enabled": True, "model_provider_id": provider.id,
            "model_failure_policy": failure_policy, "prompt_version": "scheme-pair-v1",
        })
        save_scheme(session, project_id="project", scheme_key=scheme_key, scheme=scheme)
        version_id = session.scalar(select(UnificationSchemeVersion.id).where(
            UnificationSchemeVersion.project_id == "project",
            UnificationSchemeVersion.scheme_key == scheme_key,
        ))
        imported = ingest_batch(session, project_id="project", records=records or [
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "Acme", "tax_id": None, "phone": "SECRET-PHONE-1"}),
            SourceRecordInput("project", "crm", "vendors", "2", {"name": "Acme", "tax_id": None, "phone": "SECRET-PHONE-2"}),
        ], mode=IngestMode.SYNC, input_version=input_version)
        assert imported.accepted
        session.commit()
        return submit_run(
            session, project_id="project", input_version=input_version, scheme_revision=version_id,
            idempotency_key=f"model-{input_version}", payload_hash=f"hash-{input_version}",
        )


def _execute(maker, run_id):
    with maker() as session:
        try:
            return execute_unification_run(session, run_id), None
        except ModelFailure as exc:
            return session.get(UnificationRun, run_id), exc


def test_choice_provider_receives_canonical_fields_mapped_from_source_columns(db, choice_server):
    _engine, maker = db
    run = _submit_model_run(
        maker,
        provider=_provider("perplexity_decisions", f"http://127.0.0.1:{choice_server.server_port}", send_fields=["name"]),
        field_map={"name": "company_name", "tax_id": "tax_id"},
        records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"company_name": "Acme", "tax_id": "T1"}),
            SourceRecordInput("project", "crm", "vendors", "2", {"company_name": "Acme", "tax_id": "T1"}),
        ],
    )
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    body = ChoiceProviderHandler.bodies[-1]
    assert body["state"]["left"] == {"name": "Acme"}
    assert body["state"]["right"] == {"name": "Acme"}


def test_openai_compatible_provider_is_selected_for_the_run(db, openai_server):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider(
        "openai_compatible", f"http://127.0.0.1:{openai_server.server_port}/v1", send_fields=["name"],
    ))
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    pair = completed.result_json["pairs"][0]
    recommendation = pair["recommendation"]
    assert recommendation["verdict"] == "SAME"
    assert recommendation["provider_kind"] == "openai_compatible"
    assert recommendation["prompt_version"] == "pair-v9"
    assert recommendation["model"] == "controlled-model"
    with maker() as session:
        row = session.scalar(select(DecisionModelCall).where(DecisionModelCall.run_id == run.id))
        assert row.provider_kind == "openai_compatible"
        assert row.verdict == "SAME" and row.prompt_version == "pair-v9"


@pytest.mark.parametrize("kind", ["perplexity_decisions", "typesafe_jev"])
def test_choice_provider_is_selected_for_the_run(db, choice_server, kind):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider(
        kind, f"http://127.0.0.1:{choice_server.server_port}", send_fields=["name"],
    ))
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    pair = completed.result_json["pairs"][0]
    recommendation = pair["recommendation"]
    assert recommendation["verdict"] == "SAME"
    assert recommendation["provider_kind"] == kind
    assert recommendation["prompt_version"] == "pair-v9"
    assert recommendation["model"] == "choice-response-model"
    assert recommendation["confidence"] == 0.9
    with maker() as session:
        row = session.scalar(select(DecisionModelCall).where(DecisionModelCall.run_id == run.id))
        assert row.provider_kind == kind
        assert row.verdict == "SAME" and row.prompt_version == "pair-v9"
        assert row.confidence == 0.9


def test_send_fields_filter_the_request(db, choice_server):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider(
        "perplexity_decisions", f"http://127.0.0.1:{choice_server.server_port}", send_fields=["name"],
    ))
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    body = ChoiceProviderHandler.bodies[-1]
    state = body["state"]
    assert state["left"] == {"name": "Acme"}
    assert state["right"] == {"name": "Acme"}
    serialized = json.dumps(body)
    assert "SECRET-PHONE" not in serialized
    assert "test-secret-key" not in serialized


def test_decision_provider_without_send_fields_never_calls_the_provider(db, choice_server):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider(
        "typesafe_jev", f"http://127.0.0.1:{choice_server.server_port}", send_fields=[],
    ), scheme_key="model-orgs-no-fields", input_version="input-v2")
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    assert ChoiceProviderHandler.calls == 0
    pair = completed.result_json["pairs"][0]
    assert pair["recommendation"] is None
    assert "missing_send_fields" in pair["model_error"]


def test_evidence_values_for_unlisted_fields_never_leave_the_workspace(db, choice_server):
    _engine, maker = db
    run = _submit_model_run(
        maker,
        provider=_provider("perplexity_decisions", f"http://127.0.0.1:{choice_server.server_port}", send_fields=["name"]),
        candidate_rules=[{"kind": "exact", "fields": ["name", "tax_id"]}],
        records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "Acme", "tax_id": "TAX-SECRET-9", "phone": "SECRET-PHONE-1"}),
            SourceRecordInput("project", "crm", "vendors", "2", {"name": "Acme", "tax_id": "TAX-SECRET-9", "phone": "SECRET-PHONE-2"}),
        ],
    )
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    pair = completed.result_json["pairs"][0]
    assert pair["recommendation"]["verdict"] == "SAME"
    body = ChoiceProviderHandler.bodies[-1]
    state = body["state"]
    assert state["left"] == {"name": "Acme"}
    assert state["right"] == {"name": "Acme"}
    serialized = json.dumps(body)
    assert "TAX-SECRET-9" not in serialized
    assert "SECRET-PHONE" not in serialized
    name_evidence = [item for item in state["evidence"] if item.get("rule") == "exact" and item.get("field") == "name"]
    tax_evidence = [item for item in state["evidence"] if item.get("rule") == "exact" and item.get("field") == "tax_id"]
    assert name_evidence and name_evidence[0]["value"] == "Acme"
    assert tax_evidence and "value" not in tax_evidence[0]


def test_decision_request_carries_the_configured_evidence(db, choice_server):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider(
        "perplexity_decisions", f"http://127.0.0.1:{choice_server.server_port}", send_fields=["name"],
    ))
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    body = ChoiceProviderHandler.bodies[-1]
    state = body["state"]
    assert state["entity_type"] == "organization"
    assert state["identity_granularity"] == "legal_entity"
    assert any(item.get("rule") == "exact" and item.get("field") == "name" for item in state["evidence"])


def test_run_spans_and_errors_never_carry_credentials_or_raw_values(db, choice_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    exporter = InMemorySpanExporter()
    tracing = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    try:
        _engine, maker = db
        run = _submit_model_run(maker, provider=_provider(
            "perplexity_decisions", f"http://127.0.0.1:{choice_server.server_port}", send_fields=["name"],
        ))
        completed, error = _execute(maker, run.id)
        assert error is None and completed.status == "SUCCEEDED"
        assert tracing.force_flush()
        spans = exporter.get_finished_spans()
        dump = " ".join(span.to_json() for span in spans)
        assert "test-secret-key" not in dump
        assert "SECRET-PHONE" not in dump
        model_span = next(span for span in spans if span.name == "unification.model_judge")
        assert "SECRET-PHONE" not in json.dumps(dict(model_span.attributes))
    finally:
        shutdown_tracing()


@pytest.mark.parametrize("policy", ["REVIEW", "FAIL"])
def test_missing_provider_is_a_typed_error_and_follows_policy(db, policy):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider("openai_compatible", "http://127.0.0.1:9/v1"), failure_policy=policy)
    with maker() as session:
        session.delete(session.scalar(select(LLMProvider)))
        session.commit()
    if policy == "REVIEW":
        completed, error = _execute(maker, run.id)
        assert error is None and completed.status == "SUCCEEDED"
        pair = completed.result_json["pairs"][0]
        assert "provider_missing" in pair["model_error"]
        assert pair["recommendation"] is None
        with maker() as session:
            row = session.scalar(select(DecisionModelCall).where(DecisionModelCall.run_id == run.id))
            assert row.error_code == "provider_missing" and row.retryable is False
    else:
        failed, error = _execute(maker, run.id)
        assert error is not None
        assert failed.status == "FAILED" and "provider_missing" in failed.error_message
        assert failed.result_json is None
        with maker() as session:
            assert session.scalars(select(DecisionModelCall)).all() == []


@pytest.mark.parametrize("policy", ["REVIEW", "FAIL"])
def test_credential_decrypt_failure_is_a_typed_error_and_follows_policy(db, choice_server, policy):
    _engine, maker = db
    run = _submit_model_run(maker, provider=_provider(
        "typesafe_jev", f"http://127.0.0.1:{choice_server.server_port}",
    ), failure_policy=policy)
    with maker() as session:
        session.execute(update(LLMProvider).values(encrypted_api_key="not-valid-fernet-token"))
        session.commit()
    ChoiceProviderHandler.calls = 0
    if policy == "REVIEW":
        completed, error = _execute(maker, run.id)
        assert error is None and completed.status == "SUCCEEDED"
        pair = completed.result_json["pairs"][0]
        assert "credential_unavailable" in pair["model_error"]
        assert pair["recommendation"] is None
        with maker() as session:
            row = session.scalar(select(DecisionModelCall).where(DecisionModelCall.run_id == run.id))
            assert row.error_code == "credential_unavailable" and row.retryable is False
    else:
        failed, error = _execute(maker, run.id)
        assert error is not None
        assert failed.status == "FAILED" and "credential_unavailable" in failed.error_message
    assert ChoiceProviderHandler.calls == 0


def test_unknown_provider_kind_is_an_explicit_invalid_provider_error(db):
    _engine, maker = db
    run = _submit_model_run(maker, provider=LLMProvider(
        workspace_id="workspace", provider_key="mystery", name="Mystery provider",
        base_url="http://127.0.0.1:9/v1", default_model="mystery-model",
        provider_kind="bogus",
    ))
    completed, error = _execute(maker, run.id)
    assert error is None and completed.status == "SUCCEEDED"
    pair = completed.result_json["pairs"][0]
    assert "invalid_provider" in pair["model_error"]
    assert pair["recommendation"] is None
    with maker() as session:
        row = session.scalar(select(DecisionModelCall).where(DecisionModelCall.run_id == run.id))
        assert row.error_code == "invalid_provider" and row.retryable is False


def test_provider_test_endpoint_returns_ok_for_a_choice_suggestion(public_api, choice_server):
    client, _maker, _identity = public_api
    provider = client.post("/api/v1/providers", json={
        "provider_key": "perplexity", "name": "Perplexity controlled",
        "provider_kind": "perplexity_decisions",
        "base_url": f"http://127.0.0.1:{choice_server.server_port}",
        "api_key": "test-secret-key", "send_fields": ["name"], "prompt_version": "pair-v7",
    }).json()
    result = client.post(f"/api/v1/providers/{provider['id']}/test").json()
    assert result["status"] == "ok"
    assert result["verdict"] == "SAME"
    assert result["confidence"] == 0.9
    assert result["model"] == "choice-response-model"
    assert result["provider_kind"] == "perplexity_decisions"
    assert result["prompt_version"] == "pair-v7"
    body = ChoiceProviderHandler.bodies[-1]
    assert body["state"]["left"] == {"name": "Acme"}
    assert body["state"]["right"] == {"name": "Acme"}
    assert "test-secret-key" not in json.dumps(body)


def test_provider_test_endpoint_errors_for_a_missing_key(public_api, choice_server):
    client, _maker, _identity = public_api
    provider = client.post("/api/v1/providers", json={
        "provider_key": "typesafe", "name": "Jev controlled",
        "provider_kind": "typesafe_jev",
        "base_url": f"http://127.0.0.1:{choice_server.server_port}",
        "api_key": "", "send_fields": ["name"],
    }).json()
    ChoiceProviderHandler.calls = 0
    result = client.post(f"/api/v1/providers/{provider['id']}/test").json()
    assert result["status"] == "error"
    assert "missing_credentials" in result["message"]
    assert ChoiceProviderHandler.calls == 0


def test_provider_test_endpoint_reports_credential_decrypt_failure(public_api, choice_server):
    client, maker, _identity = public_api
    provider = client.post("/api/v1/providers", json={
        "provider_key": "perplexity", "name": "Perplexity controlled",
        "provider_kind": "perplexity_decisions",
        "base_url": f"http://127.0.0.1:{choice_server.server_port}",
        "api_key": "test-secret-key", "send_fields": ["name"],
    }).json()
    with maker() as db:
        db.execute(update(LLMProvider).values(encrypted_api_key="not-valid-fernet-token"))
        db.commit()
    ChoiceProviderHandler.calls = 0
    result = client.post(f"/api/v1/providers/{provider['id']}/test").json()
    assert result["status"] == "error"
    assert "credential_unavailable" in result["message"]
    assert ChoiceProviderHandler.calls == 0


def test_provider_test_endpoint_never_reports_success_without_a_typed_suggestion(public_api, monkeypatch):
    import app.api.v1.providers_jobs as providers_jobs

    client, _maker, _identity = public_api
    provider = client.post("/api/v1/providers", json={
        "provider_key": "perplexity", "name": "Perplexity",
        "provider_kind": "perplexity_decisions", "api_key": "test-secret-key", "send_fields": ["name"],
    }).json()

    class DisabledAdapter:
        def invoke(self, request, config):
            return None

    monkeypatch.setattr(providers_jobs, "adapter_for_kind", lambda kind: DisabledAdapter())
    result = client.post(f"/api/v1/providers/{provider['id']}/test").json()
    assert result["status"] == "error"
    assert "disabled" in result["message"]
