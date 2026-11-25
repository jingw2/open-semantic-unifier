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
        if self.behavior == "invalid":
            content = "not json"
        elif self.behavior == "clean":
            content = json.dumps({
                "intent": "clean",
                "confidence": 0.9,
                "explanation": "客户名单去重属于字段清洗。",
            })
        elif self.behavior == "plan":
            content = json.dumps({
                "intent": "clean",
                "confidence": 0.9,
                "explanation": "我来清洗字段。",
                "needs_clarification": False,
                "question": None,
                "options": [],
                "datasource_id": "ds-assistant",
                "params": {"target_col": "company_name", "amount_col": "tax_id"},
                "recipe_id": "normalize_company_name",
            })
        elif self.behavior == "invalid_plan":
            content = json.dumps({
                "intent": "clean",
                "confidence": 0.9,
                "explanation": "我来清洗字段。",
                "needs_clarification": False,
                "question": None,
                "options": [],
                "datasource_id": "nope",
                "params": {"target_col": "SECRET-COLUMN", "bogus": "x"},
                "recipe_id": "not_a_recipe",
            })
        elif self.behavior == "other_source_param":
            content = json.dumps({
                "intent": "clean",
                "confidence": 0.9,
                "explanation": "我来清洗字段。",
                "needs_clarification": False,
                "question": None,
                "options": [],
                "datasource_id": "ds-assistant",
                "params": {"target_col": "city"},
                "recipe_id": None,
            })
        elif self.behavior == "answer":
            content = json.dumps({
                "intent": "answer",
                "confidence": 0.95,
                "explanation": "这是一个语义统一平台。",
                "needs_clarification": False,
                "question": None,
                "options": [],
                "datasource_id": "ds-assistant",
                "params": {"target_col": "company_name"},
                "recipe_id": "normalize_company_name",
            })
        elif self.behavior == "clarify":
            content = json.dumps({
                "intent": "unknown",
                "confidence": 0.6,
                "explanation": "你想做什么？",
                "needs_clarification": True,
                "question": "请选择一个任务。",
                "options": [
                    {"label": "清洗", "message": "清洗供应商名单"},
                    {"label": "匹配", "message": "跨系统匹配客户"},
                    {"label": "指标", "message": "统一 GMV 口径"},
                    {"label": "契约", "message": "生成数据契约"},
                ],
                "datasource_id": None,
                "params": None,
                "recipe_id": None,
            })
        else:
            content = json.dumps({
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
        db.add(DataSource(
            id="ds-other", project_id="project-one", name="cities.csv", source_type="file",
            profile_json={"total_rows": 3, "table_count": 1, "tables": [{"columns": ["city"]}]},
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
    assert "数据源页面" in prompt
    assert "SECRET-RECORD-VALUE" not in prompt


def test_assistant_corrects_entity_dedup_classified_as_clean(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "clean"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我把客户名单去重", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["intent"] == "match"
    assert payload["suggested_task"] == "match"
    assert "实体匹配" in payload["explanation"]
    prompt = AssistantHandler.requests[0]["messages"][0]["content"]
    assert "去重" in prompt
    assert "match, never clean" in prompt


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
    assert payload["needs_clarification"] is False
    assert payload["options"] == []
    assert payload["datasource_id"] is None
    assert payload["params"] is None
    assert payload["recipe_id"] is None


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


def test_assistant_keeps_valid_source_params_and_recipe(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "plan"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我清洗字段", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["intent"] == "clean"
    assert payload["datasource_id"] == "ds-assistant"
    assert payload["params"] == {"target_col": "company_name", "amount_col": "tax_id"}
    assert payload["recipe_id"] == "normalize_company_name"


def test_assistant_drops_params_from_another_source(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "other_source_param"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我清洗字段", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["datasource_id"] == "ds-assistant"
    assert payload["params"] is None


def test_assistant_drops_invalid_source_params_and_recipe(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "invalid_plan"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我清洗字段", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["intent"] == "clean"
    assert payload["datasource_id"] is None
    assert payload["params"] is None
    assert payload["recipe_id"] is None


def test_assistant_answer_shape(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "answer"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "这个产品是做什么的？", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["intent"] == "answer"
    assert payload["suggested_task"] is None
    assert payload["explanation"] == "这是一个语义统一平台。"
    assert payload["needs_clarification"] is False
    assert payload["datasource_id"] is None
    assert payload["params"] is None
    assert payload["recipe_id"] is None


def test_assistant_clarification_shape(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    AssistantHandler.behavior = "clarify"
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "帮我做点什么", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "model"
    assert payload["intent"] == "unknown"
    assert payload["suggested_task"] is None
    assert payload["needs_clarification"] is True
    assert payload["question"] == "请选择一个任务。"
    assert len(payload["options"]) == 3
    assert all("label" in item and "message" in item for item in payload["options"])


def test_assistant_history_appears_in_prompt(public_api, assistant_server):
    client, _maker, _identity = public_api
    _seed_providers(public_api, assistant_server.server_port)
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={
            "message": "帮我把客户名单去重",
            "page": "data",
            "history": [
                {"role": "user", "content": "之前问过这个问题"},
                {"role": "assistant", "content": "上次回答过"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    prompt = AssistantHandler.requests[0]["messages"][0]["content"]
    assert "之前问过这个问题" in prompt
    assert "上次回答过" in prompt


def test_assistant_unknown_fallback_adds_clarification(public_api):
    client, _maker, _identity = public_api
    response = client.post(
        "/api/v1/projects/project-one/assistant/plan",
        json={"message": "今天天气怎么样", "page": "data"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "rules"
    assert payload["intent"] == "unknown"
    assert payload["suggested_task"] is None
    assert payload["needs_clarification"] is True
    assert len(payload["options"]) == 3


def test_deterministic_intent_keywords():
    assert deterministic_intent("清洗供应商名单")[0] == "clean"
    assert deterministic_intent("帮我把客户名单去重")[0] == "match"
    assert deterministic_intent("统一 GMV 口径")[0] == "metrics"
    assert deterministic_intent("生成 LinkML 契约")[0] == "contract"
    assert deterministic_intent("看看待复核项")[0] == "review"
    assert deterministic_intent("今天天气怎么样")[0] == "unknown"
