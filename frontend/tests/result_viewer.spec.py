"""Online result viewing for CSV artifacts and contract models."""
import json
from pathlib import Path
import sys
from threading import Thread
import time
import urllib.request
import uuid

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_helpers import open_route

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.main import app
from app.models.contracts import ContractModelVersion
from app.models.entities import ArtifactVersion, Job, Project, Workspace
from app.services.storage import storage


def test_result_viewer_shows_csv_and_contract_model():
    database_path = Path("/tmp") / f"open-semantic-unifier-viewer-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    csv_bytes = '﻿name,note\n"Acme, Ltd.","line1, line2"\n'.encode('utf-8')
    object_key = "projects/project-view/artifacts/l1_cleaned.csv"
    storage.put_bytes(object_key, csv_bytes)
    model = {
        "classes": {
            "Supplier": {"attributes": {"name": {"range": "string", "required": True}}},
            "Country": {"attributes": {"country_code": {"identifier": True}}},
        },
        "enums": {"SupplierStatus": {"values": ["ACTIVE", "INACTIVE"]}},
        "relations": {"Supplier": ["Country"]},
    }
    with maker() as db:
        db.add(Workspace(id="workspace-view", name="View", slug="view"))
        db.flush()
        db.add(Project(id="project-view", workspace_id="workspace-view", name="View project", slug="view-project", description=""))
        db.flush()
        db.add(Job(
            id="job-view", project_id="project-view", stage="L1", status="SUCCESS", progress=100,
            stage_message="完成", params_json={},
            result_json={"artifacts": [{"id": "art-csv", "filename": "l1_cleaned.csv", "version": 1}]},
            error_message="",
        ))
        db.add(ArtifactVersion(
            id="art-csv", project_id="project-view", job_id="job-view", version_no=1,
            artifact_type="clean_csv", release_status="DRAFT", filename="l1_cleaned.csv",
            object_key=object_key, size_bytes=len(csv_bytes), meta_json={},
        ))
        db.add(ContractModelVersion(
            id="contract-view", project_id="project-view", source_id="source-1", version=1,
            status="DRAFT", source_sha256="x", canonical_model_json=json.dumps(model),
            artifacts_json={}, compiler_versions_json={}, validation_json={"counts": {}},
            entity_snapshot_id="snapshot-1", snapshot_version=1, created_by="test",
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "view-user", "email": "view@example.test", "display_name": "View User",
        "workspace_id": "workspace-view", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error", lifespan="off"))
    server_thread = Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=0.2).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("FastAPI test server did not become ready")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script("""
              localStorage.setItem('su_access_token', 'browser-test-token');
              localStorage.setItem('su_user_info', JSON.stringify({
                id: 'view-user', email: 'view@example.test', display_name: 'View User',
                role: 'editor', workspace_id: 'workspace-view'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            open_route(page, "#/tasks/job/job-view")
            page.get_by_role("button", name="查看", exact=True).click()
            expect(page.get_by_text("Acme, Ltd.", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_text("note", exact=True)).to_be_visible()

            open_route(page, "#/tasks/contract/contract-view")
            page.get_by_role("button", name="查看模型", exact=True).click()
            expect(page.get_by_text("Supplier", exact=True).first).to_be_visible(timeout=15000)
            expect(page.get_by_text("Country", exact=True).first).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_result_viewer_shows_csv_and_contract_model()
