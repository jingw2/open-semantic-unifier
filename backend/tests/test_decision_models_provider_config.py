"""Provider configuration fields, decision presets, and validation."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_unification_public_api import public_api


def test_builtin_presets_carry_provider_kind(public_api):
    client, _, _ = public_api
    presets = client.get("/api/v1/providers").json()["builtin_presets"]
    by_key = {item["provider_key"]: item for item in presets}
    assert len(presets) == 9
    for key in ("offline", "deepseek", "qwen", "openai", "zhipu", "moonshot", "ollama"):
        assert by_key[key]["provider_kind"] == "openai_compatible"
    assert by_key["perplexity"] == {
        "provider_key": "perplexity", "name": "Perplexity Decisions",
        "base_url": "https://api.perplexity.ai", "default_model": "pplx-decider-flash-latest",
        "provider_kind": "perplexity_decisions",
    }
    assert by_key["typesafe"] == {
        "provider_key": "typesafe", "name": "TypeSafe Jev",
        "base_url": "https://api.typesafe.ai", "default_model": "jev-latest",
        "provider_kind": "typesafe_jev",
    }


def test_create_and_list_roundtrip_new_fields(public_api):
    client, _, _ = public_api
    response = client.post("/api/v1/providers", json={
        "provider_key": "roundtrip", "name": "Roundtrip provider",
        "base_url": "https://rt.example/v1", "default_model": "rt-model",
        "api_key": "secret-key-rt",
        "provider_kind": "openai_compatible", "send_fields": ["name", "tax_id"],
        "timeout_seconds": 12.5, "max_retries": 2, "prompt_version": "pair-v2",
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider_kind"] == "openai_compatible"
    assert payload["send_fields"] == ["name", "tax_id"]
    assert payload["timeout_seconds"] == 12.5
    assert payload["max_retries"] == 2
    assert payload["prompt_version"] == "pair-v2"
    assert payload["api_key_masked"] and payload["api_key_masked"] != "secret-key-rt"
    listed = client.get("/api/v1/providers").json()["configured"]
    saved = next(item for item in listed if item["id"] == payload["id"])
    assert saved["provider_kind"] == "openai_compatible"
    assert saved["send_fields"] == ["name", "tax_id"]
    assert saved["timeout_seconds"] == 12.5
    assert saved["max_retries"] == 2
    assert saved["prompt_version"] == "pair-v2"
    assert "secret-key-rt" not in client.get("/api/v1/providers").text


def test_default_provider_configuration_values(public_api):
    client, _, _ = public_api
    response = client.post("/api/v1/providers", json={"provider_key": "defaults", "name": "Defaults"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider_kind"] == "openai_compatible"
    assert payload["send_fields"] == []
    assert payload["timeout_seconds"] == 30.0
    assert payload["max_retries"] == 0
    assert payload["prompt_version"] == "entity-pair-choice-v1"


def test_decision_kinds_fill_preset_base_url_and_model(public_api):
    client, _, _ = public_api
    response = client.post("/api/v1/providers", json={
        "provider_key": "perplexity", "name": "Perplexity", "provider_kind": "perplexity_decisions", "api_key": "pk",
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["base_url"] == "https://api.perplexity.ai"
    assert payload["default_model"] == "pplx-decider-flash-latest"
    response = client.post("/api/v1/providers", json={
        "provider_key": "typesafe", "name": "Jev", "provider_kind": "typesafe_jev",
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["base_url"] == "https://api.typesafe.ai"
    assert payload["default_model"] == "jev-latest"


def test_decision_kinds_keep_explicit_endpoint_and_model(public_api):
    client, _, _ = public_api
    response = client.post("/api/v1/providers", json={
        "provider_key": "perplexity", "name": "Perplexity self-hosted", "provider_kind": "perplexity_decisions",
        "base_url": "https://decisions.internal.example", "default_model": "decider-v2", "api_key": "pk",
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["base_url"] == "https://decisions.internal.example"
    assert payload["default_model"] == "decider-v2"


@pytest.mark.parametrize("payload", [
    {"provider_key": "x", "name": "X", "provider_kind": "bogus"},
    {"provider_key": "x", "name": "X", "send_fields": ["name", ""]},
    {"provider_key": "x", "name": "X", "send_fields": ["name", 3]},
    {"provider_key": "x", "name": "X", "timeout_seconds": 0},
    {"provider_key": "x", "name": "X", "timeout_seconds": -2.5},
    {"provider_key": "x", "name": "X", "max_retries": -1},
    {"provider_key": "x", "name": "X", "prompt_version": ""},
    {"provider_key": "x", "name": "X", "prompt_version": "   "},
])
def test_invalid_provider_configuration_is_rejected(public_api, payload):
    client, _, _ = public_api
    response = client.post("/api/v1/providers", json=payload)
    assert response.status_code == 422, response.text


def test_openai_compatible_test_endpoint_stays_unchanged(public_api):
    client, _, _ = public_api
    provider = client.post("/api/v1/providers", json={
        "provider_key": "controlled", "name": "Controlled",
        "base_url": "https://controlled.example/v1", "default_model": "controlled",
    }).json()
    result = client.post(f"/api/v1/providers/{provider['id']}/test").json()
    assert result["status"] == "error"
    assert "credentials" in result["message"]
