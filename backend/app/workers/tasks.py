"""异步任务执行器：支持大表分批抽取 Parquet + L1–L4 全引擎 + 数据库/Redis 实时进度推送"""

import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import decrypt_secret
from app.demo.catalogs import supplier_catalog_for_sources
from app.models.entities import ArtifactVersion, DataSource, Job, LLMProvider
from app.models.etl import EtlSchedule
from app.services.connectors import extract_db_to_parquet, load_file
from app.services.etl import load_job_frame, resolve_target, write_dataframe
from app.services.engines import (
    build_demo_cross_db_tables,
    build_l4_tables,
    execute_clean_fields,
    execute_l1,
    execute_l2,
    execute_l4,
)
from app.services.storage import storage
from app.services.roster import roster_catalog_for_project
from app.workers.celery_app import celery_app
from app.services.unification.releases import execute_unification_run, recover_interrupted_run
from app.services.unification.telemetry import attach_trace_context, inject_trace_context


def _publish_progress(job_id: str, status: str, progress: int, message: str, db=None) -> None:
    own_db = db is None
    sess = SessionLocal() if own_db else db
    try:
        job = sess.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = status
            job.progress = progress
            job.stage_message = message
            sess.commit()
    finally:
        if own_db:
            sess.close()

    if settings.celery_enabled:
        try:
            import redis

            r = redis.from_url(settings.redis_url)
            r.publish(
                f"job_progress:{job_id}",
                json.dumps({"job_id": job_id, "status": status, "progress": progress, "message": message}, ensure_ascii=False),
            )
        except Exception:
            pass


def _load_datasource_dataframes(db, datasource_ids: List[str], progress_cb) -> List[pd.DataFrame]:
    dfs: List[pd.DataFrame] = []
    if not datasource_ids:
        return dfs
    sources = db.query(DataSource).filter(DataSource.id.in_(datasource_ids)).all()
    with tempfile.TemporaryDirectory() as tmp:
        for idx, ds in enumerate(sources):
            if ds.source_type == "file" and ds.object_key:
                local_f = os.path.join(tmp, os.path.basename(ds.object_key))
                storage.materialize_to_local(ds.object_key, local_f)
                loaded = load_file(local_f)
                if "df" in loaded:
                    dfs.append(loaded["df"])
            elif ds.source_type in ("duckdb", "sqlite", "mysql", "postgresql", "mongodb"):
                progress_cb(12 + idx * 5, f"正在安全抽取数据库表 {ds.table_name} 落盘 Parquet...")
                cfg = json.loads(decrypt_secret(ds.encrypted_config) or "{}")
                pq_local = os.path.join(tmp, f"extract_{ds.id}.parquet")
                extract_db_to_parquet(cfg, table_name=ds.table_name, out_parquet=pq_local, custom_sql=ds.custom_sql or None)
                storage.put_file(f"projects/{ds.project_id}/extracts/{ds.id}.parquet", pq_local)
                dfs.append(pd.read_parquet(pq_local))
    return dfs


