"""Standard roster: mark a source, map its fields, and align L1 against it."""
from datetime import datetime
from pathlib import Path
import os
import shutil
import sys
import tempfile
from threading import Thread
import time
import urllib.request
import uuid

import pandas as pd
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

ROSTER_CSV = b"\xef\xbb\xbfsupplier_id,company_name,aliases,tax_id,phone\nSUP-1,Acme,Acme Ltd|ACME,9144,13800000000\nSUP-2,Beta,Beta Corp,9145,13900000000\n"


def test_roster_badge_editor_and_l1_alignment():
    database_path = Path("/tmp") / f"open-semantic-unifier-roster-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage_root = Path("/tmp") / f"open-semantic-unifier-roster-storage-{uuid.uuid4().hex}"
    storage.backend = "local"
    storage.local_root = str(storage_root)

    with maker() as db:
        db.add(Workspace(id="workspace-roster", name="Roster", slug="roster"))
        db.flush()
        db.add(Project(id="project-roster", workspace_id="workspace-roster", name="Roster project", slug="roster-project", description=""))
        db.add(DataSource(
            id="ds-roster", project_id="project-roster", name="suppliers.csv", source_type="file",
            object_key="projects/project-roster/uploads/suppliers.csv",
            profile_json={
                "total_rows": 2, "table_count": 1,
                "tables": [{"name": "suppliers.csv", "rows": 2, "cols": 5,
                            "columns": ["supplier_id", "company_name", "aliases", "tax_id", "phone"], "preview": []}],
            },
            routing_json={"recommended_stage": "L1"},
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        ))
        db.add(DataSource(
            id="ds-dirty", project_id="project-roster", name="dirty.parquet", source_type="file",
            object_key="projects/project-roster/uploads/dirty.parquet",
            profile_json={
                "total_rows": 2, "table_count": 1,
                "tables": [{"name": "dirty.parquet", "rows": 2, "cols": 1,
                            "columns": ["supplier_raw_name"], "preview": []}],
            },
            routing_json={
                "recommended_stage": "L1",
                "method": {"key": "openrefine_w3c", "label": "OpenRefine + W3C 契约", "stage": "L1", "reason": "检测到有空格噪声的小表。"},
            },
            created_at=datetime(2026, 1, 2, 0, 0, 0),
        ))
        db.commit()

    storage.put_bytes("projects/project-roster/uploads/suppliers.csv", ROSTER_CSV)
    with tempfile.TemporaryDirectory() as tmp:
        parquet_path = os.path.join(tmp, "dirty.parquet")
        pd.DataFrame({"supplier_raw_name": ["Acme Ltd", "Beta"]}).to_parquet(parquet_path, index=False)
        storage.put_file("projects/project-roster/uploads/dirty.parquet", parquet_path)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "roster-user", "email": "roster@example.test", "display_name": "Roster User",
        "workspace_id": "workspace-roster", "role": "editor",
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
                id: 'roster-user', email: 'roster@example.test', display_name: 'Roster User',
                role: 'editor', workspace_id: 'workspace-roster'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            open_route(page, "#/data")

            expect(page.get_by_text("suppliers.csv", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_text("dirty.parquet", exact=True)).to_be_visible(timeout=15000)

            page.get_by_role("button", name="设为标准花名册", exact=True).click()
            expect(page.get_by_text("标准花名册", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_role("combobox", name="名称列")).to_be_visible(timeout=15000)
            expect(page.get_by_role("button", name="保存字段", exact=True)).to_be_visible()
            page.get_by_role("button", name="保存字段", exact=True).click()
            expect(page.get_by_text("已保存花名册字段映射。", exact=False)).to_be_visible(timeout=15000)

            page.get_by_role("button", name="进入处理方法 →", exact=True).nth(0).click()
            expect(page.get_by_role("button", name="运行规则清洗", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="运行规则清洗", exact=True).click()
            expect(page.get_by_text("对齐来源：标准花名册", exact=False)).to_be_visible(timeout=30000)
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        worker_tasks.SessionLocal = original_session
        engine.dispose()
        database_path.unlink(missing_ok=True)
        shutil.rmtree(storage_root, ignore_errors=True)


if __name__ == "__main__":
    test_roster_badge_editor_and_l1_alignment()
