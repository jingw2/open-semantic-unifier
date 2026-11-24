"""多模型 Provider 管理（Fernet 加密落库 + 脱敏返回）+ 异步任务提交 + SSE 实时进度流"""

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.api.deps import require_resource_access, get_current_user, require_role
from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.models.entities import DataSource, Job, LLMProvider
from app.services.decision_models.contracts import DecisionError, DecisionRequest, DecisionSuggestion
from app.services.decision_models.providers import DECISION_PROVIDER_KINDS, adapter_for_kind
from app.services.llm_client import BUILTIN_PROVIDERS, ProviderConfig, test_llm_connectivity
from app.workers.tasks import dispatch_job

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_resource_access)], tags=["LLM Providers & Async Jobs"])

PROVIDER_KINDS = {"openai_compatible", "perplexity_decisions", "typesafe_jev"}
DEFAULT_PROMPT_VERSION = "entity-pair-choice-v1"


class SaveProviderRequest(BaseModel):
    provider_key: str
    name: str
    base_url: str = ""
    default_model: str = ""
    api_key: str = ""
    is_custom: bool = True
    is_default: bool = False
    provider_kind: str = "openai_compatible"
    send_fields: List[str] = []
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_retries: int = Field(default=0, ge=0)
    prompt_version: str = DEFAULT_PROMPT_VERSION

    @field_validator("provider_kind")
    @classmethod
    def validate_provider_kind(cls, value: str) -> str:
        if value not in PROVIDER_KINDS:
            raise ValueError("provider_kind must be one of openai_compatible, perplexity_decisions, typesafe_jev")
        return value

    @field_validator("send_fields")
    @classmethod
    def validate_send_fields(cls, fields: List[str]) -> List[str]:
        if any(not isinstance(field, str) or not field.strip() for field in fields):
            raise ValueError("send_fields entries must be non-empty strings")
        return fields

    @field_validator("prompt_version")
    @classmethod
    def validate_prompt_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt_version must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_decision_send_fields(self):
        if self.provider_kind in DECISION_PROVIDER_KINDS and not self.send_fields:
            raise ValueError("decision providers require at least one send field")
        return self


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
            "provider_kind": p.provider_kind,
            "send_fields": p.send_fields_json,
            "timeout_seconds": p.timeout_seconds,
            "max_retries": p.max_retries,
            "prompt_version": p.prompt_version,
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
    base_url = req.base_url.strip()
    default_model = req.default_model.strip()
    if req.provider_kind in DECISION_PROVIDER_KINDS:
        preset = next((item for item in BUILTIN_PROVIDERS if item["provider_key"] == req.provider_key), None)
        if preset and not base_url:
            base_url = preset["base_url"]
        if preset and not default_model:
            default_model = preset["default_model"]
    timeout_seconds = req.timeout_seconds if req.timeout_seconds is not None else settings.llm_timeout_seconds
    p = LLMProvider(
        workspace_id=ctx["workspace_id"],
        provider_key=req.provider_key.strip(),
        name=req.name.strip(),
        base_url=base_url,
        default_model=default_model,
        encrypted_api_key=encrypt_secret(req.api_key.strip()) if req.api_key.strip() else "",
        is_custom=req.is_custom,
        is_default=req.is_default,
        provider_kind=req.provider_kind,
        send_fields_json=list(req.send_fields),
        timeout_seconds=timeout_seconds,
        max_retries=req.max_retries,
        prompt_version=req.prompt_version.strip(),
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
        "provider_kind": p.provider_kind,
        "send_fields": p.send_fields_json,
        "timeout_seconds": p.timeout_seconds,
        "max_retries": p.max_retries,
        "prompt_version": p.prompt_version,
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
    if p.provider_kind in DECISION_PROVIDER_KINDS:
        return _test_decision_provider(p)
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


def _test_decision_provider(p: LLMProvider) -> dict[str, Any]:
    """Run one controlled Acme/Acme sample pair through a configured decision provider."""
    adapter = adapter_for_kind(p.provider_kind)
    if adapter is None:
        return {"status": "error", "message": f"unsupported provider kind {p.provider_kind}"}
    try:
        api_key = decrypt_secret(p.encrypted_api_key) if p.encrypted_api_key else ""
    except Exception:
        return {"status": "error", "message": "credential_unavailable: provider credentials cannot be decrypted"}
    outcome = adapter.invoke(DecisionRequest(
        project_id="provider-test",
        run_id="provider-test",
        input_version="provider-test",
        scheme_revision="provider-test",
        prompt_version=p.prompt_version or DEFAULT_PROMPT_VERSION,
        entity_type="organization",
        identity_granularity="legal_entity",
        left={"name": "Acme"},
        right={"name": "Acme"},
        field_names=tuple(p.send_fields_json) if p.send_fields_json else ("name",),
    ), ProviderConfig(
        provider_key=p.provider_key,
        model=p.default_model,
        base_url=p.base_url,
        api_key=api_key,
        name=p.name,
        timeout_seconds=p.timeout_seconds or settings.llm_timeout_seconds,
        max_retries=p.max_retries,
    ))
    if isinstance(outcome, DecisionSuggestion):
        return {
            "status": "ok",
            "message": f"{p.provider_kind} returned {outcome.verdict.value} with confidence {outcome.confidence}",
            "verdict": outcome.verdict.value,
            "confidence": outcome.confidence,
            "model": outcome.model_version,
            "provider_kind": outcome.provider_kind,
            "prompt_version": outcome.prompt_version,
        }
    if isinstance(outcome, DecisionError):
        return {"status": "error", "message": f"{outcome.code}: {outcome.message}", "code": outcome.code, "retryable": outcome.retryable}
    return {"status": "error", "message": "decision provider returned no verdict and is disabled"}


@router.post("/projects/{project_id}/jobs")
def submit_job(
    project_id: str,
    req: SubmitJobRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    st = req.stage.upper()
    if st == "L3":
        raise HTTPException(
            status_code=410,
            detail="L3 契约统一已迁移到 Contracts API；请在契约统一工作台创建版本，旧 L3 作业入口已停用。",
        )
    if st not in ("L1", "L2", "L4"):
        raise HTTPException(status_code=400, detail=f"不支持的阶段: {req.stage}")

    source_ids = set(req.datasource_ids)
    valid_sources = db.query(DataSource.id).filter(
        DataSource.id.in_(source_ids), DataSource.project_id == project_id
    ).all()
    if {row[0] for row in valid_sources} != source_ids:
        raise HTTPException(status_code=404, detail="数据源不存在于当前项目")
    if req.provider_id and not db.query(LLMProvider).filter(
        LLMProvider.id == req.provider_id,
        LLMProvider.workspace_id == ctx["workspace_id"],
    ).first():
        raise HTTPException(status_code=404, detail="Provider 不存在")

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
