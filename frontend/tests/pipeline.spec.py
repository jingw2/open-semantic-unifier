"""Assistant remembers the conversation and runs clean -> match automatically."""
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
from ui_helpers import open_route  # noqa: F401

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.main import app
from app.models.entities import DataSource, Project, Workspace
from app.services.storage import storage
import app.workers.tasks as worker_tasks

CSV = "company_name,tax_id,phone\nAcme Ltd,T1,138\nACME Ltd,T1,138\nBeta,T2,139\n"
PROFILE = {
    "total_rows": 3,
    "table_count": 1,
    "detected_keys": {"name_col": "company_name", "tax_col": "tax_id", "phone_col": "phone"},
    "tables": [{
        "name": "customers.csv", "rows": 3, "cols": 3,
        "columns": ["company_name", "tax_id", "phone"],
        "preview": [{"company_name": "Acme Ltd", "tax_id": "T1", "phone": "138"}],
    }],
}


def test_assistant_context_memory_and_clean_match_pipeline():
    database_path = Path("/tmp") / f"open-semantic-unifier-pipeline-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage_root = Path("/tmp") / f"open-semantic-unifier-pipeline-storage-{uuid.uuid4().hex}"
    storage.backend = "local"
    storage.local_root = str(storage_root)
    storage.put_bytes("projects/project-pipeline/uploads/customers.csv", CSV.encode())

    with maker() as db:
        db.add(Workspace(id="workspace-pipeline", name="Pipeline", slug="pipeline"))
        db.flush()
        db.add(Project(id="project-pipeline", workspace_id="workspace-pipeline", name="Pipeline project", slug="pipeline-project", description=""))
        db.add(DataSource(
            id="ds-pipeline", project_id="project-pipeline", name="customers.csv", source_type="file",
            object_key="projects/project-pipeline/uploads/customers.csv", profile_json=PROFILE,
            routing_json={
                "recommended_stage": "L2",
                "suggested_params": {"name_col": "company_name", "tax_col": "tax_id", "phone_col": "phone"},
            },
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "pipeline-user", "email": "pipeline@example.test", "display_name": "Pipeline User",
        "workspace_id": "workspace-pipeline", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None
    original_session = worker_tasks.SessionLocal
    worker_tasks.SessionLocal = maker

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
                id: 'pipeline-user', email: 'pipeline@example.test', display_name: 'Pipeline User',
                role: 'editor', workspace_id: 'workspace-pipeline'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            placeholder = "也可以直接说，例如：帮我把客户名单去重"
            page.get_by_placeholder(placeholder).fill("帮我把客户名单去重")
            page.get_by_placeholder(placeholder).press("Enter")
            expect(page.get_by_text("按关键词识别为 match。", exact=False)).to_be_visible(timeout=15000)

            # Reload and prove the conversation persists.
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.get_by_text("帮我把客户名单去重", exact=False)).to_be_visible(timeout=15000)
            expect(page.get_by_text("按关键词识别为 match。", exact=False)).to_be_visible()

            page.get_by_role("button", name="自动处理全流程", exact=True).click()
            expect(page.get_by_text("清洗字段（可多选）", exact=False)).to_be_visible(timeout=15000)
            expect(page.get_by_text("确认匹配字段（可多选）", exact=False)).to_be_visible()
            page.get_by_role("button", name="开始", exact=True).click()
            expect(page.get_by_text("全流程已完成", exact=False)).to_be_visible(timeout=120000)
            expect(page.get_by_text("匹配任务", exact=False)).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        worker_tasks.SessionLocal = original_session
        engine.dispose()
        database_path.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(storage_root, ignore_errors=True)


if __name__ == "__main__":
    test_assistant_context_memory_and_clean_match_pipeline()
