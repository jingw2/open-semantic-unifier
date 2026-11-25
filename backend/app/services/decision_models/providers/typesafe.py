"""TypeSafe Jev adapter (POST /v1/systemone)."""
from app.services.decision_models.providers.choice import ChoiceAdapter


class TypeSafeJevAdapter(ChoiceAdapter):
    """Maps the TypeSafe Jev choice protocol onto typed outcomes."""

    provider_kind = "typesafe_jev"
    default_base_url = "https://api.typesafe.ai"
    endpoint_path = "/v1/systemone"
    default_model = "jev-latest"