def _resolve_llm_config(db, provider_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not provider_id:
        return None
    prov = db.query(LLMProvider).filter(LLMProvider.id == provider_id).first()
    if not prov:
        return None
    return {
        "provider_key": prov.provider_key,
        "name": prov.name,
        "base_url": prov.base_url,
        "model": prov.default_model,
        "api_key": decrypt_secret(prov.encrypted_api_key) if prov.encrypted_api_key else "",
    }


def run_job_sync(job_id: str) -> Dict[str, Any]:
    """核心任务执行体（Celery Worker 与本地同步模式共用）"""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return {"error": "Job not found"}

        def cb(pct: int, msg: str):
            _publish_progress(job_id, "RUNNING", pct, msg, db=db)

        cb(5, f"正在启动 {job.stage} 语义统一引擎...")
        params = job.params_json or {}
        ds_ids = params.get("datasource_ids") or []
        llm_cfg = _resolve_llm_config(db, params.get("provider_id"))

        if job.stage == "L1":
            dfs = _load_datasource_dataframes(db, ds_ids, cb)
            if not dfs:
                raise ValueError("L1 requires a data source; load the explicit demo fixture or add your own source first")
            df = dfs[0]
            clean_fields = params.get("clean_fields")
            if isinstance(clean_fields, list) and clean_fields:
                result = execute_clean_fields(db, job.project_id, job.id, df, clean_fields, progress_cb=cb)
            else:
                roster_catalog, roster_source = roster_catalog_for_project(db, job.project_id)
                if roster_catalog is not None:
                    catalog = roster_catalog
                    reference_catalog = {"kind": "roster", "name": roster_source.name, "source_id": roster_source.id}
                else:
                    sources = db.query(DataSource).filter(DataSource.project_id == job.project_id, DataSource.id.in_(ds_ids)).all()
                    demo_catalog = supplier_catalog_for_sources(sources)
                    if demo_catalog is not None:
                        catalog = demo_catalog
                        reference_catalog = {"kind": "demo", "name": None, "source_id": None}
                    else:
                        catalog = None
                        reference_catalog = {"kind": "none", "name": None, "source_id": None}
                target_col = params.get("target_col") or ("supplier_raw_name" if "supplier_raw_name" in df.columns else df.columns[0])
                amount_col = params.get("amount_col") or ("order_amount_cny" if "order_amount_cny" in df.columns else None)
                result = execute_l1(db, job.project_id, job.id, df, target_col=target_col, amount_col=amount_col, progress_cb=cb, demo_catalog=catalog)
                result["reference_catalog"] = reference_catalog

        elif job.stage == "L2":
            input_artifact_id = params.get("input_artifact_id")
            dfs = []
            if input_artifact_id:
                artifact = db.get(ArtifactVersion, input_artifact_id)
                if artifact is None or artifact.project_id != job.project_id:
                    raise ValueError("L2 输入产物不存在于当前项目")
                dfs = [pd.read_csv(io.BytesIO(storage.get_bytes(artifact.object_key)))]
            else:
                dfs = _load_datasource_dataframes(db, ds_ids, cb)
            if not dfs:
                dfs = list(build_demo_cross_db_tables(n_erp=20_000, n_wms=15_000))
            result = execute_l2(db, job.project_id, job.id, dfs, params=params, llm_cfg=llm_cfg, progress_cb=cb)

        elif job.stage == "L3":
            raise ValueError("L3 job execution is retired; use the contracts workflow")

        elif job.stage == "L4":
            dfs = _load_datasource_dataframes(db, ds_ids, cb) if ds_ids else []
            tables = None
            data_kind = "demo"
            if dfs:
                tables = build_l4_tables(dfs)
                data_kind = "sources"
            elif ds_ids:
                sources = db.query(DataSource).filter(
                    DataSource.project_id == job.project_id, DataSource.id.in_(ds_ids),
                ).all()
                if not any((source.profile_json or {}).get("has_metric_yaml") for source in sources):
                    raise ValueError("所选数据源没有可执行的表格数据，无法计算 L4 指标；请选择订单、物流、结算数据或加载 L4 示例。")
            result = execute_l4(
                db,
                job.project_id,
                job.id,
                yaml_text=params.get("yaml_text"),
                metric_name=params.get("metric_name", "net_gmv"),
                group_by=params.get("group_by", "order_channel"),
                progress_cb=cb,
                tables=tables,
                data_kind=data_kind,
            )
        else:
            raise ValueError(f"未知处理阶段: {job.stage}")

        etl_target_id = params.get("etl_target_datasource_id")
        if etl_target_id:
            try:
                target_ds, target_cfg = resolve_target(db, job.project_id, etl_target_id)
                frame = load_job_frame(db, job.stage, result)
                rows_written = write_dataframe(
                    target_cfg,
                    params.get("etl_target_table") or "",
                    frame,
                    params.get("etl_write_mode") or "replace",
                )
                result["etl"] = {
                    "status": "SUCCEEDED",
                    "target_type": target_ds.source_type,
                    "target_table": params.get("etl_target_table"),
                    "write_mode": params.get("etl_write_mode") or "replace",
                    "rows_written": int(rows_written),
                }
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                job.status = "FAILED"
                job.error_message = f"ETL_LOAD_FAILED: {message}"
                job.stage_message = f"ETL 落库失败: {message[:120]}"
                job.result_json = result
                db.commit()
                _publish_progress(job_id, "FAILED", 100, job.stage_message, db=db)
                return result

        job.status = "SUCCESS"
        job.progress = 100
        job.stage_message = f"{job.stage} 执行完成！"
        job.result_json = result
        db.commit()
        _publish_progress(job_id, "SUCCESS", 100, job.stage_message, db=db)
        return result
    except Exception as e:
        db.rollback()
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = "FAILED"
            job.error_message = str(e)
            job.stage_message = f"执行失败: {str(e)[:120]}"
            db.commit()
        _publish_progress(job_id, "FAILED", 100, f"执行失败: {str(e)[:120]}", db=db)
        raise
    finally:
        db.close()


if celery_app is not None:
    @celery_app.task(name="semantic_unifier.run_job")
    def run_job_celery(job_id: str):
        return run_job_sync(job_id)

    @celery_app.task(name="semantic_unifier.run_due_etl_schedules")
    def run_due_etl_schedules_celery():
        return run_due_etl_schedules()

    @celery_app.task(
        bind=True,
        name="semantic_unifier.run_unification",
        acks_late=True,
        reject_on_worker_lost=True,
        max_retries=2,
    )
    def run_unification_celery(self, run_id: str):
        with attach_trace_context(self.request.headers or {}):
            try:
                if self.request.delivery_info.get("redelivered"):
                    recovery_db = SessionLocal()
                    try:
                        recovered = recover_interrupted_run(
                            recovery_db, run_id, max_attempts=settings.unification_max_attempts,
                        )
                        if recovered.status == "FAILED":
                            return {"run_id": run_id, "status": "FAILED", "error": recovered.error_message}
                    finally:
                        recovery_db.close()
                return run_unification_sync(run_id)
            except Exception as exc:
                status_db = SessionLocal()
                try:
                    from app.models.unification import UnificationRun

                    current = status_db.get(UnificationRun, run_id)
                    if current is not None and current.status == "FAILED":
                        raise exc
                finally:
                    status_db.close()
                countdown = min(30, 2 ** (self.request.retries + 1))
                raise self.retry(exc=exc, countdown=countdown)


def dispatch_job(job_id: str) -> None:
    """生产模式推给 Celery Worker；本地测试模式直接同步跑完"""
    if settings.celery_enabled and celery_app is not None:
        run_job_celery.delay(job_id)
    else:
        run_job_sync(job_id)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def schedule_job_params(schedule: EtlSchedule) -> Dict[str, Any]:
    """Build Job params from a schedule, always carrying the ETL sink options."""
    params = dict(schedule.params or {})
    params["datasource_ids"] = list(schedule.datasource_ids or [])
    params["etl_target_datasource_id"] = schedule.target_datasource_id
    params["etl_target_table"] = schedule.target_table
    params["etl_write_mode"] = schedule.write_mode
    return params


def dispatch_schedule_now(db, schedule: EtlSchedule) -> str:
    """Create a normal Job from a schedule, advance it, commit, then dispatch."""
    now = datetime.now(timezone.utc)
    job = Job(
        project_id=schedule.project_id,
        stage=schedule.stage,
        status="PENDING",
        progress=0,
        stage_message="定时任务已创建，等待执行...",
        params_json=schedule_job_params(schedule),
    )
    db.add(job)
    db.flush()
    schedule.last_run_at = now
    schedule.last_job_id = job.id
    schedule.next_run_at = now + timedelta(minutes=schedule.interval_minutes)
    db.commit()
    dispatch_job(job.id)
    return job.id


def run_due_etl_schedules() -> Dict[str, Any]:
    """Dispatch one Job for every enabled schedule whose next_run_at has passed.

    Each schedule is committed (and advanced by its interval) before dispatch, so a
    failed dispatch leaves it enabled and due on the next tick instead of double-running.
    """
    db = SessionLocal()
    dispatched = 0
    try:
        now = datetime.now(timezone.utc)
        due = [
            s
            for s in db.query(EtlSchedule).filter(EtlSchedule.enabled.is_(True)).all()
            if _as_utc(s.next_run_at) <= now
        ]
        due.sort(key=lambda s: _as_utc(s.next_run_at))
        for schedule in due:
            try:
                dispatch_schedule_now(db, schedule)
                dispatched += 1
            except Exception:
                db.rollback()
                continue
        return {"dispatched": dispatched}
    finally:
        db.close()


def run_unification_sync(run_id: str) -> dict[str, Any]:
    """Run one idempotently submitted unification task and persist its staged result."""
    db = SessionLocal()
    try:
        run = execute_unification_run(db, run_id, max_attempts=settings.unification_max_attempts)
        return run.result_json or {}
    finally:
        db.close()


def dispatch_unification_run(run_id: str) -> None:
    if settings.celery_enabled and celery_app is not None:
        run_unification_celery.apply_async(args=[run_id], headers=inject_trace_context())
    else:
        run_unification_sync(run_id)
