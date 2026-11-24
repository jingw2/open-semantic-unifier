"""Candidate generation and conservative pair decisions from configured evidence."""
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from collections import defaultdict
from enum import StrEnum
from itertools import combinations
import re
import unicodedata
from typing import Any, Callable, Protocol

from app.models.unification import SourceRevision
from app.services.decision_models.contracts import DecisionError, DecisionOutcome, DecisionSuggestion, DecisionVerdict
from app.services.llm_client import JudgeResult
from app.services.unification.telemetry import start_span
from .schemes import MatchScheme


class MatchAction(StrEnum):
    AUTO_MATCH = "AUTO_MATCH"
    REVIEW = "REVIEW"
    DISTINCT = "DISTINCT"


@dataclass(frozen=True, slots=True)
class CannotLink:
    left_source_revision_id: str
    right_source_revision_id: str
    decision_id: str


class ModelJudge(Protocol):
    def __call__(
        self,
        left: SourceRevision,
        right: SourceRevision,
        scheme: MatchScheme,
        evidence_codes: tuple[str, ...],
        evidence: tuple[dict[str, Any], ...],
    ) -> dict[str, Any] | JudgeResult | DecisionOutcome: ...


@dataclass(frozen=True, slots=True)
class PairMatch:
    left_source_revision_id: str
    right_source_revision_id: str
    candidate: bool
    action: MatchAction
    score: float
    evidence_codes: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    recommendation: dict[str, Any] | None = None
    model_error: str | None = None
    decision: DecisionSuggestion | DecisionError | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    pairs: tuple[PairMatch, ...]
    considered_pair_count: int
    candidate_count: int


def _field_value(record: SourceRevision, scheme: MatchScheme, canonical_field: str) -> Any:
    source_field = scheme.field_map.get(canonical_field)
    return record.attributes.get(source_field) if source_field else None


def _normalize(value: Any, normalizers: list[str]) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if not normalized.strip():
        return None
    if "unicode_nfkc" in normalizers:
        normalized = unicodedata.normalize("NFKC", normalized)
    if "trim" in normalizers:
        normalized = normalized.strip()
    if not normalized:
        return None
    if "casefold" in normalizers:
        normalized = normalized.casefold()
    if "digits_only" in normalizers:
        normalized = "".join(char for char in normalized if char.isdigit())
    if "strip_punctuation" in normalizers:
        normalized = re.sub(r"[^\w\s]", "", normalized, flags=re.UNICODE)
    return normalized or None


def _candidate_by_rules(
    left: SourceRevision,
    right: SourceRevision,
    scheme: MatchScheme,
    splink_score: float | None,
) -> tuple[bool, list[dict[str, Any]]]:
    if scheme.golden_pair_discovery == "evaluate_all_pairs_for_this_small_fixture":
        return True, [{"rule": "all_pairs_small_fixture"}]

    evidence: list[dict[str, Any]] = []
    rules = scheme.candidate_rules
    if not rules:
        rules = [{"kind": "exact", "fields": list(scheme.field_map)}]
        name_field = next((field for field in scheme.field_map if field.casefold() in {"name", "legal_name", "product_name"}), None)
        if name_field:
            rules.append({"kind": "name_similarity", "field": name_field, "threshold": 0.82})

    for rule in rules:
        kind = rule.get("kind")
        fields = rule.get("fields", [])
        if kind == "splink_blocking" and splink_score is not None:
            evidence.append({"rule": "splink_blocking", "probability": round(splink_score, 6)})
        elif kind == "exact":
            for field in fields:
                left_value = _normalize(_field_value(left, scheme, field), scheme.normalizers)
                right_value = _normalize(_field_value(right, scheme, field), scheme.normalizers)
                if left_value is not None and left_value == right_value:
                    evidence.append({"rule": "exact", "field": field, "value": left_value})
        elif kind == "name_similarity":
            field = rule.get("field")
            left_value = _normalize(_field_value(left, scheme, field), scheme.normalizers) if field else None
            right_value = _normalize(_field_value(right, scheme, field), scheme.normalizers) if field else None
            if left_value is not None and right_value is not None:
                score = SequenceMatcher(a=left_value, b=right_value).ratio()
                if score >= float(rule.get("threshold", 0.82)):
                    evidence.append({"rule": "name_similarity", "field": field, "score": round(score, 4)})
    return bool(evidence), evidence


