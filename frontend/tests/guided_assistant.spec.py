"""Guided assistant: chips, free text, failure handling, and auto-run."""
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
from app.models.entities import Project, Workspace
from app.services.unification.releases import execute_unification_run


def test_guided_assistant_runs_l1_and_entity_demo():
    database_path = Path("/tmp") / f"open-semantic-unifier-assistant-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="workspace-assistant", name="Assistant", slug="assistant"))
        db.flush()
        db.add(Project(id="project-assistant", workspace_id="workspace-assistant", name="Assistant project", slug="assistant-project", description=""))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "assistant-user", "email": "assistant@example.test", "display_name": "Assistant User",
        "workspace_id": "workspace-assistant", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None

    import app.workers.tasks as worker_tasks
    import app.api.v1.unification as unification_api
    original_session = worker_tasks.SessionLocal
    original_dispatch = unification_api.dispatch_unification_run
    worker_tasks.SessionLocal = maker

    def dispatch(run_id: str) -> None:
        with maker() as db:
            execute_unification_run(db, run_id)

    unification_api.dispatch_unification_run = dispatch

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
                id: 'assistant-user', email: 'assistant@example.test', display_name: 'Assistant User',
                role: 'editor', workspace_id: 'workspace-assistant'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            expect(page.get_by_text("你想完成什么？")).to_be_visible(timeout=15000)

            page.get_by_role("button", name="清洗供应商名单", exact=True).click()
            expect(page.get_by_text("规则清洗 + W3C 契约", exact=False).first).to_be_visible()

            route_pattern = "**/api/v1/projects/*/datasources/demo/L1"
            page.route(route_pattern, lambda route: route.abort())
            page.get_by_role("button", name="开始", exact=True).click()
            expect(page.get_by_role("button", name="去手动处理", exact=True)).to_be_visible(timeout=15000)
            page.unroute(route_pattern)

            page.get_by_role("button", name="清洗供应商名单", exact=True).click()
            page.get_by_role("button", name="开始", exact=True).click()
            expect(page.get_by_text("已完成", exact=False).first).to_be_visible(timeout=30000)

            page.get_by_role("button", name="查看结果", exact=True).click()
            expect(page.get_by_text("总行数", exact=False).first).to_be_visible(timeout=15000)

            page.get_by_role("button", name="解释当前页面", exact=True).click()
            expect(page.get_by_text("这是任务详情", exact=False)).to_be_visible()

            page.get_by_placeholder("也可以直接说，例如：帮我把客户名单去重").fill("帮我把客户名单去重")
            page.get_by_placeholder("也可以直接说，例如：帮我把客户名单去重").press("Enter")
            expect(page.get_by_text("跨系统匹配客户", exact=True).first).to_be_visible(timeout=15000)

            page.get_by_role("button", name="实体匹配", exact=True).click()
            page.get_by_role("button", name="开始", exact=True).click()
            expect(page.get_by_text("已完成", exact=False).first).to_be_visible(timeout=30000)
            page.get_by_role("button", name="查看结果", exact=True).click()
            expect(page.get_by_role("heading", name="实体统一工作台", exact=True)).to_be_visible(timeout=15000)
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        worker_tasks.SessionLocal = original_session
        unification_api.dispatch_unification_run = original_dispatch
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_guided_assistant_runs_l1_and_entity_demo()
