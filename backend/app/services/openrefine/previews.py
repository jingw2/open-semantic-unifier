"""OpenRefine preview, diff, adoption, and cancellation on existing Job rows."""
from __future__ import annotations

import csv
import io
import os
from typing import Any

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import ArtifactVersion, DataSource, Job
from app.services.engines import _save_artifact
from app.services.openrefine.client import OpenRefineClient, OpenRefineError, OpenRefineUnavailable
from app.services.openrefine.recipes import (
    Recipe,
    RecipeColumnMissing,
    openrefine_limits_ok,
    recipe_payload,
    render_operations,
    select_recipe,
)
from app.services.profiler import profile_items, recommend_pipeline
from app.services.storage import storage


class OpenRefinePreviewError(Exception):
    code = "OPENREFINE_PREVIEW_ERROR"


class OpenRefinePreviewNotFound(OpenRefinePreviewError):
    code = "OPENREFINE_PREVIEW_NOT_FOUND"


class OpenRefinePreviewConflict(OpenRefinePreviewError):
    code = "OPENREFINE_PREVIEW_CONFLICT"


class OpenRefineLimitExceeded(OpenRefinePreviewError):
    code = "OPENREFINE_LIMIT_EXCEEDED"


class OpenRefineNoRecipe(OpenRefinePreviewError):
    code = "OPENREFINE_NO_RECIPE"


class OpenRefineUnsupportedSource(OpenRefinePreviewError):
    code = "OPENREFINE_UNSUPPORTED_SOURCE"


