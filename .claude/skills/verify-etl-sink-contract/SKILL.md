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

## What the checker isolates, and the companion tests that cover it

The checker replaces the L1/L2/L4 transforms and uses a SQLite target so the fault space stays on the sink contract. These companion tests cover the rest:

- **Real PostgreSQL, MySQL, and MongoDB target writes.** `backend/tests/integration/test_database_connectors.py` writes and reads back each target, runs concurrent appends, and runs one full PostgreSQL-to-MySQL L1 job.
- **Concurrent writes to one target table.** `test_etl.py::test_concurrent_sqlite_sink_serializes` covers append and replace; the live connector test covers concurrent appends on PostgreSQL, MySQL, and MongoDB.
- **Real L1, L2, and L4 transforms feeding the sink.** `test_etl.py` covers L1, L2, and L4 target jobs with real engines and real SQLite/DuckDB writes.
- **Schedule runner and Celery beat.** `test_etl.py` covers run-now, due-run selection, disabled skip, the beat schedule entry, and the Compose beat service.
- **Credential handling.** `test_etl.py::test_datasource_api_does_not_expose_target_credentials` proves the API and sink errors do not contain the stored password.

The formal checker itself does not spawn live databases, run Celery beat, or execute the heavy transforms; those are intentionally in the companion suites.

## Coverage

| File | Functions | Last changed |
| --- | --- | --- |
| `backend/app/workers/tasks.py` | `run_job_sync` stage result and ETL sink block | `17ebd12` |
| `backend/app/services/etl.py` | `resolve_target`, `load_job_frame`, `write_dataframe`, `validate_table_name`, `_sink_lock` | `b08f8ab` |
| `backend/app/api/v1/providers_jobs.py` | ETL target validation on Job submission | `cec8dd5` |
| `backend/app/models/entities.py` | `Job`, `DataSource`, `ArtifactVersion` | `8c8a3bd` |

Sync commit: `b08f8ab`. The check was added in `1b29b81` and moved forward after confirming it still describes the sink code with `_sink_lock`. Move the sync commit forward only after you confirm the check still describes these functions at the new commit.

## If it fails

The message names the stage and outcome, for example:

```
FAIL: L1/sink_failure: Job is SUCCESS
```

That means the worker reported success after the target write failed, or the Job status was overwritten after the sink exception. Fix the result handling; do not weaken the check.

To reproduce the failure class first, set the sink exception path's `job.status` to `"SUCCESS"` and its `_publish_progress` call to `"SUCCESS"`, rerun the check, and confirm it fails on `L1/sink_failure`. Restore both before continuing.

## After a change

Rerun the check. If any covered file changed, read the diff and confirm the check still describes the new stage frames, target handling, or status transitions. Update the sync commit only after that confirmation. Use `/maintain-formal-verification-skill` when the sink or Job lifecycle changes materially.
