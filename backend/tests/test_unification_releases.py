"""Idempotent run submission and immutable snapshot lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys
from threading import Barrier
from threading import Event, Thread
import time

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import Base
from app.models.entities import LLMProvider, Project, Workspace
from app.models.unification import IdentityChangeRecord, SourceRecord, SourceRevision, UnifiedEntity, UnificationRun, UnificationSchemeVersion, UnificationSnapshot
from app.services.unification.contracts import IngestMode, SourceRecordInput
from app.services.unification.ingest import ingest_batch
from app.services.unification.identity import DecisionInput, apply_decision
from app.services.unification.schemes import MatchScheme, save_scheme
from app.services.unification.releases import (
    RunConflict,
    complete_run,
    diff_snapshots,
    execute_unification_run,
    fail_run,
    get_current_snapshot,
    publish_snapshot,
    recover_interrupted_run,
    recover_stale_runs,
    rollback_snapshot,
    start_run,
    submit_run,
)
from app.services.unification.locking import lock_project_identity_state


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'releases.db'}", connect_args={"check_same_thread": False, "timeout": 30})
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


def submitted(maker, key="key-1", payload="hash-1"):
    with maker() as session:
        return submit_run(session, project_id="project", input_version="input-v1", scheme_revision="scheme-v1", idempotency_key=key, payload_hash=payload)


def test_same_key_same_payload_is_idempotent_and_changed_payload_conflicts(db):
    _engine, maker = db
    first = submitted(maker)
    retry = submitted(maker)
    assert first.id == retry.id
    with pytest.raises(RunConflict, match="different payload"):
        submitted(maker, payload="hash-2")


def test_concurrent_submit_has_one_effect(db):
    _engine, maker = db
    barrier = Barrier(2)
    def submit():
        with maker() as session:
            barrier.wait()
            return submit_run(session, project_id="project", input_version="input-v1", scheme_revision="scheme-v1", idempotency_key="shared", payload_hash="same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: submit(), range(2))
    with maker() as session:
        assert first.id == second.id
        assert session.scalar(select(func.count()).select_from(UnificationRun)) == 1


def test_running_or_failed_run_cannot_publish_partial_results(db):
    _engine, maker = db
    run = submitted(maker)
    with maker() as session:
        start_run(session, run.id)
        with pytest.raises(RunConflict, match="succeeded"):
            publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")
        fail_run(session, run.id, "worker stopped before staging a complete result", max_attempts=1)
        with pytest.raises(RunConflict, match="succeeded"):
            publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")


def test_snapshot_publish_is_atomic_immutable_and_optimistically_locked(db):
    _engine, maker = db
    run = submitted(maker)
    payload = {"entities": {"e1": {"name": "A"}}, "memberships": {"erp/1": "e1"}, "attributes": {"e1": {"name": "A"}}}
    with maker() as session:
        start_run(session, run.id)
        complete_run(session, run.id, payload)
        first = publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")
    with maker() as session:
        assert get_current_snapshot(session, "project").id == first.id
        second_run = submit_run(session, project_id="project", input_version="input-v2", scheme_revision="scheme-v1", idempotency_key="key-2", payload_hash="hash-2")
        start_run(session, second_run.id)
        complete_run(session, second_run.id, {**payload, "memberships": {"erp/1": "e1", "crm/9": "e2"}})
        with pytest.raises(RunConflict, match="current snapshot version"):
            publish_snapshot(session, run_id=second_run.id, expected_current_version=None, actor_id="publisher")
        second = publish_snapshot(session, run_id=second_run.id, expected_current_version=first.version, actor_id="publisher")
        old = session.get(UnificationSnapshot, first.id)
        assert old.snapshot_json == {**payload, "identity_revision": 1, "identity_changes": []}
        assert second.version == first.version + 1


def test_publication_locks_project_identity_before_materializing_snapshot(db, monkeypatch):
    _engine, maker = db
    run = submitted(maker)
    with maker() as session:
        start_run(session, run.id)
        complete_run(session, run.id, {"entities": {}, "memberships": {}, "attributes": {}})
        import app.services.unification.releases as releases
        events = []
        original_materialize = releases._snapshot_result_at_publication

        def capture_materialization(db_session, active_run):
            assert events == ["project-lock"]
            events.append("materialize")
            return original_materialize(db_session, active_run)

        monkeypatch.setattr(releases, "lock_project_identity_state", lambda _db, _project_id: events.append("project-lock"))
        monkeypatch.setattr(releases, "_snapshot_result_at_publication", capture_materialization)
        publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")
        assert events == ["project-lock", "materialize"]


def test_rollback_takes_project_lock_before_reading_pointer(db, monkeypatch):
    import app.services.unification.releases as releases
    _engine, maker = db
    run = submitted(maker)
    with maker() as session:
        start_run(session, run.id)
        complete_run(session, run.id, {"entities": {}, "memberships": {}, "attributes": {}})
        snapshot = publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")
    events = []
    original_state = releases._current_state
    def checked_state(*args, **kwargs):
        assert events == ["locked"]
        return original_state(*args, **kwargs)
    monkeypatch.setattr(releases, "lock_project_identity_state", lambda *_: events.append("locked"))
    monkeypatch.setattr(releases, "_current_state", checked_state)
    with maker() as session:
        rollback_snapshot(session, project_id="project", snapshot_id=snapshot.id, actor_id="publisher", reason="test rollback")
    assert events == ["locked"]


def test_sqlite_project_identity_lock_serializes_concurrent_writers(db):
    _engine, maker = db
    first = maker()
    waiting = Event()
    acquired = Event()

    def acquire_in_second_session():
        with maker() as second:
            waiting.set()
            lock_project_identity_state(second, "project")
            acquired.set()
            second.rollback()

    lock_project_identity_state(first, "project")
    thread = Thread(target=acquire_in_second_session)
    thread.start()
    assert waiting.wait(timeout=2)
    time.sleep(0.15)
    assert not acquired.is_set()
    first.commit()
    thread.join(timeout=5)
    assert acquired.is_set()
    first.close()


def test_rollback_creates_a_new_snapshot_and_preserves_history(db):
    _engine, maker = db
    first = submitted(maker)
    with maker() as session:
        start_run(session, first.id)
        complete_run(session, first.id, {"entities": {"e1": {}}, "memberships": {"s1": "e1"}, "attributes": {}})
        v1 = publish_snapshot(session, run_id=first.id, expected_current_version=None, actor_id="publisher")
        second = submit_run(session, project_id="project", input_version="input-v2", scheme_revision="scheme-v1", idempotency_key="key-2", payload_hash="hash-2")
        start_run(session, second.id)
        complete_run(session, second.id, {"entities": {"e2": {}}, "memberships": {"s2": "e2"}, "attributes": {}})
        v2 = publish_snapshot(session, run_id=second.id, expected_current_version=v1.version, actor_id="publisher")
        restored = rollback_snapshot(session, project_id="project", snapshot_id=v1.id, actor_id="publisher", reason="test rollback")
        assert restored.version == v2.version + 1
        assert restored.rollback_of_id == v1.id
        assert get_current_snapshot(session, "project").id == restored.id
        assert session.get(UnificationSnapshot, v2.id).snapshot_json["memberships"] == {"s2": "e2"}


def test_concurrent_first_publish_has_one_complete_current_snapshot(db):
    _engine, maker = db
    runs = [submitted(maker, key=f"publish-{index}", payload=f"hash-{index}") for index in range(2)]
    for index, run in enumerate(runs):
        with maker() as session:
            start_run(session, run.id)
            complete_run(session, run.id, {"entities": {f"e{index}": {"name": f"Entity {index}"}}, "memberships": {f"s{index}": f"e{index}"}, "attributes": {}})
    barrier = Barrier(2)
    def publish(run_id):
        with maker() as session:
            barrier.wait()
            try:
                return publish_snapshot(session, run_id=run_id, expected_current_version=None, actor_id="publisher").snapshot_json
            except RunConflict:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, [run.id for run in runs]))
    with maker() as session:
        current = get_current_snapshot(session, "project")
        assert sum(value is not None for value in results) == 1
        assert current.version == 1
        assert current.snapshot_json in results


def test_snapshot_diff_reports_entity_membership_and_attribute_changes(db):
    before = {"entities": {"e1": {}, "e2": {}}, "memberships": {"s1": "e1", "s2": "e2"}, "attributes": {"e1": {"name": "Old"}}, "conflicts": {"e1": [{"field": "name"}]}}
    after = {"entities": {"e1": {}, "e3": {}}, "memberships": {"s1": "e3", "s3": "e3"}, "attributes": {"e1": {"name": "New"}}, "conflicts": {"e1": []}}
    delta = diff_snapshots(before, after)
    assert delta.added_entities == ("e3",)
    assert delta.removed_entities == ("e2",)
    assert delta.changed_memberships == {"s1": ("e1", "e3"), "s2": ("e2", None), "s3": (None, "e3")}
    assert delta.changed_attributes == {"e1": ({"name": "Old"}, {"name": "New"})}
    assert delta.changed_conflicts == {"e1": ([{"field": "name"}], [])}


def test_worker_stages_complete_entity_assignments_and_attributes(db):
    _engine, maker = db
    scheme = {
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
        "candidate_rules": [{"kind": "exact", "fields": ["name"]}],
        "attribute_policies": {"name": {"source_priority": ["erp"], "on_conflict": "retain_provenance"}},
    }
    with maker() as session:
        version = UnificationSchemeVersion(project_id="project", scheme_key="orgs", revision=1, scheme_json=scheme)
        session.add(version)
        session.commit()
        scheme_id = version.id
        imported = ingest_batch(session, project_id="project", records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "Acme", "tax_id": "T-1"}),
            SourceRecordInput("project", "crm", "vendors", "2", {"name": "Acme Ltd", "tax_id": "T-1"}),
        ], mode=IngestMode.SYNC, input_version="input-v1")
        assert imported.accepted
        session.commit()
        run = submit_run(session, project_id="project", input_version="input-v1", scheme_revision=scheme_id, idempotency_key="worker-run", payload_hash="worker-hash")
        result = execute_unification_run(session, run.id)
        assert result.status == "SUCCEEDED"
        assert result.result_json["entities"]
        assert len(set(result.result_json["memberships"].values())) == 1
        entity_id = next(iter(result.result_json["entities"]))
        assert result.result_json["entities"][entity_id]["status"] == "ACTIVE"
        assert set(result.result_json["source_revisions"]) == set(result.result_json["memberships"])
        assert result.result_json["attributes"][entity_id]["name"] == "Acme"
        assert result.result_json["pairs"][0]["action"] == "AUTO_MATCH"


def test_publish_freezes_current_reviewed_identity_state_and_decision_audit(db):
    _engine, maker = db
    scheme = MatchScheme(
        entity_type="organization", identity_granularity="legal_entity",
        field_map={"name": "name", "tax_id": "tax_id"},
        candidate_rules=[], strong_identifiers=[],
    )
    with maker() as session:
        save_scheme(session, project_id="project", scheme_key="orgs", scheme=scheme)
        version = session.scalar(select(UnificationSchemeVersion).where(
            UnificationSchemeVersion.project_id == "project", UnificationSchemeVersion.scheme_key == "orgs",
        ))
        imported = ingest_batch(session, project_id="project", records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "Acme"}),
            SourceRecordInput("project", "crm", "vendors", "2", {"name": "ACME"}),
        ], mode=IngestMode.SYNC, input_version="reviewable-v1")
        assert imported.accepted
        session.commit()
        run = submit_run(
            session, project_id="project", input_version="reviewable-v1", scheme_revision=version.id,
            idempotency_key="reviewed-publication", payload_hash="reviewed-publication-hash",
        )
        completed = execute_unification_run(session, run.id)
        assert len(set(completed.result_json["memberships"].values())) == 2
        revisions = session.scalars(select(SourceRevision).where(SourceRevision.id.in_(imported.source_revision_ids))).all()
        revision_by_record = {item.source_record.source_record_id: item for item in revisions}
        before_identity_revision = session.scalar(select(func.max(IdentityChangeRecord.revision)).where(
            IdentityChangeRecord.project_id == "project",
        )) or 1
        reviewed = apply_decision(session, DecisionInput(
            project_id="project", left_entity_id=revision_by_record["1"].source_record.current_entity_id,
            right_entity_id=revision_by_record["2"].source_record.current_entity_id,
            action="MERGE", reason="review confirmed same legal entity",
        ), expected_revision=before_identity_revision, actor_id="reviewer", scheme=scheme)

        snapshot = publish_snapshot(
            session, run_id=run.id, expected_current_version=None, actor_id="publisher",
        )
        memberships = snapshot.snapshot_json["memberships"]
        assert len(set(memberships.values())) == 1
        assert snapshot.snapshot_json["identity_revision"] == reviewed.revision
        assert snapshot.snapshot_json["identity_changes"][-1]["reason"] == "review confirmed same legal entity"
        assert set(snapshot.snapshot_json["entities"]) == {next(iter(memberships.values()))}


def test_worker_lease_recovery_is_bounded_and_never_publishes_partial_result(db):
    _engine, maker = db
    run = submitted(maker)
    with maker() as session:
        start_run(session, run.id, max_attempts=2)
        session.query(UnificationRun).filter(UnificationRun.id == run.id).update({"updated_at": datetime(2000, 1, 1, tzinfo=timezone.utc)})
        session.commit()
        assert recover_stale_runs(session, older_than=datetime(2020, 1, 1, tzinfo=timezone.utc), max_attempts=2) == (run.id,)
        with pytest.raises(RunConflict, match="succeeded"):
            publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")
        start_run(session, run.id, max_attempts=2)
        session.query(UnificationRun).filter(UnificationRun.id == run.id).update({"updated_at": datetime(2000, 1, 1, tzinfo=timezone.utc)})
        session.commit()
        recover_stale_runs(session, older_than=datetime(2020, 1, 1, tzinfo=timezone.utc), max_attempts=2)
        with pytest.raises(RunConflict, match="cannot start"):
            start_run(session, run.id, max_attempts=2)


def test_redelivered_lost_worker_is_reclaimed_with_bounded_attempts(db):
    _engine, maker = db
    run = submitted(maker)
    with maker() as session:
        start_run(session, run.id, max_attempts=2)
        assert recover_interrupted_run(session, run.id, max_attempts=2).status == "PENDING"
        restarted = start_run(session, run.id, max_attempts=2)
        assert restarted.last_interruption == "previous worker was lost; run reclaimed for retry"
        recovered = recover_interrupted_run(session, run.id, max_attempts=2)
        assert recovered.status == "FAILED"
        assert recovered.attempt_count == 2


def test_worker_attempt_fencing_rejects_old_success_and_failure_writes(db):
    _engine, maker = db
    run = submitted(maker)
    with maker() as session:
        first_attempt = start_run(session, run.id, max_attempts=2).attempt_count
        recover_interrupted_run(session, run.id, max_attempts=2)
        second_attempt = start_run(session, run.id, max_attempts=2).attempt_count
        assert second_attempt == first_attempt + 1
        with pytest.raises(RunConflict, match="fencing token"):
            complete_run(session, run.id, {"entities": {"stale": {}}}, expected_attempt=first_attempt)
        with pytest.raises(RunConflict, match="fencing token"):
            fail_run(session, run.id, "late failure", expected_attempt=first_attempt)
        current = session.get(UnificationRun, run.id)
        assert current.status == "RUNNING"
        assert current.result_json is None
        complete_run(session, run.id, {"entities": {"fresh": {}}}, expected_attempt=second_attempt)


def test_stale_session_cannot_fail_a_newer_worker_attempt(db):
    _engine, maker = db
    run = submitted(maker)
    stale_session = maker()
    current_session = maker()
    try:
        first_attempt = start_run(stale_session, run.id, max_attempts=2).attempt_count
        assert stale_session.get(UnificationRun, run.id).attempt_count == first_attempt
        recover_interrupted_run(current_session, run.id, max_attempts=2)
        second_attempt = start_run(current_session, run.id, max_attempts=2).attempt_count
        assert second_attempt == first_attempt + 1
        with pytest.raises(RunConflict, match="fencing token"):
            fail_run(stale_session, run.id, "late failure", max_attempts=2, expected_attempt=first_attempt)
        current = current_session.get(UnificationRun, run.id)
        assert current.status == "RUNNING"
        assert current.attempt_count == second_attempt
        assert current.error_message is None
        complete_run(current_session, run.id, {"entities": {"fresh": {}}}, expected_attempt=second_attempt)
    finally:
        stale_session.close()
        current_session.close()


def test_queued_run_rejects_source_revision_changes_instead_of_mislabeling_input(db):
    _engine, maker = db
    with maker() as session:
        first = ingest_batch(session, project_id="project", records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "Before", "tax_id": "T-1"}),
        ], mode=IngestMode.SYNC, input_version="v1")
        assert first.accepted
        session.commit()
        run = submit_run(session, project_id="project", input_version="v1", scheme_revision="missing-scheme", idempotency_key="queued-v1", payload_hash="hash-v1")
        second = ingest_batch(session, project_id="project", records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "After", "tax_id": "T-2"}),
        ], mode=IngestMode.SYNC, input_version="v2")
        assert second.accepted
        session.commit()
        with pytest.raises(RunConflict, match="input or identity revision changed"):
            execute_unification_run(session, run.id, max_attempts=1)
        failed = session.get(UnificationRun, run.id)
        assert failed.status == "FAILED"
        assert failed.result_json is None
        with pytest.raises(RunConflict, match="succeeded"):
            publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")


def _model_run(maker, *, failure_policy):
    with maker() as session:
        provider = LLMProvider(
            id="local-provider", workspace_id="workspace", provider_key="ollama", name="Local model",
            base_url="http://127.0.0.1:1/v1", default_model="unavailable-model",
        )
        session.add(provider)
        session.flush()
        scheme = MatchScheme.model_validate({
            "entity_type": "organization", "identity_granularity": "legal_entity",
            "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
            "candidate_rules": [{"kind": "exact", "fields": ["name"]}],
            "model_judgment_enabled": True, "model_provider_id": provider.id,
            "model_failure_policy": failure_policy, "prompt_version": "pair-v1",
        })
        save_scheme(session, project_id="project", scheme_key="model-orgs", scheme=scheme)
        version_id = session.scalar(select(UnificationSchemeVersion.id).where(
            UnificationSchemeVersion.project_id == "project",
            UnificationSchemeVersion.scheme_key == "model-orgs",
        ))
        imported = ingest_batch(session, project_id="project", records=[
            SourceRecordInput("project", "erp", "vendors", "1", {"name": "Acme", "tax_id": None}),
            SourceRecordInput("project", "crm", "vendors", "2", {"name": "Acme", "tax_id": None}),
        ], mode=IngestMode.SYNC, input_version="input-v1")
        assert imported.accepted
        session.commit()
        return submit_run(
            session, project_id="project", input_version="input-v1", scheme_revision=version_id,
            idempotency_key=f"model-{failure_policy}", payload_hash=f"hash-{failure_policy}",
        )


def test_model_error_with_review_fallback_keeps_pair_unresolved(db):
    _engine, maker = db
    run = _model_run(maker, failure_policy="REVIEW")
    with maker() as session:
        completed = execute_unification_run(session, run.id)
        pair = completed.result_json["pairs"][0]
        assert completed.status == "SUCCEEDED"
        assert pair["action"] == "REVIEW"
        assert "provider_error" in pair["model_error"]


def test_model_error_with_fail_fallback_fails_run_without_publishing(db):
    _engine, maker = db
    run = _model_run(maker, failure_policy="FAIL")
    with maker() as session:
        with pytest.raises(RuntimeError, match="configured model judgment failed"):
            execute_unification_run(session, run.id)
        failed = session.get(UnificationRun, run.id)
        assert failed.status == "FAILED"
        assert failed.result_json is None
        with pytest.raises(RunConflict, match="succeeded"):
            publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")


def test_run_and_snapshot_keep_trace_ids_after_exported_spans_are_cleared(db):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.services.unification.telemetry import TraceConfig, configure_tracing, current_trace_id, shutdown_tracing, start_span

    _engine, maker = db
    run = _model_run(maker, failure_policy="REVIEW")
    exporter = InMemorySpanExporter()
    provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    try:
        with maker() as session:
            with start_span("api.dispatch") as dispatch_span:
                completed = execute_unification_run(session, run.id)
            expected_run_trace = f"{dispatch_span.get_span_context().trace_id:032x}"
            assert completed.status == "SUCCEEDED"
            assert completed.trace_id == expected_run_trace
            with start_span("api.publish"):
                expected_snapshot_trace = current_trace_id()
                snapshot = publish_snapshot(session, run_id=run.id, expected_current_version=None, actor_id="publisher")
            assert snapshot.trace_id == expected_snapshot_trace
            run_id, snapshot_id = completed.id, snapshot.id
        assert provider.force_flush()
        spans = exporter.get_finished_spans()
        span_names = {span.name for span in spans}
        assert {"unification.read_input", "unification.standardize_and_match", "unification.write_result"} <= span_names
        exporter.clear()
        with maker() as session:
            assert session.get(UnificationRun, run_id).trace_id == expected_run_trace
            assert session.get(UnificationSnapshot, snapshot_id).trace_id == expected_snapshot_trace
    finally:
        shutdown_tracing()


def test_worker_uses_configured_unification_attempt_limit(db, monkeypatch):
    from app.workers import tasks

    _engine, maker = db
    captured = {}

    class CompletedRun:
        result_json = {"status": "SUCCEEDED"}

    def fake_execute(session, run_id, *, max_attempts):
        captured["run_id"] = run_id
        captured["max_attempts"] = max_attempts
        return CompletedRun()

    monkeypatch.setattr(tasks, "SessionLocal", maker)
    monkeypatch.setattr(tasks, "execute_unification_run", fake_execute)
    monkeypatch.setattr(tasks.settings, "unification_max_attempts", 7)

    assert tasks.run_unification_sync("run-configured") == {"status": "SUCCEEDED"}
    assert captured == {"run_id": "run-configured", "max_attempts": 7}


def test_unification_attempt_limit_must_be_positive():
    from app.core.config import Settings

    settings = Settings()
    settings.app_env = "development"
    settings.unification_max_attempts = 0
    with pytest.raises(ValueError, match="UNIFICATION_MAX_ATTEMPTS"):
        settings.validate_for_deployment()
