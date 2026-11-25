"""Plain-language review explanations and auto-separated pair summaries."""
import re
import unicodedata
from typing import Any, Iterable, Mapping

AUTO_SEPARATED_CODES = frozenset({
    "CONFLICT_CONSTRAINT",
    "STRONG_IDENTIFIER_CONFLICT",
    "CANNOT_LINK",
    "TRANSITIVE_CONFLICT",
})

CONSEQUENCE = "确认同一实体后，两条来源记录会合并到一个实体；保持分离则保留为两个实体。"


def _normalize(value: Any, normalizers: Iterable[str]) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    if "unicode_nfkc" in normalizers:
        text = unicodedata.normalize("NFKC", text)
    if "trim" in normalizers:
        text = text.strip()
    if "casefold" in normalizers:
        text = text.casefold()
    if "digits_only" in normalizers:
        text = "".join(char for char in text if char.isdigit())
    if "strip_punctuation" in normalizers:
        text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return text or None


def compare_attributes(left_attributes: Mapping[str, Any], right_attributes: Mapping[str, Any], scheme) -> list[dict]:
    """Compare the scheme's canonical fields and label each as MATCH, DIFFERENT, or MISSING."""
    field_map = getattr(scheme, "field_map", {}) or {}
    normalizers = getattr(scheme, "normalizers", []) or []
    rows = []
    for canonical, source in field_map.items():
        left_raw = left_attributes.get(source)
        right_raw = right_attributes.get(source)
        left_normalized = _normalize(left_raw, normalizers)
        right_normalized = _normalize(right_raw, normalizers)
        if left_normalized is None or right_normalized is None:
            status = "MISSING"
        elif left_normalized == right_normalized:
            status = "MATCH"
        else:
            status = "DIFFERENT"
        rows.append({"field": canonical, "left": left_raw, "right": right_raw, "status": status})
    return rows


def record_payload(revision, scheme) -> dict:
    source_record = revision.source_record
    field_map = getattr(scheme, "field_map", {}) or {}
    attributes = revision.attributes or {}
    return {
        "record_ref": f"{source_record.source_system}:{source_record.dataset}:{source_record.source_record_id}",
        "source_system": source_record.source_system,
        "dataset": source_record.dataset,
        "source_record_id": source_record.source_record_id,
        "attributes": {canonical: attributes.get(source) for canonical, source in field_map.items()},
    }


def explain_review(evidence_codes: Iterable[str]) -> str:
    codes = set(evidence_codes or ())
    if codes & {"STRONG_IDENTIFIER_CONFLICT", "CONFLICT_CONSTRAINT"}:
        return "强标识不一致，规则要求保持分离，但名称等证据相似，需要你确认。"
    if "TRANSITIVE_CONFLICT" in codes:
        return "传递冲突：这两条记录属于不同的已确认实体，不能合并。"
    if "CANNOT_LINK" in codes:
        return "此前有人标记这两条记录不能合并。"
    if codes & {"STRONG_IDENTIFIER_MISSING", "WEAK_EVIDENCE_ONLY"}:
        return "名称等弱证据相似，但缺少可核对的强标识，无法自动确认是否同一实体。"
    return "规则无法自动判定，需要你根据字段对比确认。"


def suggest_action(verdict: str | None, evidence_codes: Iterable[str]) -> str:
    """Only a SAME verdict backed by a matching strong identifier suggests a merge."""
    if verdict == "SAME" and "STRONG_IDENTIFIER_MATCH" in set(evidence_codes or ()):
        return "建议合并"
    return "建议分离"


def auto_separated_items(run_result: dict, revisions_by_id: dict, scheme, cap: int = 20) -> list[dict]:
    items = []
    for pair in run_result.get("pairs") or []:
        if pair.get("action") != "DISTINCT":
            continue
        codes = tuple(pair.get("evidence_codes") or ())
        if not (set(codes) & AUTO_SEPARATED_CODES):
            continue
        left = revisions_by_id.get(pair.get("left_source_revision_id"))
        right = revisions_by_id.get(pair.get("right_source_revision_id"))
        if left is None or right is None:
            continue
        left_payload = record_payload(left, scheme)
        right_payload = record_payload(right, scheme)
        items.append({
            **pair,
            "left": left_payload,
            "right": right_payload,
            "comparison": compare_attributes(left_payload["attributes"], right_payload["attributes"], scheme),
            "why": explain_review(codes),
        })
    items.sort(key=lambda item: (item["left"]["record_ref"], item["right"]["record_ref"]))
    return items[:cap]
