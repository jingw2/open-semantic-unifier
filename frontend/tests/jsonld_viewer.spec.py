"""Contract JSON-LD mapping and online instance viewer."""
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
from app.models.contracts import ContractModelVersion, ContractSource
from app.models.entities import DataSource, Project, Workspace
from app.models.unification import UnificationSnapshot
from app.services.contracts.validation import validate_contract_model
from app.services.storage import storage

LINKML = """id: https://example.org/supplier
name: supplier_model
prefixes:
  ex: https://example.org/
default_prefix: ex
classes:
  Supplier:
    attributes:
      tax_id:
        identifier: true
        required: true
      company_name:
        required: true
      city:
        range: string
"""

CSV = "tax_id,company_name,city\n91310000TEST,Acme,Shenzhen\n91310000BETA,Beta,Shanghai\n"


def test_jsonld_mapping_preview_and_export():
    database_path = Path("/tmp") / f"open-semantic-unifier-jsonld-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    storage_root = Path("/tmp") / f"open-semantic-unifier-jsonld-storage-{uuid.uuid4().hex}"
    storage.backend = "local"
    storage.local_root = str(storage_root)
    storage.put_bytes("projects/project-jsonld/uploads/instances.csv", CSV.encode())

    base_time = datetime(2026, 1, 1)
    report = validate_contract_model(linkml_yaml=LINKML, known_entity_types={"Supplier"})
    assert report.accepted, report.issues
    profile = {
        "total_rows": 2, "table_count": 1,
        "tables": [{
            "name": "instances.csv", "rows": 2, "cols": 3,
            "columns": ["tax_id", "company_name", "city"],
            "preview": [{"tax_id": "91310000TEST", "company_name": "Acme", "city": "Shenzhen"}],
        }],
    }
    with maker() as db:
        db.add(Workspace(id="workspace-jsonld", name="JSON-LD", slug="jsonld"))
        db.flush()
        db.add(Project(id="project-jsonld", workspace_id="workspace-jsonld", name="JSON-LD project", slug="jsonld-project", description=""))
        db.add(DataSource(
            id="ds-contract-yaml", project_id="project-jsonld", name="supplier.yaml", source_type="file",
            object_key="projects/project-jsonld/uploads/supplier.yaml", created_at=base_time,
        ))
        db.add(DataSource(
            id="ds-jsonld-instance", project_id="project-jsonld", name="instances.csv", source_type="file",
            object_key="projects/project-jsonld/uploads/instances.csv", profile_json=profile,
            created_at=base_time + timedelta(minutes=1),
        ))
        snapshot = UnificationSnapshot(
            project_id="project-jsonld", version=1,
            snapshot_json={"entities": {"entity-one": {"entity_type": "Supplier", "status": "ACTIVE"}}},
            actor_id="publisher",
        )
        db.add(snapshot)
        db.flush()
        db.add(ContractSource(
            id="cs-jsonld", project_id="project-jsonld", data_source_id="ds-contract-yaml",
            filename="supplier.yaml", format="yaml", sha256="ab" * 32,
            size_bytes=len(LINKML), created_by="editor-one",
        ))
        db.flush()
        db.add(ContractModelVersion(
            id="cv-jsonld", project_id="project-jsonld", source_id="cs-jsonld", version=1, status="DRAFT",
            source_sha256="ab" * 32, canonical_model_json=report.canonical_model_json,
            artifacts_json={"linkml_yaml": LINKML, "sql_ddl": "", "json_schema": "{}", "er_diagram": ""},
            compiler_versions_json={"linkml": "1.0.0"},
            validation_json={"counts": {"classes": 1}, "issues": []},
            entity_snapshot_id=snapshot.id, snapshot_version=1, created_by="editor-one",
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "jsonld-user", "email": "jsonld@example.test", "display_name": "JSON-LD User",
        "workspace_id": "workspace-jsonld", "role": "editor",
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
                id: 'jsonld-user', email: 'jsonld@example.test', display_name: 'JSON-LD User',
                role: 'editor', workspace_id: 'workspace-jsonld'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            open_route(page, "#/tasks/contract/cv-jsonld")
            expect(page.get_by_role("heading", name="契约统一工作台", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="JSON-LD", exact=True).click()
            expect(page.get_by_text("JSON-LD 实例 · v1", exact=True)).to_be_visible()
            page.get_by_label("JSON-LD 数据源").select_option(label="instances.csv")
            expect(page.get_by_label("tax_id 列")).to_have_value("tax_id", timeout=15000)
            expect(page.get_by_label("company_name 列")).to_have_value("company_name")
            expect(page.get_by_label("city 列")).to_have_value("city")

            page.get_by_role("button", name="生成预览", exact=True).click()
            expect(page.get_by_text("contract-v1.jsonld", exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_text('"@context"', exact=False)).to_be_visible()
            expect(page.get_by_text("urn:osu:91310000TEST", exact=False)).to_be_visible()
            expect(page.get_by_role("button", name="下载", exact=True)).to_be_visible()
            page.get_by_label("关闭", exact=True).click()
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
    test_jsonld_mapping_preview_and_export()
