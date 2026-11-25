#!/usr/bin/env python3
"""Validate the frozen holdout and evaluate decision models against it.

The holdout truth never reaches a provider. Input records and the scheme are
the only data sent to the matcher and the adapter; expected labels are read
after the run, for scoring only.
"""
import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.db import SessionLocal
from app.core.security import decrypt_secret
from app.models.entities import LLMProvider
from app.services.decision_models.contracts import DecisionRequest
from app.services.decision_models.providers import DECISION_PROVIDER_KINDS, adapter_for_kind
from app.services.llm_client import ProviderConfig
from app.services.unification.matching import match_records
from app.services.unification.schemes import MatchScheme
from app.services.unification.telemetry import TraceConfig, configure_tracing, shutdown_tracing

HOLDOUT = ROOT / "tests" / "holdout" / "v1"
GOLDEN = ROOT / "tests" / "golden" / "v1"
DEFAULT_SEND_FIELDS = ("name", "tax_id", "email", "gtin", "brand", "model", "voltage", "pack_count")
PROVIDER_DEFAULTS = {
    "typesafe_jev": ("https://api.typesafe.ai", "jev-latest", "TYPESAFE_API_KEY"),
    "perplexity_decisions": ("https://api.perplexity.ai", "pplx-decider-flash-latest", "PERPLEXITY_API_KEY"),
    "openai_compatible": ("", "", "OPENAI_API_KEY"),
}
LABEL_KEYS = {"truth", "expected_action", "case_id", "required_candidate", "reason"}


@dataclass(frozen=True)
class HoldoutRecord:
    id: str
    attributes: dict
    source_record: object = field(default_factory=lambda: SimpleNamespace(current_entity_id=None))


@dataclass(frozen=True)
class HoldoutDomain:
    name: str
    scheme: MatchScheme
    records: tuple[HoldoutRecord, ...]
    cases: tuple[dict, ...]


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_holdout(root: Path = HOLDOUT) -> dict:
    manifest = read(root / "manifest.json")
    actual = {str(path.relative_to(root)) for path in root.rglob("*.json") if path.name != "manifest.json"}
    if actual != set(manifest["files"]):
        raise ValueError("manifest file list differs from holdout files")
    for name, digest in manifest["files"].items():
        if sha256(root / name) != digest:
            raise ValueError(f"checksum mismatch: {name}")
    counts = {"records": 0, "pairs": 0}
    case_ids = set()
    for domain in manifest["domains"]:
        records = read(root / domain / "records.json")
        refs = {record["record_ref"] for record in records}
        keys = {(record["source_system"], record["dataset"], record["source_record_id"]) for record in records}
        if len(refs) != len(records) or len(keys) != len(records):
            raise ValueError(f"duplicate source identity: {domain}")
        scheme = read(root / domain / "scheme.json")
        if scheme["entity_type"] != domain:
            raise ValueError(f"scheme entity type mismatch: {domain}")
        cases = read(root / domain / "expected.json")
        pairs = set()
        for case in cases:
            key = frozenset((case["left"], case["right"]))
            if len(key) != 2 or not key <= refs or key in pairs or case["case_id"] in case_ids:
                raise ValueError(f"duplicate or invalid case: {case}")
            pairs.add(key)
            case_ids.add(case["case_id"])
            if case["truth"] not in {"SAME", "DIFFERENT", "UNRESOLVED"}:
                raise ValueError("invalid truth")
            if case["expected_action"] not in {"AUTO_MATCH", "DISTINCT", "REVIEW"}:
                raise ValueError("invalid action")
            if case["expected_action"] == "AUTO_MATCH" and (case["truth"] != "SAME" or not case["required_candidate"]):
                raise ValueError("unsafe automatic match oracle")
        counts["records"] += len(records)
        counts["pairs"] += len(pairs)
        for name in ("records.json", "scheme.json"):
            _assert_no_labels(read(root / domain / name), f"{domain}/{name}")
    for key, manifest_key in (("records", "record_count"), ("pairs", "pair_case_count")):
        if counts[key] != manifest[manifest_key]:
            raise ValueError(f"incorrect count: {key}")
    return manifest


