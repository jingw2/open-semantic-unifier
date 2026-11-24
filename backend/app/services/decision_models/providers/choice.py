"""Shared base for providers that answer the entity-match choice protocol.

Perplexity Decisions and TypeSafe Jev expose the same shape: one POST to a
versioned endpoint carrying a `state` and a named `questions.entity_match`
choice, answered with `answers.entity_match.choice`, `probabilities`, and
`confidence`. Both providers round probabilities and confidence to two
decimals, so the contract tolerance of 0.02 on the distribution sum holds.

Adapters here map protocol and results only; review and merge policy stays
with the caller.
"""
import json
import time
from typing import Mapping

import httpx
from opentelemetry.trace import Status, StatusCode

from app.services import llm_client
from app.services.decision_models.adapters import DecisionCapabilities
from app.services.decision_models.contracts import (
    DecisionError,
    DecisionOutcome,
    DecisionRequest,
    DecisionSuggestion,
    DecisionVerdict,
)
from app.services.unification.telemetry import current_trace_id, start_span

PROMPT_VERSION = "entity-pair-choice-v1"

VERDICT_NAMES = frozenset({"SAME", "DIFFERENT", "UNKNOWN"})

CHOICE_INSTRUCTIONS = (
    "Judge whether the two records refer to the same real-world entity using only the supplied evidence. "
    "Choose UNKNOWN when the evidence is insufficient. This is a review suggestion, not a final decision."
)

CHOICE_CRITERIA = {
    "SAME": "The two records refer to the same real-world entity.",
    "DIFFERENT": "The two records refer to different real-world entities.",
    "UNKNOWN": "The supplied evidence is insufficient to decide.",
}


