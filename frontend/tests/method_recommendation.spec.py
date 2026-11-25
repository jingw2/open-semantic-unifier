"""Method labels, direct workbench routing, and the entity-page banner."""
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
from app.models.entities import DataSource, Project, Workspace


def test_method_labels_and_banner():
    database_path = Path("/tmp") / f"open-semantic-unifier-method-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    base_time = datetime(2026, 1, 1)
    with maker() as db:
        db.add(Workspace(id="workspace-method", name="Method", slug="method"))
        db.flush()
        db.add(Project(id="project-method", workspace_id="workspace-method", name="Method project", slug="method-project", description=""))
        db.add(DataSource(
            id="ds-legacy", project_id="project-method", name="legacy.csv", source_type="file",
            profile_json={"total_rows": 10, "table_count": 1}, routing_json={"recommended_stage": "L1"},
            created_at=base_time,
        ))
        db.add(DataSource(
            id="ds-l1", project_id="project-method", name="suppliers.csv", source_type="file",
            profile_json={"total_rows": 250, "table_count": 1},
            routing_json={"recommended_stage": "L1", "method": {
                "key": "rule_clean_w3c", "label": "规则清洗 + W3C 契约", "stage": "L1",
                "reason": "检测到 1 张 250 行的小表，字段有空格和大小写噪声。",
            }},
            created_at=base_time + timedelta(minutes=1),
        ))
        db.add(DataSource(
            id="ds-l2", project_id="project-method", name="customers.parquet", source_type="file",
            profile_json={"total_rows": 35000, "table_count": 2},
            routing_json={"recommended_stage": "L2", "method": {
                "key": "splink_duckdb", "label": "Splink + DuckDB", "stage": "L2",
                "reason": "检测到 2 张表共 35000 行，需要跨库匹配。",
            }},
            created_at=base_time + timedelta(minutes=2),
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "method-user", "email": "method@example.test", "display_name": "Method User",
        "workspace_id": "workspace-method", "role": "editor",
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
                id: 'method-user', email: 'method@example.test', display_name: 'Method User',
                role: 'editor', workspace_id: 'workspace-method'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            open_route(page, "#/data")
            expect(page.get_by_text("规则清洗 + W3C 契约", exact=False).first).to_be_visible(timeout=15000)
            expect(page.get_by_text("Splink + DuckDB", exact=False).first).to_be_visible()

            open_route(page, "#/tasks/new/match")
            expect(page.get_by_role("heading", name="处理方法", exact=True)).to_be_visible()
            expect(page.get_by_text("Splink + DuckDB", exact=False).first).to_be_visible()

            open_route(page, "#/tasks/new/entity")
            expect(page.get_by_role("heading", name="实体统一工作台", exact=True)).to_be_visible()
            expect(page.get_by_text("Splink + DuckDB", exact=False).first).to_be_visible()
            expect(page.get_by_role("button", name="仍然直接匹配", exact=True)).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_method_labels_and_banner()
