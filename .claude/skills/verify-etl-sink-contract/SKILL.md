---
name: verify-etl-sink-contract
description: "Prove that an L1/L2/L4 Job reports SUCCESS only after its target database write succeeds, and that a sink or transform failure cannot masquerade as success. Use when changing the ETL sink, run_job_sync result handling, target resolution, or stage frames."
---

# Verify the ETL sink success contract

**Property.** For every L1, L2, and L4 Job that names a target table, the Job reports `SUCCESS` only after the target write succeeds. A sink failure marks the Job `FAILED` with `ETL_LOAD_FAILED`, preserves the transform result, and writes no table. A transform failure never calls the sink. A Job without a target never calls the sink.

**Harm.** A false success tells the operator the target database was updated when it was not, so downstream reports and schedules trust stale data. A sink failure that loses the transform result also destroys the evidence needed to retry.

## Run

```bash
python3 tests/formal/check_etl_sink_contract.py
```

A healthy tree prints:

```
PASS: 15 ETL sink cases across 3 stages and 5 outcomes.
```

The check runs the real `run_job_sync` sink block, `resolve_target`, `load_job_frame`, `write_dataframe`, and `validate_table_name` against a temporary SQLite database and local storage. It drives three stages through five outcomes: success, sink failure, invalid target table, transform failure, and no target.

## What it checks

For each stage `L1`, `L2`, `L4`:

- **Success.** Job `SUCCESS`, `result.etl.status == "SUCCEEDED"`, a positive row count, and at least one row in the target SQLite table.
- **Sink failure.** Job `FAILED`, `error_message` starts with `ETL_LOAD_FAILED`, the transform result is preserved, the sink was called, and no target table exists.
- **Invalid target table.** Same contract as a sink failure; the table name is rejected before any write.
- **Transform failure.** Job `FAILED`, the sink is never called, the failure is not reported as `ETL_LOAD_FAILED`, and no transform result is persisted.
- **No target.** Job `SUCCESS`, no `result.etl`, and the sink is never called.

The transform functions are replaced with small deterministic results so the fault space isolates the sink contract. The sink and worker result handling are real.

## Why this tool

The property is a finite contract over three stages and five outcomes. Exhaustive fault injection against the real worker covers every case without a hand model that could drift from the code. A model checker would restate the worker rather than exercise it.

## What it does not cover

- Real PostgreSQL, MySQL, or MongoDB writes. The live connector test covers those, including one PostgreSQL-to-MySQL L1 job.
- Concurrent runs on the same target table.
- The schedule runner and Celery beat; `run_due_etl_schedules` has separate tests.
- The real L1/L2/L4 transforms; the check isolates the sink by replacing them.
- Credential handling beyond `resolve_target` using the encrypted DataSource row.

## Coverage

| File | Functions | Last changed |
| --- | --- | --- |
| `backend/app/workers/tasks.py` | `run_job_sync` stage result and ETL sink block | `17ebd12` |
| `backend/app/services/etl.py` | `resolve_target`, `load_job_frame`, `write_dataframe`, `validate_table_name` | `cec8dd5` |
| `backend/app/api/v1/providers_jobs.py` | ETL target validation on Job submission | `cec8dd5` |
| `backend/app/models/entities.py` | `Job`, `DataSource`, `ArtifactVersion` | `8c8a3bd` |

Sync commit: `1b29b81`. The check was added in that commit and confirmed against the covered files at it. Move the sync commit forward only after you confirm the check still describes these functions at the new commit.

## If it fails

The message names the stage and outcome, for example:

```
FAIL: L1/sink_failure: Job is SUCCESS
```

That means the worker reported success after the target write failed, or the Job status was overwritten after the sink exception. Fix the result handling; do not weaken the check.

To reproduce the failure class first, set the sink exception path's `job.status` to `"SUCCESS"` and its `_publish_progress` call to `"SUCCESS"`, rerun the check, and confirm it fails on `L1/sink_failure`. Restore both before continuing.

## After a change

Rerun the check. If any covered file changed, read the diff and confirm the check still describes the new stage frames, target handling, or status transitions. Update the sync commit only after that confirmation. Use `/maintain-formal-verification-skill` when the sink or Job lifecycle changes materially.