class ChoiceAdapter:
    """Maps one choice-protocol provider onto typed decision outcomes."""

    provider_kind = ""
    default_base_url = ""
    endpoint_path = ""
    default_model = ""

    def capabilities(self) -> DecisionCapabilities:
        return DecisionCapabilities(
            verdicts=frozenset({DecisionVerdict.SAME, DecisionVerdict.DIFFERENT, DecisionVerdict.UNKNOWN}),
            distribution=True,
            explanation=False,
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
                provider_key=config.provider_key.strip() or self.provider_kind,
                model_version=config.model.strip() or self.default_model,
                prompt_version=PROMPT_VERSION,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                trace_id=current_trace_id(),
            )

    def _invoke(self, request: DecisionRequest, config: llm_client.ProviderConfig, started: float) -> DecisionOutcome:
        provider_key = config.provider_key.strip() or self.provider_kind
        model = config.model.strip() or self.default_model
        attributes = {
            "openinference.span.kind": "LLM",
            "project_id": request.project_id,
            "run_id": request.run_id,
            "input_version": request.input_version,
            "scheme_revision": request.scheme_revision,
            "prompt_version": PROMPT_VERSION,
            "scheme.prompt_version": request.prompt_version,
            "llm.provider": config.provider_key,
            "llm.provider_kind": self.provider_kind,
            "llm.model_name": model,
            "decision.input_fields": ",".join(request.field_names),
        }
        with start_span("unification.model_judge", attributes) as span:
            if not config.api_key.strip():
                return self._fail(
                    span, "missing_credentials", "provider credentials are required",
                    retryable=False, provider_key=provider_key, model_version=model, started=started,
                )
            timeout = config.timeout_seconds if config.timeout_seconds > 0 else 10.0
            try:
                response = httpx.post(
                    self._endpoint(config.base_url),
                    headers={
                        "Authorization": f"Bearer {config.api_key.strip()}",
                        "Content-Type": "application/json",
                    },
                    json=self._body(request, model),
                    timeout=timeout,
                )
            except httpx.TimeoutException:
                return self._fail(
                    span, "timeout", "provider request timed out",
                    retryable=True, provider_key=provider_key, model_version=model, started=started,
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                return self._fail(
                    span, "provider_error", f"provider request failed ({type(exc).__name__})",
                    retryable=True, provider_key=provider_key, model_version=model, started=started,
                )
            self._record_request_id(span, response)
            span.set_attribute("llm.request.duration_ms", round((time.monotonic() - started) * 1000, 2))
            if response.status_code >= 400:
                code, retryable, message = self._status_outcome(response.status_code)
                model_version = self._model_from_text(response.text, model)
                return self._fail(
                    span, code, message,
                    retryable=retryable, provider_key=provider_key, model_version=model_version, started=started,
                )
            try:
                payload = json.loads(response.text)
            except (TypeError, json.JSONDecodeError):
                return self._fail(
                    span, "invalid_json", "provider response was not valid JSON",
                    retryable=False, provider_key=provider_key, model_version=model, started=started,
                )
            model_version = self._model_from_payload(payload, model)
            return self._map_payload(span, payload, provider_key, model_version, started)

    @staticmethod
    def _status_outcome(status_code: int) -> tuple[str, bool, str]:
        if status_code in (401, 403):
            return "invalid_credentials", False, "provider rejected the credentials"
        if status_code == 422:
            return "invalid_request", False, "provider rejected the request"
        if status_code == 429:
            return "rate_limited", True, "provider rate limit exceeded"
        if 500 <= status_code <= 599:
            return "provider_error", True, f"provider returned HTTP {status_code}"
        if 400 <= status_code <= 499:
            return "provider_error", False, f"provider returned HTTP {status_code}"
        return "provider_error", True, f"provider returned HTTP {status_code}"

    def _map_payload(self, span, payload, provider_key: str, model_version: str, started: float) -> DecisionOutcome:
        parsed = self._parse_payload(payload)
        if parsed is None:
            return self._fail(
                span, "invalid_response", "provider response does not match the choice contract",
                retryable=False, provider_key=provider_key, model_version=model_version, started=started,
            )
        verdict, confidence, distribution = parsed
        usage = dict(payload["usage"]) if isinstance(payload.get("usage"), Mapping) else None
        return DecisionSuggestion(
            verdict=verdict,
            confidence=confidence,
            distribution=distribution,
            explanation=None,
            provider_kind=self.provider_kind,
            provider_key=provider_key,
            model_version=model_version,
            prompt_version=PROMPT_VERSION,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage,
            cost_usd=None,
            trace_id=current_trace_id(),
        )

    @staticmethod
    def _parse_payload(payload: object) -> tuple[DecisionVerdict, float, dict[DecisionVerdict, float]] | None:
        """Validate a provider payload and return (verdict, confidence, distribution), or None."""
        if not isinstance(payload, Mapping):
            return None
        answers = payload.get("answers")
        if not isinstance(answers, Mapping):
            return None
        answer = answers.get("entity_match")
        if not isinstance(answer, Mapping):
            return None
        if answer.get("type") != "choice":
            return None
        choice = answer.get("choice")
        if choice not in VERDICT_NAMES:
            return None
        confidence = answer.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            return None
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping) or not probabilities:
            return None
        distribution: dict[DecisionVerdict, float] = {}
        total = 0.0
        for name, value in probabilities.items():
            if name not in VERDICT_NAMES or isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                return None
            number = float(value)
            distribution[DecisionVerdict(name)] = number
            total += number
        if abs(total - 1) > 0.02:
            return None
        return DecisionVerdict(choice), float(confidence), distribution

    def _fail(self, span, code: str, message: str, *, retryable: bool,
              provider_key: str, model_version: str, started: float) -> DecisionError:
        span.set_attribute("error.code", code)
        span.set_status(Status(StatusCode.ERROR, code))
        span.set_attribute("llm.request.duration_ms", round((time.monotonic() - started) * 1000, 2))
        return DecisionError(
            code=code,
            message=message,
            retryable=retryable,
            provider_kind=self.provider_kind,
            provider_key=provider_key,
            model_version=model_version,
            prompt_version=PROMPT_VERSION,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            trace_id=current_trace_id(),
        )

    def _body(self, request: DecisionRequest, model: str) -> dict:
        left = {name: request.left[name] for name in request.field_names if name in request.left}
        right = {name: request.right[name] for name in request.field_names if name in request.right}
        return {
            "model": model,
            "state": {
                "entity_type": request.entity_type,
                "identity_granularity": request.identity_granularity,
                "left": left,
                "right": right,
                "evidence": list(request.evidence),
            },
            "questions": {
                "entity_match": {
                    "type": "choice",
                    "instructions": CHOICE_INSTRUCTIONS,
                    "criteria": dict(CHOICE_CRITERIA),
                },
            },
        }

    def _endpoint(self, base_url: str) -> str:
        url = (base_url or "").strip().rstrip("/")
        if not url:
            return self.default_base_url + self.endpoint_path
        if url.endswith(self.endpoint_path):
            return url
        if url.endswith("/v1"):
            return url + self.endpoint_path[len("/v1"):]
        return url + self.endpoint_path

    @staticmethod
    def _record_request_id(span, response) -> None:
        for header in ("x-typesafe-request-id", "x-request-id"):
            value = response.headers.get(header)
            if value:
                span.set_attribute("provider.request_id", value)
                return

    def _model_from_text(self, text: str, fallback: str) -> str:
        try:
            payload = json.loads(text) if text else None
        except (TypeError, json.JSONDecodeError):
            return fallback
        return self._model_from_payload(payload, fallback)

    @staticmethod
    def _model_from_payload(payload: object, fallback: str) -> str:
        if isinstance(payload, Mapping) and isinstance(payload.get("model"), str) and payload["model"].strip():
            return payload["model"]
        return fallback
