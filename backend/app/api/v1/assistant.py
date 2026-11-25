"""Project-scoped assistant planning; classification and explanation only."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_resource_access
from app.core.db import get_db
from app.models.entities import Project
from app.services.assistant import classify_assistant_message

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_resource_access)],
    tags=["Assistant"],
)


class AssistantTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=1000)


class AssistantPlanRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    page: str = Field(default="data", max_length=80)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=6)
    provider_id: str | None = Field(default=None, max_length=32)


@router.post("/projects/{project_id}/assistant/plan")
def assistant_plan(
    project_id: str,
    request: AssistantPlanRequest,
    ctx: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None or project.workspace_id != ctx["workspace_id"]:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        history = [turn.model_dump() for turn in request.history]
        return classify_assistant_message(db, project_id, request.message, request.page, history, request.provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
