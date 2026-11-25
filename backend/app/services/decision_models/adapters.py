"""Provider adapters that map provider results onto typed decision outcomes.

Adapters translate protocol and results only; review and merge policy stays
with the caller. The existing `unification.model_judge` span and error codes
keep flowing through `llm_client.judge_pair` unchanged.
"""
from dataclasses import dataclass
import time
from typing import Protocol, runtime_checkable

from app.services import llm_client
from app.services.decision_models.contracts import (
    DecisionError,
    DecisionOutcome,
    DecisionRequest,
    DecisionSuggestion,
    DecisionVerdict,
)
from app.services.unification.telemetry import current_trace_id


@dataclass(frozen=True, slots=True)
class DecisionCapabilities:
    verdicts: frozenset[DecisionVerdict]
    distribution: bool
    explanation: bool
    usage: bool


@runtime_checkable
class DecisionAdapter(Protocol):
    provider_kind: str

    def capabilities(self) -> DecisionCapabilities: ...

    def invoke(self, request: DecisionRequest, config: llm_client.ProviderConfig) -> DecisionOutcome: ...


class OpenAICompatibleAdapter:
    """Maps the boolean judge_pair boundary onto typed decision outcomes.

    The underlying provider only answers same/not-same, so this adapter
    produces SAME or DIFFERENT, never UNKNOWN, and no option distribution.
    """

    provider_kind = "openai_compatible"

    def capabilities(self) -> DecisionCapabilities:
        return DecisionCapabilities(
            verdicts=frozenset({DecisionVerdict.SAME, DecisionVerdict.DIFFERENT}),
            distribution=False,
            explanation=True,
            usage=True,
        )

    def invoke(self, request: DecisionRequest, config: llm_client.ProviderConfig) -> DecisionOutcome:
        started = time.monotonic()
        try:
            return self._invoke(request, config, started)
        except Exception as exc:  # A mapping bug must surface as an explicit error, never a crash.
            return DecisionError(
                code="adapter_error",
                message=f"decision adapter failed ({type(exc).__name__})",
                retryable=False,
                provider_kind=self.provider_kind,
                provider_key=config.provider_key,
                model_version=config.model,
                prompt_version=request.prompt_version,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                trace_id=current_trace_id(),
            )

    def _invoke(self, request: DecisionRequest, config: llm_client.ProviderConfig, started: float) -> DecisionOutcome:
        left = {name: request.left[name] for name in request.field_names if name in request.left}
        right = {name: request.right[name] for name in request.field_names if name in request.right}
        result = llm_client.judge_pair(llm_client.JudgeRequest(
            project_id=request.project_id,
            run_id=request.run_id,
            input_version=request.input_version,
            scheme_revision=request.scheme_revision,
            prompt_version=request.prompt_version,
            entity_type=request.entity_type,
            left=left,
            right=right,
        ), config)
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        model_version = result.model or config.model
        if result.status == "SUGGESTION":
            if result.suggestion is True:
                verdict = DecisionVerdict.SAME
            elif result.suggestion is False:
                verdict = DecisionVerdict.DIFFERENT
            else:  # Defensive: a provider gap must never masquerade as a verdict.
                verdict = DecisionVerdict.UNKNOWN
            return DecisionSuggestion(
                verdict=verdict,
                confidence=result.confidence,
                distribution=None,
                explanation=result.reason,
                provider_kind=self.provider_kind,
                provider_key=config.provider_key,
                model_version=model_version,
                prompt_version=request.prompt_version,
                latency_ms=latency_ms,
                usage=result.usage,
                cost_usd=result.cost_usd,
                trace_id=result.trace_id,
            )
        if result.status == "ERROR" and result.error is not None:
            return DecisionError(
                code=result.error.code,
                message=result.error.message,
                retryable=result.error.retryable,
                provider_kind=self.provider_kind,
                provider_key=config.provider_key,
                model_version=model_version,
                prompt_version=request.prompt_version,
                latency_ms=latency_ms,
                trace_id=result.trace_id,
            )
        if result.status == "DISABLED":
            return None
        return DecisionError(
            code="adapter_error",
            message="unexpected judge_pair status",
            retryable=False,
            provider_kind=self.provider_kind,
            provider_key=config.provider_key,
            model_version=model_version,
            prompt_version=request.prompt_version,
            latency_ms=latency_ms,
            trace_id=result.trace_id,
        )
