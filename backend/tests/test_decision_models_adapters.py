"""OpenAI-compatible decision adapter tests against a controlled HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import llm_client
from app.services.decision_models.adapters import (
    DecisionAdapter,
    DecisionCapabilities,
    OpenAICompatibleAdapter,
)
from app.services.decision_models.contracts import (
    DecisionError,
    DecisionRequest,
    DecisionSuggestion,
    DecisionVerdict,
)


class _ControlledHandler(BaseHTTPRequestHandler):
    behavior = "valid_false"
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).requests.append(json.loads(self.rfile.read(length)))
        if self.behavior == "timeout":
            time.sleep(0.2)
        if self.behavior == "provider_error":
            body = b'{"error":{"message":"Invalid API key decision-secret-must-not-appear"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.behavior == "invalid_json":
            content = "not json"
        else:
            content = (
                '{"is_same_entity":true,"confidence":0.9,"reason":"shared identifiers"}'
                if self.behavior == "valid_true"
                else '{"is_same_entity":false,"confidence":0.2,"reason":"different identifiers"}'
            )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def openai_compatible_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlledHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _invoke(server, *, behavior, left=None, right=None, field_names=("name",),
            timeout=0.05, api_key="test-secret", provider_key="local-test"):
    _ControlledHandler.requests.clear()
    _ControlledHandler.behavior = behavior
    adapter = OpenAICompatibleAdapter()
    request = DecisionRequest(
        project_id="project",
        run_id="run",
        input_version="input-v1",
        scheme_revision="scheme-v1",
        prompt_version="pair-v1",
        entity_type="organization",
        identity_granularity="name_and_strong_identifier",
        left=left if left is not None else {"name": "Acme"},
        right=right if right is not None else {"name": "Acme Ltd"},
        field_names=field_names,
    )
    config = llm_client.ProviderConfig(
        provider_key=provider_key,
        model="test-model",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key=api_key,
        timeout_seconds=timeout,
    )
    return adapter.invoke(request, config)


def test_capabilities_describe_boolean_openai_compatible_adapter():
    adapter = OpenAICompatibleAdapter()
    assert isinstance(adapter, DecisionAdapter)
    assert adapter.provider_kind == "openai_compatible"
    capabilities = adapter.capabilities()
    assert isinstance(capabilities, DecisionCapabilities)
    assert capabilities.verdicts == frozenset({DecisionVerdict.SAME, DecisionVerdict.DIFFERENT})
    assert DecisionVerdict.UNKNOWN not in capabilities.verdicts
    assert capabilities.distribution is False
    assert capabilities.explanation is True
    assert capabilities.usage is True


def test_valid_false_maps_to_different(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="valid_false")
    assert isinstance(outcome, DecisionSuggestion)
    assert outcome.verdict == DecisionVerdict.DIFFERENT
    assert outcome.confidence == 0.2
    assert outcome.explanation == "different identifiers"
    assert outcome.provider_kind == "openai_compatible"
    assert outcome.provider_key == "local-test"
    assert outcome.model_version == "test-model"
    assert outcome.prompt_version == "pair-v1"
    assert outcome.latency_ms >= 0
    assert outcome.distribution is None
    assert outcome.trace_id is None


def test_valid_true_maps_to_same(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="valid_true")
    assert isinstance(outcome, DecisionSuggestion)
    assert outcome.verdict == DecisionVerdict.SAME
    assert outcome.confidence == 0.9


def test_timeout_maps_to_retryable_error(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="timeout")
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "timeout"
    assert outcome.retryable is True
    assert outcome.message
    assert outcome.latency_ms >= 0


def test_invalid_json_maps_to_explicit_error(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="invalid_json", timeout=1)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "invalid_json"
    assert outcome.retryable is False


def test_missing_credentials_maps_to_explicit_error(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="valid_false", api_key="")
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "missing_credentials"
    assert _ControlledHandler.requests == []


def test_offline_provider_maps_to_disabled_none(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="valid_false",
                      provider_key="offline", api_key="")
    assert outcome is None
    assert _ControlledHandler.requests == []


def test_unexpected_exception_maps_to_adapter_error(openai_compatible_server, monkeypatch):
    def boom(_request, _config):
        raise RuntimeError("boom-details")
    monkeypatch.setattr(llm_client, "judge_pair", boom)
    outcome = _invoke(openai_compatible_server, behavior="valid_false")
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "adapter_error"
    assert outcome.retryable is False
    assert outcome.message
    assert "boom-details" not in outcome.message
    assert _ControlledHandler.requests == []


def test_suggestion_without_boolean_maps_to_unknown_defensively(openai_compatible_server, monkeypatch):
    def ambiguous(_request, _config):
        return llm_client.JudgeResult(
            status="SUGGESTION", suggestion=None, confidence=0.5,
            reason="ambiguous", model="test-model", trace_id=None,
        )
    monkeypatch.setattr(llm_client, "judge_pair", ambiguous)
    outcome = _invoke(openai_compatible_server, behavior="valid_false")
    assert isinstance(outcome, DecisionSuggestion)
    assert outcome.verdict == DecisionVerdict.UNKNOWN
    assert outcome.confidence == 0.5


def test_field_filtering_sends_only_configured_fields(openai_compatible_server):
    outcome = _invoke(
        openai_compatible_server,
        behavior="valid_false",
        left={"name": "Acme", "tax_id": "SECRET-TAX-ID"},
        right={"name": "Acme Ltd", "tax_id": "SECRET-TAX-ID-2"},
        field_names=("name",),
    )
    assert isinstance(outcome, DecisionSuggestion)
    prompt = _ControlledHandler.requests[0]["messages"][0]["content"]
    assert "Acme" in prompt
    assert "SECRET-TAX-ID" not in prompt
    assert '"tax_id"' not in prompt


def test_empty_field_names_sends_empty_records(openai_compatible_server):
    outcome = _invoke(openai_compatible_server, behavior="valid_false", field_names=())
    assert isinstance(outcome, DecisionSuggestion)
    prompt = _ControlledHandler.requests[0]["messages"][0]["content"]
    assert "Record A: {}" in prompt
    assert "Record B: {}" in prompt


def test_successful_trace_carries_trace_id_without_credentials(openai_compatible_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    secret = "decision-api-key-must-not-appear"
    try:
        outcome = _invoke(openai_compatible_server, behavior="valid_false", timeout=1, api_key=secret)
        assert isinstance(outcome, DecisionSuggestion)
        assert outcome.trace_id is not None
        assert provider.force_flush()
        spans = exporter.get_finished_spans()
        trace_dump = repr([(span.name, dict(span.attributes), span.events, span.status) for span in spans])
        assert secret not in trace_dump
        assert secret not in repr(outcome)
        model_span = next(span for span in spans if span.name == "unification.model_judge")
        assert model_span.attributes["openinference.span.kind"] == "LLM"
        assert "llm.request.duration_ms" in model_span.attributes
    finally:
        shutdown_tracing()


def test_provider_error_redacts_credentials_from_error_and_trace(openai_compatible_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    secret = "decision-api-key-must-not-appear"
    body_secret = "decision-secret-must-not-appear"
    try:
        outcome = _invoke(openai_compatible_server, behavior="provider_error", timeout=1, api_key=secret)
        assert isinstance(outcome, DecisionError)
        assert outcome.code == "provider_error"
        assert secret not in outcome.message
        assert body_secret not in outcome.message
        assert provider.force_flush()
        spans = " ".join(span.to_json() for span in exporter.get_finished_spans())
        assert secret not in spans
        assert body_secret not in spans
    finally:
        shutdown_tracing()
