"""Deep links reach runs and contract versions outside the first list page."""
from datetime import datetime, timedelta
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
from app.models.entities import Project, Workspace
from app.models.unification import UnificationRun


def test_deep_links_outside_the_first_list_page():
    database_path = Path("/tmp") / f"open-semantic-unifier-deep-link-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    base_time = datetime(2026, 1, 1)
    with maker() as db:
        db.add(Workspace(id="workspace-deep", name="Deep", slug="deep"))
        db.flush()
        db.add(Project(id="project-deep", workspace_id="workspace-deep", name="Deep project", slug="deep-project", description=""))
        db.flush()
        for index in range(1, 22):
            db.add(UnificationRun(
                id=f"run-{index:02d}", project_id="project-deep", input_version=f"input-{index:02d}",
                input_manifest_json=[], identity_revision=1, scheme_revision="scheme-1",
                idempotency_key=f"key-{index:02d}", payload_hash=f"hash-{index:02d}",
                status="SUCCEEDED", attempt_count=1, result_json={"pairs": []},
                created_at=base_time + timedelta(minutes=index),
            ))
        for index in range(1, 52):
            db.add(ContractModelVersion(
                id=f"contract-{index:02d}", project_id="project-deep", source_id="source-1",
                version=index, status="DRAFT", source_sha256="x", canonical_model_json="{}",
                artifacts_json={}, compiler_versions_json={}, validation_json={"counts": {}},
                entity_snapshot_id="snapshot-1", snapshot_version=1, created_by="test",
                created_at=base_time + timedelta(minutes=index),
            ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "deep-user", "email": "deep@example.test", "display_name": "Deep User",
        "workspace_id": "workspace-deep", "role": "editor",
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
                id: 'deep-user', email: 'deep@example.test', display_name: 'Deep User',
                role: 'editor', workspace_id: 'workspace-deep'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            open_route(page, "#/tasks/run/run-01")
            expect(page.get_by_role("heading", name="实体统一工作台", exact=True)).to_be_visible()
            expect(page.get_by_text("input-01")).to_be_visible()

            open_route(page, "#/tasks/contract/contract-01")
            expect(page.get_by_role("heading", name="契约统一工作台", exact=True)).to_be_visible()
            expect(page.get_by_text("v1", exact=True).first).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_deep_links_outside_the_first_list_page()
