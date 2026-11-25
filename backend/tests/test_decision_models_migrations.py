"""Migration tests for decision model calls and provider configuration columns."""
from pathlib import Path
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text

BACKEND = Path(__file__).resolve().parents[1]
NEW_REVISION = "e4f5a6b7c8d9"
CALL_COLUMNS = {
    "id", "project_id", "run_id", "left_source_revision_id", "right_source_revision_id",
    "provider_kind", "provider_key", "model_version", "prompt_version",
    "verdict", "confidence", "distribution_json", "explanation",
    "latency_ms", "usage_json", "cost_usd",
    "error_code", "error_message", "retryable", "trace_id", "created_at",
}
PROVIDER_COLUMNS = {"provider_kind", "send_fields_json", "timeout_seconds", "max_retries", "prompt_version"}
ETL_SCHEDULE_COLUMNS = {
    "id", "project_id", "name", "stage", "datasource_ids", "params",
    "target_datasource_id", "target_table", "write_mode", "interval_minutes",
    "enabled", "next_run_at", "last_run_at", "last_job_id", "created_at", "updated_at",
}


def run_alembic(database_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": str(BACKEND)}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *args],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_single_alembic_head_is_the_latest_revision():
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "heads"],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    heads = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    assert heads == [NEW_REVISION]


def test_upgrade_creates_call_table_and_provider_columns(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    run_alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert CALL_COLUMNS <= {column["name"] for column in inspector.get_columns("decision_model_calls")}
        assert PROVIDER_COLUMNS <= {column["name"] for column in inspector.get_columns("llm_providers")}
        assert "input_source_system" in {column["name"] for column in inspector.get_columns("unification_runs")}
        assert ETL_SCHEDULE_COLUMNS <= {column["name"] for column in inspector.get_columns("etl_schedules")}
        index_names = {item["name"] for item in inspector.get_indexes("etl_schedules")}
        assert "ix_etl_schedules_project_id" in index_names
        constraint_names = {item["name"] for item in inspector.get_unique_constraints("decision_model_calls")}
        assert "uq_decision_model_call_pair" in constraint_names
        index_names = {item["name"] for item in inspector.get_indexes("decision_model_calls")}
        assert {"ix_decision_model_calls_project_id", "ix_decision_model_calls_run_id"} <= index_names
    finally:
        engine.dispose()


def test_existing_provider_rows_receive_defaults(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'existing.db'}"
    run_alembic(database_url, "upgrade", "c17dbecd1a30")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO workspaces (id, name, slug, created_at) VALUES ('w1','W','w','2026-01-01')"))
        connection.execute(text(
            "INSERT INTO llm_providers (id, workspace_id, provider_key, name, base_url, default_model, "
            "encrypted_api_key, is_custom, is_default, created_at) "
            "VALUES ('p1','w1','legacy','Legacy','','','',1,0,'2026-01-01')"
        ))
    engine.dispose()
    run_alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT provider_kind, send_fields_json, timeout_seconds, max_retries, prompt_version "
                "FROM llm_providers WHERE id='p1'"
            )).one()
            assert row[0] == "openai_compatible"
            assert row[1] == "[]"
            assert float(row[2]) == 30.0
            assert row[3] == 0
            assert row[4] == "entity-pair-choice-v1"
    finally:
        engine.dispose()


def test_downgrade_removes_call_table_and_provider_columns(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    run_alembic(database_url, "upgrade", "head")
    run_alembic(database_url, "downgrade", "c17dbecd1a30")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "decision_model_calls" not in set(inspector.get_table_names())
        assert "etl_schedules" not in set(inspector.get_table_names())
        remaining = {column["name"] for column in inspector.get_columns("llm_providers")}
        assert not PROVIDER_COLUMNS & remaining
        assert "input_source_system" not in {column["name"] for column in inspector.get_columns("unification_runs")}
    finally:
        engine.dispose()
