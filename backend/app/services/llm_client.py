"""Typed OpenAI-compatible model interface for local and private deployments."""
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from opentelemetry.trace import Status, StatusCode

from app.services.unification.telemetry import current_trace_id, start_span

logger = logging.getLogger(__name__)

BUILTIN_PROVIDERS: list[dict[str, str]] = [
    {"provider_key": "offline", "name": "Offline (rules only)", "base_url": "", "default_model": "offline", "provider_kind": "openai_compatible"},
    {"provider_key": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-chat", "provider_kind": "openai_compatible"},
    {"provider_key": "qwen", "name": "Qwen (DashScope)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-plus", "provider_kind": "openai_compatible"},
    {"provider_key": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "default_model": "gpt-4o-mini", "provider_kind": "openai_compatible"},
    {"provider_key": "zhipu", "name": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "default_model": "glm-4-flash", "provider_kind": "openai_compatible"},
    {"provider_key": "moonshot", "name": "Moonshot", "base_url": "https://api.moonshot.cn/v1", "default_model": "moonshot-v1-8k", "provider_kind": "openai_compatible"},
    {"provider_key": "ollama", "name": "Ollama (local/private deployment)", "base_url": "http://127.0.0.1:11434/v1", "default_model": "qwen3.5:4b-local", "provider_kind": "openai_compatible"},
    {"provider_key": "perplexity", "name": "Perplexity Decisions", "base_url": "https://api.perplexity.ai", "default_model": "pplx-decider-flash-latest", "provider_kind": "perplexity_decisions"},
    {"provider_key": "typesafe", "name": "TypeSafe Jev", "base_url": "https://api.typesafe.ai", "default_model": "jev-latest", "provider_kind": "typesafe_jev"},
]


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_key: str
    model: str
    base_url: str
    api_key: str = ""
    name: str = ""
    timeout_seconds: float = 10.0
    max_retries: int = 0


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    project_id: str
    run_id: str
    input_version: str
    scheme_revision: str
    prompt_version: str
    entity_type: str
    left: dict[str, Any]
    right: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelError:
    code: Literal["missing_credentials", "credential_unavailable", "invalid_provider", "provider_missing", "provider_disabled", "timeout", "provider_error", "invalid_json", "invalid_response"]
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class JudgeResult:
    status: Literal["SUGGESTION", "ERROR", "DISABLED"]
    suggestion: bool | None = None
    confidence: float | None = None
    reason: str | None = None
    error: ModelError | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    cost_usd: float | None = None
    trace_id: str | None = None


def _disabled_result(message: str, trace_id: str | None = None) -> JudgeResult:
    return JudgeResult(status="DISABLED", error=ModelError("provider_disabled", message, False), trace_id=trace_id)


def _error(code: str, message: str, *, retryable: bool, model: str, trace_id: str | None) -> JudgeResult:
    return JudgeResult(
        status="ERROR", error=ModelError(code, message[:500], retryable), model=model, trace_id=trace_id,
    )


def _prompt(request: JudgeRequest) -> str:
    return (
        "You provide a review suggestion for two records. Never make the final identity decision. "
        "Return one JSON object with is_same_entity (boolean), confidence (number from 0 to 1), and reason (string).\n"
        f"Entity type: {request.entity_type}\n"
        f"Record A: {json.dumps(request.left, ensure_ascii=False, sort_keys=True)}\n"
        f"Record B: {json.dumps(request.right, ensure_ascii=False, sort_keys=True)}"
    )


def judge_pair(request: JudgeRequest, config: ProviderConfig) -> JudgeResult:
    """Ask a configured model for a suggestion; failures are always explicit values."""
    prompt = _prompt(request)
    model = config.model.strip()
    attributes = {
        "openinference.span.kind": "LLM",
        "project_id": request.project_id,
        "run_id": request.run_id,
        "input_version": request.input_version,
        "scheme_revision": request.scheme_revision,
        "prompt_version": request.prompt_version,
        "llm.provider": config.provider_key,
        "llm.model_name": model,
        "input.value": prompt,
    }

    def failed(code: str, message: str, *, retryable: bool) -> JudgeResult:
        with start_span("unification.model_judge", attributes) as span:
            span.set_attribute("error.code", code)
            span.set_status(Status(StatusCode.ERROR, code))
            return _error(code, message, retryable=retryable, model=model, trace_id=current_trace_id())

    if not model:
        return failed("invalid_provider", "model name is required", retryable=False)
    if config.provider_key.casefold() == "offline":
        with start_span("unification.model_judge", attributes) as span:
            span.set_attribute("error.code", "provider_disabled")
            span.set_status(Status(StatusCode.ERROR, "provider_disabled"))
            return _disabled_result("model judgment is disabled for the offline provider", current_trace_id())
    if config.provider_key.casefold() != "ollama" and not config.api_key.strip():
        return failed("missing_credentials", "provider credentials are required", retryable=False)
    endpoint = config.base_url.strip()
    parsed_url = urlsplit(endpoint)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        return failed("invalid_provider", "base_url must be an HTTP(S) URL without embedded credentials", retryable=False)
    started = time.monotonic()
    with start_span("unification.model_judge", attributes) as span:
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=config.api_key or "ollama",
                base_url=endpoint,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content
            if not content:
                span.set_attribute("error.code", "invalid_response")
                span.set_status(Status(StatusCode.ERROR, "invalid_response"))
                return _error("invalid_response", "provider returned an empty response", retryable=False, model=model, trace_id=current_trace_id())
            try:
                payload = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                span.set_attribute("error.code", "invalid_json")
                span.set_status(Status(StatusCode.ERROR, "invalid_json"))
                return _error("invalid_json", "provider response was not valid JSON", retryable=False, model=model, trace_id=current_trace_id())
            if not isinstance(payload, dict):
                span.set_attribute("error.code", "invalid_response")
                span.set_status(Status(StatusCode.ERROR, "invalid_response"))
                return _error("invalid_response", "provider JSON must be an object", retryable=False, model=model, trace_id=current_trace_id())
            suggestion = payload.get("is_same_entity")
            confidence = payload.get("confidence")
            reason = payload.get("reason")
            if (not isinstance(suggestion, bool) or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1
                    or not isinstance(reason, str)):
                span.set_attribute("error.code", "invalid_response")
                span.set_status(Status(StatusCode.ERROR, "invalid_response"))
                return _error("invalid_response", "provider JSON does not match the judge response contract", retryable=False, model=model, trace_id=current_trace_id())
            usage = None
            if response.usage is not None:
                usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                }
                span.set_attribute("llm.token_count.prompt", usage["input_tokens"])
                span.set_attribute("llm.token_count.completion", usage["output_tokens"])
            span.set_attribute("output.value", content)
            span.set_attribute("llm.request.duration_ms", round((time.monotonic() - started) * 1000, 2))
            return JudgeResult(
                status="SUGGESTION", suggestion=suggestion, confidence=float(confidence), reason=reason,
                model=model, usage=usage, cost_usd=None, trace_id=current_trace_id(),
            )
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            span.set_attribute("llm.request.duration_ms", duration_ms)
            span.set_attribute("error.type", type(exc).__name__)
            if isinstance(exc, TimeoutError) or type(exc).__name__ in {"APITimeoutError", "Timeout"}:
                span.set_attribute("error.code", "timeout")
                span.set_status(Status(StatusCode.ERROR, "timeout"))
                return _error("timeout", "provider request timed out", retryable=True, model=model, trace_id=current_trace_id())
            logger.warning("model request failed (%s)", type(exc).__name__)
            span.set_attribute("error.code", "provider_error")
            span.set_status(Status(StatusCode.ERROR, "provider_error"))
            return _error("provider_error", f"provider request failed ({type(exc).__name__})", retryable=True, model=model, trace_id=current_trace_id())


