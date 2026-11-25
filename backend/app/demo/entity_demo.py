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


def demo_scheme(model_provider_id: str | None = None) -> dict:
    return {
        "entity_type": "Supplier",
        "identity_granularity": "legal_entity",
        "normalizers": ["unicode_nfkc", "trim", "casefold", "strip_punctuation"],
        "field_map": {"name": "name", "tax_id": "tax_id", "phone": "phone"},
        "strong_identifiers": ["tax_id"],
        "conflict_constraints": [{"field": "tax_id", "action": "veto_auto_merge"}],
        "candidate_rules": [
            {"kind": "exact", "fields": ["tax_id"]},
            {"kind": "name_similarity", "field": "name", "threshold": 0.82},
        ],
        "model_judgment_enabled": bool(model_provider_id),
        "model_provider_id": model_provider_id,
        "model_failure_policy": "REVIEW",
        "prompt_version": "entity-pair-choice-v1",
        "revision": 1,
    }
