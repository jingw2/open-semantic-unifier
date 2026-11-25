"""Plain-language review payloads and auto-separated explanations."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_unification_public_api import public_api  # noqa: F401

from app.models.unification import (
    SourceRecord,
    SourceRevision,
    UnifiedEntity,
    UnificationRun,
    UnificationSchemeVersion,
)
from app.services.unification.reviews import (
    auto_separated_items,
    compare_attributes,
    explain_review,
    suggest_action,
)
from app.services.unification.schemes import MatchScheme


def test_missing_strong_identifier_explains_why_review_is_needed():
    assert "缺少可核对的强标识" in explain_review(("CANDIDATE", "STRONG_IDENTIFIER_MISSING", "WEAK_EVIDENCE_ONLY"))


def test_conflict_explains_why_pair_was_separated():
    assert "强标识不一致" in explain_review(("CANDIDATE", "STRONG_IDENTIFIER_CONFLICT"))


def test_comparison_marks_match_different_and_missing():
    scheme = type("Scheme", (), {"field_map": {"name": "name", "tax_id": "tax_id"}, "normalizers": []})()
    rows = compare_attributes({"name": "Acme Ltd", "tax_id": None}, {"name": "acme ltd", "tax_id": "T-1"}, scheme)
    assert [(row["field"], row["status"]) for row in rows] == [("name", "DIFFERENT"), ("tax_id", "MISSING")]


def test_suggestion_is_never_a_merge_for_unknown():
    assert suggest_action("UNKNOWN", ("CANDIDATE",)) == "建议分离"
    assert suggest_action("SAME", ("CANDIDATE", "WEAK_EVIDENCE_ONLY", "STRONG_IDENTIFIER_MISSING")) == "建议分离"
    assert suggest_action("SAME", ("CANDIDATE", "STRONG_IDENTIFIER_MATCH")) == "建议合并"
    assert suggest_action("DIFFERENT", ("CANDIDATE",)) == "建议分离"


def test_auto_separated_includes_human_cannot_link():
    class Revision:
        def __init__(self, revision_id):
            self.id = revision_id
            self.attributes = {}
            self.source_record = type("SourceRecord", (), {
                "source_system": "s", "dataset": "d", "source_record_id": revision_id,
            })()

    scheme = type("Scheme", (), {"field_map": {}, "normalizers": []})()
    pair = {"action": "DISTINCT", "evidence_codes": ["CANDIDATE", "CANNOT_LINK"], "left_source_revision_id": "L", "right_source_revision_id": "R"}
    revisions = {"L": Revision("L"), "R": Revision("R")}
    items = auto_separated_items({"pairs": [pair]}, revisions, scheme)
    assert len(items) == 1 and "CANNOT_LINK" in items[0]["evidence_codes"]


def test_auto_separated_is_empty_safe_and_capped():
    assert auto_separated_items({}, {}, None) == []

    class Revision:
        def __init__(self, revision_id):
            self.id = revision_id
            self.attributes = {}
            self.source_record = type("SourceRecord", (), {
                "source_system": "s", "dataset": "d", "source_record_id": revision_id,
            })()

    scheme = type("Scheme", (), {"field_map": {}, "normalizers": []})()
    pairs = [
        {"action": "DISTINCT", "evidence_codes": ["CONFLICT_CONSTRAINT"], "left_source_revision_id": f"L{i}", "right_source_revision_id": f"R{i}"}
        for i in range(25)
    ]
    revisions = {f"L{i}": Revision(f"L{i}") for i in range(25)} | {f"R{i}": Revision(f"R{i}") for i in range(25)}
    assert len(auto_separated_items({"pairs": pairs}, revisions, scheme, cap=20)) == 20


def test_review_endpoint_explains_pairs_and_loads_distinct_revisions(public_api):
    client, maker, _identity = public_api
    scheme = MatchScheme(
        entity_type="Supplier", identity_granularity="legal_entity",
        field_map={"name": "name", "tax_id": "tax_id"}, normalizers=[],
    )
    with maker() as db:
        for suffix in ("review-left", "review-right", "distinct-left", "distinct-right"):
            db.add(UnifiedEntity(id=f"ent-{suffix}", project_id="project-one", entity_type="Supplier"))
        db.flush()
        for suffix in ("review-left", "review-right", "distinct-left", "distinct-right"):
            db.add(SourceRecord(
                id=f"rec-{suffix}", project_id="project-one", source_system="erp",
                dataset="suppliers", source_record_id=suffix,
                current_entity_id=f"ent-{suffix}", current_revision_id=None,
            ))
        db.flush()
        attributes = {
            "review-left": {"name": "Acme Ltd", "tax_id": None},
            "review-right": {"name": "ACME, Ltd.", "tax_id": "T-1"},
            "distinct-left": {"name": "Cedar Ltd", "tax_id": "T-2"},
            "distinct-right": {"name": "Cedar Ltd", "tax_id": "T-3"},
        }
        for suffix, attrs in attributes.items():
            db.add(SourceRevision(
                id=f"rev-{suffix}", source_record_id=f"rec-{suffix}", revision_no=1,
                input_version="review-v1", attributes=attrs, content_hash=f"hash-{suffix}",
            ))
        db.flush()
        for suffix in attributes:
            db.get(SourceRecord, f"rec-{suffix}").current_revision_id = f"rev-{suffix}"
        db.add(UnificationSchemeVersion(
            id="scheme-review", project_id="project-one", scheme_key="supplier",
            revision=1, scheme_json=scheme.model_dump(),
        ))
        db.add(UnificationRun(
            id="run-review", project_id="project-one", input_version="review-v1",
            input_manifest_json=[{"source_record_id": f"rec-{suffix}", "source_revision_id": f"rev-{suffix}", "entity_id": f"ent-{suffix}"} for suffix in attributes],
            identity_revision=1, scheme_revision="scheme-review", idempotency_key="review-test",
            payload_hash="hash", status="SUCCEEDED", attempt_count=1,
            result_json={"pairs": [
                {
                    "left_source_revision_id": "rev-review-left", "right_source_revision_id": "rev-review-right",
                    "candidate": True, "action": "REVIEW", "score": 0.75,
                    "evidence_codes": ["CANDIDATE", "STRONG_IDENTIFIER_MISSING", "WEAK_EVIDENCE_ONLY"],
                    "evidence": [], "recommendation": {"verdict": "UNKNOWN"}, "model_error": None,
                },
                {
                    "left_source_revision_id": "rev-distinct-left", "right_source_revision_id": "rev-distinct-right",
                    "candidate": True, "action": "DISTINCT", "score": 1.0,
                    "evidence_codes": ["CANDIDATE", "CANNOT_LINK"],
                    "evidence": [], "recommendation": None, "model_error": None,
                },
            ]},
        ))
        db.commit()

    response = client.get("/api/v1/projects/project-one/unification/reviews", params={"run_id": "run-review"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"][0]["left_source_revision_id"] == "rev-review-left"
    assert payload["items"][0]["right_source_revision_id"] == "rev-review-right"
    assert "缺少可核对的强标识" in payload["items"][0]["why"]
    assert payload["items"][0]["suggestion"] == "建议分离"
    assert [(row["field"], row["status"]) for row in payload["items"][0]["comparison"]] == [
        ("name", "DIFFERENT"), ("tax_id", "MISSING"),
    ]
    assert payload["auto_separated_total"] == 1
    assert payload["auto_separated"][0]["left"]["attributes"]["name"] == "Cedar Ltd"
    assert "不能合并" in payload["auto_separated"][0]["why"]
