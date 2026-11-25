#!/usr/bin/env python3
"""Bounded exhaustive check of the decision dispatch field boundary.

For every field in a fixed universe, every subset of configured send fields,
and every placement of that field's value in the left record, right record, or
evidence, the serialized choice-provider body must contain no value of an
unlisted field. A listed field's value must survive in every placement.
"""
from itertools import combinations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.decision_models.contracts import DecisionRequest
from app.services.decision_models.providers.perplexity import PerplexityDecisionsAdapter

FIELDS = ("name", "tax_id", "phone", "email", "address", "model")
SENTINEL = {field: f"SECRET-{field.upper()}-VALUE" for field in FIELDS}
VALUE_KEYS = ("value", "left", "right")


def _request(field_names: tuple[str, ...], evidence: dict) -> DecisionRequest:
    return DecisionRequest(
        project_id="formal-check",
        run_id="formal-check",
        input_version="holdout-v1",
        scheme_revision="1",
        prompt_version="formal-v1",
        entity_type="organization",
        identity_granularity="legal_entity",
        left={field: SENTINEL[field] for field in FIELDS},
        right={field: SENTINEL[field] for field in FIELDS},
        field_names=field_names,
        evidence=(evidence,),
    )


def _fail(message: str) -> None:
    raise AssertionError(message)


def check() -> int:
    adapter = PerplexityDecisionsAdapter()
    cases = 0
    for probe in FIELDS:
        for size in range(len(FIELDS) + 1):
            for field_names in combinations(FIELDS, size):
                for placement in ("none", *VALUE_KEYS):
                    evidence = {"rule": "exact", "field": probe}
                    if placement != "none":
                        evidence[placement] = SENTINEL[probe]
                    body = adapter._body(_request(tuple(field_names), evidence), "formal-model")
                    state = body["state"]
                    text = json.dumps(body, sort_keys=True)
                    cases += 1
                    for field in FIELDS:
                        allowed = field in field_names
                        if allowed:
                            if state["left"].get(field) != SENTINEL[field] or state["right"].get(field) != SENTINEL[field]:
                                _fail(
                                    f"listed field was dropped from left or right: probe={probe} "
                                    f"field_names={field_names} field={field}"
                                )
                        else:
                            if field in state["left"] or field in state["right"]:
                                _fail(
                                    f"unlisted field reached left or right: probe={probe} "
                                    f"field_names={field_names} field={field}"
                                )
                            if SENTINEL[field] in text:
                                _fail(
                                    f"unlisted value reached the body: probe={probe} "
                                    f"field_names={field_names} placement={placement} field={field}"
                                )
                    entry = state["evidence"][0]
                    if entry.get("rule") != "exact" or entry.get("field") != probe:
                        _fail(f"evidence lost its structural keys: probe={probe} field_names={field_names}")
                    if probe in field_names:
                        if placement != "none" and entry.get(placement) != SENTINEL[probe]:
                            _fail(
                                f"listed evidence value was dropped: probe={probe} "
                                f"field_names={field_names} placement={placement}"
                            )
                    else:
                        leaked = [key for key in VALUE_KEYS if key in entry]
                        if leaked:
                            _fail(
                                f"unlisted evidence value survived: probe={probe} "
                                f"field_names={field_names} placement={placement} keys={leaked}"
                            )
    return cases


if __name__ == "__main__":
    checked = check()
    print(f"PASS: {checked} exhaustive cases across {len(FIELDS)} fields and every send-field subset.")