def _pair_match(
    left: SourceRevision,
    right: SourceRevision,
    scheme: MatchScheme,
    cannot_links: set[frozenset[str]],
    judge: ModelJudge | Callable[..., dict[str, Any]] | None,
    splink_score: float | None = None,
) -> PairMatch:
    candidate, candidate_evidence = _candidate_by_rules(left, right, scheme, splink_score)
    for field in scheme.strong_identifiers:
        left_value = _normalize(_field_value(left, scheme, field), scheme.normalizers)
        right_value = _normalize(_field_value(right, scheme, field), scheme.normalizers)
        if left_value is not None and left_value == right_value and not candidate:
            candidate = True
            candidate_evidence.append({"rule": "strong_identifier_block", "field": field})
    evidence_codes = ["CANDIDATE"] if candidate else ["NOT_CANDIDATE"]
    evidence: list[dict[str, Any]] = list(candidate_evidence)
    action = MatchAction.REVIEW

    if frozenset((left.id, right.id)) in cannot_links:
        action = MatchAction.DISTINCT
        evidence_codes.append("CANNOT_LINK")
        evidence.append({"rule": "human_cannot_link"})
    else:
        veto = False
        for constraint in scheme.conflict_constraints:
            field = constraint.get("field")
            left_value = _normalize(_field_value(left, scheme, field), scheme.normalizers)
            right_value = _normalize(_field_value(right, scheme, field), scheme.normalizers)
            if left_value is not None and right_value is not None and left_value != right_value:
                veto = True
                evidence_codes.append("CONFLICT_CONSTRAINT")
                evidence.append({"rule": constraint.get("action", "veto_auto_merge"), "field": field, "left": left_value, "right": right_value})

        strong_match = False
        strong_mismatch = False
        for field in scheme.strong_identifiers:
            left_value = _normalize(_field_value(left, scheme, field), scheme.normalizers)
            right_value = _normalize(_field_value(right, scheme, field), scheme.normalizers)
            if left_value is None or right_value is None:
                evidence_codes.append("STRONG_IDENTIFIER_MISSING")
                evidence.append({"rule": "strong_identifier_missing", "field": field})
            elif left_value == right_value:
                strong_match = True
                evidence_codes.append("STRONG_IDENTIFIER_MATCH")
                evidence.append({"rule": "strong_identifier_match", "field": field})
            else:
                strong_mismatch = True
                evidence_codes.append("STRONG_IDENTIFIER_CONFLICT")
                evidence.append({"rule": "strong_identifier_conflict", "field": field, "left": left_value, "right": right_value})

        if veto or strong_mismatch:
            action = MatchAction.DISTINCT
        elif strong_match:
            action = MatchAction.AUTO_MATCH
        elif candidate:
            evidence_codes.append("WEAK_EVIDENCE_ONLY")
        else:
            evidence_codes.append("INSUFFICIENT_EVIDENCE")

    recommendation = None
    model_error = None
    decision: DecisionSuggestion | DecisionError | None = None
    if candidate and judge is not None:
        try:
            with start_span("unification.model_recommendation", {"left_source_revision_id": left.id, "right_source_revision_id": right.id}):
                judged = judge(left, right, scheme, tuple(evidence_codes), tuple(evidence))
            if isinstance(judged, DecisionSuggestion):
                recommendation = {
                    "verdict": judged.verdict.value,
                    "is_same_entity": (
                        True if judged.verdict == DecisionVerdict.SAME
                        else False if judged.verdict == DecisionVerdict.DIFFERENT
                        else None
                    ),
                    "confidence": judged.confidence,
                    "distribution": {key.value: value for key, value in judged.distribution.items()} if judged.distribution else None,
                    "reason": judged.explanation,
                    "model": judged.model_version,
                    "provider_kind": judged.provider_kind,
                    "provider_key": judged.provider_key,
                    "prompt_version": judged.prompt_version,
                    "latency_ms": judged.latency_ms,
                    "usage": dict(judged.usage) if judged.usage else None,
                    "cost_usd": judged.cost_usd,
                    "trace_id": judged.trace_id,
                }
                decision = judged
            elif isinstance(judged, DecisionError):
                model_error = f"{judged.code}: {judged.message}"
                decision = judged
            elif isinstance(judged, JudgeResult):
                if judged.status == "SUGGESTION":
                    recommendation = {
                        "is_same_entity": judged.suggestion,
                        "confidence": judged.confidence,
                        "reason": judged.reason,
                        "model": judged.model,
                        "usage": judged.usage,
                        "cost_usd": judged.cost_usd,
                        "trace_id": judged.trace_id,
                    }
                elif judged.error is not None:
                    model_error = f"{judged.error.code}: {judged.error.message}"
            else:
                recommendation = judged
        except Exception as exc:  # Model failures remain separate from deterministic action.
            model_error = f"{type(exc).__name__}: {exc}"

    score = 1.0 if action in {MatchAction.AUTO_MATCH, MatchAction.DISTINCT} else (splink_score if splink_score is not None else (0.75 if candidate else 0.0))
    return PairMatch(left.id, right.id, candidate, action, score, tuple(evidence_codes), tuple(evidence), recommendation, model_error, decision)


