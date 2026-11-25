"""OpenRefine preview, adoption, cancellation, and recipe discovery."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_resource_access, require_role
from app.core.db import get_db
from app.models.entities import DataSource
from app.services.openrefine.client import OpenRefineClient, OpenRefineError, OpenRefineUnavailable
from app.services.openrefine.preview_errors import http_error
from app.services.openrefine.previews import (
    OpenRefineLimitExceeded,
    OpenRefineNoRecipe,
    OpenRefinePreviewConflict,
    OpenRefinePreviewNotFound,
    OpenRefineUnsupportedSource,
    adopt_preview,
    cancel_preview,
    create_preview,
)
from app.services.openrefine.recipes import RecipeCatalogError, load_recipes, recipe_payload, select_recipe
from app.services.storage import storage

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_resource_access)],
    tags=["OpenRefine"],
)


class PreviewRequest(BaseModel):
    data_source_id: str = Field(min_length=1, max_length=32)
    recipe_id: str | None = Field(default=None, max_length=80)


def get_openrefine_client() -> OpenRefineClient:
    return OpenRefineClient()


@router.get("/projects/{project_id}/openrefine/recipes")
def list_openrefine_recipes(
    project_id: str,
    data_source_id: str = Query(min_length=1, max_length=32),
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    source = db.get(DataSource, data_source_id)
    if source is None or source.project_id != project_id:
        raise HTTPException(status_code=404, detail={"code": "DATA_SOURCE_NOT_FOUND", "message": "数据源不存在于当前项目"})
    try:
        recipes = load_recipes()
    except RecipeCatalogError as exc:
        raise HTTPException(status_code=500, detail={"code": "RECIPE_CATALOG_INVALID", "message": str(exc)}) from exc
    selected = select_recipe(source.profile_json or {})
    return {
        "items": [recipe_payload(recipe) for recipe in recipes],
        "selected_id": selected.id if selected else None,
    }


@router.post("/projects/{project_id}/openrefine/previews")
def create_openrefine_preview(
    project_id: str,
    request: PreviewRequest,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
    client: OpenRefineClient = Depends(get_openrefine_client),
) -> dict[str, Any]:
    try:
        return create_preview(
            db, project_id=project_id, data_source_id=request.data_source_id,
            actor_id=ctx["user_id"], client=client, recipe_id=request.recipe_id,
        )
    except OpenRefineUnavailable as exc:
        raise http_error(503, exc.code, str(exc), fallback="rule_clean") from exc
    except OpenRefineError as exc:
        raise http_error(502, exc.code, str(exc)) from exc
    except (OpenRefineLimitExceeded, OpenRefineNoRecipe, OpenRefineUnsupportedSource) as exc:
        raise http_error(422, exc.code, str(exc)) from exc


@router.post("/projects/{project_id}/openrefine/previews/{preview_id}/adopt")
def adopt_openrefine_preview(
    project_id: str,
    preview_id: str,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
    client: OpenRefineClient = Depends(get_openrefine_client),
) -> dict[str, Any]:
    try:
        return adopt_preview(db, project_id=project_id, preview_id=preview_id, actor_id=ctx["user_id"], client=client)
    except OpenRefinePreviewNotFound as exc:
        raise http_error(404, exc.code, str(exc)) from exc
    except OpenRefinePreviewConflict as exc:
        raise http_error(409, exc.code, str(exc)) from exc
    except OpenRefineUnsupportedSource as exc:
        raise http_error(422, exc.code, str(exc)) from exc


@router.delete("/projects/{project_id}/openrefine/previews/{preview_id}")
def cancel_openrefine_preview(
    project_id: str,
    preview_id: str,
    ctx: dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
    client: OpenRefineClient = Depends(get_openrefine_client),
) -> dict[str, str]:
    try:
        return cancel_preview(db, project_id=project_id, preview_id=preview_id, client=client)
    except OpenRefinePreviewNotFound as exc:
        raise http_error(404, exc.code, str(exc)) from exc
    except OpenRefinePreviewConflict as exc:
        raise http_error(409, exc.code, str(exc)) from exc
