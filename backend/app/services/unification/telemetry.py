"""Optional OpenTelemetry tracing with best-effort OTLP export to Phoenix."""
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import threading
from typing import Iterator

from opentelemetry import context as otel_context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from app.core.config import settings

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_provider: TracerProvider | None = None
_config: "TraceConfig | None" = None


@dataclass(frozen=True, slots=True)
class TraceConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:6006/v1/traces"
    service_name: str = "open-semantic-unifier"
    export_timeout_seconds: float = 1.0


class _LoggingExporter(SpanExporter):
    def __init__(self, exporter: SpanExporter):
        self._exporter = exporter

    def export(self, spans):
        try:
            result = self._exporter.export(spans)
        except Exception:
            logger.exception("trace export failed; continuing business operation")
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            logger.error("trace exporter reported failure; continuing business operation")
        return result

    def shutdown(self):
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self._exporter.force_flush(timeout_millis)


def configure_tracing(config: TraceConfig | None = None, *, span_exporter: SpanExporter | None = None) -> TracerProvider | None:
    """Configure a private tracer provider; disabled/unavailable tracing is a no-op."""
    global _provider, _config
    if config is None:
        config = TraceConfig(
            enabled=settings.phoenix_enabled,
            endpoint=settings.phoenix_collector_endpoint,
            service_name=settings.otel_service_name,
            export_timeout_seconds=settings.otel_export_timeout_seconds,
        )
    if not config.enabled:
        with _lock:
            if _provider is not None:
                _provider.shutdown()
            _provider, _config = None, config
        return None
    if not config.endpoint and span_exporter is None:
        logger.error("tracing is enabled but PHOENIX_COLLECTOR_ENDPOINT is empty")
        return None
    with _lock:
        if _provider is not None and _config == config and span_exporter is None:
            return _provider
        if _provider is not None:
            _provider.shutdown()
        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            span_exporter = OTLPSpanExporter(
                endpoint=config.endpoint,
                timeout=max(0.1, config.export_timeout_seconds),
            )
        provider = TracerProvider(resource=Resource.create({
            "service.name": config.service_name,
            "openinference.project.name": config.service_name,
        }))
        provider.add_span_processor(BatchSpanProcessor(
            _LoggingExporter(span_exporter),
            schedule_delay_millis=200,
            export_timeout_millis=max(100, int(config.export_timeout_seconds * 1000)),
        ))
        _provider, _config = provider, config
        return provider


def tracer():
    if _config is not None and not _config.enabled:
        return trace.get_tracer("open_semantic_unifier")
    provider = _provider or configure_tracing()
    return provider.get_tracer("open_semantic_unifier") if provider else trace.get_tracer("open_semantic_unifier")


@contextmanager
def start_span(name: str, attributes: dict[str, str | int | float | bool] | None = None) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name, attributes=attributes or {}) as span:
        yield span


def current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    return f"{span_context.trace_id:032x}" if span_context.is_valid else None


def inject_trace_context(headers: dict[str, str] | None = None) -> dict[str, str]:
    carrier = dict(headers or {})
    propagate.inject(carrier)
    return carrier


@contextmanager
def attach_trace_context(headers: dict[str, str] | None) -> Iterator[None]:
    context = propagate.extract(headers or {})
    token = otel_context.attach(context)
    try:
        yield
    finally:
        otel_context.detach(token)


def flush_traces(timeout_millis: int = 1000) -> bool:
    provider = _provider
    return provider.force_flush(timeout_millis) if provider else True


def shutdown_tracing() -> None:
    global _provider, _config
    with _lock:
        if _provider is not None:
            _provider.shutdown()
        _provider, _config = None, None
