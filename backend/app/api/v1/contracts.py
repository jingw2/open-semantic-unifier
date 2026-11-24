"""Project-scoped contract sources, model versions, releases, and exports for the L3 workbench."""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_resource_access, require_role
from app.core.db import get_db
from app.models.contracts import ContractAuditRecord, ContractModelVersion, ContractReleaseState, ContractSource
from app.services.contracts.releases import (
    ContractReleasesError,
    ContractReleaseConflict,
    ContractSnapshotNotFound,
    ContractSourceHashMismatch,
    ContractSourceInvalid,
    ContractSourceUnavailable,
    ContractVersionInvalid,
    ContractVersionNotFound,
    create_contract_version,
    export_contract_version,
    get_contract_release,
    get_contract_version,
    list_contract_audit,
    list_contract_versions,
    publish_contract_version,
    rollback_contract_version,
)
from app.services.contracts.sources import (
    ContractSourceError,
    ContractSourceInvalid as SourceSelectInvalid,
    ContractSourceNotFound,
    ContractSourceTooLarge,
    create_contract_source,
    list_contract_sources,
)
from app.services.unification.telemetry import start_span

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_resource_access)],
    tags=["Contracts"],
)


class ContractSourceCreateRequest(BaseModel):
    data_source_id: str = Field(min_length=1, max_length=32)


class ContractVersionCreateRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=32)
    snapshot_id: str = Field(min_length=1, max_length=32)
    allowed_new_entity_types: list[str] = []


class ContractVersionPublishRequest(BaseModel):
    expected_current_version: int | None = None
    reason: str = Field(min_length=1)


class ContractVersionRollbackRequest(BaseModel):
    expected_current_version: int
    reason: str = Field(min_length=1)


def _isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def contract_source_payload(source: ContractSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "project_id": source.project_id,
        "data_source_id": source.data_source_id,
        "filename": source.filename,
        "format": source.format,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "created_by": source.created_by,
        "created_at": _isoformat_utc(source.created_at),
    }


def contract_version_payload(version: ContractModelVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "project_id": version.project_id,
        "version": version.version,
        "status": version.status,
        "source_id": version.source_id,
        "source_sha256": version.source_sha256,
        "entity_snapshot_id": version.entity_snapshot_id,
        "snapshot_version": version.snapshot_version,
        "counts": version.validation_json.get("counts", {}),
        "compiler_versions": version.compiler_versions_json,
        "created_by": version.created_by,
        "created_at": _isoformat_utc(version.created_at),
    }


def _release_payload(state: ContractReleaseState | None, project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "current_version": state.current_version if state else 0,
        "current_version_id": state.current_version_id if state else None,
    }


def contract_audit_payload(record: ContractAuditRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "project_id": record.project_id,
        "version_id": record.version_id,
        "action": record.action,
        "actor_id": record.actor_id,
        "reason": record.reason,
        "from_version": record.from_version,
        "to_version": record.to_version,
        "trace_id": record.trace_id,
        "created_at": _isoformat_utc(record.created_at),
    }


@router.post("/projects/{project_id}/contracts/sources", status_code=status.HTTP_201_CREATED)
def select_contract_source(
    project_id: str,
    request: ContractSourceCreateRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        source = create_contract_source(
            db, project_id=project_id, data_source_id=request.data_source_id, actor_id=ctx["user_id"],
        )
    except ContractSourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContractSourceTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except SourceSelectInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ContractSourceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return contract_source_payload(source)


@router.get("/projects/{project_id}/contracts/sources")
def list_project_contract_sources(
    project_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [contract_source_payload(source) for source in list_contract_sources(db, project_id=project_id)]


@router.post("/projects/{project_id}/contracts/versions", status_code=status.HTTP_201_CREATED)
def create_contract_model_version(
    project_id: str,
    request: ContractVersionCreateRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        version = create_contract_version(
            db, project_id=project_id, source_id=request.source_id, snapshot_id=request.snapshot_id,
            allowed_new_entity_types=request.allowed_new_entity_types, actor_id=ctx["user_id"],
        )
    except (ContractSourceNotFound, ContractSnapshotNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContractSourceHashMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ContractVersionInvalid as exc:
        if exc.issues:
            raise HTTPException(status_code=422, detail=[
                {"path": issue.path, "code": issue.code, "message": issue.message} for issue in exc.issues
            ]) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ContractSourceUnavailable, ContractSourceInvalid) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ContractReleasesError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return contract_version_payload(version)


@router.get("/projects/{project_id}/contracts/versions")
def list_contract_model_versions(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows, total = list_contract_versions(db, project_id=project_id, limit=limit, offset=offset)
    return {"items": [contract_version_payload(item) for item in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/projects/{project_id}/contracts/versions/{version_id}")
def get_contract_model_version(
    project_id: str,
    version_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    version = get_contract_version(db, project_id=project_id, version_id=version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="contract version not found")
    return contract_version_payload(version)


@router.post("/projects/{project_id}/contracts/versions/{version_id}/publish", status_code=status.HTTP_201_CREATED)
def publish_contract_model_version(
    project_id: str,
    version_id: str,
    request: ContractVersionPublishRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        with start_span("contracts.publish_version", {
            "project_id": project_id, "version_id": version_id, "actor_id": ctx["user_id"],
        }):
            state = publish_contract_version(
                db, project_id=project_id, version_id=version_id,
                expected_current_version=request.expected_current_version,
                reason=request.reason, actor_id=ctx["user_id"],
            )
    except ContractVersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContractReleaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target = db.get(ContractModelVersion, version_id)
    return {"project_id": project_id, "current_version": state.current_version,
            "current_version_id": state.current_version_id, "status": target.status}


@router.post("/projects/{project_id}/contracts/versions/{version_id}/rollback", status_code=status.HTTP_201_CREATED)
def rollback_contract_model_version(
    project_id: str,
    version_id: str,
    request: ContractVersionRollbackRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        with start_span("contracts.rollback_version", {
            "project_id": project_id, "version_id": version_id, "actor_id": ctx["user_id"],
        }):
            state = rollback_contract_version(
                db, project_id=project_id, version_id=version_id,
                expected_current_version=request.expected_current_version,
                reason=request.reason, actor_id=ctx["user_id"],
            )
    except ContractVersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ContractReleaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target = db.get(ContractModelVersion, version_id)
    return {"project_id": project_id, "current_version": state.current_version,
            "current_version_id": state.current_version_id, "status": target.status}


@router.get("/projects/{project_id}/contracts/release")
def get_contract_release_pointer(
    project_id: str,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _release_payload(get_contract_release(db, project_id=project_id), project_id)


@router.get("/projects/{project_id}/contracts/audit")
def list_contract_audit_records(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows, total = list_contract_audit(db, project_id=project_id, limit=limit, offset=offset)
    return {"items": [contract_audit_payload(item) for item in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/projects/{project_id}/contracts/versions/{version_id}/export")
def export_contract_model_version(
    project_id: str,
    version_id: str,
    kind: str = Query(pattern="^(model|json_schema|sql_ddl|er_diagram)$"),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    version = get_contract_version(db, project_id=project_id, version_id=version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="contract version not found")
    content, media_type = export_contract_version(version, kind)
    return Response(content=content, media_type=media_type)