def call_llm_json(prompt: str, llm_cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Legacy L1/L2 adapter; unification uses `judge_pair` and explicit outcomes."""
    cfg = llm_cfg or {}
    provider_key = str(cfg.get("provider_key") or "offline").lower()
    if provider_key == "offline" or (not cfg.get("api_key") and provider_key != "ollama"):
        return None
    from openai import OpenAI

    client = OpenAI(
        api_key=(cfg.get("api_key") or "ollama"),
        base_url=cfg.get("base_url") or "https://api.openai.com/v1",
        timeout=10,
        max_retries=0,
    )
    resp = client.chat.completions.create(
        model=cfg.get("model") or "gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    parsed = json.loads(resp.choices[0].message.content or "{}")
    parsed["_mode"] = f"LIVE ({cfg.get('name') or provider_key} / {cfg.get('model') or 'gpt-4o-mini'})"
    return parsed


def test_llm_connectivity(llm_cfg: dict[str, Any]) -> dict[str, str]:
    provider_key = str(llm_cfg.get("provider_key") or "offline").lower()
    if provider_key == "offline":
        return {"status": "ok", "message": "Offline rules-only mode is ready."}
    if not llm_cfg.get("api_key") and provider_key != "ollama":
        return {"status": "error", "message": "Provider credentials are required."}
    try:
        result = call_llm_json('Respond with a JSON object that has a single key "ok" and the boolean value true.', llm_cfg)
    except Exception as exc:
        return {"status": "error", "message": f"Provider check failed ({type(exc).__name__})."}
    return {"status": "ok", "message": f"Connection successful ({result.get('_mode')})."} if result else {"status": "error", "message": "Provider returned no response."}
