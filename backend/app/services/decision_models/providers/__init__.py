"""Provider-specific decision adapters and the configured-kind registry.

Each adapter maps one provider's request and response protocol onto the
typed decision contracts. Review and merge policy stays with the caller.
"""
from app.services.decision_models.adapters import DecisionAdapter, OpenAICompatibleAdapter
from app.services.decision_models.providers.choice import ChoiceAdapter
from app.services.decision_models.providers.perplexity import PerplexityDecisionsAdapter
from app.services.decision_models.providers.typesafe import TypeSafeJevAdapter

_ADAPTERS_BY_KIND: dict[str, DecisionAdapter] = {
    "openai_compatible": OpenAICompatibleAdapter(),
    "perplexity_decisions": PerplexityDecisionsAdapter(),
    "typesafe_jev": TypeSafeJevAdapter(),
}


def adapter_for_kind(provider_kind: str) -> DecisionAdapter | None:
    """Return the adapter for a configured provider kind, or None for unknown kinds."""
    return _ADAPTERS_BY_KIND.get(provider_kind)


__all__ = ["ChoiceAdapter", "PerplexityDecisionsAdapter", "TypeSafeJevAdapter", "adapter_for_kind"]
