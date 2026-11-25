"""Generic multi-field cleaning: selected columns only, artifact and stats."""
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_unification_public_api import public_api  # noqa: F401

from app.models.entities import Job
from app.services.engines import execute_clean_fields
from app.services.storage import storage


def test_clean_fields_cleans_only_selected_columns(public_api, tmp_path, monkeypatch):
    _client, maker, _identity = public_api
    monkeypatch.setattr(storage, "local_root", str(tmp_path / "storage"))
    frame = pd.DataFrame([
        {"name": "  Acme   Ltd  ", "city": "  Shenzhen ", "tax_id": "T-1"},
        {"name": "Beta", "city": "Shanghai", "tax_id": "T-2"},
    ])
    with maker() as db:
        job = Job(project_id="project-one", stage="L1", status="RUNNING", params_json={})
        db.add(job)
        db.commit()
        result = execute_clean_fields(db, "project-one", job.id, frame, ["name", "city"])
        db.commit()

    assert result["clean_fields"] == ["name", "city"]
    assert result["total_rows"] == 2
    assert result["changed_rows"] == 1
    assert result["changed_cells"] == 2

    clean = next(item for item in result["artifacts"] if item["type"] == "clean_csv")
    from app.models.entities import ArtifactVersion

    with maker() as db:
        artifact = db.get(ArtifactVersion, clean["id"])
        text = storage.get_bytes(artifact.object_key).decode("utf-8-sig")
    assert "Acme Ltd" in text
    assert "Shenzhen" in text
    assert "T-1" in text


def test_clean_fields_rejects_unknown_selection(public_api):
    _client, maker, _identity = public_api
    frame = pd.DataFrame([{"name": "Acme"}])
    with maker() as db:
        job = Job(project_id="project-one", stage="L1", status="RUNNING", params_json={})
        db.add(job)
        db.commit()
        try:
            execute_clean_fields(db, "project-one", job.id, frame, ["missing"])
        except ValueError as exc:
            assert "不在数据表中" in str(exc)
        else:
            raise AssertionError("unknown clean field must fail")