def _splink_candidates(records: list[SourceRevision], scheme: MatchScheme) -> dict[frozenset[str], float]:
    rules = [rule for rule in scheme.candidate_rules if rule.get("kind") == "splink_blocking"]
    if not rules:
        return {}

    import pandas as pd
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    import splink.comparison_library as comparisons

    canonical_fields = list(dict.fromkeys(field for rule in rules for field in rule.get("fields", [])))
    canonical_fields = [field for field in canonical_fields if field in scheme.field_map]
    if not canonical_fields:
        return {}

    grouped: dict[str, list[SourceRevision]] = {}
    for record in records:
        grouped.setdefault(record.source_record.source_system, []).append(record)
    db_api = DuckDBAPI()
    input_datasets = []
    for index, (source_system, source_records) in enumerate(sorted(grouped.items())):
        rows = []
        for record in source_records:
            row = {"unique_id": record.id}
            row.update({field: _normalize(_field_value(record, scheme, field), scheme.normalizers) for field in canonical_fields})
            rows.append(row)
        input_datasets.append(db_api.register(pd.DataFrame(rows), dataset_display_name=f"source_{index}"))

    blocking_rules = [block_on(field) for field in canonical_fields]
    configured_comparisons = [comparisons.ExactMatch(field) for field in canonical_fields]
    settings = SettingsCreator(
        link_type="link_and_dedupe" if len(input_datasets) > 1 else "dedupe_only",
        blocking_rules_to_generate_predictions=blocking_rules,
        comparisons=configured_comparisons,
        retain_intermediate_calculation_columns=True,
    )
    linker = Linker(input_datasets if len(input_datasets) > 1 else input_datasets[0], settings)
    predictions = linker.inference.predict().as_pandas_dataframe()
    output: dict[frozenset[str], float] = {}
    for row in predictions.to_dict(orient="records"):
        left_id, right_id = row.get("unique_id_l"), row.get("unique_id_r")
        if left_id and right_id:
            output[frozenset((str(left_id), str(right_id)))] = float(row["match_probability"])
    return output


def _name_candidate_pairs(
    records: list[SourceRevision], scheme: MatchScheme, field: str, threshold: float
) -> set[tuple[int, int]]:
    by_ngram: dict[str, list[int]] = defaultdict(list)
    values: dict[int, str] = {}
    for index, record in enumerate(records):
        value = _normalize(_field_value(record, scheme, field), scheme.normalizers)
        if value is None:
            continue
        values[index] = value
        # SequenceMatcher cannot meet a positive threshold without a shared character.
        # Character postings keep the prefilter recall-safe for short CJK names.
        size = 1
        grams = {value[i:i + size] for i in range(max(1, len(value) - size + 1))}
        for gram in grams:
            by_ngram[gram].append(index)

    possible: set[tuple[int, int]] = set()
    for indexes in by_ngram.values():
        for left, right in combinations(indexes, 2):
            possible.add((left, right))
    return {
        (left, right) for left, right in possible
        if SequenceMatcher(a=values[left], b=values[right]).ratio() >= threshold
    }


