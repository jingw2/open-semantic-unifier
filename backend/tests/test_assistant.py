"""Assistant planning endpoint: model classification with a deterministic fallback."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_unification_public_api import public_api  # noqa: F401

from app.core.security import encrypt_secret
from app.models.entities import DataSource, LLMProvider
from app.services.assistant import deterministic_intent


class AssistantHandler(BaseHTTPRequestHandler):
    behavior = "valid"
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)
        content = "not json" if self.behavior == "invalid" else json.dumps({
            "intent": "match",
            "confidence": 0.92,
            "explanation": "客户名单去重属于跨库匹配。",
        })
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def assistant_server():
    AssistantHandler.behavior = "valid"
    AssistantHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), AssistantHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _seed_providers(public_api, port: int) -> None:
    _client, maker, _identity = public_api
    with maker() as db:
        db.add(LLMProvider(
            id="prov-offline", workspace_id="ws-one", provider_key="offline", name="Offline",
            base_url="", default_model="offline", encrypted_api_key="",
            provider_kind="openai_compatible", is_default=False,
        ))
        db.flush()
        db.add(LLMProvider(
            id="prov-openai", workspace_id="ws-one", provider_key="controlled", name="Controlled",
            base_url=f"http://127.0.0.1:{port}/v1", default_model="controlled-model",
            encrypted_api_key=encrypt_secret("test-key"), provider_kind="openai_compatible",
            is_default=False,
        ))
        db.add(LLMProvider(
            id="prov-jev", workspace_id="ws-one", provider_key="typesafe", name="Jev",
            base_url="http://127.0.0.1:1", default_model="jev",
            encrypted_api_key=encrypt_secret("jev-key"), provider_kind="typesafe_jev",
            is_default=True,
        ))
        db.add(DataSource(
            id="ds-assistant", project_id="project-one", name="customers.csv", source_type="file",
            profile_json={
                "total_rows": 10, "table_count": 1,
                "tables": [{"columns": ["company_name", "tax_id"], "preview": [{"name": "SECRET-RECORD-VALUE"}]}],
            },
        ))
        db.commit()


def test_assistant_uses_chat_provider_by_kind_and_hides_preview_values(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我把客户名单去重", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["intent"] == "match"
    assert payload["suggested_task"] == "match"
    prompt = AssistantHandler.requests[0]["messages"][0]["content"]
    assert "company_name" in prompt
    assert "SECRET-RECORD-VALUE" not in prompt


def test_assistant_falls_back_to_rules_without_a_usable_provider(public_api):
    client, maker, _identity = public_api
    with maker() as db:
        db.add(LLMProvider(
            id="prov-jev", workspace_id="ws-one", provider_key="typesafe", name="Jev",
            base_url="http://127.0.0.1:1", default_model="jev",
            encrypted_api_key=encrypt_secret("jev-key"), provider_kind="typesafe_jev",
            is_default=True,
        ))
        db.commit()
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我把客户名单去重", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "rules"
    assert payload["intent"] == "match"


def test_assistant_falls_back_when_the_model_returns_invalid_json(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "invalid"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我把客户名单去重", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "rules"
    assert payload["intent"] == "match"


def test_deterministic_intent_keywords():
    assert deterministic_intent("清洗供应商名单")[0] == "clean"
    assert deterministic_intent("帮我把客户名单去重")[0] == "match"
    assert deterministic_intent("统一 GMV 口径")[0] == "metrics"
    assert deterministic_intent("生成 LinkML 契约")[0] == "contract"
    assert deterministic_intent("看看待复核项")[0] == "review"
    assert deterministic_intent("今天天气怎么样")[0] == "unknown"
