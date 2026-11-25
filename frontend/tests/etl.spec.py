"""ETL database sink and persisted schedules end-to-end (SQLite target).

The workbench runs a real L1 job against an isolated SQLite database, writes the
clean result into a second SQLite file through the ETL sink, then saves and
runs a persisted schedule through the same path. No external network is touched.
Vite is expected on 127.0.0.1:5173 and FastAPI on 127.0.0.1:8765, matching CI.
"""
from datetime import datetime
from pathlib import Path
import json
import shutil
import sqlite3
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
from app.core.security import encrypt_secret
from app.main import app
from app.models.entities import DataSource, Project, Workspace
from app.services.engines import build_demo_dirty_suppliers
from app.services.storage import storage
import app.workers.tasks as worker_tasks


def test_etl_sink_and_schedule():
    database_path = Path("/tmp") / f"open-semantic-unifier-etl-{uuid.uuid4().hex}.db"
    target_path = Path("/tmp") / f"open-semantic-unifier-etl-target-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage_root = Path("/tmp") / f"open-semantic-unifier-etl-storage-{uuid.uuid4().hex}"
    storage.backend = "local"
    storage.local_root = str(storage_root)

    with maker() as db:
        db.add(Workspace(id="workspace-etl", name="ETL", slug="etl"))
        db.flush()
        db.add(Project(id="project-etl", workspace_id="workspace-etl", name="ETL project", slug="etl-project", description=""))
        db.add(DataSource(
            id="ds-dirty", project_id="project-etl", name="dirty_suppliers.csv", source_type="file",
            object_key="projects/project-etl/uploads/dirty_suppliers.csv",
            profile_json={
                "total_rows": 60, "table_count": 1,
                "tables": [{"name": "dirty_suppliers.csv", "rows": 60, "cols": 5,
                            "columns": ["row_id", "supplier_raw_name", "source_system", "order_amount_cny", "ground_truth_id"], "preview": []}],
            },
            routing_json={
                "recommended_stage": "L1",
                "suggested_params": {"name_col": "supplier_raw_name", "amount_col": "order_amount_cny"},
            },
            created_at=datetime(2026, 1, 2, 0, 0, 0),
        ))
        db.add(DataSource(
            id="ds-target", project_id="project-etl", name="sqlite_target.db", source_type="sqlite",
            encrypted_config=encrypt_secret(json.dumps({"db_type": "sqlite", "database": str(target_path)})),
            table_name="", custom_sql="",
            profile_json={}, routing_json={},
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        ))
        db.commit()

    storage.put_bytes(
        "projects/project-etl/uploads/dirty_suppliers.csv",
        build_demo_dirty_suppliers(60).to_csv(index=False).encode("utf-8-sig"),
    )

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "etl-user", "email": "etl@example.test", "display_name": "ETL User",
        "workspace_id": "workspace-etl", "role": "editor",
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
                id: 'etl-user', email: 'etl@example.test', display_name: 'ETL User',
                role: 'editor', workspace_id: 'workspace-etl'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            open_route(page, "#/tasks/new/clean")

            expect(page.get_by_role("button", name="运行 L1", exact=True)).to_be_visible(timeout=15000)
            expect(page.locator("select[aria-label='目标数据库'] option[value='ds-target']")).to_be_attached(timeout=15000)

            page.get_by_label("目标数据库").select_option("ds-target")
            page.get_by_label("目标表").fill("clean_out")
            expect(page.get_by_role("button", name="运行 L1", exact=True)).to_be_enabled(timeout=15000)
            page.get_by_role("button", name="运行 L1", exact=True).click()

            expect(page.get_by_text("已写入数据库", exact=False)).to_be_visible(timeout=60000)
            result_line = page.get_by_text("已写入数据库", exact=False)
            expect(result_line).to_contain_text("sqlite")
            expect(result_line).to_contain_text("clean_out")
            expect(result_line).to_contain_text("写入")

            # Prove the sink really wrote rows to the target SQLite file.
            con = sqlite3.connect(str(target_path))
            try:
                assert con.execute("SELECT COUNT(*) FROM clean_out").fetchone()[0] >= 1
                columns = [row[1] for row in con.execute("PRAGMA table_info(clean_out)").fetchall()]
                assert "canonical_id" in columns
            finally:
                con.close()

            # Persist a 15-minute schedule and run it immediately.
            page.get_by_label("定时间隔").select_option("15")
            page.get_by_role("button", name="保存定时任务", exact=True).click()
            expect(page.get_by_text("下次运行", exact=False)).to_be_visible(timeout=15000)
            expect(page.get_by_role("cell", name="15 分钟", exact=True)).to_be_visible()

            page.get_by_role("button", name="立即运行", exact=True).click()
            expect(page.get_by_text("已触发运行", exact=False)).to_be_visible(timeout=30000)
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        worker_tasks.SessionLocal = original_session
        engine.dispose()
        database_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        shutil.rmtree(storage_root, ignore_errors=True)


if __name__ == "__main__":
    test_etl_sink_and_schedule()
