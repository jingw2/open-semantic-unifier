"""Built-in records and scheme for the assistant's entity demo run."""

DEMO_SCHEME_KEY = "assistant_entity_demo"
DEMO_INPUT_VERSION = "assistant-entity-demo-v1"
DEMO_SOURCE_SYSTEM = "assistant_demo"

DEMO_RECORDS = [
    {"source_system": DEMO_SOURCE_SYSTEM, "dataset": "suppliers_demo", "source_record_id": "acme-erp",
     "attributes": {"name": "Acme Ltd", "tax_id": None, "phone": None}},
    {"source_system": DEMO_SOURCE_SYSTEM, "dataset": "suppliers_demo", "source_record_id": "acme-crm",
     "attributes": {"name": "ACME, Ltd.", "tax_id": None, "phone": None}},
    {"source_system": DEMO_SOURCE_SYSTEM, "dataset": "suppliers_demo", "source_record_id": "bluefin-a",
     "attributes": {"name": "Bluefin Marine Supply", "tax_id": None, "phone": None}},
    {"source_system": DEMO_SOURCE_SYSTEM, "dataset": "suppliers_demo", "source_record_id": "bluefin-b",
     "attributes": {"name": "Bluefin Marine Supplies", "tax_id": None, "phone": None}},
    {"source_system": DEMO_SOURCE_SYSTEM, "dataset": "suppliers_demo", "source_record_id": "cedar-a",
     "attributes": {"name": "Cedar Ltd", "tax_id": "DEMO-TAX-001", "phone": None}},
    {"source_system": DEMO_SOURCE_SYSTEM, "dataset": "suppliers_demo", "source_record_id": "cedar-b",
     "attributes": {"name": "Cedar Ltd", "tax_id": "DEMO-TAX-002", "phone": None}},
]


DEMO_MATCH_FIELDS = ("name", "tax_id", "phone")


def demo_scheme(model_provider_id: str | None = None, match_fields: list[str] | None = None) -> dict:
    selected = [field for field in (match_fields or DEMO_MATCH_FIELDS) if field in DEMO_MATCH_FIELDS]
    if not selected:
        selected = ["name"]
    candidate_rules = []
    if "tax_id" in selected:
        candidate_rules.append({"kind": "exact", "fields": ["tax_id"]})
    if "name" in selected:
        candidate_rules.append({"kind": "name_similarity", "field": "name", "threshold": 0.82})
    if "phone" in selected:
        candidate_rules.append({"kind": "exact", "fields": ["phone"]})
    return {
        "entity_type": "Supplier",
        "identity_granularity": "legal_entity",
        "normalizers": ["unicode_nfkc", "trim", "casefold", "strip_punctuation"],
        "field_map": {field: field for field in selected},
        "strong_identifiers": ["tax_id"] if "tax_id" in selected else [],
        "conflict_constraints": [{"field": "tax_id", "action": "veto_auto_merge"}] if "tax_id" in selected else [],
        "candidate_rules": candidate_rules,
        "model_judgment_enabled": bool(model_provider_id),
        "model_provider_id": model_provider_id,
        "model_failure_policy": "REVIEW",
        "prompt_version": "entity-pair-choice-v1",
        "revision": 1,
    }
