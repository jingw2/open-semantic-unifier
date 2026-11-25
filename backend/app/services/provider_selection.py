"""Select usable chat and decision providers without sharing one default flag."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import LLMProvider

DECISION_PROVIDER_KINDS = ("perplexity_decisions", "typesafe_jev")


def _ordered(db: Session, workspace_id: str, kinds: tuple[str, ...]) -> list[LLMProvider]:
    return list(db.scalars(select(LLMProvider).where(
        LLMProvider.workspace_id == workspace_id,
        LLMProvider.provider_kind.in_(kinds),
    ).order_by(LLMProvider.is_default.desc(), LLMProvider.created_at.asc())).all())


def select_openai_provider(db: Session, workspace_id: str) -> LLMProvider | None:
    """Return the first usable chat provider, skipping the offline preset and empty rows."""
    for provider in _ordered(db, workspace_id, ("openai_compatible",)):
        if (provider.provider_key or "").casefold() == "offline":
            continue
        if not (provider.base_url or "").strip() and not (provider.encrypted_api_key or "").strip():
            continue
        return provider
    return None


def select_decision_provider(db: Session, workspace_id: str) -> LLMProvider | None:
    providers = _ordered(db, workspace_id, DECISION_PROVIDER_KINDS)
    return providers[0] if providers else None
