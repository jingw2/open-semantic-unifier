"""Perplexity Decisions adapter (POST /v1/decisions)."""
from app.services.decision_models.providers.choice import ChoiceAdapter


class PerplexityDecisionsAdapter(ChoiceAdapter):
    """Maps the Perplexity Decisions choice protocol onto typed outcomes."""

    provider_kind = "perplexity_decisions"
    default_base_url = "https://api.perplexity.ai"
    endpoint_path = "/v1/decisions"
    default_model = "pplx-decider-flash-latest"
