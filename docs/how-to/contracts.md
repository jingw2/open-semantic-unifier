# Contract unification (L3)

L3 compiles a real contract file and validates it against a published entity snapshot. It is experimental. The workbench page is **契约统一**. The API is under `/api/v1/projects/{project_id}/contracts`.

## Prerequisites

- A stored file data source in the project. Supported formats are LinkML YAML (`.yaml`, `.yml`), schemasheet TSV (`.tsv`), and CSV (`.csv`).
- A published entity snapshot from the unification workflow. Contract class names must match the snapshot entity types unless you list them as allowed new types.

## Create a version

1. Select a file data source as a contract source.

```bash
curl -X POST "$BASE/api/v1/projects/$PROJECT_ID/contracts/sources" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"data_source_id": "'"$DATA_SOURCE_ID"'"}'
```

2. Compile and validate a draft version.

```bash
curl -X POST "$BASE/api/v1/projects/$PROJECT_ID/contracts/versions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"source_id": "'"$SOURCE_ID"'", "snapshot_id": "'"$SNAPSHOT_ID"'"}'
```

A validation failure returns 422 with a list of `path`, `code`, and `message` entries. No version is written.

3. Publish the version. A null expected version means the project has no current version.

```bash
curl -X POST "$BASE/api/v1/projects/$PROJECT_ID/contracts/versions/$VERSION_ID/publish" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"expected_current_version": null, "reason": "initial contract"}'
```

A stale expected version returns 409. Republishing the current version is idempotent. Use rollback to return to an archived version.

4. Export a version.

```bash
curl "$BASE/api/v1/projects/$PROJECT_ID/contracts/versions/$VERSION_ID/export?kind=model" \
  -H "Authorization: Bearer $TOKEN"
```

Kinds are `model`, `json_schema`, `sql_ddl`, and `er_diagram`. Content always comes from the requested version, never the current pointer.

5. Roll back to a historical version.

```bash
curl -X POST "$BASE/api/v1/projects/$PROJECT_ID/contracts/versions/$VERSION_ID/rollback" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"expected_current_version": 2, "reason": "restore previous contract"}'
```

## Limits

- 5 MiB input, 10 MiB per generated artifact, and 30 seconds per compiler command.
- The validator supports a deliberate LinkML subset: classes, attributes or slots, enums, and the documented built-in ranges. Unsupported ranges and custom types are rejected.
- A model version is immutable after creation. Correct a contract by creating a new version.
- Artifacts are stored with the version row in the database, not in object storage.
- The legacy `/jobs` stage L3 and the demo L3 data source are retired and return 410. The legacy L4 demo seed is unchanged.
- L3 is experimental. L3-01 acceptance evidence is recorded separately; L4 is not implemented.
