# ETL Database Sink and Scheduling Design

Date: 2026-11-25
Status: implementation scope for the database ETL request.

## Intent

A job can read from any supported database source, run L1, L2, or L4, and write the result directly to a target database. The same pipeline can run on an interval. Database source coverage must be proven against real PostgreSQL, MySQL, MongoDB, SQLite, and DuckDB instances.

## Approaches

- A. Build a separate ETL engine and destination model. Rejected. The existing DataSource already holds encrypted database configuration and the existing Job already records parameters and results.
- B. Reuse DataSource as both source and target. A DB DataSource can be selected as the sink. Job params carry the target DataSource id, table name, and write mode. Chosen.
- C. In-process cron loop. Rejected. It does not survive restart and duplicates the Celery worker model.
- D. Persisted schedule rows plus Celery beat. Chosen.

## Data shapes

### Job ETL options

```python
EtlOptions = {
    "target_datasource_id": str,
    "target_table": str,
    "write_mode": "replace" | "append",
}
```

Stored in the existing Job `params_json`. No new Job columns.

### Schedule

```python
EtlSchedule = {
    "id": str,
    "project_id": str,
    "name": str,
    "stage": "L1" | "L2" | "L4",
    "datasource_ids": list[str],
    "params": dict,
    "target_datasource_id": str,
    "target_table": str,
    "write_mode": "replace" | "append",
    "interval_minutes": int,   # 5..10080
    "enabled": bool,
    "next_run_at": datetime,
    "last_run_at": datetime | None,
    "last_job_id": str | None,
}
```

One new table, one migration. It is the only new persistence introduced by this design.

### ETL result

```python
Job.result["etl"] = {
    "status": "SUCCEEDED",
    "target_type": "postgresql",
    "target_table": "supplier_clean",
    "write_mode": "replace",
    "rows_written": int,
}
```

A sink failure marks the Job FAILED with `ETL_LOAD_FAILED` and the target error, after the transform artifacts have been produced.

## Sink

`write_dataframe(config, table, frame, mode)` supports:

- sqlite: sqlite3 plus pandas
- duckdb: duckdb connection, register frame, create/replace table
- postgresql: SQLAlchemy engine with psycopg2, pandas `to_sql`
- mysql: SQLAlchemy engine with pymysql, pandas `to_sql`
- mongodb: pymongo collection replace/insert

Table and collection names are validated with `[A-Za-z_][A-Za-z0-9_.]*`. Credentials are decrypted only at the sink boundary and never logged.

Stage output selection:

- L1: the `clean_csv` artifact
- L2: the `clusters_csv` artifact (current artifact is a 2,000-row sample; the UI must say so)
- L4: `result_rows`

## Scheduling

- `POST /projects/{id}/etl/schedules` creates a schedule.
- `GET /projects/{id}/etl/schedules` lists schedules with next run and last job.
- `PATCH /etl/schedules/{id}` updates name, interval, enabled, params, or target.
- `DELETE /etl/schedules/{id}` removes it.
- `POST /etl/schedules/{id}/run-now` creates and dispatches a Job immediately and advances `next_run_at`.
- Celery beat calls `run_due_etl_schedules` every 60 seconds. A Compose `beat` service runs it. Local non-Celery mode still supports run-now and direct invocation in tests.
- Each due schedule creates a normal Job with the ETL options and dispatches it through the existing worker path.

## UI

- Workbench L1/L2/L4 gets an `写入目标数据库` panel: target DB data source, table, replace/append.
- A `定时运行` control saves a schedule at 5-minute to 7-day intervals and lists next run.
- Job results show the ETL sink row count and target.
- L2 shows that the written table is the 2,000-row cluster sample, not the full pair set.

## Testing

- Unit tests for table-name validation, SQLite and DuckDB sinks, target-job success/failure, schedule validation, run-now, and due-run selection.
- Live Docker integration test starts PostgreSQL, MySQL, and MongoDB on free ports, creates source rows, exercises `test_db_connection`, `read_db_dataframe`, `extract_db_to_parquet`, and sink writes, and reads the target back.
- Existing SQLite and DuckDB paths run in the normal suite.
- The integration test is opt-in with `RUN_DB_CONNECTOR_TESTS=true` and is wired into CI as a separate job.

## Constraints

- No source data is changed by a sink write.
- No auto-publish, auto-merge, or review decision.
- The sink is explicit per job or per schedule; no hidden default target.
- Database credentials stay encrypted at rest and out of logs.
- A schedule never runs two copies at once for the same row (single beat dispatcher; a failed dispatch keeps the schedule enabled for the next tick).
