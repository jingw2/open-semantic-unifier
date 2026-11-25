"""Typed decision-model contract tests: verdicts, suggestions, errors, requests."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.decision_models.contracts import (
    DecisionError,
    DecisionOutcome,
    DecisionRequest,
    DecisionSuggestion,
    DecisionVerdict,
)


def _suggestion(**overrides):
    values = dict(
        verdict=DecisionVerdict.SAME,
        confidence=0.9,
        distribution=None,
        explanation=None,
        provider_kind="openai_compatible",
        provider_key="local-test",
        model_version="test-model",
        prompt_version="pair-v1",
        latency_ms=12.5,
        usage=None,
        cost_usd=None,
        trace_id=None,
    )
    values.update(overrides)
    return DecisionSuggestion(**values)


def _error(**overrides):
    values = dict(
        code="timeout",
        message="provider request timed out",
        retryable=True,
        provider_kind="openai_compatible",
        provider_key="local-test",
        model_version="test-model",
        prompt_version="pair-v1",
        latency_ms=12.5,
        trace_id=None,
    )
    values.update(overrides)
    return DecisionError(**values)


def _request(**overrides):
    values = dict(
        project_id="project",
        run_id="run",
        input_version="input-v1",
        scheme_revision="scheme-v1",
        prompt_version="pair-v1",
        entity_type="organization",
        identity_granularity="name_and_strong_identifier",
        left={"name": "Acme"},
        right={"name": "Acme Ltd"},
        field_names=("name",),
    )
    values.update(overrides)
    return DecisionRequest(**values)


def test_suggestion_error_and_disabled_are_distinct_outcomes():
    suggestion = _suggestion()
    error = _error()
    assert suggestion is not None and error is not None
    assert not isinstance(suggestion, DecisionError)
    assert not isinstance(error, DecisionSuggestion)
    assert suggestion != error


def test_disabled_none_is_never_an_error():
    outcome: DecisionOutcome = None
    assert outcome is None
    assert not isinstance(outcome, DecisionError)
    assert not isinstance(outcome, DecisionSuggestion)


def test_unknown_verdict_is_distinct_from_same_and_different():
    assert DecisionVerdict.UNKNOWN != DecisionVerdict.DIFFERENT
    assert DecisionVerdict.UNKNOWN != DecisionVerdict.SAME
    assert DecisionVerdict.UNKNOWN.value == "UNKNOWN"
    assert DecisionVerdict.SAME.value == "SAME"
    assert DecisionVerdict.DIFFERENT.value == "DIFFERENT"


def test_missing_distribution_and_explanation_stay_none():
    suggestion = _suggestion()
    assert suggestion.distribution is None
    assert suggestion.explanation is None


def test_valid_distribution_over_same_and_different_sums_to_one():
    distribution = {DecisionVerdict.SAME: 0.4, DecisionVerdict.DIFFERENT: 0.6}
    suggestion = _suggestion(distribution=distribution)
    assert suggestion.distribution == distribution
    assert sum(suggestion.distribution.values()) == 1


def test_distribution_tolerates_provider_rounding():
    distribution = {
        DecisionVerdict.SAME: 0.33,
        DecisionVerdict.DIFFERENT: 0.33,
        DecisionVerdict.UNKNOWN: 0.33,
    }
    suggestion = _suggestion(distribution=distribution)
    assert suggestion.distribution == distribution


@pytest.mark.parametrize("confidence", [-0.1, 1.5, True, "0.9"])
def test_invalid_confidence_raises_value_error(confidence):
    with pytest.raises(ValueError):
        _suggestion(confidence=confidence)


def test_none_confidence_is_valid():
    assert _suggestion(confidence=None).confidence is None


@pytest.mark.parametrize("distribution", [
    {"SAME": 1.0},
    {1: 1.0},
    {None: 1.0},
])
def test_invalid_distribution_key_raises_value_error(distribution):
    with pytest.raises(ValueError):
        _suggestion(distribution=distribution)


@pytest.mark.parametrize("distribution", [
    {DecisionVerdict.SAME: 1.5},
    {DecisionVerdict.SAME: True},
    {DecisionVerdict.SAME: "1.0"},
    {DecisionVerdict.SAME: -0.1},
])
def test_invalid_distribution_value_raises_value_error(distribution):
    with pytest.raises(ValueError):
        _suggestion(distribution=distribution)


@pytest.mark.parametrize("distribution", [
    {DecisionVerdict.SAME: 0.5, DecisionVerdict.DIFFERENT: 0.4},
    {DecisionVerdict.SAME: 1.0, DecisionVerdict.DIFFERENT: 1.0},
    {},
])
def test_distribution_not_summing_to_one_raises_value_error(distribution):
    with pytest.raises(ValueError):
        _suggestion(distribution=distribution)


@pytest.mark.parametrize("overrides", [
    {"code": ""},
    {"code": "   "},
    {"message": ""},
    {"message": "  "},
    {"retryable": "yes"},
])
def test_invalid_error_fields_raise_value_error(overrides):
    with pytest.raises(ValueError):
        _error(**overrides)


def test_error_allows_empty_model_and_prompt_versions():
    error = _error(model_version="", prompt_version="")
    assert error.model_version == ""
    assert error.prompt_version == ""


@pytest.mark.parametrize("overrides", [
    {"latency_ms": -1},
    {"latency_ms": -0.001},
    {"latency_ms": True},
])
def test_invalid_latency_raises_value_error(overrides):
    with pytest.raises(ValueError):
        _suggestion(**overrides)
    with pytest.raises(ValueError):
        _error(**overrides)


@pytest.mark.parametrize("field", ["model_version", "prompt_version"])
def test_empty_model_or_prompt_version_raises_for_suggestion(field):
    with pytest.raises(ValueError):
        _suggestion(**{field: ""})


def test_suggestion_rejects_a_non_verdict():
    with pytest.raises(ValueError):
        _suggestion(verdict="SAME")


@pytest.mark.parametrize("field", [
    "project_id", "run_id", "input_version", "scheme_revision",
    "prompt_version", "entity_type", "identity_granularity",
])
def test_request_rejects_empty_identity_fields(field):
    with pytest.raises(ValueError):
        _request(**{field: ""})


@pytest.mark.parametrize("field_names", [
    ("name", ""),
    ("name", 3),
    "name",
    ["name"],
])
def test_request_rejects_invalid_field_names(field_names):
    with pytest.raises(ValueError):
        _request(field_names=field_names)


def test_request_accepts_empty_field_names():
    request = _request(field_names=())
    assert request.field_names == ()
    assert request.left == {"name": "Acme"}


def test_request_rejects_non_mapping_records():
    with pytest.raises(ValueError):
        _request(left="Acme")


def test_request_rejects_non_mapping_evidence():
    with pytest.raises(ValueError):
        _request(evidence=({"rule": "exact"}, "not a mapping"))
