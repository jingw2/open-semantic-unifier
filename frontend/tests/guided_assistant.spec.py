"""Guided assistant: conversation, model-sourced plans, and deterministic fallback."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
from ui_helpers import open_route  # noqa: F401

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.core.security import encrypt_secret
from app.main import app
from app.models.entities import DataSource, LLMProvider, Project, Workspace
from app.services.unification.releases import execute_unification_run

APP = "http://127.0.0.1:5173"


def _make_db():
    engine = create_engine(
        f"sqlite:///{Path('/tmp') / f'open-semantic-unifier-assistant-{uuid.uuid4().hex}.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def _isolated_backend(app, maker):
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
        yield
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        app.dependency_overrides.clear()
        worker_tasks.SessionLocal = original_session
        unification_api.dispatch_unification_run = original_dispatch


def _seed_workspace(maker):
    with maker() as db:
        db.add(Workspace(id="workspace-assistant", name="Assistant", slug="assistant"))
        db.flush()
        db.add(Project(id="project-assistant", workspace_id="workspace-assistant", name="Assistant project", slug="assistant-project", description=""))
        db.commit()


def _login(page):
    page.add_init_script("""
      localStorage.setItem('su_access_token', 'browser-test-token');
      localStorage.setItem('su_user_info', JSON.stringify({
        id: 'assistant-user', email: 'assistant@example.test', display_name: 'Assistant User',
        role: 'editor', workspace_id: 'workspace-assistant'
      }));
    """)


class FakeProviderHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)
        messages = payload.get("messages") or []
        content = messages[-1].get("content", "") if messages else ""
        if "解释当前页面" in content:
            plan = {
                "intent": "answer", "confidence": 0.96, "explanation": "模型说明：这里是当前页面。",
                "needs_clarification": False, "question": None, "options": [],
                "datasource_id": None, "params": None, "recipe_id": None,
            }
        elif "帮我清洗" in content:
            plan = {
                "intent": "clean", "confidence": 0.94, "explanation": "我会按标准配方清洗供应商名单。",
                "needs_clarification": False, "question": None, "options": [],
                "datasource_id": "ds-model",
                "params": {"target_col": "supplier_raw_name", "amount_col": "order_amount_cny"},
                "recipe_id": "normalize_company_name",
            }
        else:
            plan = {
                "intent": "answer", "confidence": 0.9, "explanation": "模型回答：这是一个语义统一平台。",
                "needs_clarification": False, "question": None, "options": [],
                "datasource_id": None, "params": None, "recipe_id": None,
            }
        body = json.dumps({"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _start_fake_provider():
    FakeProviderHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_guided_assistant_runs_l1_and_entity_demo():
    engine, maker = _make_db()
    _seed_workspace(maker)
    try:
        with _isolated_backend(app, maker):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                _login(page)
                page.goto(APP)
                page.wait_for_load_state("networkidle")
                expect(page.get_by_text("你想完成什么？")).to_be_visible(timeout=15000)

                page.get_by_role("button", name="清洗字段", exact=True).click()
                expect(page.get_by_text("规则清洗 + W3C 契约", exact=False).first).to_be_visible()
                expect(page.get_by_label("清洗目标字段")).to_be_visible()

                route_pattern = "**/api/v1/projects/*/datasources/demo/L1"
                page.route(route_pattern, lambda route: route.abort())
                page.get_by_role("button", name="开始", exact=True).click()
                expect(page.get_by_role("button", name="去手动处理", exact=True)).to_be_visible(timeout=15000)
                page.unroute(route_pattern)

                page.get_by_role("button", name="清洗字段", exact=True).click()
                page.get_by_role("button", name="开始", exact=True).click()
                expect(page.get_by_text("已完成", exact=False).first).to_be_visible(timeout=30000)

                page.get_by_role("button", name="查看结果", exact=True).click()
                expect(page.get_by_text("总行数", exact=False).first).to_be_visible(timeout=15000)

                page.get_by_role("button", name="解释当前页面", exact=True).click()
                expect(page.get_by_text("这是任务详情", exact=False)).to_be_visible()

                page.get_by_placeholder("也可以直接说，例如：帮我把客户名单去重").fill("帮我把客户名单去重")
                page.get_by_placeholder("也可以直接说，例如：帮我把客户名单去重").press("Enter")
                expect(page.get_by_text("跨系统匹配字段", exact=True).first).to_be_visible(timeout=15000)
                expect(page.get_by_text("确认匹配字段（可多选）", exact=False)).to_be_visible()

                page.get_by_role("button", name="实体匹配字段", exact=True).click()
                expect(page.get_by_text("确认实体匹配字段（可多选）", exact=False)).to_be_visible()
                page.get_by_role("button", name="开始", exact=True).click()
                expect(page.get_by_text("已完成", exact=False).first).to_be_visible(timeout=30000)
                page.get_by_role("button", name="查看结果", exact=True).click()
                expect(page.get_by_role("heading", name="实体统一工作台", exact=True)).to_be_visible(timeout=15000)
                browser.close()
    finally:
        engine.dispose()


def test_guided_assistant_model_plan_and_answer():
    engine, maker = _make_db()
    _seed_workspace(maker)
    provider_server, provider_thread = _start_fake_provider()
    try:
        with maker() as db:
            db.add(LLMProvider(
                id="prov-fake", workspace_id="workspace-assistant", provider_key="controlled", name="Controlled",
                base_url=f"http://127.0.0.1:{provider_server.server_port}/v1", default_model="controlled-model",
                encrypted_api_key=encrypt_secret("test-key"), provider_kind="openai_compatible", is_default=True,
            ))
            db.add(DataSource(
                id="ds-model", project_id="project-assistant", name="suppliers.csv", source_type="file",
                profile_json={
                    "total_rows": 120, "table_count": 1,
                    "tables": [{"columns": ["row_id", "supplier_raw_name", "source_system", "order_amount_cny", "ground_truth_id"]}],
                },
                routing_json={"recommended_stage": "L1"},
            ))
            db.add(DataSource(
                id="ds-metrics", project_id="project-assistant", name="demo_metrics.yaml", source_type="file",
                profile_json={"has_metric_yaml": True, "total_rows": 0, "table_count": 0, "tables": [{"columns": ["YAML"]}]},
                routing_json={"recommended_stage": "L4"},
            ))
            db.commit()
        with _isolated_backend(app, maker):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                _login(page)
                page.goto(APP)
                page.wait_for_load_state("networkidle")
                expect(page.get_by_text("你想完成什么？")).to_be_visible(timeout=15000)
                assistant = page.locator("aside").last
                box = assistant.bounding_box()
                assert box and abs(box["height"] - page.viewport_size["height"]) <= 1, box

                page.get_by_placeholder("也可以直接说，例如：帮我把客户名单去重").fill("帮我清洗供应商字段")
                page.get_by_placeholder("也可以直接说，例如：帮我把客户名单去重").press("Enter")
                expect(page.get_by_text("我会按标准配方清洗供应商名单。", exact=False)).to_be_visible(timeout=15000)
                expect(page.get_by_text("数据源：suppliers.csv", exact=False)).to_be_visible()
                expect(page.get_by_text("目标列：supplier_raw_name", exact=False)).to_be_visible()
                expect(page.get_by_text("模型理解", exact=False)).to_be_visible()
                expect(page.get_by_role("button", name="开始", exact=True)).to_be_visible()

                page.get_by_role("button", name="解释当前页面", exact=True).click()
                expect(page.get_by_text("模型说明：这里是当前页面。", exact=False)).to_be_visible(timeout=15000)

                page.get_by_role("button", name="统一指标口径", exact=True).click()
                page.get_by_role("button", name="开始", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("不能计算 L4 指标")
                page.get_by_label("选择数据源").select_option(label="demo_metrics.yaml")
                page.get_by_role("button", name="开始", exact=True).click()
                expect(page.get_by_text("已完成", exact=False).first).to_be_visible(timeout=60000)
                browser.close()
    finally:
        provider_server.shutdown()
        provider_thread.join(timeout=2)
        provider_server.server_close()
        engine.dispose()


if __name__ == "__main__":
    test_guided_assistant_runs_l1_and_entity_demo()
    test_guided_assistant_model_plan_and_answer()
