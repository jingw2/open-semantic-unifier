"""Real browser workflow for the Contracts workbench against FastAPI and an isolated SQLite database."""
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
from app.models.unification import UnificationSnapshot
from app.services.storage import storage

FIXTURE = BACKEND / "tests" / "fixtures" / "contracts" / "minimal_schema.yaml"


def test_contracts_workbench_real_browser_flow_selects_compiles_publishes_and_exports():
    database_path = Path("/tmp") / f"open-semantic-unifier-contracts-e2e-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    object_key = "projects/project-live/uploads/minimal_schema.yaml"
    storage.put_bytes(object_key, FIXTURE.read_bytes())
    invalid_key = "projects/project-live/uploads/invalid_schema.yaml"
    invalid_schema = (
        "id: https://example.org/contracts/invalid\n"
        "name: invalid_contract\n"
        "prefixes:\n"
        "  linkml: https://w3id.org/linkml/\n"
        "  invalid: https://example.org/contracts/invalid/\n"
        "default_prefix: invalid\n"
        "imports:\n"
        "  - linkml:types\n"
        "default_range: string\n"
        "classes:\n"
        "  Other:\n"
        "    attributes:\n"
        "      name: {}\n"
    ).encode()
    storage.put_bytes(invalid_key, invalid_schema)
    with maker() as db:
        db.add(Workspace(id="workspace-live", name="Live Contracts E2E", slug="live-contracts-e2e"))
        db.flush()
        db.add(Project(id="project-live", workspace_id="workspace-live", name="Contract master", slug="contract-master", description=""))
        db.flush()
        db.add(DataSource(id="ds-contract-file", project_id="project-live", name="minimal_schema.yaml", source_type="file", object_key=object_key))
        db.add(DataSource(id="ds-contract-invalid", project_id="project-live", name="invalid_schema.yaml", source_type="file", object_key=invalid_key))
        db.add(UnificationSnapshot(
            id="snapshot-contract", project_id="project-live", version=1,
            snapshot_json={"entities": {"e1": {"entity_type": "Task"}}},
            actor_id="browser-contract",
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "browser-contract", "email": "contract@example.test", "display_name": "Browser Contract",
        "workspace_id": "workspace-live", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None

    api_port = 8765
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error", lifespan="off"))
    server_thread = Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=0.2).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("FastAPI test server did not become ready")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(accept_downloads=True)
            page.add_init_script("""
              localStorage.setItem('su_access_token', 'browser-test-token');
              localStorage.setItem('su_user_info', JSON.stringify({
                id: 'browser-contract', email: 'contract@example.test', display_name: 'Browser Contract',
                role: 'editor', workspace_id: 'workspace-live'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            open_route(page, "#/tasks/new/contract")
            expect(page.get_by_role("heading", name="契约统一工作台")).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)

            page.get_by_label("文件数据源").select_option("ds-contract-file")
            page.get_by_role("button", name="添加契约源").click()
            expect(page.get_by_role("status")).to_contain_text("契约源已登记")
            expect(page.get_by_role("cell", name="minimal_schema.yaml")).to_be_visible()
            expect(page.get_by_role("cell", name="linkml_yaml")).to_be_visible()

            page.get_by_label("契约源").select_option(index=1)
            page.get_by_label("实体快照").select_option(index=1)
            page.get_by_role("button", name="编译并校验").click()
            expect(page.get_by_role("status")).to_contain_text("模型版本 v1 已创建", timeout=30000)
            expect(page.get_by_role("cell", name="DRAFT")).to_be_visible()
            expect(page.get_by_role("cell", name="类 1 · 属性 3 · 枚举 1 · 关系 0")).to_be_visible()

            page.get_by_role("button", name="发布与审计 尚未发布").click()
            page.get_by_label("目标版本").select_option(index=1)
            page.get_by_label("发布原因").fill("发布首版契约模型")
            page.get_by_role("button", name="发布版本").click()
            expect(page.get_by_text("当前发布：v1")).to_be_visible()
            expect(page.get_by_role("cell", name="PUBLISH")).to_be_visible()
            expect(page.get_by_role("cell", name="发布首版契约模型")).to_be_visible()

            page.get_by_role("button", name="版本历史 1 个版本").click()
            with page.expect_download() as download_event:
                page.get_by_role("button", name="model", exact=True).click()
            download = download_event.value
            assert download.suggested_filename == "contract-v1-model.json"
            expect(page.get_by_role("alert")).to_have_count(0)

            page.get_by_text("契约源与模型版本").click()
            page.get_by_label("文件数据源").select_option("ds-contract-invalid")
            page.get_by_role("button", name="添加契约源").click()
            expect(page.get_by_role("status")).to_contain_text("契约源已登记")
            invalid_source = page.get_by_label("契约源").locator("option", has_text="invalid_schema.yaml").get_attribute("value")
            page.get_by_label("契约源").select_option(value=invalid_source)
            page.get_by_label("实体快照").select_option(index=1)
            page.get_by_role("button", name="编译并校验").click()
            expect(page.get_by_text("校验未通过，共 1 个问题：")).to_be_visible(timeout=30000)
            expect(page.get_by_text("UNKNOWN_ENTITY_TYPE")).to_be_visible()
            expect(page.get_by_text("classes.Other")).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


def test_contracts_acceptance_with_supplier_contract():
    database_path = Path("/tmp") / f"open-semantic-unifier-contracts-acceptance-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    fixture = BACKEND / "tests" / "fixtures" / "contracts" / "supplier_contract.yaml"
    object_key = "projects/project-acceptance/uploads/supplier_contract.yaml"
    storage.put_bytes(object_key, fixture.read_bytes())
    with maker() as db:
        db.add(Workspace(id="workspace-acceptance", name="Contracts Acceptance", slug="contracts-acceptance"))
        db.flush()
        db.add(Project(id="project-acceptance", workspace_id="workspace-acceptance", name="Supplier contracts", slug="supplier-contracts", description=""))
        db.flush()
        db.add(DataSource(id="ds-supplier", project_id="project-acceptance", name="supplier_contract.yaml", source_type="file", object_key=object_key))
        db.add(UnificationSnapshot(
            id="snapshot-supplier", project_id="project-acceptance", version=1,
            snapshot_json={"entities": {
                "e1": {"entity_type": "Supplier"},
            }},
            actor_id="acceptance",
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "acceptance-editor", "email": "acceptance@example.test", "display_name": "Acceptance Editor",
        "workspace_id": "workspace-acceptance", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None

    api_port = 8765
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error", lifespan="off"))
    server_thread = Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=0.2).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("FastAPI test server did not become ready")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(accept_downloads=True)
            page.add_init_script("""
              localStorage.setItem('su_access_token', 'browser-test-token');
              localStorage.setItem('su_user_info', JSON.stringify({
                id: 'acceptance-editor', email: 'acceptance@example.test', display_name: 'Acceptance Editor',
                role: 'editor', workspace_id: 'workspace-acceptance'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            open_route(page, "#/tasks/new/contract")
            expect(page.get_by_role("heading", name="契约统一工作台")).to_be_visible()

            page.get_by_label("文件数据源").select_option("ds-supplier")
            page.get_by_role("button", name="添加契约源").click()
            expect(page.get_by_role("status")).to_contain_text("契约源已登记")
            expect(page.get_by_role("cell", name="supplier_contract.yaml")).to_be_visible()

            page.get_by_label("契约源").select_option(index=1)
            page.get_by_label("实体快照").select_option(index=1)
            page.get_by_role("button", name="编译并校验").click()
            expect(page.get_by_text("UNKNOWN_ENTITY_TYPE", exact=False)).to_be_visible(timeout=30000)
            expect(page.get_by_role("button", name="允许 Country 并重新校验", exact=True)).to_be_visible()
            page.get_by_role("button", name="允许 Country 并重新校验", exact=True).click()
            expect(page.get_by_role("status")).to_contain_text("模型版本 v1 已创建", timeout=30000)
            expect(page.get_by_role("cell", name="类 2 · 属性 8 · 枚举 1 · 关系 2")).to_be_visible()

            page.get_by_role("button", name="发布与审计 尚未发布").click()
            page.get_by_label("目标版本").select_option(index=1)
            page.get_by_label("发布原因").fill("L3-01 acceptance")
            page.get_by_role("button", name="发布版本").click()
            expect(page.get_by_text("当前发布：v1")).to_be_visible()
            expect(page.get_by_role("cell", name="PUBLISH")).to_be_visible()

            page.get_by_role("button", name="版本历史 1 个版本").click()
            with page.expect_download() as download_event:
                page.get_by_role("button", name="model", exact=True).click()
            download = download_event.value
            assert download.suggested_filename == "contract-v1-model.json"
            content = Path(download.path()).read_text(encoding="utf-8")
            assert '"relations":{"Supplier":["Country","Supplier"]}' in content, content
            assert '"SupplierStatus"' in content and '"Country"' in content
            expect(page.get_by_role("alert")).to_have_count(0)
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_contracts_workbench_real_browser_flow_selects_compiles_publishes_and_exports()
    test_contracts_acceptance_with_supplier_contract()
