"""Exhaustive fault-injection check for the ETL sink contract.

Property: for every L1/L2/L4 Job that names a target table, the Job reports
SUCCESS only after the sink write succeeds. A sink failure marks the Job FAILED
with ETL_LOAD_FAILED, preserves the transform result, and writes nothing. A
transform failure never calls the sink. A Job without a target never calls it.

Run: python3 tests/formal/check_etl_sink_contract.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.workers.tasks as worker_tasks
from app.core.db import Base
from app.core.security import encrypt_secret
from app.models.entities import ArtifactVersion, DataSource, Job, Project, Workspace
from app.services.engines import _save_artifact
from app.services.storage import storage

SOURCE_CSV = b"company_name,city\n  Acme  ,Shenzhen\nBeta,Shanghai\n"
SOURCE_PROFILE = {
    "total_rows": 2, "table_count": 1,
    "tables": [{"name": "source.csv", "rows": 2, "cols": 2, "columns": ["company_name", "city"]}],
}
STAGES = ("L1", "L2", "L4")
OUTCOMES = ("success", "sink_failure", "invalid_table", "transform_failure", "empty_replace")


class Failure(RuntimeError):
    pass


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def _seed(root: Path, stage: str):
    root.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{root / 'state.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage.backend = "local"
    storage.local_root = str(root / "storage")
    storage.put_bytes("projects/p/etl/source.csv", SOURCE_CSV)
    target_path = root / "target.db"
    with maker() as db:
        db.add(Workspace(id="ws", name="WS", slug=f"ws-etl-{stage}-{root.name}"))
        db.flush()
        db.add(Project(id="p", workspace_id="ws", name="P", slug=f"p-etl-{stage}-{root.name}"))
        db.add(DataSource(
            id="ds-source", project_id="p", name="source.csv", source_type="file",
            object_key="projects/p/etl/source.csv", profile_json=SOURCE_PROFILE,
        ))
        db.add(DataSource(
            id="ds-target", project_id="p", name="target.db", source_type="sqlite",
            encrypted_config=encrypt_secret(json.dumps({"db_type": "sqlite", "database": str(target_path)})),
            profile_json={},
        ))
        db.flush()
        params = {
            "datasource_ids": ["ds-source"] if stage in ("L1", "L2") else [],
            "etl_target_datasource_id": "ds-target",
            "etl_target_table": f"out_{stage}",
            "etl_write_mode": "replace",
        }
        if stage == "L1":
            params["clean_fields"] = ["company_name"]
        job = Job(project_id="p", stage=stage, status="PENDING", progress=0, params_json=params)
        db.add(job)
        db.commit()
        return engine, maker, job.id, target_path


def _fake_transform(stage: str, *, empty: bool = False):
    if stage == "L1":
        def run(db, project_id, job_id, df, fields, progress_cb=None):
            payload = "company_name\n" if empty else df.to_csv(index=False)
            art = _save_artifact(db, project_id, job_id, "clean_csv", "clean.csv", payload.encode())
            db.commit()
            return {"stage": "L1", "clean_fields": fields, "total_rows": len(df),
                    "artifacts": [{"id": art.id, "type": "clean_csv", "filename": art.filename, "version": art.version_no}]}
        return run
    if stage == "L2":
        def run(db, project_id, job_id, dfs, params, llm_cfg=None, progress_cb=None):
            payload = b"unique_id,cluster_id\n" if empty else b"unique_id,cluster_id\nsrc0_0,1\nsrc0_1,1\n"
            art = _save_artifact(db, project_id, job_id, "clusters_csv", "clusters.csv", payload)
            db.commit()
            return {"stage": "L2", "unique_clusters": 1,
                    "artifacts": [{"id": art.id, "type": "clusters_csv", "filename": art.filename, "version": art.version_no}]}
        return run
    def run(db, project_id, job_id, **kwargs):
        return {"stage": "L4", "selected_metric": "net_gmv",
                "result_rows": [] if empty else [{"order_channel": "CROSS_BORDER_DTC", "net_gmv": 100.0}]}
    return run


def _run_case(root: Path, stage: str, outcome: str) -> int:
    engine, maker, job_id, target_path = _seed(root, stage)
    calls: list[dict] = []
    original_session = worker_tasks.SessionLocal
    original_transform = {
        "L1": worker_tasks.execute_clean_fields,
        "L2": worker_tasks.execute_l2,
        "L4": worker_tasks.execute_l4,
    }[stage]
    original_write = worker_tasks.write_dataframe
    worker_tasks.SessionLocal = maker
    fake = _fake_transform(stage, empty=(outcome == "empty_replace"))
    if outcome == "transform_failure":
        def fail_transform(*_args, **_kwargs):
            raise RuntimeError("transform failed")
        transform = fail_transform
    else:
        transform = fake
    if stage == "L1":
        worker_tasks.execute_clean_fields = transform
    elif stage == "L2":
        worker_tasks.execute_l2 = transform
    else:
        worker_tasks.execute_l4 = transform

    if outcome == "sink_failure":
        def fail_write(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            raise RuntimeError("target down")
        worker_tasks.write_dataframe = fail_write
    elif outcome in ("transform_failure", "no_target"):
        def record_write(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return 1
        worker_tasks.write_dataframe = record_write

    if outcome == "empty_replace":
        con = sqlite3.connect(str(target_path))
        try:
            con.execute(f'CREATE TABLE "out_{stage}" (x text)')
            con.execute(f'INSERT INTO "out_{stage}" VALUES (?)', ("keep",))
            con.commit()
        finally:
            con.close()

    if outcome == "invalid_table":
        with maker() as db:
            job = db.get(Job, job_id)
            params = dict(job.params_json or {})
            params["etl_target_table"] = "bad;table"
            job.params_json = params
            db.commit()

    if outcome == "no_target":
        with maker() as db:
            job = db.get(Job, job_id)
            params = dict(job.params_json or {})
            for key in ("etl_target_datasource_id", "etl_target_table", "etl_write_mode"):
                params.pop(key, None)
            job.params_json = params
            db.commit()

    try:
        if outcome == "transform_failure":
            try:
                worker_tasks.run_job_sync(job_id)
            except Exception:
                pass
            else:
                raise Failure(f"{stage}/{outcome}: transform failure did not raise")
        elif outcome in ("sink_failure", "invalid_table", "empty_replace"):
            try:
                worker_tasks.run_job_sync(job_id)
            except worker_tasks.JobExecutionError:
                pass
            else:
                raise Failure(f"{stage}/{outcome}: sink failure did not raise JobExecutionError")
        else:
            worker_tasks.run_job_sync(job_id)
    finally:
        worker_tasks.SessionLocal = original_session
        if stage == "L1":
            worker_tasks.execute_clean_fields = original_transform
        elif stage == "L2":
            worker_tasks.execute_l2 = original_transform
        else:
            worker_tasks.execute_l4 = original_transform
        worker_tasks.write_dataframe = original_write

    with maker() as db:
        job = db.get(Job, job_id)
        _assert(job is not None, f"{stage}/{outcome}: Job disappeared")
        result = job.result_json or {}

        if outcome == "success":
            _assert(job.status == "SUCCESS", f"{stage}/{outcome}: Job is {job.status}")
            _assert(result.get("etl", {}).get("status") == "SUCCEEDED", f"{stage}/{outcome}: no successful ETL result")
            _assert(result["etl"]["rows_written"] > 0, f"{stage}/{outcome}: no rows reported")
            con = sqlite3.connect(str(target_path))
            try:
                count = con.execute(f'SELECT COUNT(*) FROM "out_{stage}"').fetchone()[0]
            finally:
                con.close()
            _assert(count > 0, f"{stage}/{outcome}: target table is empty")
        elif outcome == "no_target":
            _assert(job.status == "SUCCESS", f"{stage}/{outcome}: Job is {job.status}")
            _assert("etl" not in result, f"{stage}/{outcome}: ETL ran without a target")
            _assert(calls == [], f"{stage}/{outcome}: sink was called without a target")
        elif outcome == "transform_failure":
            _assert(job.status == "FAILED", f"{stage}/{outcome}: Job is {job.status}")
            _assert("ETL_LOAD_FAILED" not in (job.error_message or ""), f"{stage}/{outcome}: sink error masked transform failure")
            _assert(calls == [], f"{stage}/{outcome}: sink was called after transform failure")
            _assert(not result, f"{stage}/{outcome}: transform result was persisted")
        else:
            _assert(job.status == "FAILED", f"{stage}/{outcome}: Job is {job.status}")
            _assert((job.error_message or "").startswith("ETL_LOAD_FAILED"), f"{stage}/{outcome}: wrong error {job.error_message!r}")
            _assert(result, f"{stage}/{outcome}: transform result was not preserved")
            if outcome == "sink_failure":
                _assert(calls, f"{stage}/{outcome}: sink was never called")
            con = sqlite3.connect(str(target_path))
            try:
                exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (f"out_{stage}",)).fetchone()
                rows = con.execute(f'SELECT x FROM "out_{stage}"').fetchall() if outcome == "empty_replace" else []
            finally:
                con.close()
            if outcome == "empty_replace":
                _assert(exists is not None, f"{stage}/{outcome}: target table was dropped")
                _assert([row[0] for row in rows] == ["keep"], f"{stage}/{outcome}: target rows changed")
            else:
                _assert(exists is None, f"{stage}/{outcome}: failed sink created a target table")
    engine.dispose()
    return 1


def main() -> int:
    cases = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for stage in STAGES:
            for outcome in OUTCOMES:
                cases += _run_case(root / f"{stage}-{outcome}", stage, outcome)
            cases += _run_case(root / f"{stage}-no_target", stage, "no_target")

    print(f"PASS: {cases} ETL sink cases across {len(STAGES)} stages and {len(OUTCOMES) + 1} outcomes.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1)
