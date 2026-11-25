"""OpenRefine preview, adoption, cancellation, diff, and fallback API."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_unification_public_api import public_api  # noqa: F401

from app.api.v1.openrefine import get_openrefine_client
from app.main import app
from app.models.entities import DataSource, Job
from app.services.openrefine.client import OpenRefineError
from app.services.openrefine.previews import diff_csv
from app.services.storage import storage

BEFORE = b"\xef\xbb\xbfid,company_name,city\n1,\"Acme, Ltd\",Shenzhen\n2,Beta,Shanghai\n"
AFTER = b"\xef\xbb\xbfid,company_name,city\n1,Acme Ltd,Shenzhen\n2,Beta,Shanghai\n"
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


class FakeClient:
    def __init__(self, *, healthy: bool = True, cleaned: bytes = AFTER, fail_apply: bool = False):
        self.healthy = healthy
        self.cleaned = cleaned
        self.fail_apply = fail_apply
        self.deleted: list[str] = []
        self.created = 0

    def health(self) -> bool:
        return self.healthy

    def create_project(self, name: str, file_bytes: bytes, format: str = "csv") -> str:
        self.created += 1
        return "or-project-1"

    def apply_recipe(self, project_id: str, operations: list[dict]) -> None:
        if self.fail_apply:
            raise OpenRefineError("OPENREFINE_RECIPE_FAILED", "openrefine exploded")

    def export_csv(self, project_id: str, project_name: str) -> bytes:
        return self.cleaned

    def delete_project(self, project_id: str) -> None:
        self.deleted.append(project_id)


def _seed_source(maker, object_key: str = "projects/project-one/uploads/dirty.csv") -> str:
    with maker() as db:
        source = DataSource(
            project_id="project-one", name="dirty.csv", source_type="file",
            object_key=object_key, profile_json=PROFILE,
            routing_json={"recommended_stage": "L1"},
        )
        db.add(source)
        db.commit()
        return source.id


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    monkeypatch.setattr(storage, "local_root", str(root))
    storage.put_bytes("projects/project-one/uploads/dirty.csv", BEFORE)
    return root


def _use_client(fake: FakeClient):
    app.dependency_overrides[get_openrefine_client] = lambda: fake


def test_diff_counts_changed_rows_and_cells_with_bom_and_quotes():
    diff = diff_csv(BEFORE, AFTER)
    assert diff["changed_rows"] == 1
    assert diff["changed_cells"] == 1
    assert diff["examples"][0] == {"row": 1, "column": "company_name", "before": "Acme, Ltd", "after": "Acme Ltd"}
    assert diff["structure_changed"] is False


def test_preview_adopt_creates_a_new_source_and_keeps_the_original(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    fake = FakeClient()
    _use_client(fake)
    try:
        response = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["engine"] == "openrefine"
        assert preview["recipe"]["id"] == "normalize_company_name"
        assert preview["diff"]["changed_rows"] == 1

        adopted = client.post(
            f"/api/v1/projects/project-one/openrefine/previews/{preview['preview_id']}/adopt",
        )
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["name"] == "dirty（OpenRefine 清洗）"
        assert storage.get_bytes("projects/project-one/uploads/dirty.csv") == BEFORE
        assert fake.deleted == ["or-project-1"]

        with maker() as db:
            rows = db.query(DataSource).filter(DataSource.project_id == "project-one").all()
            assert len(rows) == 2
            assert db.get(DataSource, source_id).object_key.endswith("dirty.csv")
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_preview_refuses_an_oversized_source(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    with maker() as db:
        source = db.get(DataSource, source_id)
        source.profile_json = {**PROFILE, "total_rows": 50_001}
        db.commit()
    _use_client(FakeClient())
    try:
        response = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "OPENREFINE_LIMIT_EXCEEDED"
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_unavailable_openrefine_returns_the_fallback_code(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    _use_client(FakeClient(healthy=False))
    try:
        response = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["code"] == "OPENREFINE_UNAVAILABLE"
        assert detail["fallback"] == "rule_clean"
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_failed_recipe_leaves_the_original_and_records_a_failed_job(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    _use_client(FakeClient(fail_apply=True))
    try:
        response = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        )
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "OPENREFINE_RECIPE_FAILED"
        assert storage.get_bytes("projects/project-one/uploads/dirty.csv") == BEFORE
        with maker() as db:
            jobs = db.query(Job).filter(Job.project_id == "project-one").all()
            assert len(jobs) == 1
            assert jobs[0].status == "FAILED"
            assert "OPENREFINE_RECIPE_FAILED" in jobs[0].error_message
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_adopt_twice_and_cancel_after_adopt_conflict(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    _use_client(FakeClient())
    try:
        preview = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        ).json()
        preview_id = preview["preview_id"]
        assert client.post(f"/api/v1/projects/project-one/openrefine/previews/{preview_id}/adopt").status_code == 200
        assert client.post(f"/api/v1/projects/project-one/openrefine/previews/{preview_id}/adopt").status_code == 409
        assert client.delete(f"/api/v1/projects/project-one/openrefine/previews/{preview_id}").status_code == 409
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_cancel_deletes_the_temporary_project(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    fake = FakeClient()
    _use_client(fake)
    try:
        preview = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        ).json()
        response = client.delete(f"/api/v1/projects/project-one/openrefine/previews/{preview['preview_id']}")
        assert response.status_code == 200
        assert response.json() == {"status": "cancelled"}
        assert fake.deleted == ["or-project-1"]
        with maker() as db:
            job = db.get(Job, preview["preview_id"])
            assert job.status == "CANCELLED"
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_preview_from_another_project_is_hidden(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    _use_client(FakeClient())
    try:
        preview = client.post(
            "/api/v1/projects/project-one/openrefine/previews",
            json={"data_source_id": source_id},
        ).json()
        response = client.post(
            f"/api/v1/projects/project-two/openrefine/previews/{preview['preview_id']}/adopt",
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_openrefine_client, None)


def test_recipe_listing_reports_the_selected_recipe(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    response = client.get(
        "/api/v1/projects/project-one/openrefine/recipes",
        params={"data_source_id": source_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selected_id"] == "normalize_company_name"
    assert "normalize_company_name" in {item["id"] for item in payload["items"]}