def _read_csv(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    header = [str(cell) for cell in rows[0]]
    body = [[str(cell) for cell in row] for row in rows[1:]]
    return header, body


def diff_csv(before: bytes, after: bytes) -> dict[str, Any]:
    before_header, before_rows = _read_csv(before)
    after_header, after_rows = _read_csv(after)
    before_index = {name: index for index, name in enumerate(before_header)}
    after_index = {name: index for index, name in enumerate(after_header)}
    common = [name for name in after_header if name in before_index]
    columns_added = [name for name in after_header if name not in before_index]
    columns_removed = [name for name in before_header if name not in after_index]
    structure_changed = before_header != after_header or len(before_rows) != len(after_rows)

    changed_rows = 0
    changed_cells = 0
    examples: list[dict[str, Any]] = []
    for row_number in range(max(len(before_rows), len(after_rows))):
        before_row = before_rows[row_number] if row_number < len(before_rows) else []
        after_row = after_rows[row_number] if row_number < len(after_rows) else []
        row_changed = False
        for column in common:
            left = before_row[before_index[column]] if before_index[column] < len(before_row) else ""
            right = after_row[after_index[column]] if after_index[column] < len(after_row) else ""
            if left != right:
                row_changed = True
                changed_cells += 1
                if len(examples) < 20:
                    examples.append({
                        "row": row_number + 1,
                        "column": column,
                        "before": left,
                        "after": right,
                    })
        if len(after_rows) != len(before_rows):
            row_changed = True
        if row_changed:
            changed_rows += 1

    return {
        "changed_rows": changed_rows,
        "changed_cells": changed_cells,
        "before_rows": len(before_rows),
        "after_rows": len(after_rows),
        "columns_added": columns_added,
        "columns_removed": columns_removed,
        "structure_changed": structure_changed,
        "examples": examples,
    }


def _profile_for_source(source: DataSource, content: bytes) -> dict[str, Any]:
    profile = dict(source.profile_json or {})
    if not profile.get("tables"):
        try:
            frame = pd.read_csv(io.BytesIO(content))
        except Exception as exc:
            raise OpenRefineUnsupportedSource("数据源不是可读取的 CSV 文件") from exc
        profile = profile_items([{"kind": "table", "name": source.name, "df": frame}])
    return profile


def _job_preview(db: Session, project_id: str, preview_id: str) -> Job:
    job = db.get(Job, preview_id)
    if job is None or job.project_id != project_id or (job.params_json or {}).get("engine") != "openrefine":
        raise OpenRefinePreviewNotFound("OpenRefine 预览不存在")
    return job


def create_preview(
    db: Session,
    *,
    project_id: str,
    data_source_id: str,
    actor_id: str,
    client: OpenRefineClient,
    recipe_id: str | None = None,
) -> dict[str, Any]:
    source = db.get(DataSource, data_source_id)
    if source is None or source.project_id != project_id:
        raise OpenRefineUnsupportedSource("数据源不存在于当前项目")
    if source.source_type != "file" or not source.object_key:
        raise OpenRefineUnsupportedSource("OpenRefine 预览只支持已上传的文件数据源")
    if not (source.name.casefold().endswith(".csv") or source.object_key.casefold().endswith(".csv")):
        raise OpenRefineUnsupportedSource("OpenRefine 预览目前只支持 CSV 文件")

    content = storage.get_bytes(source.object_key)
    profile = _profile_for_source(source, content)
    allowed, reason = openrefine_limits_ok(profile, len(content))
    if not allowed:
        raise OpenRefineLimitExceeded(reason or "数据超过 OpenRefine 限制")

    recipe: Recipe | None = None
    if recipe_id:
        from app.services.openrefine.recipes import load_recipes

        recipe = next((item for item in load_recipes() if item.id == recipe_id), None)
    if recipe is None:
        recipe = select_recipe(profile)
    if recipe is None:
        raise OpenRefineNoRecipe("没有匹配当前数据的内置配方")
    try:
        operations = render_operations(recipe, profile)
    except RecipeColumnMissing as exc:
        raise OpenRefineNoRecipe(f"配方 {recipe.name} 不适用于当前数据") from exc

    job = Job(
        project_id=project_id,
        stage="L1",
        status="RUNNING",
        progress=10,
        stage_message="正在准备 OpenRefine 清洗…",
        params_json={
            "engine": "openrefine",
            "recipe_id": recipe.id,
            "recipe_version": recipe.version,
            "datasource_id": source.id,
            "actor_id": actor_id,
        },
        result_json={},
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    openrefine_project_id = ""
    try:
        if not client.health():
            raise OpenRefineUnavailable()
        openrefine_project_id = client.create_project(f"osu-{job.id}", content)
        client.apply_recipe(openrefine_project_id, operations)
        cleaned = client.export_csv(openrefine_project_id, f"osu-{job.id}")
        artifact = _save_artifact(
            db, project_id, job.id, "openrefine_csv", f"{os.path.splitext(source.name)[0]}_openrefine.csv", cleaned,
            meta={"recipe_id": recipe.id, "recipe_version": recipe.version},
        )
        diff = diff_csv(content, cleaned)
    except OpenRefineError as exc:
        _fail_job(db, job, exc.code, exc.message)
        if openrefine_project_id:
            _best_effort_delete(client, openrefine_project_id)
        raise
    except Exception as exc:
        db.rollback()
        _fail_job(db, job, "OPENREFINE_PREVIEW_FAILED", str(exc)[:200] or "preview failed")
        if openrefine_project_id:
            _best_effort_delete(client, openrefine_project_id)
        raise

    job.status = "SUCCESS"
    job.progress = 100
    job.stage_message = "OpenRefine 清洗完成，等待采用"
    job.result_json = {
        "engine": "openrefine",
        "recipe": recipe_payload(recipe),
        "diff": diff,
        "artifact_id": artifact.id,
        "openrefine_project_id": openrefine_project_id,
        "source_datasource_id": source.id,
        "adopted": False,
    }
    db.commit()
    return {
        "preview_id": job.id,
        "engine": "openrefine",
        "recipe": recipe_payload(recipe),
        "diff": diff,
        "artifact_id": artifact.id,
        "fallback_reason": None,
    }


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
    job.status = "FAILED"
    job.error_message = f"{code}: {message}"
    job.stage_message = "OpenRefine 清洗失败"
    job.result_json = {"engine": "openrefine", "error_code": code, "error_message": message}
    db.commit()


def _best_effort_delete(client: OpenRefineClient, project_id: str) -> None:
    try:
        client.delete_project(project_id)
    except OpenRefineError:
        pass


def adopt_preview(
    db: Session, *, project_id: str, preview_id: str, actor_id: str, client: OpenRefineClient,
) -> dict[str, Any]:
    job = _job_preview(db, project_id, preview_id)
    result = dict(job.result_json or {})
    if job.status != "SUCCESS" or result.get("engine") != "openrefine":
        raise OpenRefinePreviewConflict("预览尚未成功完成")
    if result.get("adopted"):
        raise OpenRefinePreviewConflict("这个预览已经采用过")

    artifact = db.get(ArtifactVersion, result.get("artifact_id"))
    if artifact is None or artifact.project_id != project_id:
        raise OpenRefinePreviewNotFound("清洗产物不存在")
    cleaned = storage.get_bytes(artifact.object_key)
    source = db.get(DataSource, result.get("source_datasource_id"))
    if source is None or source.project_id != project_id:
        raise OpenRefinePreviewNotFound("原始数据源不存在")

    try:
        frame = pd.read_csv(io.BytesIO(cleaned))
    except Exception as exc:
        raise OpenRefineUnsupportedSource("清洗产物不是可读取的 CSV") from exc
    profile = profile_items([{"kind": "table", "name": source.name, "df": frame}])
    object_key = f"projects/{project_id}/openrefine/{job.id}/adopted_cleaned.csv"
    storage.put_bytes(object_key, cleaned)

    stem = os.path.splitext(source.name)[0]
    name = f"{stem}（OpenRefine 清洗）"[:180]
    adopted = DataSource(
        id=job.id,
        project_id=project_id,
        name=name,
        source_type="file",
        object_key=object_key,
        profile_json=profile,
        routing_json=recommend_pipeline(profile, openrefine_available=settings.openrefine_enabled),
    )
    db.add(adopted)
    try:
        db.flush()
        result.update({"adopted": True, "adopted_data_source_id": adopted.id, "adopted_by": actor_id})
        job.result_json = result
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise OpenRefinePreviewConflict("这个预览已经采用过") from exc

    openrefine_project_id = result.get("openrefine_project_id")
    if openrefine_project_id:
        _best_effort_delete(client, openrefine_project_id)
    return {"data_source_id": adopted.id, "name": adopted.name}


def cancel_preview(
    db: Session, *, project_id: str, preview_id: str, client: OpenRefineClient,
) -> dict[str, str]:
    job = _job_preview(db, project_id, preview_id)
    result = dict(job.result_json or {})
    if result.get("adopted"):
        raise OpenRefinePreviewConflict("已采用的预览不能撤销")
    if job.status == "CANCELLED":
        return {"status": "cancelled"}

    openrefine_project_id = result.get("openrefine_project_id")
    if openrefine_project_id:
        _best_effort_delete(client, openrefine_project_id)
    result["cancelled"] = True
    job.result_json = result
    job.status = "CANCELLED"
    job.stage_message = "OpenRefine 预览已撤销"
    db.commit()
    return {"status": "cancelled"}
