# API workflow

Use the interactive schema at `http://localhost:8000/docs` for the exact request and response models. Authenticate with `POST /api/v1/auth/login`, then send `Authorization: Bearer <access_token>` for workspace endpoints.

Typical workflow:

1. `GET /api/v1/projects` and select or create a project.
2. Configure a project scheme under `/api/v1/projects/{project_id}/unification/schemes`.
3. Submit a batch to `/api/v1/projects/{project_id}/unification/batches`.
4. Submit a run to `/api/v1/projects/{project_id}/unification/runs` with an `Idempotency-Key`; poll the returned run resource.
5. Read review candidates, then submit decisions with the current `expected_revision`.
6. Publish an immutable snapshot and query its entities, mappings, decisions, and delta exports by explicit `snapshot_id`.

For a manual attribute correction, read the entity's `/attribute-overrides/preview` with the current snapshot ID and field. Review the returned live source attributes, then submit the override with the preview's identity revision and member revision pairs, the new value, and a reason. After membership or source revisions change, the old correction must be reconfirmed against a fresh preview before publication. A correction is effective in a newly published snapshot; prior snapshots remain unchanged.

Project Serving clients use `/api/v1/serve/{project_slug}/unification/resolve`, a project API key, and an explicit snapshot ID. Do not use a draft run as a published serving version.

All mutating calls should preserve their returned resource IDs and revision values. A stale review returns a conflict so the client can refresh before deciding again. Never put provider credentials in URLs or request logs.