def _assert_no_labels(value, where: str) -> None:
    if isinstance(value, dict):
        leaked = LABEL_KEYS & set(value)
        if leaked:
            raise ValueError(f"label keys in input {where}: {sorted(leaked)}")
        for item in value.values():
            _assert_no_labels(item, where)
    elif isinstance(value, list):
        for item in value:
            _assert_no_labels(item, where)


def _normalized_values(value) -> set[str]:
    values = set()
    if isinstance(value, dict):
        for item in value.values():
            values |= _normalized_values(item)
    elif isinstance(value, list):
        for item in value:
            values |= _normalized_values(item)
    elif isinstance(value, str) and len(value.strip()) >= 3:
        values.add(value.strip().casefold())
    return values


def assert_no_golden_overlap(root: Path = HOLDOUT, golden: Path = GOLDEN) -> None:
    if not golden.exists():
        return
    golden_values: set[str] = set()
    golden_refs: set[str] = set()
    golden_keys: set[tuple[str, str, str]] = set()
    golden_cases: set[str] = set()
    for domain in read(golden / "manifest.json")["domains"]:
        records = read(golden / domain / "records.json")
        for record in records:
            golden_refs.add(record["record_ref"])
            golden_keys.add((record["source_system"], record["dataset"], record["source_record_id"]))
            golden_values |= _normalized_values(record["attributes"])
        for case in read(golden / domain / "expected.json"):
            golden_cases.add(case["case_id"])
    for domain in read(root / "manifest.json")["domains"]:
        for record in read(root / domain / "records.json"):
            if record["record_ref"] in golden_refs:
                raise ValueError(f"holdout record ref overlaps golden: {record['record_ref']}")
            key = (record["source_system"], record["dataset"], record["source_record_id"])
            if key in golden_keys:
                raise ValueError(f"holdout source key overlaps golden: {key}")
            overlap = _normalized_values(record["attributes"]) & golden_values
            if overlap:
                raise ValueError(f"holdout attribute value overlaps golden: {sorted(overlap)}")
        for case in read(root / domain / "expected.json"):
            if case["case_id"] in golden_cases:
                raise ValueError(f"holdout case id overlaps golden: {case['case_id']}")


def load_holdout(root: Path = HOLDOUT) -> dict[str, HoldoutDomain]:
    manifest = validate_holdout(root)
    assert_no_golden_overlap(root)
    domains = {}
    for domain in manifest["domains"]:
        records = tuple(
            HoldoutRecord(id=record["record_ref"], attributes=dict(record["attributes"]))
            for record in read(root / domain / "records.json")
        )
        scheme = MatchScheme.model_validate(read(root / domain / "scheme.json"))
        cases = tuple(read(root / domain / "expected.json"))
        domains[domain] = HoldoutDomain(domain, scheme, records, cases)
    return domains


def _judge(adapter, config: ProviderConfig, send_fields: tuple[str, ...], prompt_version: str):
    def judge(left, right, scheme, _evidence_codes, evidence):
        return adapter.invoke(
            _request(left, right, scheme, evidence, send_fields, prompt_version),
            config,
        )
    return judge


