"""Entity review explains why, compares fields, and keeps raw evidence collapsed."""
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
from ui_helpers import open_route

from app.api.deps import get_current_user, require_resource_access
from app.core.db import Base, get_db
from app.core.security import encrypt_secret
from app.main import app
from app.models.entities import LLMProvider, Project, ReviewItem, Workspace
from app.services.unification.releases import execute_unification_run


class ChoiceHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).calls += 1
        body = json.dumps({
            "model": "controlled-flash",
            "answers": {"entity_match": {
                "type": "choice", "choice": "SAME", "confidence": 0.9,
                "probabilities": {"SAME": 0.8, "DIFFERENT": 0.15, "UNKNOWN": 0.05},
            }},
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_review_readability_in_entity_and_l2_queues():
    database_path = Path("/tmp") / f"open-semantic-unifier-review-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    choice_server = ThreadingHTTPServer(("127.0.0.1", 0), ChoiceHandler)
    choice_thread = Thread(target=choice_server.serve_forever, daemon=True)
    choice_thread.start()
    choice_port = choice_server.server_address[1]
    with maker() as db:
        db.add(Workspace(id="workspace-review", name="Review", slug="review"))
        db.flush()
        db.add(Project(id="project-review", workspace_id="workspace-review", name="Review project", slug="review-project", description=""))
        db.add(LLMProvider(
            id="provider-review", workspace_id="workspace-review", provider_key="typesafe", name="Jev",
            base_url=f"http://127.0.0.1:{choice_port}", default_model="controlled-flash",
            encrypted_api_key=encrypt_secret("test-key"), provider_kind="typesafe_jev",
            send_fields_json=["name", "tax_id"], is_default=True,
        ))
        db.add(ReviewItem(
            id="review-l2", project_id="project-review", job_id="job-l2", stage="L2",
            case_type="互联网简称/花名", left_text="深圳市腾讯计算机系统有限公司", right_text="鹅厂（Tencent Holdings）",
            score=0.95, llm_suggestion={"is_same_entity": True, "confidence": 0.95, "reason": "名称高度相似，可能是同一实体。"},
            decision="PENDING",
        ))
        db.commit()

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "review-user", "email": "review@example.test", "display_name": "Review User",
        "workspace_id": "workspace-review", "role": "editor",
    }
    app.dependency_overrides[require_resource_access] = lambda: None

    import app.api.v1.unification as unification_api
    original_dispatch = unification_api.dispatch_unification_run

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

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script("""
              localStorage.setItem('su_access_token', 'browser-test-token');
              localStorage.setItem('su_user_info', JSON.stringify({
                id: 'review-user', email: 'review@example.test', display_name: 'Review User',
                role: 'editor', workspace_id: 'workspace-review'
              }));
            """)
            page.goto("http://127.0.0.1:5173")
            page.wait_for_load_state("networkidle")

            demo = page.request.post("http://127.0.0.1:8765/api/v1/projects/project-review/unification/demo")
            assert demo.ok, demo.text()
            run_id = demo.json()["run_id"]
            for _ in range(100):
                detail = page.request.get(f"http://127.0.0.1:8765/api/v1/projects/project-review/unification/runs/{run_id}").json()
                if detail.get("status") in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(0.1)
            assert detail["status"] == "SUCCEEDED", detail

            open_route(page, f"#/tasks/run/{run_id}")
            page.get_by_role("button", name="人工复核 2 待处理").click()
            expect(page.get_by_text("这两条记录是不是同一实体？").first).to_be_visible()
            expect(page.get_by_text("缺少可核对的强标识", exact=False).first).to_be_visible()
            expect(page.get_by_text("name", exact=True).first).to_be_visible()
            expect(page.get_by_text("tax_id", exact=True).first).to_be_visible()
            expect(page.get_by_text("模型建议 · SAME", exact=False).first).to_be_visible()
            expect(page.get_by_text("倾向同一实体", exact=False).first).to_be_visible()
            expect(page.get_by_text("查看原始证据", exact=True).first).to_be_visible()
            page.get_by_text("查看原始证据", exact=True).first.click()
            expect(page.get_by_text("STRONG_IDENTIFIER_MISSING", exact=True).first).to_be_visible()
            expect(page.get_by_text("系统已自动保持分离 (1)", exact=False)).to_be_visible()
            page.get_by_text("系统已自动保持分离 (1)", exact=False).click()
            expect(page.get_by_text("强标识不一致", exact=False).first).to_be_visible()

            open_route(page, "#/reviews")
            expect(page.get_by_text("模型理由：名称高度相似，可能是同一实体。", exact=False)).to_be_visible()
            expect(page.get_by_text("推入原因：L2 灰色边界样本", exact=False)).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        choice_server.shutdown()
        choice_thread.join(timeout=3)
        choice_server.server_close()
        app.dependency_overrides.clear()
        unification_api.dispatch_unification_run = original_dispatch
        engine.dispose()
        database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_review_readability_in_entity_and_l2_queues()
