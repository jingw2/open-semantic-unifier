"""Typed decision suggestions must never override deterministic precedence rules."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.decision_models.contracts import DecisionError, DecisionSuggestion, DecisionVerdict
from app.services.unification.matching import CannotLink, MatchAction, match_records
from app.services.unification.schemes import MatchScheme


def record(record_id, attributes):
    return SimpleNamespace(id=record_id, source_record=SimpleNamespace(source_system="erp"), attributes=attributes)


def same_suggestion(**overrides):
    values = dict(
        verdict=DecisionVerdict.SAME,
        confidence=0.99,
        distribution={DecisionVerdict.SAME: 0.99, DecisionVerdict.DIFFERENT: 0.01},
        explanation="model says same",
        provider_kind="openai_compatible",
        provider_key="fake-provider",
        model_version="fake-model",
        prompt_version="pair-v1",
        latency_ms=3.0,
        usage={"input_tokens": 11, "output_tokens": 4},
        cost_usd=0.0002,
        trace_id="trace-pair",
    )
    values.update(overrides)
    return DecisionSuggestion(**values)


def _conflict_scheme():
    return MatchScheme.model_validate({
        "entity_type": "product", "identity_granularity": "sellable_variant",
        "field_map": {"name": "name", "gtin": "gtin", "voltage": "voltage"},
        "strong_identifiers": ["gtin"],
        "conflict_constraints": [{"field": "voltage", "action": "veto_auto_merge"}],
        "normalizers": ["unicode_nfkc", "trim"],
    })


def _conflict_records():
    return [
        record("left", {"name": "Widget", "gtin": "TEST-001", "voltage": "110 V"}),
        record("right", {"name": "Widget", "gtin": "TEST-001", "voltage": "220 V"}),
    ]


def _weak_evidence_scheme():
    return MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
        "candidate_rules": [{"kind": "name_similarity", "field": "name", "threshold": 0.7}],
    })


def _weak_evidence_records():
    return [
        record("left", {"name": "Acme Industrial", "tax_id": None}),
        record("right", {"name": "Acme Industrials", "tax_id": ""}),
    ]


def test_same_suggestion_cannot_override_configured_conflict():
    result = match_records(
        _conflict_records(), _conflict_scheme(), constraints=[],
        judge=lambda *_: same_suggestion(),
    )
    pair = result.pairs[0]
    assert pair.action == MatchAction.DISTINCT
    assert "CONFLICT_CONSTRAINT" in pair.evidence_codes
    assert pair.decision is not None and pair.decision.verdict == DecisionVerdict.SAME
    assert pair.recommendation["verdict"] == "SAME"
    assert pair.recommendation["is_same_entity"] is True


def test_same_suggestion_cannot_override_cannot_link():
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
    })
    records = [record("left", {"tax_id": "TEST-001"}), record("right", {"tax_id": "TEST-001"})]
    result = match_records(
        records, scheme, constraints=[CannotLink("left", "right", "decision-7")],
        judge=lambda *_: same_suggestion(),
    )
    pair = result.pairs[0]
    assert pair.action == MatchAction.DISTINCT
    assert "CANNOT_LINK" in pair.evidence_codes
    assert pair.decision is not None and pair.decision.verdict == DecisionVerdict.SAME


def test_weak_evidence_same_suggestion_stays_review():
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: same_suggestion(),
    )
    pair = result.pairs[0]
    assert pair.action == MatchAction.REVIEW
    assert pair.action != MatchAction.AUTO_MATCH
    assert pair.recommendation["verdict"] == "SAME"
    assert pair.decision is not None


def test_different_suggestion_does_not_create_persistent_cannot_link():
    suggestion = same_suggestion(verdict=DecisionVerdict.DIFFERENT)
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: suggestion,
    )
    pair = result.pairs[0]
    assert pair.action == MatchAction.REVIEW
    assert pair.action != MatchAction.DISTINCT
    assert "CANNOT_LINK" not in pair.evidence_codes
    assert pair.recommendation["verdict"] == "DIFFERENT"
    assert pair.recommendation["is_same_entity"] is False
    # A later run with no suggestion reaches the same deterministic verdict:
    # the model never recorded a constraint.
    again = match_records(_weak_evidence_records(), _weak_evidence_scheme(), constraints=[])
    assert again.pairs[0].action == MatchAction.REVIEW


def test_decision_error_sets_model_error_and_keeps_action():
    error = DecisionError(
        code="timeout", message="provider request timed out", retryable=True,
        provider_kind="openai_compatible", provider_key="fake-provider",
        model_version="fake-model", prompt_version="pair-v1", latency_ms=9.0,
    )
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: error,
    )
    pair = result.pairs[0]
    assert pair.model_error == "timeout: provider request timed out"
    assert isinstance(pair.decision, DecisionError)
    assert pair.recommendation is None
    assert pair.action == MatchAction.REVIEW


def test_none_outcome_sets_no_typed_decision():
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: None,
    )
    pair = result.pairs[0]
    assert pair.decision is None
    assert pair.recommendation is None


def test_suggestion_recommendation_carries_typed_fields():
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: same_suggestion(confidence=0.9, distribution={DecisionVerdict.SAME: 0.9, DecisionVerdict.DIFFERENT: 0.1}),
    )
    recommendation = result.pairs[0].recommendation
    assert recommendation["verdict"] == "SAME"
    assert recommendation["is_same_entity"] is True
    assert recommendation["confidence"] == 0.9
    assert recommendation["distribution"] == {"SAME": 0.9, "DIFFERENT": 0.1}
    assert recommendation["reason"] == "model says same"
    assert recommendation["model"] == "fake-model"
    assert recommendation["provider_kind"] == "openai_compatible"
    assert recommendation["provider_key"] == "fake-provider"
    assert recommendation["prompt_version"] == "pair-v1"
    assert recommendation["latency_ms"] == 3.0
    assert recommendation["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert recommendation["cost_usd"] == 0.0002
    assert recommendation["trace_id"] == "trace-pair"


def test_unknown_verdict_maps_to_none_is_same_entity():
    suggestion = same_suggestion(verdict=DecisionVerdict.UNKNOWN, confidence=None, distribution=None)
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: suggestion,
    )
    pair = result.pairs[0]
    assert pair.recommendation["verdict"] == "UNKNOWN"
    assert pair.recommendation["is_same_entity"] is None
    assert pair.action == MatchAction.REVIEW


def test_legacy_judge_result_keeps_dict_recommendation_without_typed_decision():
    from app.services.llm_client import JudgeResult
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: JudgeResult(
            status="SUGGESTION", suggestion=True, confidence=0.9,
            reason="legacy says same", model="legacy-model",
        ),
    )
    pair = result.pairs[0]
    assert pair.decision is None
    assert pair.recommendation["is_same_entity"] is True
    assert pair.recommendation["model"] == "legacy-model"


def test_dict_judge_branch_stays_compatible_without_typed_decision():
    result = match_records(
        _weak_evidence_records(), _weak_evidence_scheme(), constraints=[],
        judge=lambda *_: {"is_same_entity": True, "confidence": 0.8},
    )
    pair = result.pairs[0]
    assert pair.decision is None
    assert pair.recommendation == {"is_same_entity": True, "confidence": 0.8}
