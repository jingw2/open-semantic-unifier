"""HTTP run submission enters the trace-aware worker dispatch path."""
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.main import app
from app.models.entities import Project, Workspace
from app.services.unification.schemes import MatchScheme, save_scheme
from app.services.unification.telemetry import TraceConfig, configure_tracing, current_trace_id, shutdown_tracing


def test_http_run_submission_dispatches_under_api_trace(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run-api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="api-ws", name="API Workspace", slug="api-ws"))
        db.flush()
        db.add(Project(id="api-project", workspace_id="api-ws", name="API Project", slug="api-project"))
        db.commit()
        scheme = save_scheme(db, project_id="api-project", scheme_key="org", scheme=MatchScheme(
            entity_type="organization", identity_granularity="legal_entity", field_map={"name": "name"},
        ))

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "api-user", "workspace_id": "api-ws", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None
    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    dispatch_contexts = []
    try:
        with patch("app.api.v1.unification.dispatch_unification_run", side_effect=lambda _run_id: dispatch_contexts.append(current_trace_id())):
            response = TestClient(app).post(
                "/api/v1/projects/api-project/unification/runs",
                headers={"Idempotency-Key": "http-api-run-1"},
                json={"scheme_key": "org", "scheme_revision": scheme.revision, "input_version": "api-input-v1"},
            )
        assert response.status_code == 202, response.text
        assert len(dispatch_contexts) == 1
        assert dispatch_contexts[0]
        assert response.json()["status"] == "PENDING"
        assert provider.force_flush()
        api_span = next(span for span in exporter.get_finished_spans() if span.name == "unification.api_submit")
        assert f"{api_span.context.trace_id:032x}" == dispatch_contexts[0]
    finally:
        app.dependency_overrides.clear()
        shutdown_tracing()
        engine.dispose()
