"""Real browser workflow against FastAPI and an isolated SQLite database."""
from pathlib import Path
import json
import sys
from threading import Thread
import time
import urllib.request
import uuid

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import uvicorn

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_helpers import open_route

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.models.entities import Project, User, Workspace, WorkspaceMember
from app.services.unification.releases import execute_unification_run
from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing


def test_workbench_real_browser_flow_publishes_reviewed_snapshot_and_exports():
    database_path = Path("/tmp") / f"open-semantic-unifier-browser-e2e-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="workspace-live", name="Live E2E", slug="live-e2e"))
        db.flush()
        db.add(Project(id="project-live", workspace_id="workspace-live", name="Supplier master", slug="supplier-master", description=""))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "browser-reviewer", "email": "browser@example.test", "display_name": "Browser Reviewer",
        "workspace_id": "workspace-live", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None
    exporter = InMemorySpanExporter()
    trace_provider = configure_tracing(TraceConfig(enabled=True), span_exporter=exporter)
    def dispatch_inline(run_id: str):
        with maker() as db:
            execute_unification_run(db, run_id)

    import app.api.v1.unification as unification_api
    original_route_dispatch = unification_api.dispatch_unification_run
    unification_api.dispatch_unification_run = dispatch_inline

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
                id: 'browser-reviewer', email: 'browser@example.test', display_name: 'Browser Reviewer',
                role: 'editor', workspace_id: 'workspace-live'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            open_route(page, "#/tasks/new/entity")
            expect(page.get_by_role("heading", name="实体统一工作台")).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)

            page.get_by_label("强标识字段（逗号分隔）").fill("")
            field_map = page.get_by_label("标准字段 → 来源字段（JSON）")
            field_map.fill('{"name":')
            expect(field_map).to_have_value('{"name":')
            field_map.fill('{"name": "name", "tax_id": "tax_id"}')
            page.get_by_role("button", name="保存方案").click()
            expect(page.get_by_role("status")).to_contain_text("方案已保存")
            page.get_by_role("button", name="保存方案").click()
            expect(page.get_by_text("方案 v1 已保存")).to_be_visible()
            page.get_by_role("button", name="导入批次").click()
            expect(page.get_by_role("status")).to_contain_text("批次已接收")
            page.get_by_role("button", name="运行记录 0 次").click()
            page.get_by_role("button", name="提交运行").click()
            expect(page.get_by_text("运行完成")).to_be_visible(timeout=10000)
            page.get_by_role("button", name="运行记录 1 次").click()
            expect(page.get_by_text("trace_id:")).to_be_visible()
            expect(page.get_by_role("link", name="在 Phoenix 中打开")).to_be_visible()

            page.get_by_role("button", name="人工复核 1 待处理").click()
            expect(page.get_by_text("CANDIDATE", exact=True).first).to_be_visible()
            page.once("dialog", lambda dialog: dialog.accept("人工确认同一供应商"))
            page.get_by_role("button", name="确认同一实体").click()
            expect(page.get_by_text("已记录合并裁决")).to_be_visible()
            expect(page.get_by_role("button", name="人工复核 0 待处理")).to_be_visible()

            page.once("dialog", lambda dialog: dialog.accept("撤销错误合并"))
            page.get_by_role("button", name="撤销").click()
            expect(page.get_by_text("撤销错误合并")).to_be_visible()
            expect(page.get_by_role("button", name="人工复核 1 待处理")).to_be_visible()
            page.once("dialog", lambda dialog: dialog.accept("复核后再次确认同一供应商"))
            page.get_by_role("button", name="确认同一实体").click()
            expect(page.get_by_text("已记录合并裁决")).to_be_visible()

            page.get_by_role("button", name="快照与导出 尚未发布").click()
            page.get_by_role("button", name="发布快照").click()
            expect(page.get_by_text("已发布快照 v1")).to_be_visible()
            expect(page.get_by_text("supplier · ACTIVE", exact=True).first).to_be_visible()

            outputs = {}
            for kind in ("entities", "mappings", "decisions", "delta"):
                with page.expect_download() as download_event:
                    page.get_by_role("button", name=kind, exact=True).click()
                download = download_event.value
                destination = Path("/tmp") / download.suggested_filename
                download.save_as(destination)
                outputs[kind] = json.loads(destination.read_text())
                direct = page.request.get(f"http://127.0.0.1:8765/api/v1/projects/project-live/unification/snapshots/{outputs[kind]['snapshot_id']}/export?kind={kind}")
                assert direct.ok and outputs[kind] == direct.json()
            snapshot_ids = {payload["snapshot_id"] for payload in outputs.values()}
            assert len(snapshot_ids) == 1
            entity_ids = {entity["entity_id"] for entity in outputs["entities"]["entities"]}
            assert len(entity_ids) == 2
            assignments = {mapping["source_key"][3]: mapping["entity_id"] for mapping in outputs["mappings"]["mappings"]}
            assert assignments["S-1001"] == assignments["P-77"]
            assert assignments["P-78"] != assignments["P-77"]
            assert set(assignments.values()) == entity_ids
            assert [decision["reason"] for decision in outputs["decisions"]["decisions"] if decision["change_type"] == "MERGE"] == ["人工确认同一供应商", "复核后再次确认同一供应商"]
            assert outputs["decisions"]["identity_revision"] == 4
            assert all(change["trace_id"] for change in outputs["decisions"]["decisions"])
            assert outputs["delta"]["delta"]["added_entities"]
            target_id = outputs["entities"]["entities"][0]["entity_id"]
            replies = iter(['"Verified name"', "Verified document"])
            def override_dialog(dialog):
                dialog.accept() if dialog.type == "confirm" else dialog.accept(next(replies))
            page.on("dialog", override_dialog)
            page.get_by_role("button", name="覆盖属性").first.click()
            expect(page.get_by_role("status")).to_contain_text("属性覆盖已记录")
            page.get_by_role("button", name="运行记录 1 次").click()
            page.get_by_role("button", name="提交运行").click()
            expect(page.get_by_text("运行完成")).to_be_visible(timeout=10000)
            page.get_by_role("button", name="快照与导出 v1").click()
            page.get_by_role("button", name="发布快照").click()
            expect(page.get_by_text("已发布快照 v2")).to_be_visible()
            expect(page.get_by_text('"Verified name"')).to_be_visible()
            previous = page.request.get(f"http://127.0.0.1:8765/api/v1/projects/project-live/unification/snapshots/{next(iter(snapshot_ids))}/export?kind=entities")
            assert previous.ok and previous.json() == outputs["entities"]
            assert target_id in {entity["entity_id"] for entity in outputs["entities"]["entities"]}
            page.remove_listener("dialog", override_dialog)
            def rollback_dialog(dialog):
                dialog.accept() if dialog.type == "confirm" else dialog.accept("Restore verified snapshot")
            page.on("dialog", rollback_dialog)
            page.locator("div.rounded-xl.border").filter(has_text="快照 v1").get_by_role("button", name="从此版本回滚").click()
            expect(page.get_by_text("已创建回滚快照 v3")).to_be_visible()
            changes = page.request.get("http://127.0.0.1:8765/api/v1/projects/project-live/unification/identity-changes").json()["items"]
            assert any(change["change_type"] == "ROLLBACK" and change["reason"] == "Restore verified snapshot" for change in changes)
            assert trace_provider.force_flush()
            span_names = {span.name for span in exporter.get_finished_spans()}
            assert {"unification.api_review_decision", "unification.api_identity_undo", "unification.api_publish_snapshot"} <= span_names
            page.screenshot(path="/tmp/open-semantic-unifier-workbench.png", full_page=True)
            page.remove_listener("dialog", rollback_dialog)

            # Exercise the customer golden pair through the UI in a clean project.
            with maker() as db:
                db.add_all([
                    Project(id="project-customer", workspace_id="workspace-live", name="Customer golden", slug="customer-golden"),
                    Project(id="project-replay", workspace_id="workspace-live", name="Customer replay", slug="customer-replay"),
                ])
                db.commit()
            fixture = Path(__file__).resolve().parents[2] / "tests/golden/v1/customer"
            records = [{key: row[key] for key in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((fixture / "records.json").read_text()) if row["record_ref"] in {"customer:erp:02", "customer:secondary:02"}]
            page.reload()
            page.wait_for_load_state("networkidle")
            page.locator("select").first.select_option("project-customer")
            open_route(page, "#/tasks/new/entity")
            page.get_by_label("方案名称").fill("customers")
            page.get_by_label("实体类型").select_option("customer")
            page.get_by_label("身份粒度").fill("person")
            page.get_by_label("强标识字段（逗号分隔）").fill("")
            page.get_by_label("标准字段 → 来源字段（JSON）").fill('{"name":"name"}')
            page.get_by_text("高级匹配设置：候选规则与冲突约束").click()
            page.get_by_label("候选召回规则").fill('[{"kind":"name_similarity","field":"name","threshold":0.82}]')
            page.get_by_label("冲突约束").fill('[]')
            page.get_by_label("批次 JSON").fill(json.dumps(records, ensure_ascii=False))
            page.get_by_role("button", name="保存方案").click()
            expect(page.get_by_role("status")).to_contain_text("方案已保存")
            page.get_by_role("button", name="导入批次").click()
            expect(page.get_by_role("status")).to_contain_text("批次已接收")
            page.get_by_role("button", name="运行记录 0 次").click()
            page.get_by_role("button", name="提交运行").click()
            expect(page.get_by_text("运行完成")).to_be_visible(timeout=10000)
            page.get_by_role("button", name="人工复核 1 待处理").click()
            page.once("dialog", lambda dialog: dialog.accept("Customer reviewed"))
            page.get_by_role("button", name="确认同一实体").click()
            expect(page.get_by_role("button", name="人工复核 0 待处理")).to_be_visible()
            page.get_by_role("button", name="快照与导出 尚未发布").click()
            page.get_by_role("button", name="发布快照").click()
            expect(page.get_by_text("已发布快照 v1")).to_be_visible()
            expect(page.get_by_text("customer · ACTIVE", exact=True)).to_be_visible()
            expect(page.get_by_text("测试顾客乙")).to_be_visible()
            customer_outputs = {}
            for kind in ("entities", "mappings", "decisions", "delta"):
                with page.expect_download() as event:
                    page.get_by_role("button", name=kind, exact=True).click()
                path = Path("/tmp") / f"l20-{kind}-{uuid.uuid4().hex}.json"
                event.value.save_as(path)
                customer_outputs[kind] = json.loads(path.read_text())
                path.unlink()
            customer_snapshot = customer_outputs["entities"]["snapshot_id"]
            customer_base = "http://127.0.0.1:8765/api/v1/projects/project-customer/unification"
            for kind, content in customer_outputs.items():
                direct = page.request.get(f"{customer_base}/snapshots/{customer_snapshot}/export?kind={kind}")
                assert direct.ok and direct.json() == content
            assert len(customer_outputs["entities"]["entities"]) == 1
            assert {item["source_key"][-1] for item in customer_outputs["mappings"]["mappings"]} == {"L02", "R02"}
            assert len({item["entity_id"] for item in customer_outputs["mappings"]["mappings"]}) == 1
            assert any(item["reason"] == "Customer reviewed" for item in customer_outputs["decisions"]["decisions"])
            assert customer_outputs["delta"]["delta"]["added_entities"]

            # Exercise the real authentication and workspace guard for the same snapshot.
            with maker() as db:
                db.add(Workspace(id="foreign-workspace", name="Foreign", slug="foreign"))
                db.add(User(id="foreign-user", email="foreign@example.test", display_name="Foreign", password_hash="unused"))
                db.flush()
                db.add(WorkspaceMember(user_id="foreign-user", workspace_id="foreign-workspace", role="editor"))
                db.commit()
            foreign_token = create_access_token("foreign-user", "foreign@example.test", "editor", "foreign-workspace")
            user_override = app.dependency_overrides.pop(get_current_user)
            access_override = app.dependency_overrides.pop(require_resource_access)
            try:
                for kind in customer_outputs:
                    denied = page.request.get(f"{customer_base}/snapshots/{customer_snapshot}/export?kind={kind}", headers={"Authorization": f"Bearer {foreign_token}"})
                    assert denied.status == 404
                    assert "测试顾客乙" not in denied.text() and foreign_token not in denied.text()
            finally:
                app.dependency_overrides[get_current_user] = user_override
                app.dependency_overrides[require_resource_access] = access_override

            # A separate project reproduces review and publication using HTTP alone.
            replay = "http://127.0.0.1:8765/api/v1/projects/project-replay/unification"
            saved_scheme = page.request.get(customer_base + "/schemes/customers").json()
            assert page.request.put(replay + "/schemes/customers", data=saved_scheme).ok
            assert page.request.post(replay + "/batches", data={"input_version": "replay", "records": records}).status == 201
            replay_run = page.request.post(replay + "/runs", headers={"Idempotency-Key": "l20-replay"}, data={"scheme_key": "customers", "input_version": "replay"})
            assert replay_run.status == 202
            run_id = replay_run.json()["run_id"]
            pairs = page.request.get(replay + "/reviews", params={"run_id": run_id}).json()["items"]
            assert len(pairs) == 1
            decision = {"run_id": run_id, "left_source_revision_id": pairs[0]["left_source_revision_id"], "right_source_revision_id": pairs[0]["right_source_revision_id"], "action": "MERGE", "reason": "Customer reviewed", "expected_revision": 1}
            assert page.request.post(replay + "/reviews", data=decision).status == 201
            result = page.request.post(replay + "/snapshots", data={"run_id": run_id, "expected_current_version": 0})
            assert result.status == 201
            replay_id = result.json()["snapshot_id"]
            replay_mappings = page.request.get(f"{replay}/snapshots/{replay_id}/export?kind=mappings").json()["mappings"]
            assert {item["source_key"][-1] for item in replay_mappings} == {"L02", "R02"}
            assert len({item["entity_id"] for item in replay_mappings}) == 1
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        unification_api.dispatch_unification_run = original_route_dispatch
        if trace_provider:
            trace_provider.force_flush()
        shutdown_tracing()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_workbench_real_browser_flow_publishes_reviewed_snapshot_and_exports()
