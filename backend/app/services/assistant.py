"""Deterministic and model-assisted intent planning for the workbench assistant."""
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.entities import DataSource, Project
from app.services.llm_client import call_llm_json
from app.services.provider_selection import select_openai_provider

INTENTS = ("clean", "match", "metrics", "contract", "review", "unknown")
KEYWORDS = {
    "match": ("匹配", "客户", "跨库", "关联", "认亲"),
    "clean": ("清洗", "名单", "供应商", "标准化", "去重"),
    "metrics": ("指标", "gmv", "口径", "报表"),
    "contract": ("契约", "标准", "linkml", "w3c"),
    "review": ("复核", "审核", "裁决"),
}


def data_summary(db: Session, project_id: str) -> dict[str, Any]:
    sources = db.scalars(select(DataSource).where(DataSource.project_id == project_id)).all()
    total_rows = 0
    table_count = 0
    field_names: set[str] = set()
    for source in sources:
        profile = source.profile_json or {}
        total_rows += int(profile.get("total_rows") or 0)
        table_count += int(profile.get("table_count") or 0)
        for table in profile.get("tables") or []:
            for column in table.get("columns") or []:
                if isinstance(column, str) and column:
                    field_names.add(column)
    return {
        "source_count": len(sources),
        "total_rows": total_rows,
        "table_count": table_count,
        "field_names": sorted(field_names)[:20],
    }


def deterministic_intent(message: str) -> tuple[str, float, str]:
    text = (message or "").casefold()
    for intent, keywords in KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return intent, 0.85, f"按关键词识别为 {intent}。"
    return "unknown", 0.4, "没听懂。请从下面的任务里选一个。"


def _provider_config(provider) -> dict[str, Any]:
    return {
        "provider_key": provider.provider_key,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.default_model,
        "api_key": decrypt_secret(provider.encrypted_api_key) if provider.encrypted_api_key else "",
    }


def classify_assistant_message(db: Session, project_id: str, message: str, page: str) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    provider = select_openai_provider(db, project.workspace_id)
    if provider is not None:
        summary = data_summary(db, project_id)
        prompt = (
            "Classify the user's goal for a semantic unification product. Return one JSON object with "
            "intent in [clean, match, metrics, contract, review, unknown], confidence from 0 to 1, "
            "and explanation in Chinese. Do not decide or execute any data action.\n"
            f"Page: {page}\nData summary: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
            f"User: {message}"
        )
        try:
            result = call_llm_json(prompt, _provider_config(provider))
            intent = result.get("intent") if isinstance(result, dict) else None
            confidence = result.get("confidence") if isinstance(result, dict) else None
            explanation = result.get("explanation") if isinstance(result, dict) else None
            if (
                intent in INTENTS
                and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                and 0.55 <= float(confidence) <= 1
                and isinstance(explanation, str) and explanation.strip()
            ):
                return {
                    "intent": intent,
                    "confidence": float(confidence),
                    "explanation": explanation.strip(),
                    "suggested_task": intent,
                    "source": "model",
                }
        except Exception:
            pass
    intent, confidence, explanation = deterministic_intent(message)
    return {
        "intent": intent,
        "confidence": confidence,
        "explanation": explanation,
        "suggested_task": intent,
        "source": "rules",
    }
