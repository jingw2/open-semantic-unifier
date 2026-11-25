"""Deterministic and model-assisted intent planning for the workbench assistant."""
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.entities import DataSource, Project
from app.services.llm_client import call_llm_json
from app.services.openrefine.recipes import load_recipes
from app.services.provider_selection import select_openai_provider

INTENTS = ("clean", "match", "entity", "metrics", "contract", "review", "answer", "unknown")
KEYWORDS = {
    "match": ("匹配", "客户", "跨库", "关联", "认亲"),
    "clean": ("清洗", "名单", "供应商", "标准化", "去重"),
    "metrics": ("指标", "gmv", "口径", "报表"),
    "contract": ("契约", "标准", "linkml", "w3c"),
    "review": ("复核", "审核", "裁决"),
}
ENTITY_NOUNS = ("客户", "供应商", "公司", "企业", "联系人")
DEDUP_TERMS = ("去重", "重复", "重名")

# metric_name values are the built-in metrics in DEFAULT_METRICS_YAML.
ALLOWED_METRIC_NAMES = frozenset({"net_gmv", "signed_fulfillment_rate", "settled_revenue"})

ALLOWED_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "clean": ("target_col", "amount_col"),
    "match": ("name_col", "tax_col", "phone_col", "city_col"),
    "metrics": ("metric_name", "group_by"),
}

CLARIFY_OPTIONS = (
    {"label": "清洗供应商名单", "message": "清洗供应商名单"},
    {"label": "跨系统匹配客户", "message": "跨系统匹配客户"},
    {"label": "统一 GMV 口径", "message": "统一 GMV 口径"},
)


def _builtin_recipe_ids() -> frozenset[str]:
    try:
        return frozenset(recipe.id for recipe in load_recipes())
    except Exception:
        return frozenset({
            "merge_name_columns", "normalize_company_name", "normalize_punctuation",
            "normalize_whitespace", "remove_legal_suffix", "split_contact_column",
        })


