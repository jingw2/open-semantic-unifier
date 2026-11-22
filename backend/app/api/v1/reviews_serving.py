"""人工复核队列与映射沉淀 + 产物草稿/发布/回滚 + 对外生产 Serving API（项目级 X-API-Key 鉴权）"""

import os
import tempfile
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_role, verify_serving_api_key
from app.core.db import get_db
from app.core.security import generate_project_api_key
from app.models.entities import ArtifactVersion, CanonicalMapping, Project, ProjectApiKey, ReviewItem
from app.services.engines import (
    MASTER_SUPPLIERS,
    _calc_supplier_score,
    compile_and_query_metric_yaml,
    validate_with_schema_text,
)
from app.services.storage import storage

router = APIRouter(prefix="/api/v1", tags=["HITL Review, Release & Serving API"])


class ReviewDecisionRequest(BaseModel):
    decision: str  # APPROVED | REJECTED
    canonical_id: Optional[str] = None
    canonical_name: Optional[str] = None


class CreateApiKeyRequest(BaseModel):
    name: str


class ServeReconcileRequest(BaseModel):
    query: str


class ServeValidateRequest(BaseModel):
    entity_name: str = "ShipmentOrder"
    payload: Dict[str, Any]


class ServeMetricQueryRequest(BaseModel):
    metric_name: str = "net_gmv"
    group_by: Optional[str] = "order_channel"


# ==================== 1. 人工复核队列 & 可复用映射字典 ====================

