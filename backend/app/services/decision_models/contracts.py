"""Typed contracts for pluggable decision-model suggestions on entity pairs.

DecisionOutcome has exactly three distinct states: a suggestion, an error,
or None for a disabled decision model. None never means failure.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class DecisionVerdict(StrEnum):
    SAME = "SAME"
    DIFFERENT = "DIFFERENT"
    UNKNOWN = "UNKNOWN"


def _require_non_empty(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _require_number_in_unit_interval(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"{field} must be a number in [0, 1]")


def _require_non_negative_number(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """One candidate pair with only the fields configured for external dispatch."""

    project_id: str
    run_id: str
    input_version: str
    scheme_revision: str
    prompt_version: str
    entity_type: str
    identity_granularity: str
    left: Mapping[str, object]
    right: Mapping[str, object]
    field_names: tuple[str, ...] = ()
    evidence: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "project_id", "run_id", "input_version", "scheme_revision",
            "prompt_version", "entity_type", "identity_granularity",
        ):
            _require_non_empty(getattr(self, field), field)
        if not isinstance(self.left, Mapping) or not isinstance(self.right, Mapping):
            raise ValueError("left and right must be mappings")
        object.__setattr__(self, "left", dict(self.left))
        object.__setattr__(self, "right", dict(self.right))
        if not isinstance(self.field_names, tuple) or any(
            not isinstance(name, str) or not name for name in self.field_names
        ):
            raise ValueError("field_names must be a tuple of non-empty strings")
        if not isinstance(self.evidence, tuple) or any(not isinstance(item, Mapping) for item in self.evidence):
            raise ValueError("evidence must be a tuple of mappings")
        object.__setattr__(self, "evidence", tuple(dict(item) for item in self.evidence))


@dataclass(frozen=True, slots=True)
class DecisionSuggestion:
    verdict: DecisionVerdict
    confidence: float | None
    distribution: Mapping[DecisionVerdict, float] | None
    explanation: str | None
    provider_kind: str
    provider_key: str
    model_version: str
    prompt_version: str
    latency_ms: float
    usage: Mapping[str, object] | None = None
    cost_usd: float | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, DecisionVerdict):
            raise ValueError("verdict must be a DecisionVerdict")
        if self.confidence is not None:
            _require_number_in_unit_interval(self.confidence, "confidence")
        if self.distribution is not None:
            if not isinstance(self.distribution, Mapping) or not self.distribution:
                raise ValueError("distribution must be a non-empty mapping")
            total = 0.0
            for key, value in self.distribution.items():
                if not isinstance(key, DecisionVerdict):
                    raise ValueError("distribution keys must be DecisionVerdict values")
                _require_number_in_unit_interval(value, "distribution value")
                total += value
            if abs(total - 1) > 0.02:
                raise ValueError("distribution values must sum to approximately 1")
            object.__setattr__(self, "distribution", dict(self.distribution))
        if self.explanation is not None and not isinstance(self.explanation, str):
            raise ValueError("explanation must be a string or None")
        _require_non_empty(self.provider_kind, "provider_kind")
        _require_non_empty(self.provider_key, "provider_key")
        _require_non_empty(self.model_version, "model_version")
        _require_non_empty(self.prompt_version, "prompt_version")
        _require_non_negative_number(self.latency_ms, "latency_ms")
        if self.usage is not None:
            if not isinstance(self.usage, Mapping):
                raise ValueError("usage must be a mapping or None")
            object.__setattr__(self, "usage", dict(self.usage))
        if self.cost_usd is not None and (isinstance(self.cost_usd, bool) or not isinstance(self.cost_usd, (int, float))):
            raise ValueError("cost_usd must be a number or None")
        if self.trace_id is not None and not isinstance(self.trace_id, str):
            raise ValueError("trace_id must be a string or None")


@dataclass(frozen=True, slots=True)
class DecisionError:
    code: str
    message: str
    retryable: bool
    provider_kind: str
    provider_key: str
    model_version: str
    prompt_version: str
    latency_ms: float
    trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.code, "code")
        _require_non_empty(self.message, "message")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a bool")
        _require_non_empty(self.provider_kind, "provider_kind")
        _require_non_empty(self.provider_key, "provider_key")
        for field in ("model_version", "prompt_version"):
            if not isinstance(getattr(self, field), str):
                raise ValueError(f"{field} must be a string")
        _require_non_negative_number(self.latency_ms, "latency_ms")
        if self.trace_id is not None and not isinstance(self.trace_id, str):
            raise ValueError("trace_id must be a string or None")


DecisionOutcome = DecisionSuggestion | DecisionError | None
