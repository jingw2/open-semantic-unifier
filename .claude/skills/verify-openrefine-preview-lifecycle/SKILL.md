---
name: verify-openrefine-preview-lifecycle
description: "Prove that an OpenRefine preview never creates a data source or mutates the original, fails into a terminal job at every fault point, cleans up its temporary project, and can be adopted only once. Use when changing openrefine previews, the preview UI, jobs, storage, or adoption."
---

# Verify the OpenRefine preview lifecycle

**Property.** For every fault point in a preview, the call either fails with a terminal Job, no new DataSource, unchanged original bytes, and no temporary OpenRefine project left behind, or it succeeds and stores exactly one DRAFT artifact without creating a DataSource. Adoption creates exactly one DataSource, happens once, and cannot be cancelled afterwards.

**Harm.** A failure that leaves the Job RUNNING hides a failed preview, a temporary project consumes the sidecar workspace, a new DataSource appears before the user adopts, or the original file changes under the user. Each makes the workbench report something that did not happen.

## Run

```bash
python3 tests/formal/check_openrefine_preview_lifecycle.py
```

A healthy tree prints:

```
PASS: 16 preview lifecycle cases across 2 recipes and 6 fault points.
```

The check runs real `create_preview`, `adopt_preview`, and `cancel_preview` code against a temporary SQLite database and local storage. It injects a failure at each external step and uses two real recipes: one field recipe and the per-column recipe.

## What it checks

For each of `normalize_company_name` and `normalize_selected_columns`, the check covers:

- Failures at `health`, `create`, `apply`, `export`, artifact save, and diff.
- Success, adoption, second adoption, and cancel after adoption.
- Cancel before adoption.

On every failure it asserts:

- Exactly one Job exists and its status is `FAILED` with an error message.
- No DataSource was created.
- No ArtifactVersion row remains.
- The original object bytes are unchanged.
- A temporary project, when created, was deleted.
- No delete is attempted when no project was created.

On success and adoption it asserts:

- One DRAFT artifact exists, and no DataSource exists before adoption.
- Adoption creates exactly one DataSource whose id is the preview Job id.
- The original bytes remain unchanged.
- The temporary project is deleted after adoption.
- A second adoption is rejected, and an adopted preview cannot be cancelled.
- Cancel before adoption marks the Job `CANCELLED`, creates no DataSource, and deletes the temporary project.

## Why this tool

The property is about a finite set of external calls in one transaction, not an unbounded input space. Exhaustive fault injection against the real service code covers every failure point without a hand model that could drift from the code. A TLA+ model would restate the transaction, not test it.

## What it does not cover

- The real OpenRefine HTTP client, CSRF, and import polling. The checker substitutes a controlled client.
- S3 storage; it uses the local storage backend.
- Concurrent double adoption. That is covered by the database primary-key guard and `backend/tests/test_openrefine_api.py`.
- A process crash between the Job commit and cleanup. The checker injects exceptions, not `SIGKILL`.
- Diff content correctness and GREL operation validity.

## Coverage

| File | Functions | Last changed |
| --- | --- | --- |
| `backend/app/services/openrefine/previews.py` | `create_preview`, `_fail_job`, `_best_effort_delete`, `adopt_preview`, `cancel_preview` | `22f9f63` |
| `backend/app/services/openrefine/recipes.py` | `render_operations`, `recipe_payload`, `select_recipe` | `22f9f63` |
| `backend/app/services/openrefine/client.py` | `OpenRefineError`, `OpenRefineUnavailable` | `1829633` |
| `backend/app/services/storage.py` | `get_bytes`, `put_bytes` on the local backend | `eee0afb` |
| `backend/app/models/entities.py` | `Job`, `DataSource`, `ArtifactVersion` | `8c8a3bd` |

Sync commit: `bcd66e2`. The check was added in that commit and confirmed against the covered files at it. Move the sync commit forward only after you confirm the check still describes these functions at the new commit.

## If it fails

The failure names the recipe and fault point, for example:

```
FAIL: normalize_company_name/save_artifact: temporary project was not deleted
```

Interpret it as the invariant in the sentence, not as a test flake. A leaked project means the cleanup path in `create_preview` regressed. A RUNNING Job means `_fail_job` or the generic exception path changed. A new DataSource means adoption ran during preview. Do not weaken the check to pass.

To reproduce the failure class first, remove the `_best_effort_delete` call from the generic `except Exception` block in `create_preview`, rerun the check, and confirm it fails on `save_artifact`. Restore the call before continuing.

## After a change

Rerun the check. If any covered file changed, read the diff and confirm the check still describes the new behavior. Update the sync commit only after that confirmation. Use `/maintain-formal-verification-skill` when the preview transaction, adoption semantics, or storage boundary changes materially.
