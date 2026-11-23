"""Opt-in checks against real local model and Phoenix services."""
import os
from pathlib import Path
import sys
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.llm_client import JudgeRequest, ProviderConfig, judge_pair
from app.services.unification.telemetry import configure_tracing, flush_traces, shutdown_tracing, start_span


@pytest.mark.skipif(os.getenv("RUN_LOCAL_MODEL_INTEGRATION") != "true", reason="requires an explicitly enabled local model")
def test_local_private_model_returns_a_typed_suggestion():
    request = JudgeRequest(
        project_id="integration-project",
        run_id="integration-run",
        input_version="integration-input",
        scheme_revision="integration-scheme",
        prompt_version="pair-v1",
        entity_type="organization",
        left={"name": "Acme Corporation", "tax_id": "TEST-1"},
        right={"name": "Acme Corp.", "tax_id": "TEST-1"},
    )
    result = judge_pair(request, ProviderConfig(
        provider_key="ollama",
        model=os.getenv("LOCAL_MODEL_NAME", "qwen3.5:4b-local"),
        base_url=os.getenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1"),
        timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
    ))
    assert result.status == "SUGGESTION", result.error
    assert isinstance(result.suggestion, bool)
    assert result.model
    assert result.cost_usd is None
    if os.getenv("PHOENIX_ENABLED") == "true":
        from phoenix.client import Client

        assert flush_traces(5000)
        client = Client(base_url=os.getenv("PHOENIX_ENDPOINT", "http://127.0.0.1:6006"))
        deadline = time.monotonic() + 10
        trace = None
        while time.monotonic() < deadline and trace is None:
            traces = client.traces.get_traces(
                project_identifier=os.getenv("OTEL_SERVICE_NAME", "open-semantic-unifier"),
                include_spans=True,
                limit=100,
            )
            trace = next((item for item in traces if item["trace_id"] == result.trace_id), None)
            if trace is None:
                time.sleep(0.25)
        assert trace is not None
        assert any(span["name"] == "unification.model_judge" for span in trace["spans"])


@pytest.mark.skipif(os.getenv("PHOENIX_ENABLED") != "true", reason="requires a real Phoenix collector")
def test_phoenix_query_returns_the_emitted_trace_and_spans():
    from phoenix.client import Client

    configure_tracing()
    trace_id = None
    span_name = f"integration.phoenix.{uuid.uuid4().hex}"
    try:
        with start_span(span_name) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
        assert flush_traces(5000)
        collector_endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")
        phoenix_endpoint = os.getenv("PHOENIX_ENDPOINT", collector_endpoint.removesuffix("/v1/traces"))
        client = Client(base_url=phoenix_endpoint)
        match = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and match is None:
            traces = client.traces.get_traces(
                project_identifier=os.getenv("OTEL_SERVICE_NAME", "open-semantic-unifier"),
                include_spans=True,
                limit=100,
            )
            match = next((trace for trace in traces if trace["trace_id"] == trace_id), None)
            if match is None:
                time.sleep(0.25)
        assert match is not None
        assert any(span["name"] == span_name for span in match["spans"])
    finally:
        shutdown_tracing()
