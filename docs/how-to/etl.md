# Writing job results to a database and scheduling runs

A job can read from any supported database source, run L1, L2, or L4, and write the result directly to a target database. The same pipeline can run on an interval through persisted schedules.

## Target databases

A job's sink is an existing DB DataSource (SQLite, DuckDB, PostgreSQL, MySQL, or MongoDB) selected as the target. Credentials stay encrypted in the DataSource record and are decrypted only at the sink boundary; they never appear in logs or error messages.

| Stage | Written frame |
| --- | --- |
| L1 | The `clean_csv` artifact (cleaned rows plus `canonical_id`, `canonical_name`, `reconcile_score`) |
| L2 | The `clusters_csv` artifact. This is a 2,000-row sample of the cluster output, not the full pair set. |
| L4 | The metric `result_rows` |

Write modes are `replace` (drop and recreate) and `append`. Table and collection names must match `[A-Za-z_][A-Za-z0-9_.]*`.

When a job is submitted with `etl_target_datasource_id`, `etl_target_table`, and `etl_write_mode`, the sink runs after the transform and before the final success commit. A sink failure marks the job FAILED with `ETL_LOAD_FAILED` and keeps the transform artifacts, so the stage output is still available for inspection.

## Schedules

Schedules persist under `POST /api/v1/projects/{project_id}/etl/schedules`. Each schedule names a stage, source data sources, a target DB DataSource, a target table, a write mode, and an interval in minutes (5 through 10080).

- `GET /api/v1/projects/{project_id}/etl/schedules` lists schedules with next-run and last-job details.
- `PATCH /api/v1/projects/{project_id}/etl/schedules/{schedule_id}` updates name, interval, enabled, params, or target.
- `DELETE /api/v1/projects/{project_id}/etl/schedules/{schedule_id}` removes a schedule.
- `POST /api/v1/projects/{project_id}/etl/schedules/{schedule_id}/run-now` creates and dispatches a Job immediately and advances the schedule.

In the Compose deployment a `beat` service calls `run_due_etl_schedules` every 60 seconds. Each due, enabled schedule creates a normal Job, advances its `next_run_at` by the interval, and commits before dispatch, so a failed dispatch never double-runs the same tick. Local non-Celery mode supports `run-now` and direct invocation in tests.

## Workbench UI

The workbench L1/L2/L4 page has an `ETL 写入与定时` card. Pick a target database from the project's DB data sources (SQLite, DuckDB, PostgreSQL, MySQL, or MongoDB), fill a target table, and choose replace or append. `不写入数据库` keeps the current behaviour of only producing artifacts. With a target selected, the run button stays disabled until a table name is present, and the submitted job carries `etl_target_datasource_id`, `etl_target_table`, and `etl_write_mode` alongside the normal stage parameters.

The card also saves persisted schedules: choose an interval (15 minutes, 1 hour, 6 hours, or 24 hours) and press `保存定时任务`. Existing schedules list their name, stage, interval, and next run, with `立即运行` and `删除` actions. A saved schedule includes the current stage, selected sources, stage parameters, and the configured provider id when one is set.

A successful job whose result includes a sink shows the outcome line: target type, table, write mode, and rows written. On a sink failure the job stays FAILED and the error surfaces in the status line; the UI never reports a fake success. On L2 the card notes that the written table is the 2,000-row cluster sample, not the full pair set.

## Testing the live connectors

The sink and scheduling paths are covered by `backend/tests/test_etl.py` (SQLite and DuckDB sinks, target jobs, failure handling, and schedule selection). Live server connectors are proven by an opt-in test that starts isolated PostgreSQL 16, MySQL 8, and MongoDB 7 containers:

```bash
RUN_DB_CONNECTOR_TESTS=true python3 -m pytest backend/tests/integration/test_database_connectors.py -q
```

That test exercises `test_db_connection`, `read_db_dataframe`, `extract_db_to_parquet`, `write_dataframe`, and a read-back of the written target for each server, plus one full L1 job from a live PostgreSQL source to a live MySQL target.
