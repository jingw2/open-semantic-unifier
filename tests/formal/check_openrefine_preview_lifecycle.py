"""Exhaustive fault-injection check for the OpenRefine preview lifecycle.

Property: for every fault point in the preview transaction, the transaction either
returns successfully after storing exactly one DRAFT artifact and creating no
DataSource, or fails with a terminal Job, no new DataSource, unchanged original
bytes, and no leaked temporary OpenRefine project. A successful adoption creates
exactly one DataSource and can happen once.

Run: python3 tests/formal/check_openrefine_preview_lifecycle.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.openrefine.previews as previews
from app.core.db import Base
from app.models.entities import ArtifactVersion, DataSource, Job, Project, Workspace
from app.services.openrefine.client import OpenRefineError
from app.services.openrefine.previews import OpenRefinePreviewConflict, adopt_preview, cancel_preview, create_preview
from app.services.storage import storage

BEFORE = b'id,company_name,city\n1,"  Acme, Ltd ",Shenzhen\n2,Beta,Shanghai\n'
AFTER = b"id,company_name,city\n1,Acme Ltd,Shenzhen\n2,Beta,Shanghai\n"
PROFILE = {
    "total_rows": 2,
    "table_count": 1,
    "has_whitespace_noise": True,
    "detected_keys": {"name_col": "company_name"},
    "tables": [{
        "name": "dirty.csv", "rows": 2, "cols": 3,
        "columns": ["id", "company_name", "city"],
        "preview": [{"id": "1", "company_name": " Acme, Ltd ", "city": "Shenzhen"}],
    }],
}
FAILURE_POINTS = ("health", "create", "apply", "export", "save_artifact", "diff")
RECIPES = ("normalize_company_name", "normalize_selected_columns")


class FakeClient:
    def __init__(self, fail_at: str | None = None):
        self.fail_at = fail_at
        self.deleted: list[str] = []
        self.created = 0

    def health(self) -> bool:
        if self.fail_at == "health":
            return False
        return True

    def create_project(self, name: str, file_bytes: bytes, format: str = "csv") -> str:
        if self.fail_at == "create":
            raise OpenRefineError("OPENREFINE_CREATE_FAILED", "create failed")
        self.created += 1
        return "or-project-1"

    def apply_recipe(self, project_id: str, operations: list[dict]) -> None:
        if self.fail_at == "apply":
            raise OpenRefineError("OPENREFINE_RECIPE_FAILED", "apply failed")

    def export_csv(self, project_id: str, project_name: str) -> bytes:
        if self.fail_at == "export":
            raise OpenRefineError("OPENREFINE_EXPORT_FAILED", "export failed")
        return AFTER

    def delete_project(self, project_id: str) -> None:
        self.deleted.append(project_id)


class Failure(RuntimeError):
    pass


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def _seed(tmp: Path):
    tmp.mkdir(parents=True, exist_ok=True)
    db_path = tmp / "state.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage.backend = "local"
    storage.local_root = str(tmp / "storage")
    with maker() as db:
        db.add(Workspace(id="ws", name="WS", slug="ws-openrefine-check"))
        db.flush()
        db.add(Project(id="project", workspace_id="ws", name="P", slug="p-openrefine-check"))
        db.add(DataSource(
            id="ds-source", project_id="project", name="dirty.csv", source_type="file",
            object_key="projects/project/uploads/dirty.csv", profile_json=PROFILE,
        ))
        db.commit()
    storage.put_bytes("projects/project/uploads/dirty.csv", BEFORE)
    return engine, maker


def _preview(maker, client, recipe: str, *, fail_save: bool = False, fail_diff: bool = False):
    original_save = previews._save_artifact
    original_diff = previews.diff_csv
    if fail_save:
        def boom(*_args, **_kwargs):
            raise RuntimeError("artifact storage failed")
        previews._save_artifact = boom
    if fail_diff:
        def boom_diff(*_args, **_kwargs):
            raise RuntimeError("diff failed")
        previews.diff_csv = boom_diff
    try:
        with maker() as db:
            kwargs = {"data_source_id": "ds-source", "actor_id": "checker", "client": client, "recipe_id": recipe}
            if recipe == "normalize_selected_columns":
                kwargs["columns"] = ["company_name"]
            return create_preview(db, project_id="project", **kwargs)
    finally:
        previews._save_artifact = original_save
        previews.diff_csv = original_diff


def _snapshot(maker) -> dict:
    with maker() as db:
        return {
            "jobs": db.query(Job).order_by(Job.created_at).all(),
            "sources": db.query(DataSource).all(),
            "artifacts": db.query(ArtifactVersion).all(),
        }


def _check_failure(label: str, maker, recipe: str, fail_at: str) -> None:
    client = FakeClient(fail_at)
    try:
        _preview(
            maker, client, recipe,
            fail_save=fail_at == "save_artifact",
            fail_diff=fail_at == "diff",
        )
    except Exception:
        pass
    else:
        raise Failure(f"{label}: preview did not raise")

    state = _snapshot(maker)
    jobs = state["jobs"]
    _assert(len(jobs) == 1, f"{label}: expected exactly one Job, got {len(jobs)}")
    _assert(jobs[0].status == "FAILED", f"{label}: Job status is {jobs[0].status}, expected FAILED")
    _assert(jobs[0].error_message, f"{label}: failed Job has no error code")
    _assert(len(state["sources"]) == 1, f"{label}: failure created a DataSource")
    _assert(not state["artifacts"], f"{label}: failure left an ArtifactVersion row")
    _assert(storage.get_bytes("projects/project/uploads/dirty.csv") == BEFORE, f"{label}: original bytes changed")
    if fail_at in ("apply", "export", "save_artifact", "diff"):
        _assert(client.deleted == ["or-project-1"], f"{label}: temporary project was not deleted")
    else:
        _assert(client.deleted == [], f"{label}: deletion was attempted without a project")


def _check_success_and_adoption(label: str, maker, recipe: str) -> None:
    client = FakeClient()
    result = _preview(maker, client, recipe)
    _assert(result["engine"] == "openrefine", f"{label}: wrong engine")
    _assert(result["diff"]["changed_rows"] == 1, f"{label}: wrong diff")
    state = _snapshot(maker)
    _assert(len(state["artifacts"]) == 1, f"{label}: expected one DRAFT artifact")
    _assert(len(state["sources"]) == 1, f"{label}: preview created a DataSource before adoption")
    _assert(storage.get_bytes("projects/project/uploads/dirty.csv") == BEFORE, f"{label}: original bytes changed")

    with maker() as db:
        adopted = adopt_preview(db, project_id="project", preview_id=result["preview_id"], actor_id="checker", client=client)
    state = _snapshot(maker)
    _assert(len(state["sources"]) == 2, f"{label}: adoption did not create exactly one DataSource")
    _assert(adopted["data_source_id"] == result["preview_id"], f"{label}: adopted source id is not the Job id")
    _assert(storage.get_bytes("projects/project/uploads/dirty.csv") == BEFORE, f"{label}: adoption changed original bytes")
    _assert(client.deleted == ["or-project-1"], f"{label}: temporary project was not deleted after adoption")

    with maker() as db:
        try:
            adopt_preview(db, project_id="project", preview_id=result["preview_id"], actor_id="checker", client=client)
        except OpenRefinePreviewConflict:
            pass
        else:
            raise Failure(f"{label}: second adoption was accepted")
        try:
            cancel_preview(db, project_id="project", preview_id=result["preview_id"], client=client)
        except OpenRefinePreviewConflict:
            pass
        else:
            raise Failure(f"{label}: adopted preview could be cancelled")


def _check_cancel(label: str, maker, recipe: str) -> None:
    client = FakeClient()
    result = _preview(maker, client, recipe)
    with maker() as db:
        outcome = cancel_preview(db, project_id="project", preview_id=result["preview_id"], client=client)
    _assert(outcome == {"status": "cancelled"}, f"{label}: cancel returned {outcome!r}")
    state = _snapshot(maker)
    _assert(state["jobs"][0].status == "CANCELLED", f"{label}: Job is not CANCELLED")
    _assert(len(state["sources"]) == 1, f"{label}: cancel created a DataSource")
    _assert(client.deleted == ["or-project-1"], f"{label}: cancel did not delete the temporary project")


def main() -> int:
    cases = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for recipe in RECIPES:
            for fail_at in FAILURE_POINTS:
                label = f"{recipe}/{fail_at}"
                engine, maker = _seed(Path(tmpdir) / f"{recipe}-{fail_at}")
                try:
                    _check_failure(label, maker, recipe, fail_at)
                    cases += 1
                finally:
                    engine.dispose()

            label = f"{recipe}/success"
            engine, maker = _seed(Path(tmpdir) / f"{recipe}-success")
            try:
                _check_success_and_adoption(label, maker, recipe)
                cases += 1
            finally:
                engine.dispose()

            label = f"{recipe}/cancel"
            engine, maker = _seed(Path(tmpdir) / f"{recipe}-cancel")
            try:
                _check_cancel(label, maker, recipe)
                cases += 1
            finally:
                engine.dispose()

    print(f"PASS: {cases} preview lifecycle cases across {len(RECIPES)} recipes and {len(FAILURE_POINTS)} fault points.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1)
