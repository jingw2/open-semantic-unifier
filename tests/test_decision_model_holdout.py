"""Holdout integrity, leakage controls, and evaluator scoring."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_decision_models as holdout_eval
from app.services.decision_models.contracts import DecisionSuggestion, DecisionVerdict


class FakeAdapter:
    provider_kind = "perplexity_decisions"

    def __init__(self, verdict: str):
        self.verdict = DecisionVerdict(verdict)
        self.requests = []

    def invoke(self, request, config):
        self.requests.append(request)
        return DecisionSuggestion(
            verdict=self.verdict,
            confidence=0.9,
            distribution=None,
            explanation=None,
            provider_kind=self.provider_kind,
            provider_key=config.provider_key,
            model_version="fake-model",
            prompt_version=request.prompt_version,
            latency_ms=1.5,
            usage=None,
            cost_usd=None,
            trace_id=None,
        )


def _evaluate(verdict: str):
    domains = holdout_eval.load_holdout()
    adapter = FakeAdapter(verdict)
    report = holdout_eval.evaluate(
        domains,
        provider_kind="perplexity_decisions",
        model="fake-model",
        base_url="https://controlled.invalid",
        api_key="controlled-key",
        send_fields=holdout_eval.DEFAULT_SEND_FIELDS,
        adapter=adapter,
    )
    return report, adapter, domains


def test_holdout_is_frozen_and_disjoint_from_golden():
    manifest = holdout_eval.validate_holdout()
    holdout_eval.assert_no_golden_overlap()
    assert manifest["version"] == "1.0.0"
    assert manifest["record_count"] == 60
    assert manifest["pair_case_count"] == 30
    assert manifest["domains"] == ["vendor", "contact", "catalog_item"]


def test_holdout_inputs_do_not_carry_labels():
    root = holdout_eval.HOLDOUT
    for domain in holdout_eval.read(root / "manifest.json")["domains"]:
        for name in ("records.json", "scheme.json"):
            holdout_eval._assert_no_labels(holdout_eval.read(root / domain / name), f"{domain}/{name}")


def test_controlled_adapter_never_receives_truth_and_scores_every_domain():
    report, adapter, domains = _evaluate("SAME")
    assert report["status"] == "COMPLETE"
    assert set(report["domains"]) == set(domains)
    assert adapter.requests
    for request in adapter.requests:
        assert "truth" not in request.left and "truth" not in request.right
        assert "expected_action" not in request.left and "expected_action" not in request.right
    aggregate = report["aggregate"]
    assert aggregate["candidate_recall"] == pytest.approx(29 / 30, abs=1e-4)
    assert aggregate["same_correct"] == aggregate["same_pairs"] == 18
    assert aggregate["false_same"] == 9
    assert aggregate["false_same_rate"] == 1.0
    assert aggregate["different_recall"] == 0.0
    assert aggregate["judged_pairs"] == 27
    assert aggregate["suggestion_accuracy"] == pytest.approx(18 / 27, abs=1e-4)
    assert report["domains"]["vendor"]["candidate_recall"] == 0.9
    assert report["domains"]["contact"]["candidate_recall"] == 1.0
    assert report["domains"]["catalog_item"]["candidate_recall"] == 1.0


def test_unknown_verdicts_are_visible_per_domain():
    report, _adapter, _domains = _evaluate("UNKNOWN")
    assert report["aggregate"]["false_same"] == 0
    assert report["aggregate"]["unknown"] == report["aggregate"]["judged_pairs"] == 27
    for domain in report["domains"].values():
        assert domain["suggestion_accuracy"] == 0.0
        assert domain["verdict_counts"] == {"UNKNOWN": domain["judged_pairs"] + domain["unresolved_pairs"]}


def test_missing_key_is_blocked_without_fake_scores():
    domains = holdout_eval.load_holdout()
    report = holdout_eval.evaluate(
        domains,
        provider_kind="typesafe_jev",
        model="jev-latest",
        base_url="https://api.typesafe.ai",
        api_key=None,
        send_fields=("name",),
    )
    assert report["status"] == "BLOCKED"
    assert "no API key" in report["reason"]
    assert "domains" not in report


def test_saved_provider_config_loads_without_exposing_the_key(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.core.db import Base
    from app.core.security import encrypt_secret
    from app.models.entities import LLMProvider, Workspace

    engine = create_engine(f"sqlite:///{tmp_path / 'providers.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        session.add(Workspace(id="w", name="W", slug="w"))
        session.flush()
        provider = LLMProvider(
            workspace_id="w", provider_key="typesafe", name="Jev",
            base_url="https://api.typesafe.ai", default_model="jev-latest",
            encrypted_api_key=encrypt_secret("secret-key"), provider_kind="typesafe_jev",
            send_fields_json=["name", "tax_id"], prompt_version="entity-pair-choice-v1",
            timeout_seconds=12.5, max_retries=2,
        )
        session.add(provider)
        session.commit()
        provider_id = provider.id
    config = holdout_eval.provider_config_from_db(provider_id, maker)
    assert config["provider_kind"] == "typesafe_jev"
    assert config["api_key"] == "secret-key"
    assert config["send_fields"] == ("name", "tax_id")
    assert config["timeout_seconds"] == 12.5 and config["max_retries"] == 2
    engine.dispose()


def test_cli_validates_and_blocks_without_a_key(monkeypatch, capsys):
    assert holdout_eval.main(["--validate-only"]) == 0
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    exit_code = holdout_eval.main(["--provider-kind", "perplexity_decisions", "--send-fields", "name"])
    assert exit_code == 2
    output = capsys.readouterr().out
    assert '"status": "BLOCKED"' in output
