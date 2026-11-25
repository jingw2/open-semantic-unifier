"""L2 workbench supports choosing one or more match fields before running."""
from datetime import datetime
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
from app.models.entities import DataSource, Project, Workspace

PROFILE = {
    "total_rows": 10,
    "table_count": 1,
    "detected_keys": {"name_col": "company_name", "tax_col": "tax_id", "phone_col": "phone"},
    "tables": [{
        "name": "customers.csv", "rows": 10, "cols": 3,
        "columns": ["company_name", "tax_id", "phone"],
        "preview": [{"company_name": "Acme", "tax_id": "T-1", "phone": "138"}],
    }],
}


def test_l2_match_field_selection_sends_selected_fields():
    database_path = Path("/tmp") / f"open-semantic-unifier-fields-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="workspace-fields", name="Fields", slug="fields"))
        db.flush()
        db.add(Project(id="project-fields", workspace_id="workspace-fields", name="Fields project", slug="fields-project", description=""))
        db.add(DataSource(
            id="ds-fields", project_id="project-fields", name="customers.csv", source_type="file",
            profile_json=PROFILE,
            routing_json={
                "recommended_stage": "L2",
                "suggested_params": {"name_col": "company_name", "tax_col": "tax_id", "phone_col": "phone"},
            },
            created_at=datetime(2026, 1, 1),
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "fields-user", "email": "fields@example.test", "display_name": "Fields User",
        "workspace_id": "workspace-fields", "role": "editor",
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
                id: 'fields-user', email: 'fields@example.test', display_name: 'Fields User',
                role: 'editor', workspace_id: 'workspace-fields'
              }));
            """)
            captured: list[dict] = []

            def handle_jobs(route):
                if route.request.method != "POST":
                    route.continue_()
                    return
                captured.append(route.request.post_data_json)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"id":"job-fields","stage":"L2","status":"SUCCESS","progress":100,"stage_message":"ok","result":{}}',
                )

            page.route("**/api/v1/projects/*/jobs", handle_jobs)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            open_route(page, "#/tasks/new/match")
            expect(page.get_by_role("heading", name="处理方法", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_text("匹配字段（可单选或多选）", exact=True)).to_be_visible()
            expect(page.get_by_label("匹配字段 company_name")).to_be_checked()
            expect(page.get_by_label("匹配字段 tax_id")).to_be_checked()

            page.get_by_label("匹配字段 company_name").uncheck()
            page.get_by_label("匹配字段 phone").uncheck()
            page.get_by_role("button", name="运行 L2", exact=True).click()
            expect(page.get_by_text("L2 任务状态：SUCCESS", exact=False)).to_be_visible(timeout=15000)
            assert captured and captured[-1]["params"]["match_fields"] == ["tax_id"]

            open_route(page, "#/tasks/new/clean")
            expect(page.get_by_label("清洗目标字段")).to_be_visible(timeout=15000)
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_l2_match_field_selection_sends_selected_fields()