def _source_fields(profile: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for table in profile.get("tables") or []:
        for column in table.get("columns") or []:
            if isinstance(column, str) and column and column not in fields:
                fields.append(column)
    return fields


def _load_sources(db: Session, project_id: str) -> list[DataSource]:
    return list(db.scalars(select(DataSource).where(DataSource.project_id == project_id)).all())


def deterministic_intent(message: str) -> tuple[str, float, str]:
    text = (message or "").casefold()
    for intent, keywords in KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return intent, 0.85, f"按关键词识别为 {intent}。"
    return "unknown", 0.4, "没听懂。请从下面的任务里选一个。"


def is_entity_dedup(message: str) -> bool:
    text = (message or "").casefold()
    return any(term in text for term in DEDUP_TERMS) and any(noun in text for noun in ENTITY_NOUNS)


def _provider_config(provider) -> dict[str, Any]:
    return {
        "provider_key": provider.provider_key,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.default_model,
        "api_key": decrypt_secret(provider.encrypted_api_key) if provider.encrypted_api_key else "",
    }


def _plan(
    intent: str,
    confidence: float,
    explanation: str,
    source: str,
    suggested_task: str | None,
    *,
    needs_clarification: bool = False,
    question: str | None = None,
    options: list[dict[str, str]] | None = None,
    datasource_id: str | None = None,
    params: dict[str, Any] | None = None,
    recipe_id: str | None = None,
) -> dict[str, Any]:
    return {
        "intent": intent,
        "confidence": confidence,
        "explanation": explanation,
        "source": source,
        "suggested_task": suggested_task,
        "needs_clarification": needs_clarification,
        "question": question,
        "options": options or [],
        "datasource_id": datasource_id,
        "params": params,
        "recipe_id": recipe_id,
    }


def _rules_plan(intent: str, confidence: float, explanation: str) -> dict[str, Any]:
    if intent == "unknown":
        return _plan(
            "unknown", confidence, explanation, "rules", None,
            needs_clarification=True, question=explanation, options=list(CLARIFY_OPTIONS),
        )
    return _plan(intent, confidence, explanation, "rules", intent)


def _coerce_bool(value: Any) -> bool:
    return bool(value)


def _parse_options(raw: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw[:3]:
            if not isinstance(item, dict):
                continue
            label = item.get("label")
            message = item.get("message")
            if isinstance(label, str) and label.strip() and isinstance(message, str) and message.strip():
                options.append({"label": label.strip(), "message": message.strip()})
    return options


def _prune_params(
    intent: str,
    raw_params: Any,
    allowed_fields: set[str],
) -> dict[str, Any] | None:
    allowed = ALLOWED_PARAM_KEYS.get(intent)
    if not allowed or not isinstance(raw_params, dict):
        return None
    params: dict[str, Any] = {}
    for key in allowed:
        value = raw_params.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if intent == "metrics":
            if key == "metric_name":
                if value not in ALLOWED_METRIC_NAMES:
                    continue
            elif value not in allowed_fields:
                continue
        elif value not in allowed_fields:
            continue
        params[key] = value
    return params or None


def _build_prompt(page: str, summaries: list[dict[str, Any]], history: list[dict[str, Any]] | None, message: str) -> str:
    turns: list[dict[str, str]] = []
    for turn in (history or [])[-6:]:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
            turns.append({"role": turn["role"], "content": turn["content"][:1000]})
    return (
        "Plan the user's goal for a semantic unification product. Return one JSON object with "
        "intent in [clean, match, entity, metrics, contract, review, answer, unknown], "
        "confidence from 0 to 1, explanation in Chinese, needs_clarification (boolean), "
        "question (string or null), options (up to 3 objects with label and message), "
        "datasource_id (a data source id or null), params (object or null), and recipe_id "
        "(a built-in recipe id or null). Do not execute or decide any data action.\n"
        "Intent definitions:\n"
        "- clean: normalize or reformat fields in one table (trim, casing, punctuation, split or merge columns).\n"
        "- match: find or deduplicate the same real-world entity, including requests to 去重 an entity list "
        "such as 客户名单 or 供应商名单. Entity deduplication is match, never clean.\n"
        "- entity: run an entity unification demo over example records.\n"
        "- metrics: unify metric definitions or report numbers.\n"
        "- contract: create, compile, or validate a data contract.\n"
        "- review: inspect or decide review items.\n"
        "- answer: answer a question about the product or the current page without proposing a task.\n"
        "- unknown: the goal is unclear; ask one short clarifying question.\n"
        "Allowed params by intent:\n"
        "- clean: target_col, amount_col\n"
        "- match: name_col, tax_col, phone_col, city_col\n"
        "- metrics: metric_name, group_by\n"
        "- others: none\n"
        f"Built-in recipe ids: {', '.join(sorted(_builtin_recipe_ids()))}\n"
        f"Page: {page}\n"
        f"Data sources: {json.dumps(summaries, ensure_ascii=False, sort_keys=True)}\n"
        f"Conversation (last 6 turns): {json.dumps(turns, ensure_ascii=False, sort_keys=True)}\n"
        f"User: {message}"
    )


def _model_plan(
    result: dict[str, Any],
    message: str,
    fields_by_id: dict[str, list[str]],
    project_fields: set[str],
) -> dict[str, Any] | None:
    intent = result.get("intent")
    confidence = result.get("confidence")
    explanation = result.get("explanation")
    if (
        intent not in INTENTS
        or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
        or not 0.55 <= float(confidence) <= 1
        or not isinstance(explanation, str) or not explanation.strip()
    ):
        return None
    confidence = float(confidence)
    explanation = explanation.strip()

    if intent == "clean" and is_entity_dedup(message):
        intent = "match"
        explanation = "客户或供应商名单去重是实体匹配，不是字段清洗。已按实体匹配处理。"

    needs_clarification = _coerce_bool(result.get("needs_clarification"))
    question = result.get("question")
    if not (isinstance(question, str) and question.strip()):
        question = None
    options = _parse_options(result.get("options"))

    datasource_id = result.get("datasource_id")
    if not (isinstance(datasource_id, str) and datasource_id in fields_by_id):
        datasource_id = None
    allowed_fields = set(fields_by_id.get(datasource_id, [])) if datasource_id else project_fields
    params = _prune_params(intent, result.get("params"), allowed_fields)

    recipe_id = result.get("recipe_id")
    if not (isinstance(recipe_id, str) and recipe_id in _builtin_recipe_ids()):
        recipe_id = None

    if intent == "answer":
        return _plan("answer", confidence, explanation, "model", None)
    if intent == "unknown":
        return _plan(
            "unknown", confidence, explanation, "model", None,
            needs_clarification=needs_clarification, question=question, options=options,
        )
    return _plan(
        intent, confidence, explanation, "model", intent,
        needs_clarification=needs_clarification, question=question, options=options,
        datasource_id=datasource_id, params=params, recipe_id=recipe_id,
    )


def classify_assistant_message(
    db: Session,
    project_id: str,
    message: str,
    page: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    sources = _load_sources(db, project_id)
    fields_by_id = {source.id: _source_fields(source.profile_json or {}) for source in sources}
    project_fields = set().union(*fields_by_id.values()) if fields_by_id else set()

    provider = select_openai_provider(db, project.workspace_id)
    if provider is not None:
        summaries = [
            {
                "id": source.id,
                "name": source.name,
                "row_count": int((source.profile_json or {}).get("total_rows") or 0),
                "role": (source.routing_json or {}).get("role"),
                "field_names": fields_by_id[source.id][:20],
            }
            for source in sources
        ]
        prompt = _build_prompt(page, summaries, history, message)
        try:
            result = call_llm_json(prompt, _provider_config(provider))
            if isinstance(result, dict):
                plan = _model_plan(result, message, fields_by_id, project_fields)
                if plan is not None:
                    return plan
        except Exception:
            pass

    intent, confidence, explanation = deterministic_intent(message)
    return _rules_plan(intent, confidence, explanation)
