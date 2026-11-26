"""Capture representative README screenshots from a seeded demo project.

Requires the FastAPI test server on 127.0.0.1:8765 and Vite on 127.0.0.1:5173
with VITE_API_TARGET=http://127.0.0.1:8765. This is a development utility, not
part of the CI suite.

Run from the repository root:
    python3 frontend/tests/capture_screenshots.py
"""
from datetime import datetime, timedelta
from pathlib import Path
import re
import sys
from threading import Thread
import time
import urllib.request
import uuid

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn

BACKEND = Path(__file__).resolve().parents[2] / "backend"
ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets" / "screenshots"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_helpers import open_route

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.main import app
from app.models.entities import DataSource, Project, Workspace
from app.services.unification.releases import execute_unification_run

APP = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8765"


def _seed(maker):
    base = datetime(2026, 1, 1)
    suppliers = {
        "total_rows": 250, "table_count": 1, "has_whitespace_noise": True,
        "detected_keys": {"name_col": "supplier_raw_name"},
        "tables": [{
            "name": "suppliers.csv", "rows": 250, "cols": 4,
            "columns": ["supplier_raw_name", "city", "tax_id", "order_amount_cny"],
            "preview": [{"supplier_raw_name": " Acme  Ltd ", "city": "Shenzhen", "tax_id": "91310000", "order_amount_cny": "128000"}],
        }],
    }
    customers = {
        "total_rows": 35000, "table_count": 2,
        "detected_keys": {"name_col": "company_name", "tax_col": "tax_id", "phone_col": "phone"},
        "tables": [{
            "name": "customers.parquet", "rows": 35000, "cols": 4,
            "columns": ["company_name", "tax_id", "phone", "city"],
            "preview": [{"company_name": "Acme Ltd", "tax_id": "91310000", "phone": "13800000000", "city": "Shenzhen"}],
        }],
    }
    with maker() as db:
        db.add(Workspace(id="workspace-demo", name="示例空间", slug="demo"))
        db.flush()
        db.add(Project(id="project-demo", workspace_id="workspace-demo", name="供应商与客户统一", slug="demo-project", description=""))
        db.add(DataSource(
            id="ds-suppliers", project_id="project-demo", name="suppliers.csv", source_type="file",
            profile_json=suppliers,
            routing_json={
                "recommended_stage": "L1",
                "method": {
                    "key": "openrefine_w3c", "label": "OpenRefine + W3C 契约", "stage": "L1",
                    "reason": "检测到 1 张 250 行的小表，字段有空格和大小写噪声。",
                },
                "suggested_params": {"name_col": "supplier_raw_name", "amount_col": "order_amount_cny"},
            },
            created_at=base + timedelta(minutes=1),
        ))
        db.add(DataSource(
            id="ds-customers", project_id="project-demo", name="customers.parquet", source_type="file",
            profile_json=customers,
            routing_json={
                "recommended_stage": "L2",
                "method": {"key": "splink_duckdb", "label": "Splink + DuckDB", "stage": "L2", "reason": "检测到 2 张表共 35,000 行，需要跨库匹配。"},
                "suggested_params": {"name_col": "company_name", "tax_col": "tax_id", "phone_col": "phone", "city_col": "city"},
            },
            created_at=base,
        ))
        db.commit()


def _demo_run(maker) -> str:
    import app.api.v1.unification as unification_api
    original = unification_api.dispatch_unification_run

    def dispatch(run_id: str) -> None:
        with maker() as db:
            execute_unification_run(db, run_id)

    unification_api.dispatch_unification_run = dispatch
    try:
        response = httpx.post(f"{API}/api/v1/projects/project-demo/unification/demo", timeout=30)
        response.raise_for_status()
        return response.json()["run_id"]
    finally:
        unification_api.dispatch_unification_run = original


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    db_path = Path("/tmp") / f"open-semantic-unifier-screenshots-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    _seed(maker)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "screenshot-user", "email": "screenshot@example.test", "display_name": "示例用户",
        "workspace_id": "workspace-demo", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error", lifespan="off"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{API}/health", timeout=0.2).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("FastAPI test server did not become ready")

        run_id = _demo_run(maker)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            page.add_init_script("""
              localStorage.setItem('su_access_token', 'screenshot-token');
              localStorage.setItem('su_user_info', JSON.stringify({
                id: 'screenshot-user', email: 'screenshot@example.test', display_name: '示例用户',
                role: 'editor', workspace_id: 'workspace-demo'
              }));
            """)
            page.goto(APP)
            page.wait_for_load_state("networkidle")

            open_route(page, "#/data")
            expect(page.get_by_text("本项目已接入数据源", exact=False)).to_be_visible(timeout=15000)
            page.screenshot(path=str(ASSETS / "overview.png"))

            page.get_by_role("button", name="跨系统匹配字段", exact=True).click()
            expect(page.get_by_text("确认匹配字段（可多选）", exact=False)).to_be_visible(timeout=15000)
            page.get_by_label("选择数据源").select_option(label="customers.parquet")
            for field in ("company_name", "tax_id", "phone"):
                page.get_by_label(field, exact=True).check()
            page.wait_for_timeout(500)
            page.screenshot(path=str(ASSETS / "assistant-plan.png"))

            page.get_by_role("button", name="新对话", exact=True).click()
            open_route(page, f"#/tasks/run/{run_id}")
            expect(page.get_by_role("heading", name="实体统一工作台", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name=re.compile("人工复核")).first.click()
            expect(page.get_by_text("这两条记录是不是同一实体？", exact=False).first).to_be_visible(timeout=15000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(ASSETS / "review.png"))
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
