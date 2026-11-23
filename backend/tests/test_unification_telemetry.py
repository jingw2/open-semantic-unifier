"""Trace context propagation and best-effort collector behavior."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import time
from unittest.mock import patch

from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.services.unification import telemetry


class _FailedCollector(BaseHTTPRequestHandler):
    received = Event()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.received.set()
        self.send_response(503)
        self.end_headers()

    def log_message(self, *_args):
        pass


def test_worker_trace_uses_propagated_parent_and_keeps_run_context():
    exporter = InMemorySpanExporter()
    provider = telemetry.configure_tracing(
        telemetry.TraceConfig(enabled=True, endpoint="http://127.0.0.1:6006/v1/traces"),
        span_exporter=exporter,
    )
    with telemetry.start_span("api.submit", {"project_id": "project", "run_id": "run"}) as parent:
        headers = telemetry.inject_trace_context()
    with telemetry.attach_trace_context(headers):
        with telemetry.start_span("worker.run", {"input_version": "input-v1", "scheme_revision": "scheme-v1", "prompt_version": "pair-v1"}) as child:
            child_trace_id = telemetry.current_trace_id()
    assert provider.force_flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["worker.run"].context.trace_id == parent.get_span_context().trace_id
    assert spans["worker.run"].parent.span_id == parent.get_span_context().span_id
    assert child_trace_id == f"{parent.get_span_context().trace_id:032x}"
    assert dict(spans["worker.run"].attributes)["input_version"] == "input-v1"
    telemetry.shutdown_tracing()


def test_disabling_tracing_stops_an_existing_exporter():
    exporter = InMemorySpanExporter()
    provider = telemetry.configure_tracing(
        telemetry.TraceConfig(enabled=True), span_exporter=exporter,
    )
    telemetry.configure_tracing(telemetry.TraceConfig(enabled=False))
    with telemetry.start_span("business.after_disable"):
        pass
    provider.force_flush()
    assert not exporter.get_finished_spans()
    telemetry.shutdown_tracing()


def test_collector_failure_does_not_block_business():
    telemetry.shutdown_tracing()
    _FailedCollector.received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailedCollector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        telemetry.configure_tracing(telemetry.TraceConfig(
            enabled=True,
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
            export_timeout_seconds=0.2,
        ))
        started = time.monotonic()
        with telemetry.start_span("business.operation"):
            outcome = "complete"
        duration = time.monotonic() - started
        assert telemetry.flush_traces(5000)
        assert _FailedCollector.received.wait(timeout=5)
        assert outcome == "complete"
        assert duration < 0.1
    finally:
        telemetry.shutdown_tracing()
        server.shutdown()
        thread.join(timeout=2)


def test_exporter_failure_is_reported_synchronously():
    class FailedExporter:
        def export(self, _spans):
            return SpanExportResult.FAILURE

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    with patch.object(telemetry.logger, "error") as log_error:
        telemetry._LoggingExporter(FailedExporter()).export([])
    log_error.assert_called_once()
    assert "trace exporter reported failure" in log_error.call_args.args[0]