def _candidate_pair_indexes(
    records: list[SourceRevision], scheme: MatchScheme, splink_scores: dict[frozenset[str], float]
) -> set[tuple[int, int]]:
    if scheme.golden_pair_discovery == "evaluate_all_pairs_for_this_small_fixture":
        return set(combinations(range(len(records)), 2))

    index_by_id = {record.id: index for index, record in enumerate(records)}
    candidates = {
        tuple(sorted((index_by_id[left_id], index_by_id[right_id])))
        for pair in splink_scores
        for left_id, right_id in [tuple(pair)]
        if left_id in index_by_id and right_id in index_by_id
    }
    rules = scheme.candidate_rules
    if not rules:
        rules = [{"kind": "exact", "fields": list(scheme.field_map)}]
        name_field = next((field for field in scheme.field_map if field.casefold() in {"name", "legal_name", "product_name"}), None)
        if name_field:
            rules.append({"kind": "name_similarity", "field": name_field, "threshold": 0.82})

    exact_fields = set(scheme.strong_identifiers)
    for rule in rules:
        if rule.get("kind") == "exact":
            exact_fields.update(rule.get("fields", []))
        elif rule.get("kind") == "name_similarity":
            field = rule.get("field")
            if field in scheme.field_map:
                candidates.update(_name_candidate_pairs(records, scheme, field, float(rule.get("threshold", 0.82))))
    for field in exact_fields:
        if field not in scheme.field_map:
            continue
        value_groups: dict[str, list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            value = _normalize(_field_value(record, scheme, field), scheme.normalizers)
            if value is not None:
                value_groups[value].append(index)
        for indexes in value_groups.values():
            candidates.update(combinations(indexes, 2))
    return candidates


def match_records(
    records: list[SourceRevision],
    scheme: MatchScheme,
    constraints: list[CannotLink],
    judge: ModelJudge | Callable[..., dict[str, Any]] | None = None,
) -> MatchResult:
    """Evaluate pairs without consulting labels or directly mutating entity state."""
    cannot_links = {frozenset((item.left_source_revision_id, item.right_source_revision_id)) for item in constraints}
    with start_span("unification.candidate_generation", {"input_record_count": len(records), "scheme_revision": scheme.revision}):
        splink_scores = _splink_candidates(records, scheme)
        pair_indexes = _candidate_pair_indexes(records, scheme, splink_scores)
    index_by_id = {record.id: index for index, record in enumerate(records)}
    for constraint in constraints:
        left = index_by_id.get(constraint.left_source_revision_id)
        right = index_by_id.get(constraint.right_source_revision_id)
        if left is not None and right is not None:
            pair_indexes.add(tuple(sorted((left, right))))
    with start_span("unification.pair_rules", {"candidate_pair_count": len(pair_indexes), "scheme_revision": scheme.revision}):
        evaluated = tuple(
            _pair_match(records[left_index], records[right_index], scheme, cannot_links, judge,
                        splink_scores.get(frozenset((records[left_index].id, records[right_index].id))))
            for left_index, right_index in sorted(pair_indexes)
        )
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    initial_entities: dict[str, int] = {}
    for index, record in enumerate(records):
        entity_id = getattr(record.source_record, "current_entity_id", None)
        if entity_id:
            if entity_id in initial_entities:
                union(index, initial_entities[entity_id])
            else:
                initial_entities[entity_id] = index

    forbidden = [pair for pair in evaluated if pair.action == MatchAction.DISTINCT]
    revised: list[PairMatch] = []
    for pair in evaluated:
        left_index = index_by_id[pair.left_source_revision_id]
        right_index = index_by_id[pair.right_source_revision_id]
        left_root, right_root = find(left_index), find(right_index)
        if pair.action != MatchAction.AUTO_MATCH or left_root == right_root:
            revised.append(pair)
            continue

        crosses_forbidden = any(
            {find(index_by_id[item.left_source_revision_id]), find(index_by_id[item.right_source_revision_id])}
            == {left_root, right_root}
            for item in forbidden
        )
        proposed = [
            index for index in range(len(records))
            if find(index) in {left_root, right_root}
        ]
        canonical_fields = set(scheme.strong_identifiers)
        canonical_fields.update(item.get("field") for item in scheme.conflict_constraints)
        scheme_conflict = False
        for field in canonical_fields:
            values = {
                value for index in proposed
                if (value := _normalize(_field_value(records[index], scheme, field), scheme.normalizers)) is not None
            }
            if len(values) > 1:
                scheme_conflict = True
                break
        if crosses_forbidden or scheme_conflict:
            revised.append(replace(
                pair,
                action=MatchAction.DISTINCT,
                evidence_codes=pair.evidence_codes + ("TRANSITIVE_CONFLICT",),
                evidence=pair.evidence + ({"rule": "transitive_merge_veto", "reason": "cannot_link" if crosses_forbidden else "scheme_conflict"},),
            ))
            forbidden.append(revised[-1])
        else:
            union(left_index, right_index)
            revised.append(pair)
    pairs = tuple(revised)
    return MatchResult(pairs, len(records) * (len(records) - 1) // 2, sum(pair.candidate for pair in pairs))
