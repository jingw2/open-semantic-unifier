"""OpenRefine preview, diff, adopt, cancel, and fallback in the real workbench."""
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
from app.api.v1.openrefine import get_openrefine_client
from app.core.config import settings
from app.core.db import Base, get_db
from app.main import app
from app.models.entities import DataSource, Project, Workspace
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
    healthy = True
    cleaned = AFTER

    def health(self) -> bool:
        return type(self).healthy

    def create_project(self, name: str, file_bytes: bytes, format: str = "csv") -> str:
        return "or-project"

    def apply_recipe(self, project_id: str, operations: list[dict]) -> None:
        return None

    def export_csv(self, project_id: str, project_name: str) -> bytes:
        return type(self).cleaned

    def delete_project(self, project_id: str) -> None:
        return None


def test_openrefine_preview_adopt_cancel_and_fallback():
    database_path = Path("/tmp") / f"open-semantic-unifier-openrefine-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage_root = Path("/tmp") / f"open-semantic-unifier-openrefine-storage-{uuid.uuid4().hex}"
    storage.backend = "local"
    storage.local_root = str(storage_root)
    settings.openrefine_enabled = True

    with maker() as db:
        db.add(Workspace(id="workspace-or", name="OpenRefine", slug="openrefine"))
        db.flush()
        db.add(Project(id="project-or", workspace_id="workspace-or", name="OpenRefine project", slug="openrefine-project", description=""))
        db.add(DataSource(
            id="ds-openrefine", project_id="project-or", name="dirty.csv", source_type="file",
            object_key="projects/project-or/uploads/dirty.csv",
            profile_json=PROFILE,
            routing_json={
                "recommended_stage": "L1",
                "method": {
                    "key": "openrefine_w3c", "label": "OpenRefine + W3C 契约", "stage": "L1",
                    "reason": "检测到有空格噪声的小表。",
                },
            },
            created_at=datetime(2026, 1, 1),
        ))
        db.commit()

    storage.put_bytes("projects/project-or/uploads/dirty.csv", BEFORE)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "openrefine-user", "email": "openrefine@example.test", "display_name": "OpenRefine User",
        "workspace_id": "workspace-or", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None
    app.dependency_overrides[get_openrefine_client] = lambda: FakeClient()

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
                id: 'openrefine-user', email: 'openrefine@example.test', display_name: 'OpenRefine User',
                role: 'editor', workspace_id: 'workspace-or'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            open_route(page, "#/tasks/new/clean")
            expect(page.get_by_role("heading", name="处理方法", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_text("ETL 写入与定时", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="OpenRefine 预览", exact=True)).to_be_visible()
            expect(page.get_by_text("字段设置", exact=True)).to_be_visible(timeout=15000)
            page.get_by_label("名称字段").select_option("city")
            page.get_by_role("button", name="OpenRefine 预览", exact=True).click()
            expect(page.get_by_text("OpenRefine 清洗预览 · 公司名称归一", exact=False)).to_be_visible(timeout=15000)
            expect(page.get_by_text("Acme, Ltd", exact=True)).to_be_visible()
            expect(page.get_by_text("Acme Ltd", exact=True).first).to_be_visible()
            expect(page.get_by_role("button", name="撤销", exact=True)).to_be_visible()

            page.get_by_role("button", name="撤销", exact=True).click()
            expect(page.get_by_text("OpenRefine 清洗预览 · 公司名称归一", exact=False)).to_have_count(0)

            page.get_by_role("button", name="OpenRefine 预览", exact=True).click()
            expect(page.get_by_role("button", name="采用并生成新数据源", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="采用并生成新数据源", exact=True).click()
            expect(page.get_by_text("已生成新数据源：dirty（OpenRefine 清洗）", exact=False)).to_be_visible(timeout=15000)

            FakeClient.healthy = False
            open_route(page, "#/data")
            expect(page.get_by_text("dirty（OpenRefine 清洗）", exact=False).first).to_be_visible(timeout=15000)
            open_route(page, "#/tasks/new/clean")
            page.get_by_role("button", name="OpenRefine 预览", exact=True).click()
            expect(page.get_by_text("OpenRefine 不可用，已降级为规则清洗。", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_role("button", name="运行规则清洗", exact=True).last).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(storage_root, ignore_errors=True)


if __name__ == "__main__":
    test_openrefine_preview_adopt_cancel_and_fallback()
