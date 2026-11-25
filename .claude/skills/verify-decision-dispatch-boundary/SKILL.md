---
name: verify-decision-dispatch-boundary
description: "Prove that a decision request never sends values from record fields outside the configured send fields. Use when changing decision model providers, DecisionRequest, matching evidence, or the run's field_names wiring."
---

# Verify the decision dispatch boundary

**Property.** For every decision request, the serialized choice-provider body contains no value from a record field outside `field_names`, whether the value sits in `left`, `right`, or an evidence entry. A listed field's value survives in every placement.

**Harm.** An unlisted value leaves the workspace to a third-party provider. A dropped listed value makes the model judge without the configured evidence.

## Run

```bash
python3 tests/formal/check_decision_dispatch_boundary.py
```

A healthy tree prints `PASS: 1536 exhaustive cases across 6 fields and every send-field subset.`

## What it checks

The check calls the real `PerplexityDecisionsAdapter._body`, the shared body builder for Perplexity and TypeSafe. It enumerates every field in a fixed six-field universe, every subset as `field_names`, and every placement of the probe value in `left`, `right`, an evidence `value`, an evidence `left`, an evidence `right`, or no evidence value. It asserts two things.

- Safety. No unlisted value appears anywhere in the serialized body, in records or evidence.
- Liveness. Every listed value survives in left and right, and an evidence value survives when its field is listed. Evidence keeps its `rule` and `field` keys either way.

## What it does not cover

- The OpenAI-compatible adapter. It sends no evidence and its filtering has separate backend tests.
- The provider configuration path that supplies `field_names`, and the database row itself.
- Non-string values. The filter is key based, so the value type does not change the result.
- The HTTP transport after serialization.

## Coverage

| File | Functions | Last changed |
| --- | --- | --- |
| `backend/app/services/decision_models/providers/choice.py` | `_evidence_without_unlisted_values`, `ChoiceAdapter._body` | `f4c3a01` |
| `backend/app/services/decision_models/contracts.py` | `DecisionRequest` | `7b918f9` |
| `backend/app/services/unification/matching.py` | Evidence entries that carry `value`, `left`, `right` | `507254c` |
| `backend/app/services/unification/releases.py` | `field_names` selection and the empty-send-fields guard | `f4c3a01` |

Sync commit: `f4c3a01`. The check was added after that commit and describes the covered code at it. Move the sync commit forward only after you confirm the check still describes these functions at the new commit.

## If it fails

The message names the probe field, the `field_names` subset, the placement, and the field whose value escaped. A failure means the filter regressed or the evidence shape changed. Fix the code, or change the check only when the intended behavior changed. Do not weaken the property.

To reproduce the failure class first, replace the body of `_evidence_without_unlisted_values` with `return list(evidence)`, rerun the check, and confirm it reports an unlisted value reaching the body. Restore the function before you continue.

## After a change

Rerun the check. If a covered file changed, read the diff and confirm the check still describes it. Update the sync commit only after that confirmation. Use `/maintain-formal-verification-skill` when the model or the covered behavior changes materially.
