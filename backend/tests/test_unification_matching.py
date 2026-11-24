"""Behavioral tests for evidence-based and safety-first matching."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.unification.schemes import MatchScheme
from app.services.unification.matching import CannotLink, MatchAction, match_records
from app.services.llm_client import JudgeResult


def record(record_id, source, attributes):
    return SimpleNamespace(id=record_id, source_record=SimpleNamespace(source_system=source), attributes=attributes)


def test_null_identifier_never_matches():
    scheme = MatchScheme.model_validate({
        "entity_type": "person", "identity_granularity": "natural_person",
        "field_map": {"name": "name", "person_ref": "person_ref"},
        "strong_identifiers": ["person_ref"], "normalizers": ["unicode_nfkc", "trim"],
    })
    result = match_records([
        record("left", "erp", {"name": "Same name", "person_ref": None}),
        record("right", "secondary", {"name": "Same name", "person_ref": ""}),
    ], scheme, constraints=[])
    pair = result.pairs[0]
    assert pair.candidate
    assert pair.action == MatchAction.REVIEW
    assert "STRONG_IDENTIFIER_MISSING" in pair.evidence_codes


def test_spec_conflict_vetoes_identifier_and_model_suggestion():
    scheme = MatchScheme.model_validate({
        "entity_type": "product", "identity_granularity": "sellable_variant",
        "field_map": {"name": "name", "gtin": "gtin", "voltage": "voltage"},
        "strong_identifiers": ["gtin"],
        "conflict_constraints": [{"field": "voltage", "action": "veto_auto_merge"}],
        "normalizers": ["unicode_nfkc", "trim"],
    })
    result = match_records([
        record("left", "erp", {"name": "Widget", "gtin": "TEST-001", "voltage": "110 V"}),
        record("right", "secondary", {"name": "Widget", "gtin": "TEST-001", "voltage": "220 V"}),
    ], scheme, constraints=[], judge=lambda *_: JudgeResult(
        status="SUGGESTION", suggestion=True, confidence=1.0, reason="model says same", model="test-model",
    ))
    pair = result.pairs[0]
    assert pair.action == MatchAction.DISTINCT
    assert pair.recommendation["is_same_entity"] is True
    assert pair.action == MatchAction.DISTINCT
    assert "CONFLICT_CONSTRAINT" in pair.evidence_codes


def test_shared_contact_is_review():
    scheme = MatchScheme.model_validate({
        "entity_type": "person", "identity_granularity": "natural_person",
        "field_map": {"name": "name", "phone": "phone", "person_ref": "person_ref"},
        "strong_identifiers": ["person_ref"],
        "candidate_rules": [{"kind": "splink_blocking", "fields": ["phone"]}],
    })
    result = match_records([
        record("left", "erp", {"name": "Person A", "phone": "TEST-HOME"}),
        record("right", "secondary", {"name": "Person B", "phone": "TEST-HOME"}),
    ], scheme, constraints=[])
    pair = result.pairs[0]
    assert pair.candidate
    assert pair.action == MatchAction.REVIEW
    assert "WEAK_EVIDENCE_ONLY" in pair.evidence_codes


def test_splink_blocking_uses_configured_normalizers():
    scheme = MatchScheme.model_validate({
        "entity_type": "person", "identity_granularity": "natural_person",
        "field_map": {"name": "name", "phone": "phone", "person_ref": "person_ref"},
        "strong_identifiers": ["person_ref"], "normalizers": ["unicode_nfkc", "trim"],
        "candidate_rules": [{"kind": "splink_blocking", "fields": ["phone"]}],
    })
    result = match_records([
        record("left", "erp", {"name": "Person A", "phone": " 555-0100 ", "person_ref": None}),
        record("right", "secondary", {"name": "Person B", "phone": "555-0100", "person_ref": None}),
    ], scheme, constraints=[])
    assert result.pairs[0].candidate
    assert "WEAK_EVIDENCE_ONLY" in result.pairs[0].evidence_codes


def test_unknown_does_not_auto_merge():
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"},
        "strong_identifiers": ["tax_id"],
        "candidate_rules": [{"kind": "name_similarity", "field": "name", "threshold": 0.7}],
    })
    result = match_records([
        record("left", "erp", {"name": "Acme Industrial", "tax_id": None}),
        record("right", "secondary", {"name": "Acme Industrials", "tax_id": ""}),
    ], scheme, constraints=[])
    pair = result.pairs[0]
    assert pair.action == MatchAction.REVIEW
    assert pair.action != MatchAction.AUTO_MATCH


def test_model_failure_is_reported_without_changing_deterministic_action():
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
        "candidate_rules": [{"kind": "name_similarity", "field": "name", "threshold": 0.5}],
    })
    def failing_judge(*_):
        raise TimeoutError("judge timed out")
    result = match_records([
        record("left", "erp", {"name": "Acme", "tax_id": None}),
        record("right", "secondary", {"name": "Acme", "tax_id": None}),
    ], scheme, constraints=[], judge=failing_judge)
    assert result.pairs[0].action == MatchAction.REVIEW
    assert result.pairs[0].model_error == "TimeoutError: judge timed out"
    assert result.pairs[0].recommendation is None


def test_cannot_link_overrides_strong_identifier_equality():
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
    })
    records = [record("left", "erp", {"tax_id": "TEST-001"}), record("right", "secondary", {"tax_id": "TEST-001"})]
    result = match_records(records, scheme, constraints=[CannotLink("left", "right", "decision-7")])
    assert result.pairs[0].action == MatchAction.DISTINCT
    assert "CANNOT_LINK" in result.pairs[0].evidence_codes


def test_production_rules_return_only_generated_candidate_pairs():
    scheme = MatchScheme.model_validate({
        "entity_type": "organization", "identity_granularity": "legal_entity",
        "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"],
        "candidate_rules": [{"kind": "exact", "fields": ["name"]}],
    })
    result = match_records([
        record("a", "erp", {"name": "Acme", "tax_id": "1"}),
        record("b", "secondary", {"name": "Acme", "tax_id": "2"}),
        record("c", "secondary", {"name": "Other", "tax_id": "3"}),
    ], scheme, constraints=[])
    assert result.considered_pair_count == 3
    assert len(result.pairs) == 1
    assert result.pairs[0].candidate


def test_short_unicode_name_similarity_is_not_lost_by_candidate_blocking():
    scheme = MatchScheme.model_validate({
        "entity_type": "person", "identity_granularity": "natural_person",
        "field_map": {"name": "name", "person_ref": "person_ref"}, "strong_identifiers": ["person_ref"],
        "normalizers": ["unicode_nfkc", "trim"],
        "candidate_rules": [{"kind": "name_similarity", "field": "name", "threshold": 0.6}],
    })
    result = match_records([
        record("left", "erp", {"name": "王小明", "person_ref": None}),
        record("right", "secondary", {"name": "王晓明", "person_ref": None}),
    ], scheme, constraints=[])
    assert len(result.pairs) == 1
    assert result.pairs[0].candidate


def test_zero_name_similarity_threshold_is_rejected():
    try:
        MatchScheme.model_validate({
            "entity_type": "person", "identity_granularity": "natural_person",
            "field_map": {"name": "name"},
            "candidate_rules": [{"kind": "name_similarity", "field": "name", "threshold": 0}],
        })
    except ValueError as error:
        assert "greater than 0" in str(error)
    else:
        raise AssertionError("zero threshold must not be accepted")


def test_transitive_merge_is_vetoed_by_component_conflict():
    scheme = MatchScheme.model_validate({
        "entity_type": "supplier", "identity_granularity": "legal_entity",
        "field_map": {"tax_id": "tax_id", "voltage": "voltage"},
        "strong_identifiers": ["tax_id"],
        "conflict_constraints": [{"field": "voltage", "action": "veto_auto_merge"}],
        "golden_pair_discovery": "evaluate_all_pairs_for_this_small_fixture",
    })
    result = match_records([
        record("a", "erp", {"tax_id": "T-1", "voltage": "110"}),
        record("b", "crm", {"tax_id": "T-1"}),
        record("c", "registry", {"tax_id": "T-1", "voltage": "220"}),
    ], scheme, constraints=[])
    pairs = {(pair.left_source_revision_id, pair.right_source_revision_id): pair for pair in result.pairs}
    assert pairs[("a", "b")].action == MatchAction.AUTO_MATCH
    assert pairs[("a", "c")].action == MatchAction.DISTINCT
    assert pairs[("b", "c")].action == MatchAction.DISTINCT
    assert "TRANSITIVE_CONFLICT" in pairs[("b", "c")].evidence_codes


def test_run_cannot_link_blocks_transitive_auto_match():
    scheme = MatchScheme.model_validate({
        "entity_type": "person", "identity_granularity": "natural_person",
        "field_map": {"person_ref": "person_ref"}, "strong_identifiers": ["person_ref"],
        "golden_pair_discovery": "evaluate_all_pairs_for_this_small_fixture",
    })
    result = match_records([
        record("a", "erp", {"person_ref": "P-1"}),
        record("b", "crm", {"person_ref": "P-1"}),
        record("c", "registry", {"person_ref": "P-1"}),
    ], scheme, constraints=[CannotLink("a", "c", "human-decision")])
    pair = next(pair for pair in result.pairs if (pair.left_source_revision_id, pair.right_source_revision_id) == ("b", "c"))
    assert pair.action == MatchAction.DISTINCT
    assert "TRANSITIVE_CONFLICT" in pair.evidence_codes
