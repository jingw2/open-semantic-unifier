"""OpenAI-compatible local/private model boundary tests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import time
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import llm_client


class _OpenAIHandler(BaseHTTPRequestHandler):
    behavior = "valid"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length))
        if self.behavior == "timeout":
            time.sleep(0.2)
        if self.behavior == "provider_error":
            body = b'{"error":{"message":"Invalid API key test-secret-must-not-appear-in-trace"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        content = "not json" if self.behavior == "invalid_json" else '{"is_same_entity":false,"confidence":0.2,"reason":"different identifiers"}'
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _request_and_config(server, *, behavior, timeout=0.05, api_key="test-secret"):
    _OpenAIHandler.behavior = behavior
    module = llm_client
    assert getattr(module, "ProviderConfig", None) is not None
    assert getattr(module, "JudgeRequest", None) is not None
    assert callable(getattr(module, "judge_pair", None))
    request = module.JudgeRequest(
        project_id="project", run_id="run", input_version="input-v1",
        scheme_revision="scheme-v1", prompt_version="pair-v1", entity_type="organization",
        left={"name": "Acme"}, right={"name": "Acme Ltd"},
    )
    config = module.ProviderConfig(
        provider_key="local-test", model="test-model",
        base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key=api_key,
        timeout_seconds=timeout,
    )
    return module.judge_pair(request, config)


def test_timeout_returns_explicit_model_error(openai_compatible_server):
    result = _request_and_config(openai_compatible_server, behavior="timeout")
    assert result.status == "ERROR"
    assert result.error.code == "timeout"
    assert result.suggestion is None


def test_invalid_json_returns_explicit_model_error(openai_compatible_server):
    result = _request_and_config(openai_compatible_server, behavior="invalid_json", timeout=1)
    assert result.status == "ERROR"
    assert result.error.code == "invalid_json"
    assert result.suggestion is None


def test_missing_required_credentials_is_explicit():
    module = llm_client
    request = module.JudgeRequest(
        project_id="project", run_id="run", input_version="input-v1",
        scheme_revision="scheme-v1", prompt_version="pair-v1", entity_type="organization",
        left={"name": "Acme"}, right={"name": "Acme Ltd"},
    )
    result = module.judge_pair(request, module.ProviderConfig(
        provider_key="private-openai-compatible", model="private-model",
        base_url="http://127.0.0.1:11434/v1", api_key="",
    ))
    assert result.status == "ERROR"
    assert result.error.code == "missing_credentials"


def test_successful_model_trace_never_contains_provider_credential(openai_compatible_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    secret = "model-secret-must-not-appear-in-trace"
    try:
        result = _request_and_config(openai_compatible_server, behavior="valid", timeout=1, api_key=secret)
        assert result.status == "SUGGESTION"
        assert result.suggestion is False
        assert result.cost_usd is None
        assert result.trace_id is not None
        assert provider.force_flush()
        spans = exporter.get_finished_spans()
        trace_dump = repr([(span.name, dict(span.attributes), span.events, span.status) for span in spans])
        assert secret not in trace_dump
        model_span = next(span for span in spans if span.name == "unification.model_judge")
        assert model_span.attributes["openinference.span.kind"] == "LLM"
        assert "llm.request.duration_ms" in model_span.attributes
    finally:
        shutdown_tracing()


def test_provider_error_trace_redacts_response_secrets(openai_compatible_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    secret = "test-secret-must-not-appear-in-trace"
    try:
        result = _request_and_config(openai_compatible_server, behavior="provider_error", timeout=1, api_key=secret)
        assert result.status == "ERROR"
        assert provider.force_flush()
        spans = " ".join(span.to_json() for span in exporter.get_finished_spans())
        assert secret not in spans
    finally:
        shutdown_tracing()

