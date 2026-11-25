"""Real browser workflow for decision-model providers and review evidence.

The API and the configured decision provider run against an isolated SQLite
database and a controlled ThreadingHTTPServer on 127.0.0.1. No external
network is touched. Vite is expected on 127.0.0.1:5173 and FastAPI on
127.0.0.1:8765, matching the CI frontend job.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
import uvicorn

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.main import app
from app.models.entities import Project, Workspace
from app.services.unification.releases import execute_unification_run
from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

CHOICE_RESPONSE = {
    "model": "controlled-flash",
    "answers": {"entity_match": {
        "type": "choice",
        "choice": "SAME",
        "confidence": 0.9,
        "probabilities": {"SAME": 0.8, "DIFFERENT": 0.15, "UNKNOWN": 0.05},
    }},
    "usage": {"input_tokens": 120, "output_tokens": 3},
}


class ControlledChoiceHandler(BaseHTTPRequestHandler):
    """Controlled choice-protocol provider; records every request body."""

    bodies: list[dict] = []
    calls = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        type(self).calls += 1
        body = json.dumps(CHOICE_RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_decision_models_workbench_provider_test_and_review_evidence():
    database_path = Path("/tmp") / f"open-semantic-unifier-decision-models-e2e-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        db.add(Workspace(id="workspace-live", name="Live Decision Models", slug="live-decision-models"))
        db.flush()
        db.add(Project(id="project-live", workspace_id="workspace-live", name="Supplier master", slug="supplier-master", description=""))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "browser-decision", "email": "decision@example.test", "display_name": "Decision Reviewer",
        "workspace_id": "workspace-live", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None
    configure_tracing(TraceConfig(enabled=False))

    def dispatch_inline(run_id: str):
        with maker() as db:
            execute_unification_run(db, run_id)

    import app.api.v1.unification as unification_api
    original_route_dispatch = unification_api.dispatch_unification_run
    unification_api.dispatch_unification_run = dispatch_inline

    ControlledChoiceHandler.bodies = []
    ControlledChoiceHandler.calls = 0
    choice_server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledChoiceHandler)
    choice_port = choice_server.server_address[1]
    choice_thread = Thread(target=choice_server.serve_forever, daemon=True)
    choice_thread.start()

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
                id: 'browser-decision', email: 'decision@example.test', display_name: 'Decision Reviewer',
                role: 'editor', workspace_id: 'workspace-live'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")
            expect(page.get_by_role("alert")).to_have_count(0)

            # 1. Create a perplexity_decisions provider pointing at the controlled server.
            page.get_by_role("button", name="模型 Provider").click()
            page.get_by_role("button", name="Perplexity Decisions").click()
            page.get_by_label("Base URL").fill(f"http://127.0.0.1:{choice_port}")
            page.get_by_label("API Key").fill("test-secret-key")
            page.get_by_label("发送字段").fill("name")
            page.get_by_role("button", name="保存并加密入库").click()
            expect(page.get_by_text("已配置模型 Provider (1)")).to_be_visible()

            provider_payload = page.request.get("http://127.0.0.1:8765/api/v1/providers").json()
            decision_providers = [item for item in provider_payload["configured"] if item["provider_kind"] == "perplexity_decisions"]
            assert len(decision_providers) == 1, provider_payload
            provider_id = decision_providers[0]["id"]
            assert decision_providers[0]["provider_kind"] == "perplexity_decisions"
            assert decision_providers[0]["send_fields"] == ["name"]

            # Edit keeps the stored key when the API key field is blank.
            page.get_by_role("button", name="编辑").click()
            page.get_by_label("最大重试次数").fill("1")
            page.get_by_role("button", name="更新 Provider").click()
            expect(page.get_by_text("已配置模型 Provider (1)")).to_be_visible()
            updated = [item for item in page.request.get("http://127.0.0.1:8765/api/v1/providers").json()["configured"] if item["id"] == provider_id]
            assert updated and updated[0]["max_retries"] == 1
            assert updated[0]["send_fields"] == ["name"]
            assert updated[0]["api_key_masked"]

            # 2. Test button surfaces the typed suggestion, never a bare success.
            page.get_by_role("button", name="测试连通性").click()
            expect(page.get_by_text("perplexity_decisions → SAME")).to_be_visible(timeout=10000)
            expect(page.get_by_text("置信度 0.9")).to_be_visible()
            expect(page.get_by_text("模型 controlled-flash")).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)

            # 3. Configure a scheme with that provider and run a two-record customer pair.
            page.get_by_role("button", name="实体统一工作台").click()
            expect(page.get_by_role("heading", name="实体统一工作台")).to_be_visible()

            page.get_by_label("方案名称").fill("customers")
            page.get_by_label("实体类型").select_option("customer")
            page.get_by_label("身份粒度").fill("person")
            page.get_by_label("强标识字段（逗号分隔）").fill("")
            page.get_by_label("标准字段 → 来源字段（JSON）").fill('{"name":"name","tax_id":"tax_id"}')
            page.get_by_text("高级匹配设置：候选规则与冲突约束").click()
            page.get_by_label("候选召回规则").fill('[{"kind":"exact","fields":["tax_id"]},{"kind":"name_similarity","field":"name","threshold":0.82}]')
            page.get_by_label("冲突约束").fill('[]')
            page.get_by_label("启用决策模型建议").check()
            page.get_by_label("决策 Provider").select_option(value=provider_id)
            page.get_by_role("button", name="保存方案").click()
            expect(page.get_by_role("status")).to_contain_text("方案已保存")

            records = [
                {"source_system": "erp", "dataset": "customers", "source_record_id": "C-1", "attributes": {"name": "Contoso Ltd", "tax_id": "TAX-SECRET-9", "phone": "SECRET-1"}},
                {"source_system": "crm", "dataset": "customers", "source_record_id": "C-2", "attributes": {"name": "Contoso, Ltd.", "tax_id": "TAX-SECRET-9", "phone": "SECRET-2"}},
            ]
            page.get_by_label("批次 JSON").fill(json.dumps(records, ensure_ascii=False))
            page.get_by_role("button", name="导入批次").click()
            expect(page.get_by_role("status")).to_contain_text("批次已接收")

            page.get_by_role("button", name="运行记录 0 次").click()
            page.get_by_role("button", name="提交运行").click()
            expect(page.get_by_text("运行完成")).to_be_visible(timeout=10000)

            page.get_by_role("button", name="人工复核 1 待处理").click()
            expect(page.get_by_text("模型建议 · SAME")).to_be_visible()
            expect(page.get_by_text("置信度 90%")).to_be_visible()
            expect(page.get_by_text("模型 controlled-flash")).to_be_visible()
            expect(page.get_by_text("类型 perplexity_decisions")).to_be_visible()
            expect(page.get_by_text("Provider perplexity")).to_be_visible()
            expect(page.get_by_text("80%", exact=True)).to_be_visible()
            expect(page.get_by_text("15%", exact=True)).to_be_visible()
            expect(page.get_by_text("5%", exact=True)).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)

            # The provider received only `name` in both records and in evidence values.
            assert ControlledChoiceHandler.calls >= 1
            bodies = ControlledChoiceHandler.bodies
            assert bodies
            for body in bodies:
                state = body["state"]
                assert state["left"].keys() == {"name"}, body
                assert state["right"].keys() == {"name"}, body
                for item in state["evidence"]:
                    if item.get("field") == "tax_id":
                        assert item.keys().isdisjoint({"value", "left", "right"}), item
            serialized = json.dumps(bodies)
            assert "TAX-SECRET-9" not in serialized
            assert "SECRET-1" not in serialized and "SECRET-2" not in serialized
            browser.close()
    finally:
        choice_server.shutdown()
        choice_thread.join(timeout=3)
        choice_server.server_close()
        server.should_exit = True
        server_thread.join(timeout=3)
        app.dependency_overrides.clear()
        unification_api.dispatch_unification_run = original_route_dispatch
        shutdown_tracing()
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_decision_models_workbench_provider_test_and_review_evidence()
