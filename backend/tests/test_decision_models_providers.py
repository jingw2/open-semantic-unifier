"""Perplexity Decisions and TypeSafe Jev provider adapter tests.

Every test runs against a controlled ThreadingHTTPServer on 127.0.0.1 or a
monkeypatched httpx.post; no test touches an external network.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import llm_client
from app.services.decision_models.adapters import DecisionAdapter, DecisionCapabilities
from app.services.decision_models.contracts import (
    DecisionError,
    DecisionRequest,
    DecisionSuggestion,
    DecisionVerdict,
)
from app.services.decision_models.providers import (
    PerplexityDecisionsAdapter,
    TypeSafeJevAdapter,
)

PROVIDERS = [
    pytest.param({
        "adapter": PerplexityDecisionsAdapter,
        "kind": "perplexity_decisions",
        "path": "/v1/decisions",
        "default_model": "pplx-decider-flash-latest",
        "default_base_url": "https://api.perplexity.ai",
    }, id="perplexity"),
    pytest.param({
        "adapter": TypeSafeJevAdapter,
        "kind": "typesafe_jev",
        "path": "/v1/systemone",
        "default_model": "jev-latest",
        "default_base_url": "https://api.typesafe.ai",
    }, id="typesafe"),
]


def _answer(**overrides):
    answer = {
        "type": "choice",
        "choice": "SAME",
        "confidence": 0.9,
        "probabilities": {"SAME": 0.8, "DIFFERENT": 0.15, "UNKNOWN": 0.05},
    }
    answer.update(overrides)
    return answer


_DEFAULT_USAGE = {"input_tokens": 120, "output_tokens": 3}


def _payload(answers=None, model="response-model", usage=_DEFAULT_USAGE):
    payload = {"model": model, "answers": answers if answers is not None else {"entity_match": _answer()}}
    if usage is not None:
        payload["usage"] = usage
    return payload


INVALID_PAYLOADS = [
    {"model": "response-model"},  # answers missing
    {"model": "response-model", "answers": []},  # answers not a mapping
    {"model": "response-model", "answers": {"entity_score": _answer()}},  # entity_match missing
    {"model": "response-model", "answers": {"entity_match": "choice"}},  # answer not a mapping
    {"model": "response-model", "answers": {"entity_match": _answer(type="noul")}},  # wrong answer type
    {"model": "response-model", "answers": {"entity_match": _answer(type=None)}},  # answer type missing
    {"model": "response-model", "answers": {"entity_match": _answer(choice="MAYBE")}},  # unknown choice
    {"model": "response-model", "answers": {"entity_match": _answer(choice=None)}},  # choice missing
    {"model": "response-model", "answers": {"entity_match": _answer(confidence=2.0)}},
    {"model": "response-model", "answers": {"entity_match": _answer(confidence="0.9")}},
    {"model": "response-model", "answers": {"entity_match": _answer(confidence=True)}},
    {"model": "response-model", "answers": {"entity_match": _answer(probabilities=None)}},  # probabilities missing
    {"model": "response-model", "answers": {"entity_match": _answer(probabilities={})}},  # probabilities empty
    {"model": "response-model", "answers": {"entity_match": _answer(
        probabilities={"SAME": 0.5, "DIFFERENT": 0.4, "MAYBE": 0.1})}},  # unknown probability key
    {"model": "response-model", "answers": {"entity_match": _answer(
        probabilities={"SAME": 0.5, "DIFFERENT": 0.5, "UNKNOWN": 0.5})}},  # sum far from 1
]


class _ControlledHandler(BaseHTTPRequestHandler):
    status = 200
    payload = _payload()
    raw_body = None
    delay = 0.0
    response_headers: dict[str, str] = {}
    requests: list[dict] = []
    request_headers: list[dict] = []
    request_paths: list[str] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).requests.append(json.loads(self.rfile.read(length)))
        type(self).request_headers.append({name.lower(): value for name, value in self.headers.items()})
        type(self).request_paths.append(self.path)
        if type(self).delay:
            time.sleep(type(self).delay)
        body = type(self).raw_body
        if body is None:
            body = json.dumps(type(self).payload).encode()
        self.send_response(type(self).status)
        for name, value in type(self).response_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture(params=PROVIDERS)
def provider(request):
    spec = request.param
    return spec["adapter"](), spec


@pytest.fixture
def choice_server():
    _ControlledHandler.status = 200
    _ControlledHandler.payload = _payload()
    _ControlledHandler.raw_body = None
    _ControlledHandler.delay = 0.0
    _ControlledHandler.response_headers = {}
    _ControlledHandler.requests = []
    _ControlledHandler.request_headers = []
    _ControlledHandler.request_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlledHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _request(**overrides):
    values = dict(
        project_id="project",
        run_id="run",
        input_version="input-v1",
        scheme_revision="scheme-v1",
        prompt_version="pair-v1",
        entity_type="organization",
        identity_granularity="name_and_strong_identifier",
        left={"name": "Acme", "tax_id": "TAX-SECRET-1"},
        right={"name": "Acme Ltd", "tax_id": "TAX-SECRET-2"},
        field_names=("name",),
        evidence=({"rule": "exact-name", "match": True},),
    )
    values.update(overrides)
    return DecisionRequest(**values)


def _config(server, *, model="", base_url=None, api_key="test-secret",
            timeout=1.0, provider_key="test-provider"):
    if base_url is None:
        base_url = f"http://127.0.0.1:{server.server_port}" if server is not None else ""
    return llm_client.ProviderConfig(
        provider_key=provider_key,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout,
    )


def _invoke(adapter, server, *, model="", base_url=None, api_key="test-secret",
            timeout=1.0, provider_key="test-provider", **request_overrides):
    return adapter.invoke(
        _request(**request_overrides),
        _config(server, model=model, base_url=base_url, api_key=api_key,
                timeout=timeout, provider_key=provider_key),
    )


def test_capabilities_describe_choice_providers(provider):
    adapter, spec = provider
    assert isinstance(adapter, DecisionAdapter)
    assert adapter.provider_kind == spec["kind"]
    capabilities = adapter.capabilities()
    assert isinstance(capabilities, DecisionCapabilities)
    assert capabilities.verdicts == frozenset({DecisionVerdict.SAME, DecisionVerdict.DIFFERENT, DecisionVerdict.UNKNOWN})
    assert capabilities.distribution is True
    assert capabilities.explanation is False
    assert capabilities.usage is True


def test_post_hits_the_provider_decision_path(provider, choice_server):
    adapter, spec = provider
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionSuggestion)
    assert _ControlledHandler.request_paths[-1] == spec["path"]


def test_model_defaults_when_blank_and_explicit_model_wins(provider, choice_server):
    adapter, spec = provider
    _invoke(adapter, choice_server, model="")
    assert _ControlledHandler.requests[-1]["model"] == spec["default_model"]
    _invoke(adapter, choice_server, model="explicit-model")
    assert _ControlledHandler.requests[-1]["model"] == "explicit-model"


def test_authorization_is_bearer_and_x_api_key_absent(provider, choice_server):
    adapter, spec = provider
    _invoke(adapter, choice_server, api_key="test-api-key")
    headers = _ControlledHandler.request_headers[-1]
    assert headers.get("authorization") == "Bearer test-api-key"
    assert "x-api-key" not in headers
    assert headers.get("content-type") == "application/json"


def test_request_state_sends_only_configured_fields(provider, choice_server):
    adapter, spec = provider
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionSuggestion)
    body = _ControlledHandler.requests[-1]
    state = body["state"]
    assert state["entity_type"] == "organization"
    assert state["identity_granularity"] == "name_and_strong_identifier"
    assert state["left"] == {"name": "Acme"}
    assert state["right"] == {"name": "Acme Ltd"}
    assert state["evidence"] == [{"rule": "exact-name", "match": True}]
    serialized = json.dumps(body)
    assert "TAX-SECRET-1" not in serialized
    assert "TAX-SECRET-2" not in serialized
    for key in ("project_id", "run_id", "input_version", "scheme_revision", "api_key"):
        assert key not in serialized
    question = body["questions"]["entity_match"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"SAME", "DIFFERENT", "UNKNOWN"}
    assert question["instructions"]


def test_empty_field_names_send_empty_records(provider, choice_server):
    adapter, spec = provider
    outcome = _invoke(adapter, choice_server, field_names=())
    assert isinstance(outcome, DecisionSuggestion)
    state = _ControlledHandler.requests[-1]["state"]
    assert state["left"] == {}
    assert state["right"] == {}


@pytest.mark.parametrize("choice", ["SAME", "DIFFERENT", "UNKNOWN"])
def test_choices_map_to_verdicts_with_full_metadata(provider, choice_server, choice):
    adapter, spec = provider
    _ControlledHandler.payload = _payload(answers={"entity_match": _answer(choice=choice)})
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionSuggestion)
    assert outcome.verdict == DecisionVerdict(choice)
    assert outcome.confidence == 0.9
    assert outcome.distribution == {
        DecisionVerdict.SAME: 0.8,
        DecisionVerdict.DIFFERENT: 0.15,
        DecisionVerdict.UNKNOWN: 0.05,
    }
    assert outcome.explanation is None
    assert outcome.provider_kind == spec["kind"]
    assert outcome.provider_key == "test-provider"
    assert outcome.model_version == "response-model"
    assert outcome.prompt_version == "pair-v1"
    assert outcome.latency_ms >= 0
    assert outcome.usage == {"input_tokens": 120, "output_tokens": 3}
    assert outcome.cost_usd is None
    assert outcome.trace_id is None


def test_missing_usage_stays_none_and_cost_never_estimated(provider, choice_server):
    adapter, spec = provider
    _ControlledHandler.payload = _payload(usage=None)
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionSuggestion)
    assert outcome.usage is None
    assert outcome.cost_usd is None


def test_suggestion_records_the_requests_prompt_version(provider, choice_server):
    adapter, spec = provider
    outcome = _invoke(adapter, choice_server, prompt_version="configured-pair-v7")
    assert isinstance(outcome, DecisionSuggestion)
    assert outcome.prompt_version == "configured-pair-v7"


def test_errors_record_the_requests_prompt_version(provider, choice_server):
    adapter, spec = provider
    _ControlledHandler.status = 500
    _ControlledHandler.payload = {"error": {"message": "provider failure"}}
    outcome = _invoke(adapter, choice_server, prompt_version="configured-pair-v8")
    assert isinstance(outcome, DecisionError)
    assert outcome.prompt_version == "configured-pair-v8"


def test_missing_credentials_error_without_http_request(provider, choice_server):
    adapter, spec = provider
    outcome = _invoke(adapter, choice_server, api_key="  ")
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "missing_credentials"
    assert outcome.retryable is False
    assert _ControlledHandler.requests == []


@pytest.mark.parametrize("status, expected_code, retryable", [
    (401, "invalid_credentials", False),
    (403, "invalid_credentials", False),
    (422, "invalid_request", False),
    (429, "rate_limited", True),
    (500, "provider_error", True),
    (418, "provider_error", False),
])
def test_http_errors_map_to_typed_decision_errors(provider, choice_server, status, expected_code, retryable):
    adapter, spec = provider
    _ControlledHandler.status = status
    _ControlledHandler.payload = {"model": "error-response-model", "error": {"message": "decision-body-secret-must-not-appear"}}
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == expected_code
    assert outcome.retryable is retryable
    assert outcome.model_version == "error-response-model"
    assert outcome.prompt_version == "pair-v1"
    assert "decision-body-secret-must-not-appear" not in outcome.message
    assert "test-secret" not in outcome.message


def test_typesafe_529_maps_to_retryable_provider_error(choice_server):
    adapter = TypeSafeJevAdapter()
    _ControlledHandler.status = 529
    _ControlledHandler.payload = {"error": {"message": "provider unavailable"}}
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "provider_error"
    assert outcome.retryable is True


def test_timeout_maps_to_retryable_timeout_error(provider, choice_server):
    adapter, spec = provider
    _ControlledHandler.delay = 0.3
    outcome = _invoke(adapter, choice_server, timeout=0.05)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "timeout"
    assert outcome.retryable is True


def test_invalid_json_maps_to_invalid_json_error(provider, choice_server):
    adapter, spec = provider
    _ControlledHandler.raw_body = b"not json"
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "invalid_json"
    assert outcome.retryable is False


@pytest.mark.parametrize("payload", INVALID_PAYLOADS)
def test_invalid_payload_maps_to_invalid_response(provider, choice_server, payload):
    adapter, spec = provider
    _ControlledHandler.payload = payload
    outcome = _invoke(adapter, choice_server)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "invalid_response"
    assert outcome.retryable is False
    assert outcome.model_version == "response-model"


def test_base_url_normalization_never_hits_the_network(provider, monkeypatch):
    adapter, spec = provider
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return httpx.Response(200, json=_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    cases = ["", spec["default_base_url"], spec["default_base_url"] + "/",
             spec["default_base_url"] + "/v1", spec["default_base_url"] + spec["path"],
             spec["default_base_url"] + spec["path"] + "/"]
    for base_url in cases:
        outcome = _invoke(adapter, None, base_url=base_url, model="")
        assert isinstance(outcome, DecisionSuggestion)
        assert captured["url"] == spec["default_base_url"] + spec["path"]
    assert captured["json"]["model"] == spec["default_model"]


def test_timeout_uses_configured_seconds_with_minimum(provider, monkeypatch):
    adapter, spec = provider
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return httpx.Response(200, json=_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    _invoke(adapter, None, timeout=0)
    assert captured["timeout"] == 10.0
    _invoke(adapter, None, timeout=-3)
    assert captured["timeout"] == 10.0
    _invoke(adapter, None, timeout=7)
    assert captured["timeout"] == 7.0


def test_unexpected_exception_maps_to_adapter_error(provider, monkeypatch):
    adapter, spec = provider

    def boom(url, **kwargs):
        raise RuntimeError("boom-details-must-not-leak")

    monkeypatch.setattr(httpx, "post", boom)
    outcome = _invoke(adapter, None)
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "adapter_error"
    assert outcome.retryable is False
    assert "RuntimeError" in outcome.message
    assert "boom-details-must-not-leak" not in outcome.message
    assert "test-secret" not in outcome.message


def test_adapter_error_falls_back_to_provider_kind_when_key_blank(provider, monkeypatch):
    adapter, spec = provider

    def boom(url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "post", boom)
    outcome = _invoke(adapter, None, provider_key="")
    assert isinstance(outcome, DecisionError)
    assert outcome.code == "adapter_error"
    assert outcome.provider_key == spec["kind"]


def test_successful_span_carries_metadata_without_records_or_credentials(provider, choice_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    adapter, spec = provider
    exporter = InMemorySpanExporter()
    tracing = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    secret = "provider-api-key-must-not-appear"
    try:
        outcome = _invoke(adapter, choice_server, api_key=secret)
        assert isinstance(outcome, DecisionSuggestion)
        assert outcome.trace_id is not None
        assert tracing.force_flush()
        spans = exporter.get_finished_spans()
        model_span = next(span for span in spans if span.name == "unification.model_judge")
        attributes = dict(model_span.attributes)
        assert attributes["openinference.span.kind"] == "LLM"
        assert attributes["llm.provider"] == "test-provider"
        assert attributes["llm.provider_kind"] == spec["kind"]
        assert attributes["llm.model_name"] == spec["default_model"]
        assert attributes["prompt_version"] == "pair-v1"
        assert attributes["scheme.prompt_version"] == "pair-v1"
        assert attributes["decision.input_fields"] == "name"
        assert "llm.request.duration_ms" in attributes
        trace_dump = repr([(span.name, dict(span.attributes), span.status) for span in spans])
        assert secret not in trace_dump
        assert "TAX-SECRET-1" not in trace_dump
        assert "Acme" not in trace_dump
    finally:
        shutdown_tracing()


def test_error_span_records_error_code_without_secrets(provider, choice_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    adapter, spec = provider
    _ControlledHandler.status = 500
    _ControlledHandler.payload = {"error": {"message": "provider-body-secret-must-not-appear"}}
    exporter = InMemorySpanExporter()
    tracing = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    secret = "provider-api-key-must-not-appear"
    try:
        outcome = _invoke(adapter, choice_server, api_key=secret)
        assert isinstance(outcome, DecisionError)
        assert outcome.code == "provider_error"
        assert secret not in outcome.message
        assert "provider-body-secret-must-not-appear" not in outcome.message
        assert tracing.force_flush()
        spans = exporter.get_finished_spans()
        model_span = next(span for span in spans if span.name == "unification.model_judge")
        assert model_span.attributes["error.code"] == "provider_error"
        dump = " ".join(span.to_json() for span in spans)
        assert secret not in dump
        assert "provider-body-secret-must-not-appear" not in dump
        assert "TAX-SECRET-1" not in dump
    finally:
        shutdown_tracing()


def test_provider_request_id_captured_into_span(provider, choice_server):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

    adapter, spec = provider
    _ControlledHandler.response_headers = {"x-typesafe-request-id": "ts-9", "x-request-id": "req-123"}
    exporter = InMemorySpanExporter()
    tracing = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    try:
        outcome = _invoke(adapter, choice_server)
        assert isinstance(outcome, DecisionSuggestion)
        assert tracing.force_flush()
        model_span = next(
            span for span in exporter.get_finished_spans() if span.name == "unification.model_judge"
        )
        assert model_span.attributes["provider.request_id"] == "ts-9"
    finally:
        shutdown_tracing()
