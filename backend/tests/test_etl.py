"""ETL sink and schedule unit tests: sinks, target jobs, failure handling, and scheduling."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.deps import get_current_user
from app.core.db import Base, get_db
from app.core.security import encrypt_secret
from app.main import app
from app.models.entities import DataSource, Job, Project, Workspace
from app.models.etl import EtlSchedule
from app.services import etl as etl_service
from app.services.connectors import read_db_dataframe
from app.services.storage import storage
from app.workers.tasks import JobExecutionError, run_due_etl_schedules, run_job_sync


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'etl.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="ws", name="WS", slug="ws"))
        db.flush()
        db.add(Project(id="proj", workspace_id="ws", name="P", slug="p"))
        db.commit()

    monkeypatch.setattr("app.workers.tasks.SessionLocal", maker)
    monkeypatch.setattr(storage, "local_root", str(tmp_path / "storage"))
    identity = {"workspace_id": "ws", "role": "editor", "user_id": "u1"}

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: identity.copy()
    try:
        yield TestClient(app), maker, tmp_path
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# --------------------------- table-name validation ---------------------------

def test_validate_table_name():
    for good in ("clean_suppliers", "schema.supplier_clean", "_a.1", "A_1.b_2"):
        assert etl_service.validate_table_name(good) == good
    for bad in ("", "1abc", "a b", "a-b", "a;drop", "a`b", 'a"b', "..", "a..-b"):
        with pytest.raises(ValueError):
            etl_service.validate_table_name(bad)


# --------------------------- sqlite and duckdb sinks ---------------------------

def test_sqlite_sink_replace_and_append(tmp_path):
    cfg = {"db_type": "sqlite", "database": str(tmp_path / "sink.db")}
    frame = pd.DataFrame([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
    assert etl_service.write_dataframe(cfg, "t", frame, "replace") == 2
    assert etl_service.write_dataframe(cfg, "t", frame, "append") == 2
    out = read_db_dataframe(cfg, "t", limit=None)
    assert len(out) == 4


def test_duckdb_sink_replace_and_append(tmp_path):
    cfg = {"db_type": "duckdb", "database": str(tmp_path / "sink.duckdb")}
    frame = pd.DataFrame([{"a": 1}, {"a": 2}])
    assert etl_service.write_dataframe(cfg, "t", frame, "replace") == 2
    assert etl_service.write_dataframe(cfg, "t", frame, "append") == 2
    out = read_db_dataframe(cfg, "t", limit=None)
    assert len(out) == 4


def test_empty_replace_is_rejected_and_preserves_target(tmp_path):
    cfg = {"db_type": "sqlite", "database": str(tmp_path / "empty.db")}
    etl_service.write_dataframe(cfg, "t", pd.DataFrame([{"x": "keep"}]), "replace")
    with pytest.raises(ValueError, match="empty result"):
        etl_service.write_dataframe(cfg, "t", pd.DataFrame(), "replace")
    out = read_db_dataframe(cfg, "t", limit=None)
    assert out["x"].tolist() == ["keep"]


def test_concurrent_sqlite_sink_serializes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cfg = {"db_type": "sqlite", "database": str(tmp_path / "concurrent.db")}
    left = pd.DataFrame([{"x": "a1"}, {"x": "a2"}])
    right = pd.DataFrame([{"x": "b1"}, {"x": "b2"}])
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda frame: etl_service.write_dataframe(cfg, "append_t", frame, "append"), [left, right])) == [2, 2]
    assert len(read_db_dataframe(cfg, "append_t", limit=None)) == 4

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda frame: etl_service.write_dataframe(cfg, "replace_t", frame, "replace"), [left, right])) == [2, 2]
    final = read_db_dataframe(cfg, "replace_t", limit=None)
    assert sorted(final["x"].tolist()) in (["a1", "a2"], ["b1", "b2"])


# --------------------------- target job success ---------------------------

def _sqlite_target(tmp_path, source_id="tgt"):
    return DataSource(
        id=source_id, project_id="proj", name="target", source_type="sqlite",
        encrypted_config=encrypt_secret(json.dumps({"db_type": "sqlite", "database": str(tmp_path / "target.db")})),
    )


def test_l1_job_writes_target_sqlite(api):
    _client, maker, tmp_path = api
    storage.put_bytes("projects/proj/uploads/suppliers.csv", b"supplier_raw_name,order_amount_cny\nAcme,100\nBeta,200\n")
    with maker() as db:
        src = DataSource(id="src", project_id="proj", name="suppliers.csv", source_type="file",
                         object_key="projects/proj/uploads/suppliers.csv")
        db.add_all([src, _sqlite_target(tmp_path)])
        job = Job(project_id="proj", stage="L1", status="PENDING", params_json={
            "datasource_ids": ["src"],
            "etl_target_datasource_id": "tgt",
            "etl_target_table": "supplier_clean",
            "etl_write_mode": "replace",
        })
        db.add(job)
        db.commit()
        job_id = job.id

    result = run_job_sync(job_id)

    assert result["etl"]["status"] == "SUCCEEDED"
    assert result["etl"]["target_type"] == "sqlite"
    assert result["etl"]["rows_written"] == 2
    out = read_db_dataframe({"db_type": "sqlite", "database": str(tmp_path / "target.db")}, "supplier_clean", limit=None)
    assert len(out) == 2


def test_empty_l1_result_does_not_destroy_existing_target(api):
    _client, maker, tmp_path = api
    target_path = tmp_path / "target.db"
    etl_service.write_dataframe({"db_type": "sqlite", "database": str(target_path)}, "supplier_clean",
                                pd.DataFrame([{"x": "keep"}]), "replace")
    storage.put_bytes("projects/proj/uploads/empty.csv", b"company_name\n")
    with maker() as db:
        src = DataSource(id="src", project_id="proj", name="empty.csv", source_type="file",
                         object_key="projects/proj/uploads/empty.csv")
        db.add_all([src, _sqlite_target(tmp_path)])
        job = Job(project_id="proj", stage="L1", status="PENDING", params_json={
            "datasource_ids": ["src"],
            "clean_fields": ["company_name"],
            "etl_target_datasource_id": "tgt",
            "etl_target_table": "supplier_clean",
            "etl_write_mode": "replace",
        })
        db.add(job)
        db.commit()
        job_id = job.id

    with pytest.raises(JobExecutionError, match="ETL_LOAD_FAILED"):
        run_job_sync(job_id)

    with maker() as db:
        failed = db.get(Job, job_id)
        assert failed.status == "FAILED"
        assert failed.error_message.startswith("ETL_LOAD_FAILED")
    out = read_db_dataframe({"db_type": "sqlite", "database": str(target_path)}, "supplier_clean", limit=None)
    assert out["x"].tolist() == ["keep"]


def test_l2_job_writes_target_sqlite(api):
    _client, maker, tmp_path = api
    row_count = 2100
    storage.put_bytes(
        "projects/proj/uploads/customers.csv",
        ("company_name,tax_id,phone\n" + "".join(
            f"Company {index},T{index},{100000 + index}\n" for index in range(row_count)
        )).encode(),
    )
    with maker() as db:
        src = DataSource(id="src", project_id="proj", name="customers.csv", source_type="file",
                         object_key="projects/proj/uploads/customers.csv")
        db.add_all([src, _sqlite_target(tmp_path)])
        job = Job(project_id="proj", stage="L2", status="PENDING", params_json={
            "datasource_ids": ["src"],
            "match_fields": ["company_name", "tax_id", "phone"],
            "etl_target_datasource_id": "tgt",
            "etl_target_table": "match_sample",
            "etl_write_mode": "replace",
        })
        db.add(job)
        db.commit()
        job_id = job.id

    result = run_job_sync(job_id)

    assert result["etl"]["status"] == "SUCCEEDED"
    assert result["total_rows"] == row_count
    assert result["etl"]["rows_written"] == row_count
    out = read_db_dataframe({"db_type": "sqlite", "database": str(tmp_path / "target.db")}, "match_sample", limit=None)
    assert len(out) == row_count


def test_l4_job_writes_target_duckdb(api):
    _client, maker, tmp_path = api
    storage.put_bytes(
        "projects/proj/uploads/wide.csv",
        b"order_id,order_channel,total_amount,delivery_status,settled_amount\n"
        b"O1,CROSS_BORDER_DTC,100,DELIVERED,80\n"
        b"O2,DOMESTIC_B2B,50,EXCEPTION,0\n",
    )
    with maker() as db:
        src = DataSource(id="src", project_id="proj", name="wide.csv", source_type="file",
                         object_key="projects/proj/uploads/wide.csv")
        tgt = DataSource(id="tgt", project_id="proj", name="target", source_type="duckdb",
                         encrypted_config=encrypt_secret(json.dumps({"db_type": "duckdb", "database": str(tmp_path / "target.duckdb")})))
        db.add_all([src, tgt])
        job = Job(project_id="proj", stage="L4", status="PENDING", params_json={
            "datasource_ids": ["src"],
            "etl_target_datasource_id": "tgt",
            "etl_target_table": "metrics",
            "etl_write_mode": "replace",
        })
        db.add(job)
        db.commit()
        job_id = job.id

    result = run_job_sync(job_id)

    assert result["etl"]["status"] == "SUCCEEDED"
    assert result["etl"]["target_type"] == "duckdb"
    assert result["etl"]["rows_written"] == len(result["result_rows"])
    out = read_db_dataframe({"db_type": "duckdb", "database": str(tmp_path / "target.duckdb")}, "metrics", limit=None)
    assert len(out) == len(result["result_rows"])


# --------------------------- sink failure preserves result ---------------------------

def test_sink_failure_marks_job_failed_and_keeps_result(api, monkeypatch):
    _client, maker, tmp_path = api
    storage.put_bytes("projects/proj/uploads/suppliers.csv", b"supplier_raw_name,order_amount_cny\nAcme,100\n")
    with maker() as db:
        src = DataSource(id="src", project_id="proj", name="suppliers.csv", source_type="file",
                         object_key="projects/proj/uploads/suppliers.csv")
        db.add_all([src, _sqlite_target(tmp_path)])
        job = Job(project_id="proj", stage="L1", status="PENDING", params_json={
            "datasource_ids": ["src"],
            "etl_target_datasource_id": "tgt",
            "etl_target_table": "supplier_clean",
            "etl_write_mode": "replace",
        })
        db.add(job)
        db.commit()
        job_id = job.id

    def boom(*args, **kwargs):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr("app.workers.tasks.write_dataframe", boom)
    with pytest.raises(JobExecutionError, match="ETL_LOAD_FAILED: target unreachable"):
        run_job_sync(job_id)

    with maker() as db:
        failed = db.get(Job, job_id)
        assert failed.status == "FAILED"
        assert failed.error_message.startswith("ETL_LOAD_FAILED:")
        assert "target unreachable" in failed.error_message
        assert failed.result_json["stage"] == "L1"
        assert failed.result_json["artifacts"]


def test_sink_failure_is_returned_as_a_failed_job(api, monkeypatch):
    client, maker, tmp_path = api
    storage.put_bytes("projects/proj/uploads/suppliers.csv", b"supplier_raw_name,order_amount_cny\nAcme,100\n")
    with maker() as db:
        db.add_all([
            DataSource(id="src", project_id="proj", name="suppliers.csv", source_type="file",
                       object_key="projects/proj/uploads/suppliers.csv"),
            _sqlite_target(tmp_path),
        ])
        db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr("app.workers.tasks.write_dataframe", boom)
    response = client.post("/api/v1/projects/proj/jobs", json={
        "stage": "L1",
        "datasource_ids": ["src"],
        "params": {"clean_fields": ["supplier_raw_name"]},
        "etl_target_datasource_id": "tgt",
        "etl_target_table": "supplier_clean",
        "etl_write_mode": "replace",
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "FAILED"
    assert payload["error_message"].startswith("ETL_LOAD_FAILED: target unreachable")
    assert payload["result"]["artifacts"]


def test_datasource_api_does_not_expose_target_credentials(api, tmp_path):
    client, maker, _tmp_path = api
    with maker() as db:
        db.add(DataSource(
            id="secret-target", project_id="proj", name="secret", source_type="postgresql",
            encrypted_config=encrypt_secret(json.dumps({
                "db_type": "postgresql", "host": "127.0.0.1", "port": 1,
                "user": "u", "password": "super-secret-password", "database": "d",
            })),
        ))
        db.commit()
    payload = client.get("/api/v1/projects/proj/datasources").json()
    assert "super-secret-password" not in json.dumps(payload)
    assert "encrypted_config" not in json.dumps(payload)

    with pytest.raises(RuntimeError) as exc:
        etl_service.write_dataframe(
            {"db_type": "postgresql", "host": "127.0.0.1", "port": 1, "user": "u",
             "password": "super-secret-password", "database": "d"},
            "t", pd.DataFrame([{"a": 1}]), "replace",
        )
    assert "super-secret-password" not in str(exc.value)


def test_celery_beat_configures_the_etl_tick():
    import os
    import subprocess

    code = "from app.workers.celery_app import celery_app; print(celery_app.conf.beat_schedule)"
    env = {**os.environ, "CELERY_ENABLED": "true", "REDIS_URL": "redis://127.0.0.1:1/0"}
    result = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]),
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "semantic_unifier.run_due_etl_schedules" in result.stdout


def test_compose_has_a_beat_service():
    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
    beat = compose["services"]["beat"]
    assert "beat" in beat["command"]
    assert beat["depends_on"]


# --------------------------- schedule validation and lifecycle ---------------------------

def _valid_schedule_body():
    return {
        "name": "nightly clean",
        "stage": "L1",
        "datasource_ids": [],
        "params": {},
        "target_datasource_id": "tgt",
        "target_table": "supplier_clean",
        "write_mode": "replace",
        "interval_minutes": 60,
        "enabled": True,
    }


def _seed_sources(maker):
    with maker() as db:
        db.add_all([
            DataSource(id="src", project_id="proj", name="s.csv", source_type="file", object_key="x"),
            DataSource(id="tgt", project_id="proj", name="target", source_type="sqlite",
                       encrypted_config=encrypt_secret(json.dumps({"db_type": "sqlite", "database": "/tmp/t.db"}))),
        ])
        db.commit()


def test_create_schedule_validation(api):
    client, maker, _tmp_path = api
    _seed_sources(maker)
    base = _valid_schedule_body()

    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "interval_minutes": 2}).status_code == 422
    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "stage": "L3"}).status_code == 400
    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "write_mode": "upsert"}).status_code == 400
    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "target_table": "1bad;"}).status_code == 400
    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "target_datasource_id": "src"}).status_code == 400
    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "target_datasource_id": "missing"}).status_code == 404
    assert client.post("/api/v1/projects/proj/etl/schedules", json={**base, "datasource_ids": ["missing"]}).status_code == 404

    ok = client.post("/api/v1/projects/proj/etl/schedules", json=base)
    assert ok.status_code == 201, ok.text
    assert ok.json()["id"]


def test_schedule_is_project_scoped(api):
    client, maker, _tmp_path = api
    _seed_sources(maker)
    with maker() as db:
        db.add(Project(id="proj2", workspace_id="ws", name="P2", slug="p2"))
        db.commit()
    created = client.post("/api/v1/projects/proj/etl/schedules", json=_valid_schedule_body())
    sid = created.json()["id"]
    assert client.get("/api/v1/projects/proj/etl/schedules").status_code == 200
    assert client.patch(f"/api/v1/projects/proj2/etl/schedules/{sid}", json={"enabled": False}).status_code == 404
    assert client.delete(f"/api/v1/projects/proj2/etl/schedules/{sid}").status_code == 404
    assert client.post(f"/api/v1/projects/proj2/etl/schedules/{sid}/run-now").status_code == 404


def test_run_now_dispatches_and_advances(api, monkeypatch):
    client, maker, _tmp_path = api
    _seed_sources(maker)
    created = client.post("/api/v1/projects/proj/etl/schedules", json=_valid_schedule_body())
    sid = created.json()["id"]

    dispatched = []
    monkeypatch.setattr("app.workers.tasks.dispatch_job", lambda job_id: dispatched.append(job_id))

    response = client.post(f"/api/v1/projects/proj/etl/schedules/{sid}/run-now")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"]
    assert dispatched == [body["job_id"]]

    with maker() as db:
        schedule = db.get(EtlSchedule, sid)
        assert schedule.last_job_id == body["job_id"]
        assert schedule.last_run_at is not None
        assert schedule.next_run_at > schedule.last_run_at


def test_due_runner_selects_enabled_due_and_skips_disabled(api, monkeypatch):
    _client, maker, _tmp_path = api
    dispatched = []
    monkeypatch.setattr("app.workers.tasks.dispatch_job", lambda job_id: dispatched.append(job_id))
    now = datetime.now(timezone.utc)
    with maker() as db:
        db.add_all([
            EtlSchedule(id="due", project_id="proj", name="due", stage="L1", datasource_ids=[], params={},
                        target_datasource_id="tgt", target_table="t", write_mode="replace",
                        interval_minutes=5, enabled=True, next_run_at=now - timedelta(minutes=1)),
            EtlSchedule(id="off", project_id="proj", name="off", stage="L1", datasource_ids=[], params={},
                        target_datasource_id="tgt", target_table="t", write_mode="replace",
                        interval_minutes=5, enabled=False, next_run_at=now - timedelta(minutes=1)),
            EtlSchedule(id="future", project_id="proj", name="future", stage="L1", datasource_ids=[], params={},
                        target_datasource_id="tgt", target_table="t", write_mode="replace",
                        interval_minutes=5, enabled=True, next_run_at=now + timedelta(hours=1)),
        ])
        db.commit()

    result = run_due_etl_schedules()

    assert result == {"dispatched": 1}
    assert len(dispatched) == 1
    with maker() as db:
        due = db.get(EtlSchedule, "due")
        assert due.last_job_id
        assert due.next_run_at.replace(tzinfo=timezone.utc) > now
