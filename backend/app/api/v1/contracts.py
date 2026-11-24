"""Project-scoped contract source ingestion for the L3 model workbench."""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_resource_access, require_role
from app.core.db import get_db
from app.models.contracts import ContractSource
from app.services.contracts.sources import (
    ContractSourceError,
    ContractSourceInvalid,
    ContractSourceNotFound,
    ContractSourceTooLarge,
    create_contract_source,
    list_contract_sources,
)

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_resource_access)],
    tags=["Contracts"],
)


class ContractSourceCreateRequest(BaseModel):
    data_source_id: str = Field(min_length=1, max_length=32)


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
    except ContractSourceInvalid as exc:
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