@router.get("/projects/{project_id}/reviews")
def list_reviews(project_id: str, ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.query(ReviewItem).filter(ReviewItem.project_id == project_id).order_by(ReviewItem.created_at.desc()).limit(50).all()
    mappings = db.query(CanonicalMapping).filter(CanonicalMapping.project_id == project_id).order_by(CanonicalMapping.created_at.desc()).limit(100).all()
    return {
        "reviews": [
            {
                "id": r.id,
                "stage": r.stage,
                "case_type": r.case_type,
                "left_text": r.left_text,
                "right_text": r.right_text,
                "score": r.score,
                "llm_suggestion": r.llm_suggestion,
                "decision": r.decision,
            }
            for r in items
        ],
        "mappings": [
            {
                "id": m.id,
                "raw_alias": m.raw_alias,
                "canonical_id": m.canonical_id,
                "canonical_name": m.canonical_name,
                "source": m.source,
            }
            for m in mappings
        ],
    }


@router.post("/reviews/{review_id}/decide")
def decide_review(
    review_id: str,
    req: ReviewDecisionRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    if req.decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="decision 仅支持 APPROVED 或 REJECTED")
    item = db.query(ReviewItem).filter(ReviewItem.id == review_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="复核条目不存在")

    item.decision = req.decision
    mapping_created = None
    if req.decision == "APPROVED":
        cid = req.canonical_id or (item.llm_suggestion or {}).get("recommended_id") or f"STD-{item.id[:6].upper()}"
        cname = req.canonical_name or (item.llm_suggestion or {}).get("recommended_name") or item.right_text
        item.resolved_canonical_id = cid
        item.resolved_canonical_name = cname

        # 沉淀为项目级可复用映射字典（下次跑 L1/L2 或调对外 Serving API 自动生效）
        existing = (
            db.query(CanonicalMapping)
            .filter(CanonicalMapping.project_id == item.project_id, CanonicalMapping.raw_alias == item.left_text)
            .first()
        )
        if existing:
            existing.canonical_id = cid
            existing.canonical_name = cname
            existing.source = "HUMAN_REVIEW"
            mapping_created = existing
        else:
            m = CanonicalMapping(
                project_id=item.project_id,
                raw_alias=item.left_text,
                canonical_id=cid,
                canonical_name=cname,
                source="HUMAN_REVIEW",
            )
            db.add(m)
            db.flush()
            mapping_created = m

    db.commit()
    return {
        "review_id": item.id,
        "decision": item.decision,
        "mapping_saved": {"raw_alias": mapping_created.raw_alias, "canonical_id": mapping_created.canonical_id, "canonical_name": mapping_created.canonical_name} if mapping_created else None,
    }


# ==================== 2. 产物版本管理：草稿 / 发布 / 一键回滚 / 下载 ====================

@router.get("/projects/{project_id}/artifacts")
def list_artifacts(project_id: str, ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(ArtifactVersion).filter(ArtifactVersion.project_id == project_id).order_by(ArtifactVersion.created_at.desc()).limit(60).all()
    return [
        {
            "id": a.id,
            "job_id": a.job_id,
            "version_no": a.version_no,
            "artifact_type": a.artifact_type,
            "release_status": a.release_status,
            "filename": a.filename,
            "size_bytes": a.size_bytes,
            "meta": a.meta_json,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/artifacts/{artifact_id}/publish")
def publish_artifact_version(
    artifact_id: str,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    """草稿 → 发布（同时将同项目同类型的旧 PUBLISHED 版本置为 ARCHIVED，支持随时回滚到任意历史版本）"""
    art = db.query(ArtifactVersion).filter(ArtifactVersion.id == artifact_id).first()
    if not art:
        raise HTTPException(status_code=404, detail="产物版本不存在")

    db.query(ArtifactVersion).filter(
        ArtifactVersion.project_id == art.project_id,
        ArtifactVersion.artifact_type == art.artifact_type,
        ArtifactVersion.release_status == "PUBLISHED",
    ).update({"release_status": "ARCHIVED"})

    art.release_status = "PUBLISHED"
    db.commit()
    return {"id": art.id, "artifact_type": art.artifact_type, "version_no": art.version_no, "release_status": "PUBLISHED"}


@router.get("/artifacts/{artifact_id}/content")
def download_artifact(artifact_id: str, db: Session = Depends(get_db)):
    art = db.query(ArtifactVersion).filter(ArtifactVersion.id == artifact_id).first()
    if not art:
        raise HTTPException(status_code=404, detail="产物不存在")
    raw = storage.get_bytes(art.object_key)
    media = "text/html; charset=utf-8" if art.filename.endswith(".html") else "application/octet-stream"
    headers = {} if art.filename.endswith(".html") else {"Content-Disposition": f'attachment; filename="{art.filename}"'}
    return Response(content=raw, media_type=media, headers=headers)


# ==================== 3. 项目级 Serving API Key 管理 ====================

@router.get("/projects/{project_id}/api-keys")
def list_project_api_keys(project_id: str, ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(ProjectApiKey).filter(ProjectApiKey.project_id == project_id).order_by(ProjectApiKey.created_at.desc()).all()
    return [{"id": k.id, "name": k.name, "key_prefix": k.key_prefix, "is_active": k.is_active, "created_at": k.created_at.isoformat()} for k in rows]


@router.post("/projects/{project_id}/api-keys")
def create_project_api_key(
    project_id: str,
    req: CreateApiKeyRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    raw_key, prefix, kh = generate_project_api_key()
    rec = ProjectApiKey(project_id=project_id, name=req.name.strip(), key_prefix=prefix, key_hash=kh, is_active=True)
    db.add(rec)
    db.commit()
    return {"id": rec.id, "name": rec.name, "key_prefix": prefix, "api_key_once": raw_key}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: str, ctx: Dict[str, str] = Depends(require_role("admin", "editor")), db: Session = Depends(get_db)):
    rec = db.query(ProjectApiKey).filter(ProjectApiKey.id == key_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    rec.is_active = False
    db.commit()
    return {"revoked": True}


# ==================== 4. 对外生产 Serving API（X-API-Key 鉴权） ====================

@router.post("/serve/{project_slug}/reconcile")
def serve_reconcile(
    req: ServeReconcileRequest,
    proj: Project = Depends(verify_serving_api_key),
    db: Session = Depends(get_db),
):
    """对外标准实体对齐 API：优先命中人工沉淀映射字典，其次匹配标准花名册"""
    q = req.query.strip()
    hit = db.query(CanonicalMapping).filter(CanonicalMapping.project_id == proj.id, CanonicalMapping.raw_alias == q).first()
    if hit:
        return {"project": proj.slug, "query": q, "matched": True, "source": "HUMAN_REVIEW_DICT", "canonical_id": hit.canonical_id, "canonical_name": hit.canonical_name, "score": 100.0}

    cands = []
    for item in MASTER_SUPPLIERS:
        sc = _calc_supplier_score(q, item)
        if sc >= 50:
            cands.append({"canonical_id": item["id"], "canonical_name": item["name"], "score": sc})
    cands.sort(key=lambda x: x["score"], reverse=True)
    if cands:
        top = cands[0]
        return {"project": proj.slug, "query": q, "matched": top["score"] >= 80, "source": "MASTER_CATALOG", **top, "candidates": cands[:3]}
    return {"project": proj.slug, "query": q, "matched": False, "source": "NONE", "candidates": []}


@router.post("/serve/{project_slug}/validate")
def serve_validate_contract(
    req: ServeValidateRequest,
    proj: Project = Depends(verify_serving_api_key),
    db: Session = Depends(get_db),
):
    """对外契约校验 API：严格使用当前 PUBLISHED 状态的 L3 LinkML JSON Schema"""
    pub = (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.project_id == proj.id, ArtifactVersion.artifact_type == "l3_contract", ArtifactVersion.release_status == "PUBLISHED")
        .order_by(ArtifactVersion.version_no.desc())
        .first()
    )
    if not pub:
        raise HTTPException(status_code=409, detail="该项目尚未发布任何 L3 契约版本（请先在发布中心点击“发布到线上 API”）。")
    schema_text = storage.get_bytes(pub.object_key).decode("utf-8")
    res = validate_with_schema_text(schema_text, req.entity_name, req.payload)
    return {"project": proj.slug, "contract_version": pub.version_no, **res}


@router.post("/serve/{project_slug}/metrics/query")
def serve_query_metric(
    req: ServeMetricQueryRequest,
    proj: Project = Depends(verify_serving_api_key),
    db: Session = Depends(get_db),
):
    """对外指标语义层 API：严格使用当前 PUBLISHED 状态的 L4 YAML 指标说明书编译查询"""
    pub = (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.project_id == proj.id, ArtifactVersion.artifact_type == "l4_metrics", ArtifactVersion.release_status == "PUBLISHED")
        .order_by(ArtifactVersion.version_no.desc())
        .first()
    )
    if not pub:
        raise HTTPException(status_code=409, detail="该项目尚未发布任何 L4 指标版本（请先在发布中心点击“发布到线上 API”）。")
    yaml_text = storage.get_bytes(pub.object_key).decode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        duck_path = os.path.join(tmp, "unified.duckdb")
        storage.materialize_to_local(f"projects/{proj.id}/duckdb/unified.duckdb", duck_path)
        sql, rows, meta = compile_and_query_metric_yaml(yaml_text, duck_path, metric_name=req.metric_name, group_by=req.group_by)
    return {"project": proj.slug, "metrics_version": pub.version_no, "metric": req.metric_name, "label": meta.get("label"), "compiled_sql": sql, "rows": rows}
