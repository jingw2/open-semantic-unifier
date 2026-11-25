"""Four-menu shell, task entry points, and the publish center."""
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


def test_menu_shell_reaches_tasks_reviews_settings_and_publish():
    database_path = Path("/tmp") / f"open-semantic-unifier-menu-shell-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="workspace-shell", name="Shell", slug="shell"))
        db.flush()
        db.add(Project(id="project-shell", workspace_id="workspace-shell", name="Shell project", slug="shell-project", description=""))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "shell-user", "email": "shell@example.test", "display_name": "Shell User",
        "workspace_id": "workspace-shell", "role": "editor",
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
                id: 'shell-user', email: 'shell@example.test', display_name: 'Shell User',
                role: 'editor', workspace_id: 'workspace-shell'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            for label in ("数据", "任务", "复核", "设置"):
                expect(page.get_by_role("button", name=label, exact=True)).to_be_visible()

            page.get_by_role("button", name="任务", exact=True).click()
            expect(page.get_by_role("heading", name="任务", exact=True)).to_be_visible()

            page.get_by_role("button", name="复核", exact=True).click()
            expect(page.get_by_role("heading", name="复核", exact=True)).to_be_visible()
            expect(page.get_by_text("人工复核队列", exact=True).first).to_be_visible()
            expect(page.get_by_text("实体配对", exact=True).first).to_be_visible()

            page.get_by_role("button", name="设置", exact=True).click()
            expect(page.get_by_role("button", name="模型 Provider", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="团队", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="API Key", exact=True)).to_be_visible()

            open_route(page, "#/tasks/publish")
            expect(page.get_by_role("heading", name="发布中心", exact=True)).to_be_visible()
            expect(page.get_by_text("还没有已生成的版本")).to_be_visible()

            open_route(page, "#/tasks/new/entity")
            expect(page.get_by_role("heading", name="实体统一工作台", exact=True)).to_be_visible()

            open_route(page, "#/tasks/job/missing-job")
            expect(page.get_by_text("任务不存在")).to_be_visible()
            expect(page.get_by_role("button", name="返回任务").first).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_menu_shell_reaches_tasks_reviews_settings_and_publish()
