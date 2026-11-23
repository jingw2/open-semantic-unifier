from pathlib import Path
import os
import subprocess
import sys
import pytest

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

BACKEND = Path(__file__).resolve().parents[1]


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


def test_upgrade_empty_database_creates_all_model_tables(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    run_alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"projects", "users", "workspaces", "unified_entities", "unified_source_records", "unified_source_revisions", "unification_scheme_versions", "unification_entity_catalog_versions", "unification_catalog_entity_versions", "unification_runs", "unification_snapshots", "unification_project_snapshot_state", "alembic_version"} <= tables
        for table in ("unification_runs", "unification_identity_changes", "unification_snapshots"):
            assert "trace_id" in {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_upgrade_existing_schema_preserves_data_and_adds_identity_tables(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'existing.db'}"
    engine = create_engine(database_url)
    run_alembic(database_url, "upgrade", "3a5d3f7c8e11")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO workspaces (id, name, slug, created_at) VALUES ('w1','Workspace','workspace','2026-01-01')"))
        connection.execute(text("INSERT INTO projects (id, workspace_id, name, slug, description, created_at) VALUES ('p1','w1','Keep me','keep-me','','2026-01-01')"))
        connection.execute(text("DROP TABLE alembic_version"))  # Emulate a pre-Alembic application database.
    engine.dispose()
    run_alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT name FROM projects WHERE id='p1'")).scalar_one() == "Keep me"
        assert {"unified_entities", "unified_source_records", "unified_source_revisions"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_downgrade_removes_only_new_identity_tables(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    run_alembic(database_url, "upgrade", "head")
    run_alembic(database_url, "downgrade", "3a5d3f7c8e11")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert not {"unified_entities", "unified_source_records", "unified_source_revisions"} & tables
        assert "projects" in tables
    finally:
        engine.dispose()


def test_migrated_schema_enforces_revision_ownership_with_foreign_keys_enabled(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'foreign-keys.db'}"
    run_alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO workspaces (id,name,slug,created_at) VALUES ('w','W','w','2026-01-01')"))
            connection.execute(text("INSERT INTO projects (id,workspace_id,name,slug,description,created_at) VALUES ('p','w','P','p','','2026-01-01')"))
            connection.execute(text("INSERT INTO unified_entities (id,project_id,entity_type,status,created_at) VALUES ('e1','p','vendor','ACTIVE','2026-01-01'),('e2','p','vendor','ACTIVE','2026-01-01')"))
            connection.execute(text("INSERT INTO unified_source_records (id,project_id,source_system,dataset,source_record_id,current_entity_id,current_revision_id,created_at) VALUES ('s1','p','erp','vendors','1','e1',NULL,'2026-01-01'),('s2','p','erp','vendors','2','e2',NULL,'2026-01-01')"))
            connection.execute(text("INSERT INTO unified_source_revisions (id,source_record_id,revision_no,input_version,attributes,content_hash,created_at) VALUES ('r1','s1',1,'v1','{}','h1','2026-01-01'),('r2','s2',1,'v1','{}','h2','2026-01-01')"))
            connection.execute(text("UPDATE unified_source_records SET current_revision_id='r1' WHERE id='s1'"))
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text("UPDATE unified_source_records SET current_revision_id='r2' WHERE id='s1'"))
    finally:
        engine.dispose()


def test_scheme_migration_downgrade_preserves_t1_identity_tables(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'scheme-downgrade.db'}"
    run_alembic(database_url, "upgrade", "head")
    run_alembic(database_url, "downgrade", "e072c1d2a362")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "unification_scheme_versions" not in tables
        assert "unification_entity_catalog_versions" not in tables
        assert "unification_catalog_entity_versions" not in tables
        assert {"unified_entities", "unified_source_records", "unified_source_revisions"} <= tables
    finally:
        engine.dispose()
