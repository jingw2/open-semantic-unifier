"""多模型 Provider 管理（Fernet 加密落库 + 脱敏返回）+ 异步任务提交 + SSE 实时进度流"""

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_role
from app.core.db import SessionLocal, get_db
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.models.entities import Job, LLMProvider
from app.services.llm_client import BUILTIN_PROVIDERS, test_llm_connectivity
from app.workers.tasks import dispatch_job

router = APIRouter(prefix="/api/v1", tags=["LLM Providers & Async Jobs"])


class SaveProviderRequest(BaseModel):
    provider_key: str
    name: str
    base_url: str = ""
    default_model: str = ""
    api_key: str = ""
    is_custom: bool = True
    is_default: bool = False


class SubmitJobRequest(BaseModel):
    stage: str  # L1 | L2 | L3 | L4
    datasource_ids: List[str] = []
    provider_id: Optional[str] = None
    params: Dict[str, Any] = {}


@router.get("/providers")
def list_providers(ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    saved = db.query(LLMProvider).filter(LLMProvider.workspace_id == ctx["workspace_id"]).all()
    saved_out = [
        {
            "id": p.id,
            "provider_key": p.provider_key,
            "name": p.name,
            "base_url": p.base_url,
            "default_model": p.default_model,
            "api_key_masked": mask_secret(decrypt_secret(p.encrypted_api_key)) if p.encrypted_api_key else "",
            "is_custom": p.is_custom,
            "is_default": p.is_default,
        }
        for p in saved
    ]
    return {"builtin_presets": BUILTIN_PROVIDERS, "configured": saved_out}


@router.post("/providers")
def create_provider(
    req: SaveProviderRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    if req.is_default:
        db.query(LLMProvider).filter(LLMProvider.workspace_id == ctx["workspace_id"]).update({"is_default": False})
    p = LLMProvider(
        workspace_id=ctx["workspace_id"],
        provider_key=req.provider_key.strip(),
        name=req.name.strip(),
        base_url=req.base_url.strip(),
        default_model=req.default_model.strip(),
        encrypted_api_key=encrypt_secret(req.api_key.strip()) if req.api_key.strip() else "",
        is_custom=req.is_custom,
        is_default=req.is_default,
    )
    db.add(p)
    db.commit()
    return {
        "id": p.id,
        "provider_key": p.provider_key,
        "name": p.name,
        "base_url": p.base_url,
        "default_model": p.default_model,
        "api_key_masked": mask_secret(req.api_key.strip()) if req.api_key.strip() else "",
        "is_custom": p.is_custom,
        "is_default": p.is_default,
    }


@router.delete("/providers/{provider_id}")
def delete_provider(
    provider_id: str,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    p = db.query(LLMProvider).filter(LLMProvider.id == provider_id, LLMProvider.workspace_id == ctx["workspace_id"]).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    db.delete(p)
    db.commit()
    return {"deleted": True}


@router.post("/providers/{provider_id}/test")
def test_saved_provider(
    provider_id: str,
    ctx: Dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = db.query(LLMProvider).filter(LLMProvider.id == provider_id, LLMProvider.workspace_id == ctx["workspace_id"]).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    try:
        return test_llm_connectivity(
            {
                "provider_key": p.provider_key,
                "name": p.name,
                "base_url": p.base_url,
                "model": p.default_model,
                "api_key": decrypt_secret(p.encrypted_api_key) if p.encrypted_api_key else "",
            }
        )
    except Exception as e:
        return {"status": "error", "message": f"连接失败: {e}"}


@router.post("/projects/{project_id}/jobs")
def submit_job(
    project_id: str,
    req: SubmitJobRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    st = req.stage.upper()
    if st not in ("L1", "L2", "L3", "L4"):
        raise HTTPException(status_code=400, detail=f"不支持的阶段: {req.stage}")

    full_params = dict(req.params)
    full_params["datasource_ids"] = req.datasource_ids
    full_params["provider_id"] = req.provider_id

    job = Job(project_id=project_id, stage=st, status="PENDING", progress=0, stage_message="任务已创建，等待执行...", params_json=full_params)
    db.add(job)
    db.commit()
    db.refresh(job)

    dispatch_job(job.id)
    db.refresh(job)
    return {
        "id": job.id,
        "stage": job.stage,
        "status": job.status,
        "progress": job.progress,
        "stage_message": job.stage_message,
        "result": job.result_json,
        "error_message": job.error_message,
    }


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: str, ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(Job).filter(Job.project_id == project_id).order_by(Job.created_at.desc()).limit(30).all()
    return [
        {
            "id": j.id,
            "stage": j.stage,
            "status": j.status,
            "progress": j.progress,
            "stage_message": j.stage_message,
            "result": j.result_json,
            "error_message": j.error_message,
            "created_at": j.created_at.isoformat(),
        }
        for j in rows
    ]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    j = db.query(Job).filter(Job.id == job_id).first()
    if not j:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "id": j.id,
        "stage": j.stage,
        "status": j.status,
        "progress": j.progress,
        "stage_message": j.stage_message,
        "result": j.result_json,
        "error_message": j.error_message,
    }


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str):
    """SSE 实时推送任务进度，前端可直接 EventSource 订阅"""
    async def event_gen():
        for _ in range(120):
            db = SessionLocal()
            try:
                j = db.query(Job).filter(Job.id == job_id).first()
                if not j:
                    yield f"data: {json.dumps({'status': 'NOT_FOUND'})}\n\n"
                    return
                payload = {"id": j.id, "status": j.status, "progress": j.progress, "message": j.stage_message}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if j.status in ("SUCCESS", "FAILED"):
                    return
            finally:
                db.close()
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
