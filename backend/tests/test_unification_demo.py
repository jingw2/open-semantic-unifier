"""The assistant entity demo is idempotent, scoped to demo records, and never merges."""
from pathlib import Path
import sys
import time

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_unification_public_api import public_api  # noqa: F401

from app.models.unification import (
    IdentityDecisionRecord,
    SourceRecord,
    SourceRevision,
    UnifiedEntity,
    UnificationRun,
    UnificationSnapshot,
)
from app.services.unification.releases import execute_unification_run


def test_demo_run_is_scoped_to_demo_records_and_never_merges(public_api):
    client, maker, _identity = public_api
    with maker() as db:
        db.add(UnifiedEntity(id="ent-erp", project_id="project-one", entity_type="Supplier"))
        db.flush()
        db.add(SourceRecord(
            id="rec-erp", project_id="project-one", source_system="erp", dataset="suppliers",
            source_record_id="ERP-1", current_entity_id="ent-erp", current_revision_id=None,
        ))
        db.flush()
        db.add(SourceRevision(
            id="rev-erp", source_record_id="rec-erp", revision_no=1, input_version="erp-v1",
            attributes={"name": "Cedar Holdings", "tax_id": "DEMO-TAX-001"}, content_hash="hash-erp",
        ))
        db.flush()
        db.get(SourceRecord, "rec-erp").current_revision_id = "rev-erp"
        db.commit()

    import app.api.v1.unification as unification_module
    original_dispatch = unification_module.dispatch_unification_run

    def dispatch(run_id: str) -> None:
        with maker() as db:
            execute_unification_run(db, run_id)

    unification_module.dispatch_unification_run = dispatch
    try:
        response = client.post("/api/v1/projects/project-one/unification/demo")
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        deadline = time.time() + 20
        detail = {}
        while time.time() < deadline:
            detail = client.get(f"/api/v1/projects/project-one/unification/runs/{run_id}").json()
            if detail.get("status") in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.1)
        assert detail["status"] == "SUCCEEDED", detail

        with maker() as db:
            run = db.get(UnificationRun, run_id)
            assert run.input_source_system == "assistant_demo"
            manifest_ids = [item["source_record_id"] for item in run.input_manifest_json]
            assert len(manifest_ids) == 6
            source_systems = {
                row.source_system for row in db.scalars(select(SourceRecord).where(SourceRecord.id.in_(manifest_ids)))
            }
            assert source_systems == {"assistant_demo"}
            assert "rec-erp" not in manifest_ids
            assert db.scalar(select(IdentityDecisionRecord).where(
                IdentityDecisionRecord.project_id == "project-one",
                IdentityDecisionRecord.action == "MERGE",
            )) is None
            assert db.scalar(select(UnificationSnapshot).where(
                UnificationSnapshot.project_id == "project-one",
            )) is None

        pairs = detail["result"]["pairs"]
        actions = [pair["action"] for pair in pairs]
        assert len(pairs) == 3
        assert "REVIEW" in actions and "DISTINCT" in actions
        assert "AUTO_MATCH" not in actions

        second = client.post("/api/v1/projects/project-one/unification/demo")
        assert second.status_code == 202, second.text
        assert second.json()["run_id"] == run_id
    finally:
        unification_module.dispatch_unification_run = original_dispatch
