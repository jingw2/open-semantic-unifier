"""Persisted ETL schedule CRUD and on-demand dispatch."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_resource_access, require_role
from app.core.db import get_db
from app.models.entities import DataSource
from app.models.etl import EtlSchedule
from app.services.etl import SUPPORTED_SINK_TYPES, validate_table_name
from app.workers.tasks import dispatch_schedule_now

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_resource_access)], tags=["ETL Schedules"])

VALID_STAGES = {"L1", "L2", "L4"}
VALID_WRITE_MODES = {"replace", "append"}


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _schedule_payload(schedule: EtlSchedule) -> Dict[str, Any]:
    return {
        "id": schedule.id,
        "project_id": schedule.project_id,
        "name": schedule.name,
        "stage": schedule.stage,
        "datasource_ids": schedule.datasource_ids,
        "params": schedule.params,
        "target_datasource_id": schedule.target_datasource_id,
        "target_table": schedule.target_table,
        "write_mode": schedule.write_mode,
        "interval_minutes": schedule.interval_minutes,
        "enabled": schedule.enabled,
        "next_run_at": _utc_iso(schedule.next_run_at),
        "last_run_at": _utc_iso(schedule.last_run_at),
        "last_job_id": schedule.last_job_id,
        "created_at": _utc_iso(schedule.created_at),
    }


class CreateScheduleRequest(BaseModel):
    name: str
    stage: str
    datasource_ids: List[str] = []
    params: Dict[str, Any] = {}
    target_datasource_id: str
    target_table: str
    write_mode: str = "replace"
    interval_minutes: int = Field(ge=5, le=10080)
    enabled: bool = True


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    stage: Optional[str] = None
    datasource_ids: Optional[List[str]] = None
    params: Optional[Dict[str, Any]] = None
    target_datasource_id: Optional[str] = None
    target_table: Optional[str] = None
    write_mode: Optional[str] = None
    interval_minutes: Optional[int] = Field(default=None, ge=5, le=10080)
    enabled: Optional[bool] = None


def _get_schedule(db: Session, project_id: str, schedule_id: str) -> EtlSchedule:
    schedule = (
        db.query(EtlSchedule)
        .filter(EtlSchedule.id == schedule_id, EtlSchedule.project_id == project_id)
        .first()
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return schedule


def _validate_schedule(
    db: Session,
    project_id: str,
    stage: str,
    datasource_ids: List[str],
    target_datasource_id: str,
    target_table: str,
    write_mode: str,
) -> None:
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"不支持的阶段: {stage}")
    if write_mode not in VALID_WRITE_MODES:
        raise HTTPException(status_code=400, detail="写模式必须是 replace 或 append")
    try:
        validate_table_name(target_table)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if datasource_ids:
        found = {
            row[0]
            for row in db.query(DataSource.id)
            .filter(DataSource.id.in_(datasource_ids), DataSource.project_id == project_id)
            .all()
        }
        if found != set(datasource_ids):
            raise HTTPException(status_code=404, detail="数据源不存在于当前项目")
    target = db.query(DataSource).filter(
        DataSource.id == target_datasource_id, DataSource.project_id == project_id
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail="目标数据源不存在于当前项目")
    if target.source_type not in SUPPORTED_SINK_TYPES:
        raise HTTPException(status_code=400, detail=f"目标数据源不是可写入的数据库类型: {target.source_type}")


@router.post("/projects/{project_id}/etl/schedules", status_code=201)
def create_schedule(
    project_id: str,
    req: CreateScheduleRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    _validate_schedule(
        db, project_id, req.stage, req.datasource_ids, req.target_datasource_id, req.target_table, req.write_mode
    )
    now = datetime.now(timezone.utc)
    schedule = EtlSchedule(
        project_id=project_id,
        name=req.name.strip(),
        stage=req.stage,
        datasource_ids=list(req.datasource_ids),
        params=dict(req.params),
        target_datasource_id=req.target_datasource_id,
        target_table=req.target_table,
        write_mode=req.write_mode,
        interval_minutes=req.interval_minutes,
        enabled=req.enabled,
        next_run_at=now + timedelta(minutes=req.interval_minutes),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return _schedule_payload(schedule)


@router.get("/projects/{project_id}/etl/schedules")
def list_schedules(
    project_id: str,
    ctx: Dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(EtlSchedule)
        .filter(EtlSchedule.project_id == project_id)
        .order_by(EtlSchedule.created_at.desc())
        .all()
    )
    return [_schedule_payload(s) for s in rows]


@router.patch("/projects/{project_id}/etl/schedules/{schedule_id}")
def update_schedule(
    project_id: str,
    schedule_id: str,
    req: UpdateScheduleRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    schedule = _get_schedule(db, project_id, schedule_id)
    data = req.model_dump(exclude_unset=True)
    if not data:
        return _schedule_payload(schedule)

    stage = data.get("stage", schedule.stage)
    datasource_ids = data.get("datasource_ids", schedule.datasource_ids)
    target_datasource_id = data.get("target_datasource_id", schedule.target_datasource_id)
    target_table = data.get("target_table", schedule.target_table)
    write_mode = data.get("write_mode", schedule.write_mode)
    _validate_schedule(db, project_id, stage, datasource_ids, target_datasource_id, target_table, write_mode)

    for field, value in data.items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)
    return _schedule_payload(schedule)


@router.delete("/projects/{project_id}/etl/schedules/{schedule_id}")
def delete_schedule(
    project_id: str,
    schedule_id: str,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    schedule = _get_schedule(db, project_id, schedule_id)
    db.delete(schedule)
    db.commit()
    return {"deleted": True}


@router.post("/projects/{project_id}/etl/schedules/{schedule_id}/run-now")
def run_schedule_now(
    project_id: str,
    schedule_id: str,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    schedule = _get_schedule(db, project_id, schedule_id)
    job_id = dispatch_schedule_now(db, schedule)
    db.refresh(schedule)
    return {
        "job_id": job_id,
        "last_run_at": _utc_iso(schedule.last_run_at),
        "next_run_at": _utc_iso(schedule.next_run_at),
    }
