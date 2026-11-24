#!/usr/bin/env python3
"""Run the public Golden inputs through a live Open Semantic Unifier API."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "tests" / "golden" / "v1"


def request_json(base_url: str, token: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API returned HTTP {exc.code} for {path}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach {base_url}: {exc.reason}") from exc


def record_key(source_system: str, dataset: str, source_record_id: str) -> tuple[str, str, str]:
    return source_system.strip().casefold(), dataset.strip().casefold(), source_record_id.strip()


def run_domain(base_url: str, token: str, project_id: str, domain: str, golden_version: str) -> tuple[dict, str, int]:
    domain_dir = GOLDEN_ROOT / domain
    records = json.loads((domain_dir / "records.json").read_text(encoding="utf-8"))
    scheme = json.loads((domain_dir / "scheme.json").read_text(encoding="utf-8"))
    # expected.json is read only for case IDs and pair references. Truth and actions are never loaded.
    pair_cases = json.loads((domain_dir / "expected.json").read_text(encoding="utf-8"))
    base_path = f"/api/v1/projects/{project_id}/unification"
    scheme_result = request_json(base_url, token, "PUT", f"{base_path}/schemes/{domain}", scheme)
    actual = request_json(base_url, token, "POST", f"{base_path}/match", {
        "scheme_key": domain,
        "scheme_revision": scheme_result["revision"],
        "input_version": f"golden-{golden_version}-{domain}",
        "records": [
            {key: record[key] for key in ("source_system", "dataset", "source_record_id", "attributes")}
            for record in records
        ],
    })

    by_ref: dict[str, dict] = {}
    by_source_key = {
        record_key(row["source_system"], row["dataset"], row["source_record_id"]): row
        for row in actual["identities"]
    }
    assignments = {}
    for record in records:
        identity = by_source_key[record_key(record["source_system"], record["dataset"], record["source_record_id"])]
        by_ref[record["record_ref"]] = identity
        assignments[record["record_ref"]] = identity["entity_id"]

    actual_pairs = {
        frozenset((pair["left_source_revision_id"], pair["right_source_revision_id"])): pair
        for pair in actual["pairs"]
    }
    pairs = []
    for case in pair_cases:
        left = by_ref[case["left"]]
        right = by_ref[case["right"]]
        result = actual_pairs[frozenset((left["source_revision_id"], right["source_revision_id"]))]
        pairs.append({
            "case_id": case["case_id"],
            "candidate": result["candidate"],
            "action": result["action"],
            "score": result.get("score"),
            "evidence_codes": result.get("evidence_codes", []),
            "evidence": result.get("evidence", []),
            "model_error": result.get("model_error"),
        })

    prediction = {"assignments": assignments, "pairs": pairs}
    return prediction, actual["execution_id"], actual["scheme_revision"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-id", default=os.getenv("OPEN_SEMANTIC_UNIFIER_PROJECT_ID"))
    parser.add_argument("--token", default=os.getenv("OPEN_SEMANTIC_UNIFIER_TOKEN"))
    args = parser.parse_args()
    if not args.project_id or not args.token:
        parser.error("--project-id/OPEN_SEMANTIC_UNIFIER_PROJECT_ID and --token/OPEN_SEMANTIC_UNIFIER_TOKEN are required")

    manifest = json.loads((GOLDEN_ROOT / "manifest.json").read_text(encoding="utf-8"))
    predictions = {}
    execution_ids = []
    scheme_revisions = []
    try:
        for domain in manifest["domains"]:
            predictions[domain], execution_id, scheme_revision = run_domain(
                args.base_url, args.token, args.project_id, domain, manifest["version"]
            )
            execution_ids.append(f"{domain}:{execution_id}")
            scheme_revisions.append(f"{domain}:{scheme_revision}")
    except (RuntimeError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output = {
        "golden_version": manifest["version"],
        "run_id": ";".join(execution_ids),
        "scheme_revision": ";".join(scheme_revisions),
        "domains": predictions,
    }
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "golden_version": manifest["version"], "executions": execution_ids}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