def _request(left, right, scheme, evidence, send_fields, prompt_version):
    return DecisionRequest(
        project_id="holdout-evaluation",
        run_id=f"holdout-{scheme.entity_type}",
        input_version="holdout-v1",
        scheme_revision=str(scheme.revision),
        prompt_version=prompt_version,
        entity_type=scheme.entity_type,
        identity_granularity=scheme.identity_granularity,
        left=left.attributes,
        right=right.attributes,
        field_names=send_fields,
        evidence=tuple(evidence),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def score_domain(domain: HoldoutDomain, result) -> dict:
    pair_by_key = {
        frozenset((pair.left_source_revision_id, pair.right_source_revision_id)): pair
        for pair in result.pairs
    }
    counts = Counter()
    action_counts = Counter()
    verdict_counts = Counter()
    latency = []
    costs = []
    required = 0
    recalled = 0
    for case in domain.cases:
        key = frozenset((case["left"], case["right"]))
        pair = pair_by_key.get(key)
        counts["cases"] += 1
        if case["required_candidate"]:
            required += 1
            if pair is not None:
                recalled += 1
        if pair is None:
            action_counts["NOT_EVALUATED"] += 1
            counts["review_volume"] += 1
            continue
        action = pair.action.value
        action_counts[action] += 1
        if action == "REVIEW":
            counts["review_volume"] += 1
        if action == "AUTO_MATCH" and case["truth"] == "DIFFERENT":
            counts["false_merges"] += 1
        if case["truth"] == "UNRESOLVED":
            counts["unresolved_pairs"] += 1
        if pair.model_error:
            counts["model_errors"] += 1
            verdict_counts["ERROR"] += 1
        if pair.recommendation:
            counts["model_calls"] += 1
            latency.append(float(pair.recommendation.get("latency_ms") or 0.0))
            cost = pair.recommendation.get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                costs.append(float(cost))
        verdict = (pair.recommendation or {}).get("verdict")
        if verdict:
            verdict_counts[verdict] += 1
        if case["truth"] not in {"SAME", "DIFFERENT"}:
            continue
        counts["labeled_pairs"] += 1
        if verdict is None:
            continue
        counts["judged_pairs"] += 1
        if case["truth"] == "SAME":
            counts["same_pairs"] += 1
            if verdict == "SAME":
                counts["same_correct"] += 1
            elif verdict == "UNKNOWN":
                counts["unknown"] += 1
        else:
            counts["different_pairs"] += 1
            if verdict == "DIFFERENT":
                counts["different_correct"] += 1
            elif verdict == "SAME":
                counts["false_same"] += 1
            elif verdict == "UNKNOWN":
                counts["unknown"] += 1
    return {
        "cases": counts["cases"],
        "candidate_recall": _rate(recalled, required),
        "required_candidates": required,
        "candidates_recalled": recalled,
        "action_counts": dict(sorted(action_counts.items())),
        "review_volume": counts["review_volume"],
        "false_merges": counts["false_merges"],
        "labeled_pairs": counts["labeled_pairs"],
        "unresolved_pairs": counts["unresolved_pairs"],
        "model_calls": counts["model_calls"],
        "model_errors": counts["model_errors"],
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "same_pairs": counts["same_pairs"],
        "same_correct": counts["same_correct"],
        "same_recall": _rate(counts["same_correct"], counts["same_pairs"]),
        "different_pairs": counts["different_pairs"],
        "different_correct": counts["different_correct"],
        "different_recall": _rate(counts["different_correct"], counts["different_pairs"]),
        "false_same": counts["false_same"],
        "false_same_rate": _rate(counts["false_same"], counts["different_pairs"]),
        "unknown": counts["unknown"],
        "unknown_rate": _rate(counts["unknown"], counts["judged_pairs"]),
        "judged_pairs": counts["judged_pairs"],
        "suggestion_accuracy": _rate(counts["same_correct"] + counts["different_correct"], counts["judged_pairs"]),
        "latency_ms_mean": round(sum(latency) / len(latency), 2) if latency else None,
        "latency_ms_p95": _percentile(latency, 0.95),
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_usd_mean": round(sum(costs) / len(costs), 6) if costs else None,
    }


def evaluate(
    domains: dict[str, HoldoutDomain],
    *,
    provider_kind: str,
    model: str,
    base_url: str,
    api_key: str | None,
    send_fields: tuple[str, ...],
    prompt_version: str = "entity-pair-choice-v1",
    adapter=None,
    timeout_seconds: float = 30.0,
    max_retries: int = 0,
) -> dict:
    if adapter is None:
        adapter = adapter_for_kind(provider_kind)
    if adapter is None:
        raise ValueError(f"unsupported provider kind: {provider_kind}")
    if provider_kind in DECISION_PROVIDER_KINDS and not send_fields:
        raise ValueError("decision providers require at least one send field")
    if api_key is None:
        return {
            "status": "BLOCKED",
            "reason": f"no API key for {provider_kind}; set the provider key in the workbench or export the documented environment variable",
            "provider": {"kind": provider_kind, "model": model, "base_url": base_url, "send_fields": list(send_fields)},
        }
    config = ProviderConfig(
        provider_key=provider_kind,
        model=model,
        base_url=base_url,
        api_key=api_key,
        name=provider_kind,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    configure_tracing(TraceConfig(enabled=False))
    try:
        reports = {}
        for name, domain in domains.items():
            judge = _judge(adapter, config, send_fields, prompt_version)
            result = match_records(list(domain.records), domain.scheme, [], judge)
            reports[name] = score_domain(domain, result)
    finally:
        shutdown_tracing()
    aggregate = _aggregate(reports)
    status = "COMPLETE" if aggregate["model_calls"] > 0 and aggregate["model_errors"] < aggregate["model_calls"] else "FAILED"
    return {
        "status": status,
        "provider": {
            "kind": provider_kind,
            "model": model,
            "base_url": base_url,
            "send_fields": list(send_fields),
            "prompt_version": prompt_version,
        },
        "domains": reports,
        "aggregate": aggregate,
    }


def _aggregate(reports: dict[str, dict]) -> dict:
    keys = [
        "cases", "required_candidates", "candidates_recalled", "review_volume", "false_merges",
        "labeled_pairs", "unresolved_pairs", "model_calls", "model_errors", "same_pairs",
        "same_correct", "different_pairs", "different_correct", "false_same", "unknown", "judged_pairs",
    ]
    aggregate = {key: sum(report[key] for report in reports.values()) for key in keys}
    aggregate["candidate_recall"] = _rate(aggregate["candidates_recalled"], aggregate["required_candidates"])
    aggregate["same_recall"] = _rate(aggregate["same_correct"], aggregate["same_pairs"])
    aggregate["different_recall"] = _rate(aggregate["different_correct"], aggregate["different_pairs"])
    aggregate["false_same_rate"] = _rate(aggregate["false_same"], aggregate["different_pairs"])
    aggregate["unknown_rate"] = _rate(aggregate["unknown"], aggregate["judged_pairs"])
    aggregate["suggestion_accuracy"] = _rate(aggregate["same_correct"] + aggregate["different_correct"], aggregate["judged_pairs"])
    action_counts = Counter()
    verdict_counts = Counter()
    for report in reports.values():
        action_counts.update(report["action_counts"])
        verdict_counts.update(report["verdict_counts"])
    aggregate["action_counts"] = dict(sorted(action_counts.items()))
    aggregate["verdict_counts"] = dict(sorted(verdict_counts.items()))
    weighted_latency = sum(
        (report["latency_ms_mean"] or 0.0) * report["model_calls"] for report in reports.values()
    )
    aggregate["latency_ms_mean"] = round(weighted_latency / aggregate["model_calls"], 2) if aggregate["model_calls"] else None
    aggregate["cost_usd_total"] = round(
        sum(report["cost_usd_total"] or 0.0 for report in reports.values()), 6,
    ) or None
    return aggregate


def _defaults(provider_kind: str) -> tuple[str, str, str]:
    return PROVIDER_DEFAULTS.get(provider_kind, ("", "", "OPENAI_API_KEY"))


def provider_config_from_db(provider_id: str, session_factory=SessionLocal) -> dict:
    """Read one saved provider without returning its credential to the caller's output."""
    session = session_factory()
    try:
        provider = session.query(LLMProvider).filter(LLMProvider.id == provider_id).first()
        if provider is None:
            raise ValueError(f"provider not found: {provider_id}")
        return {
            "provider_kind": provider.provider_kind or "openai_compatible",
            "model": provider.default_model or "",
            "base_url": provider.base_url or "",
            "api_key": decrypt_secret(provider.encrypted_api_key) if provider.encrypted_api_key else None,
            "send_fields": tuple(provider.send_fields_json or ()),
            "prompt_version": provider.prompt_version or "entity-pair-choice-v1",
            "timeout_seconds": provider.timeout_seconds or 30.0,
            "max_retries": provider.max_retries or 0,
        }
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout-root", type=Path, default=HOLDOUT)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--provider-id", default=None, help="use one saved provider, including its encrypted credential")
    parser.add_argument("--provider-kind", default="typesafe_jev")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--send-fields", default=None, help="comma-separated canonical fields")
    parser.add_argument("--prompt-version", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        manifest = validate_holdout(args.holdout_root)
        assert_no_golden_overlap(args.holdout_root)
        if args.validate_only:
            print(json.dumps({"status": "VALID", "version": manifest["version"], "records": manifest["record_count"], "pairs": manifest["pair_case_count"]}, indent=2))
            return 0
        domains = load_holdout(args.holdout_root)
    except ValueError as exc:
        print(json.dumps({"status": "INVALID", "reason": str(exc)}, indent=2))
        return 2

    provider_cfg = None
    if args.provider_id:
        try:
            provider_cfg = provider_config_from_db(args.provider_id)
        except ValueError as exc:
            print(json.dumps({"status": "INVALID", "reason": str(exc)}, indent=2))
            return 2
    provider_kind = provider_cfg["provider_kind"] if provider_cfg else args.provider_kind
    default_base, default_model, default_env = _defaults(provider_kind)
    base_url = args.base_url if args.base_url is not None else (provider_cfg["base_url"] if provider_cfg else default_base)
    model = args.model if args.model is not None else (provider_cfg["model"] if provider_cfg else default_model)
    if args.send_fields:
        send_fields = tuple(field.strip() for field in args.send_fields.split(",") if field.strip())
    elif provider_cfg:
        send_fields = provider_cfg["send_fields"]
    else:
        send_fields = DEFAULT_SEND_FIELDS
    api_key = args.api_key
    if api_key is None and provider_cfg:
        api_key = provider_cfg["api_key"]
    if api_key is None:
        env_name = args.api_key_env or default_env
        api_key = os.environ.get(env_name) or None
    prompt_version = args.prompt_version or (provider_cfg["prompt_version"] if provider_cfg else "entity-pair-choice-v1")
    try:
        report = evaluate(
            domains,
            provider_kind=provider_kind,
            model=model,
            base_url=base_url,
            api_key=api_key,
            send_fields=send_fields,
            prompt_version=prompt_version,
            timeout_seconds=provider_cfg["timeout_seconds"] if provider_cfg else 30.0,
            max_retries=provider_cfg["max_retries"] if provider_cfg else 0,
        )
    except ValueError as exc:
        print(json.dumps({"status": "INVALID", "reason": str(exc)}, indent=2))
        return 2
    report["holdout"] = {
        "version": manifest["version"],
        "manifest_sha256": sha256(args.holdout_root / "manifest.json"),
        "record_count": manifest["record_count"],
        "pair_case_count": manifest["pair_case_count"],
        "frozen_at": manifest.get("frozen_at"),
    }
    report["as_of"] = date.today().isoformat()
    report["leakage_controls"] = {
        "truth_in_input": False,
        "golden_overlap": "checked, none",
        "holdout_used_for_tuning": False,
        "send_fields": list(send_fields),
    }
    report["limitations"] = [
        "The holdout is synthetic. It checks behavior on authored scenarios, not customer data.",
        "The provider is a hosted model. Verdicts can vary between runs; repeat the run before treating small differences as signal.",
        "Confidence is not a calibrated probability and is not used as an automatic merge threshold.",
        "This report does not change the V0.1 or L3 product status.",
    ]
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(json.dumps({"status": report["status"], "output": str(args.output)}, indent=2))
    else:
        print(text)
    return 0 if report["status"] == "COMPLETE" else 2 if report["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
