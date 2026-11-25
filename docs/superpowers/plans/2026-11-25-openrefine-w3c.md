# OpenRefine Runtime and W3C Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run built-in OpenRefine cleaning recipes against a project data source, show a before/after diff, let the user adopt or cancel, and export LinkML/JSON Schema/JSON-LD W3C artifacts that can be viewed online.

**Architecture:** A pinned OpenRefine service runs as a Compose sidecar with no published port. A backend adapter drives its HTTP API; a file-backed recipe registry selects a read-only recipe from the data profile. A preview endpoint stores its cleaned CSV as a DRAFT artifact and records the run on the existing Job row, so adoption creates a new DataSource and never mutates the original. JSON-LD is generated on demand from a contract version's stored canonical model and LinkML YAML plus a chosen data source; no new tables or migrations.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, httpx, pytest, React 19, TypeScript, Vite, Tailwind CSS, Playwright, Docker Compose, OpenRefine 3.10.1.

**Spec:** `docs/superpowers/specs/2026-11-25-guided-assistant-design.md` (section 11)

## Global Constraints

- Four top-level menus stay unchanged: 数据 / 任务 / 复核 / 设置.
- OpenRefine never receives a file larger than 10 MiB, more than 50,000 rows, or more than 256 columns. A single small table falls back to rule cleaning; multi-table or larger data routes to Splink + DuckDB.
- The original DataSource and its object are never modified. Only “采用” creates a new DataSource; “撤销” deletes the temporary OpenRefine project.
- Recipes are built-in, versioned, read-only JSON files. Users may switch recipes but cannot upload or edit one.
- No RDF/OWL output. W3C output is LinkML YAML, JSON Schema, and JSON-LD instances only.
- No database migration. Preview state lives in the existing `jobs` row (`params_json`, `result_json`); recipe files live on disk.
- The assistant must never auto-adopt an OpenRefine result, auto-publish a contract, merge entities, or decide a review.
- The OpenRefine service is internal-only: no `ports:` entry, fixed image built from the official release tarball with its published SHA-256, fixed base image digest.
- All new Playwright specs start their own backend on 127.0.0.1:8765 and use `open_route` from `frontend/tests/ui_helpers.py`.
- Backend tests use `python3 -m pytest`; frontend specs are standalone scripts under `frontend/tests/`.
- Keep the existing neutral theme. No gradients, emoji, or decorative headers.

## Review Focus

1. OpenRefine is down, times out, or returns HTML: preview must fail without creating a DataSource, the Job must be FAILED with a stable code, and the UI must offer rule cleaning. Tested in Task 7.
2. A CSV with quoted commas, a UTF-8 BOM, or duplicate header names: diff rows must stay aligned and adoption must preserve the cleaned bytes. Tested in Task 7.
3. A preview is adopted twice, cancelled after adoption, or referenced from another project: the API must return 409/404 and never create a duplicate or cross-project DataSource. Tested in Task 7.
4. JSON-LD mapping names an unknown class or slot, omits the ID slot, maps two slots to one column, or has no contract version: the API must return a clear 404/422. Tested in Task 8.
5. An oversized or multi-table source is selected: it must never be sent to OpenRefine and must get the documented fallback. Tested in Task 6 (limits) and Task 7 (route).

---

### Task 6: OpenRefine Runtime, Adapter, and Recipe Registry

**Files:**
- Create: `openrefine/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `backend/app/core/config.py`
- Create: `backend/app/services/openrefine/__init__.py`
- Create: `backend/app/services/openrefine/client.py`
- Create: `backend/app/services/openrefine/recipes.py`
- Create: `backend/app/services/openrefine/recipes/*.json`
- Test: `backend/tests/test_openrefine_client.py`
- Test: `backend/tests/test_openrefine_recipes.py`
- Modify: `docs/superpowers/specs/2026-11-25-guided-assistant-design.md`

**Interfaces:**
- Consumes: nothing.
- Produces: `OpenRefineClient(base_url: str | None = None, timeout: float | None = None)` with `health() -> bool`, `create_project(name: str, file_bytes: bytes, format: str = "csv") -> str`, `apply_recipe(project_id: str, operations: list[dict]) -> None`, `export_csv(project_id: str, project_name: str) -> bytes`, `delete_project(project_id: str) -> None`.
- Produces: `OpenRefineUnavailable`, `OpenRefineError` (with `.code` and `.message`).
- Produces: `load_recipes() -> list[Recipe]`, `select_recipe(profile: dict, user_goal: str | None = None) -> Recipe | None`, `recipe_payload(recipe: Recipe) -> dict`.
- Produces: `openrefine_limits_ok(profile: dict, size_bytes: int) -> tuple[bool, str | None]`; `settings.openrefine_url`, `settings.openrefine_enabled`, `settings.openrefine_timeout_seconds`.
- Produces Compose service `openrefine` and volume `openrefine_data`.

- [x] **Step 1: Write failing recipe tests**

```python
def test_recipe_catalog_is_read_only_and_versioned():
    recipes = load_recipes()
    assert {r.id for r in recipes} >= {"normalize_company_name", "normalize_whitespace", "split_contact_column"}
    assert all(r.version and r.operations for r in recipes)

def test_select_recipe_prefers_company_normalization_for_dirty_name_column():
    profile = {"total_rows": 250, "table_count": 1, "has_whitespace_noise": True,
               "detected_keys": {"name_col": "supplier_raw_name", "phone_col": None},
               "tables": [{"name": "suppliers.csv", "rows": 250, "cols": 3,
                           "columns": ["supplier_raw_name", "city", "tax_id"]}]}
    recipe = select_recipe(profile)
    assert recipe.id == "normalize_company_name"

def test_limits_reject_large_or_multitable_sources():
    ok, reason = openrefine_limits_ok({"total_rows": 50_001, "table_count": 1}, 1024)
    assert not ok and "50,000" in reason
    assert not openrefine_limits_ok({"total_rows": 10, "table_count": 2}, 1024)[0]
    assert openrefine_limits_ok({"total_rows": 10, "table_count": 1}, 10 * 1024 * 1024 + 1)[0] is False
```

- [x] **Step 2: Run the recipe tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_openrefine_recipes.py -q`
Expected: FAIL with `ModuleNotFoundError: app.services.openrefine` or missing names.

- [x] **Step 3: Implement the recipe registry and six recipe files**

`recipes.py` loads every `recipes/*.json` with `json.load`, validates required keys, and returns immutable `Recipe` dataclasses sorted by `priority`.
Recipe selection rules, in order:
1. `split_contact_column` when the name candidate column contains `;`, `、`, or `，` in the profile preview and a contact-like column exists.
2. `merge_name_columns` when both `first_name` and `last_name` columns exist.
3. `normalize_company_name` when `has_whitespace_noise` or a name column exists.
4. `normalize_whitespace` for any name column.
5. `normalize_punctuation` when a name column exists.
6. `remove_legal_suffix` for a company/supplier column.
Return `None` when no table has a text column.

Six recipe JSON files: `normalize_company_name.json`, `normalize_whitespace.json`, `normalize_punctuation.json`, `remove_legal_suffix.json`, `split_contact_column.json`, `merge_name_columns.json`. Each has `id`, `name`, `description`, `version` (`"1.0.0"`), `priority`, `match` (`column_terms`, `requires_whitespace_noise`, `requires_columns`), `limits`, and `operations`.

Use only operations verified against OpenRefine 3.10.1:
- `core/text-transform` with `engineConfig: {"facets": [], "mode": "row-based"}`, `columnName`, `expression`, `onError: "keep-original"`.
- `core/column-split` with the same `engineConfig`, `columnName`, `mode: "separator"`, `separator`, `regex: false`, `maxColumns: 50`, `guessCellType: true`, `removeOriginalColumn: true`.
- `core/column-addition` with the same `engineConfig`, `newColumnName`, `columnInsertIndex: 1`, `baseColumnName`, `expression`, `onError: "keep-original"`.

GREL expressions:
- whitespace: `value.trim().replace(/\s+/,' ')`
- punctuation: `value.trim().replace(/[（）()【】\[\]·,，.。]/g,'').replace(/\s+/g,' ')`
- legal suffix: `value.trim().replace(/(股份有限公司|有限责任公司|有限公司|集团)$/,'')`
- company normalization: punctuation + lowercase + suffix, composed in one expression.

- [x] **Step 4: Run the recipe tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_openrefine_recipes.py -q`
Expected: PASS.

- [x] **Step 5: Write failing client tests against a fake OpenRefine server**

The fake `http.server` exposes:
- `GET /command/core/get-version` → `{"version": "3.10.1"}`
- `GET /command/core/get-csrf-token` → `{"token": "test-token"}`
- `POST /command/core/create-project-from-upload` → 302 `Location: /project?project=42`; record multipart fields and reject a missing `csrf_token` query param.
- `POST /command/core/apply-operations` → `{"code": "ok"}`; record the operations JSON.
- `POST /command/core/export-rows/probe.csv` → CSV bytes with `text/csv`.
- `POST /command/core/delete-project` → `{"code": "ok"}`.
- A `behavior` switch for `html_error`, `timeout`, and `error_json`.

Tests must assert:
```python
def test_create_apply_export_delete_round_trip(fake_openrefine):
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=2)
    assert client.health() is True
    pid = client.create_project("probe", b"id,name\n1,  Acme  \n")
    client.apply_recipe(pid, [{"op": "core/text-transform"}])
    assert client.export_csv(pid, "probe") == b"id,name\n1,Acme\n"
    client.delete_project(pid)
    assert fake_openrefine.deleted == [pid]

def test_unavailable_maps_to_stable_error(fake_openrefine):
    fake_openrefine.behavior = "html_error"
    with pytest.raises(OpenRefineError) as exc:
        OpenRefineClient(base_url=fake_openrefine.url, timeout=1).apply_recipe("42", [])
    assert exc.value.code == "OPENREFINE_BAD_RESPONSE"
```

- [x] **Step 6: Run the client tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_openrefine_client.py -q`
Expected: FAIL with missing module/class.

- [x] **Step 7: Implement the client**

Use one `httpx.Client` per call chain with `timeout=settings.openrefine_timeout_seconds`.
- `health()` returns `True` only for 200 plus a JSON body with `version`; connection errors and timeouts return `False`.
- `create_project` obtains a CSRF token, posts multipart with `project-file`, `format`, `separator`, `encoding=UTF-8`, `ignore-lines=0`, `header-line=1`, and `csrf_token` in the query string. Accept a JSON body with `project`, or a 302 `Location` containing `project=`. A 200 with `importing` and `jobID` polls `get-importing-job-status` up to 30 times at 0.5 s.
- `apply_recipe` posts `operations` as compact JSON with `csrf_token` in the query string. A JSON body with `code == "error"` raises `OpenRefineError("OPENREFINE_RECIPE_FAILED", message)`.
- `export_csv` posts `project`, `format=csv`, `quoteAll=""`, `engine={"facets":[],"mode":"row-based"}` to `/command/core/export-rows/{quote(project_name)}.csv`; rejects HTML and empty bodies.
- `delete_project` posts `project`; a timeout is logged and ignored only after a successful preview.
- Never log file bytes, API responses containing record data, or credentials.
- A connection error raises `OpenRefineUnavailable`; other failures raise `OpenRefineError` with codes `OPENREFINE_BAD_RESPONSE`, `OPENREFINE_CREATE_FAILED`, `OPENREFINE_RECIPE_FAILED`, `OPENREFINE_EXPORT_FAILED`, `OPENREFINE_TIMEOUT`.

- [x] **Step 8: Run the client tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_openrefine_client.py -q`
Expected: PASS.

- [x] **Step 9: Add settings and the pinned Compose service**

`Settings` adds:
```python
openrefine_enabled: bool = os.getenv("OPENREFINE_ENABLED", "false").lower() == "true"
openrefine_url: str = os.getenv("OPENREFINE_URL", "http://openrefine:3333")
openrefine_timeout_seconds: float = float(os.getenv("OPENREFINE_TIMEOUT_SECONDS", "60"))
```

`openrefine/Dockerfile`:
```dockerfile
FROM eclipse-temurin:21-jre@sha256:d77e090ae1f6970c047b443c6f0a6c1358b0426d44432bb5a7753408cd43de0d
ARG OPENREFINE_VERSION=3.10.1
ARG OPENREFINE_SHA256=806e38e415fa98c2dee400de575992514557a4e37e82c670b7cd2e34d667c4ba
RUN curl -fsSL -o /tmp/openrefine.tar.gz "https://github.com/OpenRefine/OpenRefine/releases/download/${OPENREFINE_VERSION}/openrefine-linux-${OPENREFINE_VERSION}.tar.gz" \
 && echo "${OPENREFINE_SHA256}  /tmp/openrefine.tar.gz" | sha256sum -c - \
 && mkdir -p /opt && tar -xzf /tmp/openrefine.tar.gz -C /opt \
 && mv "/opt/openrefine-${OPENREFINE_VERSION}" /opt/openrefine \
 && rm /tmp/openrefine.tar.gz && mkdir -p /data
WORKDIR /opt/openrefine
EXPOSE 3333
CMD ["./refine", "-i", "0.0.0.0", "-d", "/data", "-m", "1024M"]
```

Compose service, with no `ports:`:
```yaml
  openrefine:
    build:
      context: ./openrefine
    restart: unless-stopped
    environment:
      OPENREFINE_MEMORY: 1024M
    volumes:
      - openrefine_data:/data
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:3333/command/core/get-version"]
      interval: 10s
      timeout: 3s
      retries: 12
      start_period: 30s
```
Backend and worker environment add `OPENREFINE_ENABLED: "true"` and `OPENREFINE_URL: http://openrefine:3333`; both get `depends_on: openrefine: condition: service_healthy`. Add `openrefine_data` to volumes.

Write back to the spec section 11.1: there is no official OpenRefine Docker image, so the project builds one from the official GitHub release tarball, pinned by published SHA-256 and a pinned base image digest.

- [x] **Step 10: Verify Compose config and the real API contract**

Run: `docker compose config --quiet`
Expected: exit 0.

Run a live smoke test with the built image (temporary container, not the full stack):
```bash
docker build -t osu-openrefine:test openrefine
docker run -d --rm --name or-smoke -p 127.0.0.1:3334:3333 osu-openrefine:test
python3 - <<'PY'
import time, httpx
for _ in range(30):
    try:
        if httpx.get("http://127.0.0.1:3334/command/core/get-version", timeout=1).status_code == 200:
            break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("openrefine did not start")
PY
docker stop or-smoke
```
Expected: the version endpoint returns 200.

- [x] **Step 11: Run tests and commit**

Run: `python3 -m pytest backend/tests/test_openrefine_client.py backend/tests/test_openrefine_recipes.py -q`
Expected: PASS.

```bash
git add openrefine docker-compose.yml backend/app/core/config.py backend/app/services/openrefine backend/tests/test_openrefine_client.py backend/tests/test_openrefine_recipes.py docs/superpowers/specs/2026-11-25-guided-assistant-design.md
git commit -m "feat(openrefine): add the pinned runtime, client, and built-in recipes"
```

---

### Task 7: Preview, Diff, Adoption, and Method Upgrade

**Files:**
- Create: `backend/app/services/openrefine/previews.py`
- Create: `backend/app/api/v1/openrefine.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/profiler.py`
- Modify: `backend/app/api/v1/projects_sources.py`
- Modify: `backend/app/api/v1/unification.py`
- Modify: `frontend/src/pages/WorkbenchPage.tsx`
- Modify: `frontend/src/pages/DataSourcesPage.tsx`
- Test: `backend/tests/test_openrefine_api.py`
- Test: `frontend/tests/openrefine_diff.spec.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Task 6's `OpenRefineClient`, `select_recipe`, `load_recipes`, `openrefine_limits_ok`.
- Produces: `diff_csv(before: bytes, after: bytes) -> dict` with `changed_rows`, `changed_cells`, `examples` (max 20), `before_rows`, `after_rows`, `columns_added`, `columns_removed`.
- Produces: `get_openrefine_client() -> OpenRefineClient` FastAPI dependency.
- Produces API:
  - `GET /api/v1/projects/{project_id}/openrefine/recipes?data_source_id=...` → `{"items": [...], "selected_id": "..."}`.
  - `POST /api/v1/projects/{project_id}/openrefine/previews` `{data_source_id, recipe_id?}` → preview payload with `preview_id`, `engine`, `recipe`, `diff`, `artifact_id`, `fallback_reason`.
  - `POST /api/v1/projects/{project_id}/openrefine/previews/{preview_id}/adopt` → `{data_source_id, name}`.
  - `DELETE /api/v1/projects/{project_id}/openrefine/previews/{preview_id}` → `{status: "cancelled"}`.
- Produces: `recommend_pipeline(profile, user_goal=None, openrefine_available=False)`; L1 method becomes `{"key": "openrefine_w3c", "label": "OpenRefine + W3C 契约", "stage": "L1"}` when available.

- [x] **Step 1: Write failing diff and preview API tests**

```python
def test_diff_counts_changed_rows_and_cells_with_bom_and_quotes():
    before = b'\xef\xbb\xbfid,name\n1,"Acme, Ltd"\n2,Beta\n'
    after = b'\xef\xbb\xbfid,name\n1,"Acme Ltd"\n2,Beta\n'
    diff = diff_csv(before, after)
    assert diff["changed_rows"] == 1
    assert diff["changed_cells"] == 1
    assert diff["examples"][0]["column"] == "name"
    assert diff["examples"][0]["before"] == "Acme, Ltd"

def test_preview_adopt_creates_a_new_source_and_keeps_the_original(client, project, fake_openrefine):
    before = client.get(f"/api/v1/projects/{project}/datasources").json()
    response = client.post(f"/api/v1/projects/{project}/openrefine/previews",
                           json={"data_source_id": before[0]["id"]})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["engine"] == "openrefine"
    assert preview["diff"]["changed_rows"] == 1
    adopted = client.post(f"/api/v1/projects/{project}/openrefine/previews/{preview['preview_id']}/adopt")
    assert adopted.status_code == 200
    after = client.get(f"/api/v1/projects/{project}/datasources").json()
    assert len(after) == len(before) + 1
    assert after[-1]["id"] == adopted.json()["data_source_id"]
    assert all(item["id"] != after[-1]["id"] for item in before)

def test_preview_refuses_multi_table_or_oversized_sources(client, project, large_source):
    response = client.post(f"/api/v1/projects/{project}/openrefine/previews",
                           json={"data_source_id": large_source.id})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "OPENREFINE_LIMIT_EXCEEDED"

def test_unavailable_openrefine_returns_fallback_code(client, project, fake_openrefine):
    fake_openrefine.behavior = "html_error"
    response = client.post(f"/api/v1/projects/{project}/openrefine/previews",
                           json={"data_source_id": project.ds_id})
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "OPENREFINE_UNAVAILABLE", "fallback": "rule_clean"}

def test_adopt_twice_and_cancel_after_adopt_conflict(client, project, preview):
    assert client.post(f"/api/v1/projects/{project}/openrefine/previews/{preview['preview_id']}/adopt").status_code == 200
    assert client.post(f"/api/v1/projects/{project}/openrefine/previews/{preview['preview_id']}/adopt").status_code == 409
    assert client.delete(f"/api/v1/projects/{project}/openrefine/previews/{preview['preview_id']}").status_code == 409
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_openrefine_api.py -q`
Expected: FAIL with 404 (router absent).

- [x] **Step 3: Implement diff and preview orchestration**

`diff_csv` uses `csv.reader` with `utf-8-sig`, compares by row index, treats a missing cell as a change, counts distinct changed rows/cells, and returns at most 20 examples with `row`, `column`, `before`, `after`. It never raises on ragged rows; it reports `structure_changed: true`.

`create_preview(db, project_id, data_source_id, recipe_id, actor_id)`:
1. Load the DataSource; require `source_type == "file"` and non-empty `object_key`; load bytes with `storage.get_bytes`.
2. Compute the profile from the bytes (`pandas.read_csv`) or reuse `DataSource.profile_json`; run `openrefine_limits_ok(profile, len(bytes))`; raise `OpenRefineLimitExceeded`.
3. Select the requested recipe (must be in `load_recipes()`) or `select_recipe(profile)`. If none, raise `OpenRefineNoRecipe`.
4. Create `Job(stage="L1", status="RUNNING", progress=10, params_json={"engine":"openrefine", "recipe_id":..., "datasource_id":..., "actor_id":...})` and commit.
5. `client.health()` false → mark Job FAILED with code `OPENREFINE_UNAVAILABLE` and raise `OpenRefineUnavailable`.
6. Create the project with a safe name `osu-{job.id}`, apply operations, export CSV.
7. Package the cleaned CSV as a zip-free CSV artifact via `_save_artifact` type `openrefine_csv`, filename `{stem}_openrefine.csv`.
8. Compute diff; set Job `status=SUCCESS`, `result_json={"engine":"openrefine", "recipe":..., "diff":..., "artifact_id":..., "cleaned_key":..., "project_name":..., "adopted": False}`; commit.
9. On any client error, mark Job FAILED with `error_message` and re-raise.

`adopt_preview(db, project_id, preview_id, actor_id)`:
- Load Job in project, require `params_json.engine == "openrefine"`, `status == "SUCCESS"`, `result.adopted` false; otherwise 409.
- Load cleaned bytes from the artifact, store them at `projects/{project_id}/openrefine/{job.id}/adopted_cleaned.csv`, build a profile with `profile_items`, call `recommend_pipeline(..., openrefine_available=settings.openrefine_enabled)`.
- Insert a new DataSource named `{original_stem}（OpenRefine 清洗）`, `source_type="file"`, `object_key`, `profile_json`, `routing_json`; set `result.adopted=true`, `result.adopted_data_source_id`; commit.
- Best-effort delete the temporary OpenRefine project; never fail adoption because cleanup failed.

`cancel_preview` requires the same Job checks, deletes the OpenRefine project best-effort, marks `status="CANCELLED"`, `result.cancelled=true`; commit.

- [x] **Step 4: Implement the API and method upgrade**

Add the router in `main.py`. All endpoints require `require_resource_access`; preview/adopt/cancel require admin/editor. Scope every Job and DataSource by `project_id`. Convert domain errors to:
- 404 `OPENREFINE_PREVIEW_NOT_FOUND`, `DATA_SOURCE_NOT_FOUND`
- 409 `OPENREFINE_PREVIEW_CONFLICT`
- 422 `OPENREFINE_LIMIT_EXCEEDED`, `OPENREFINE_NO_RECIPE`, `OPENREFINE_UNSUPPORTED_SOURCE`
- 502 `OPENREFINE_RECIPE_FAILED`
- 503 `OPENREFINE_UNAVAILABLE` with `fallback: "rule_clean"`

Method upgrade:
```python
def _method_for_stage(stage, *, table_count=0, total_rows=0, legacy=False, openrefine_available=False):
    if stage == "L1" and openrefine_available:
        return {"key": "openrefine_w3c", "label": "OpenRefine + W3C 契约", "stage": "L1",
                "description": "用内置 OpenRefine 配方清洗，再导出 W3C 标准数据。",
                "reason": reason, "available": True}
```
`recommend_pipeline` passes the flag. `list_datasources` and the demo endpoint compute `openrefine_available = settings.openrefine_enabled and openrefine_limits_ok(profile, size)[0]` for L1. Legacy routing in `method_for_routing(routing, openrefine_available=False)` upgrades only L1 without a stored method.

- [x] **Step 5: Run backend tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_openrefine_api.py backend/tests/test_profiler.py backend/tests/test_unification_demo.py -q`
Expected: PASS.

- [x] **Step 6: Add the workbench preview UI**

In `WorkbenchPage.tsx`, when `routing.method.key === "openrefine_w3c"` and stage L1:
- Show buttons `OpenRefine 预览` and `运行规则清洗`.
- `OpenRefine 预览` calls the preview endpoint and renders a diff card: changed rows, changed cells, columns added/removed, a two-column before/after table for up to 20 examples, and buttons `采用并生成新数据源` / `撤销`.
- Adopt refreshes the data source list and shows `已生成：{name}` with a `查看数据源` link.
- 503 shows `OpenRefine 不可用，已降级为规则清洗` and a `运行规则清洗` button.
- The generic L1 job flow stays for the rule-cleaning fallback.

In `DataSourcesPage.tsx`, `METHOD_LABELS` adds `openrefine_w3c: "OpenRefine + W3C 契约"` and the method button text stays `进入处理方法`.

- [x] **Step 7: Add the Playwright diff spec**

The spec seeds a workspace/project/data source (small dirty CSV in local storage), starts a fake OpenRefine server in-process, overrides `get_openrefine_client`, starts FastAPI on 8765, opens `#/tasks/new/match`, clicks `OpenRefine 预览`, asserts the diff count and one before/after example, clicks `采用并生成新数据源`, and asserts the new source name. It also covers the 503 fallback by switching the fake to `html_error` and asserting `已降级为规则清洗`.

Run: `python3 frontend/tests/openrefine_diff.spec.py`
Expected: exit 0.

- [x] **Step 8: Add the spec to CI and commit**

Append `python frontend/tests/openrefine_diff.spec.py` to the frontend job.

```bash
git add backend/app/services/openrefine/previews.py backend/app/api/v1/openrefine.py backend/app/main.py backend/app/services/profiler.py backend/app/api/v1/projects_sources.py backend/app/api/v1/unification.py backend/tests/test_openrefine_api.py frontend/src/pages/WorkbenchPage.tsx frontend/src/pages/DataSourcesPage.tsx frontend/tests/openrefine_diff.spec.py .github/workflows/ci.yml
git commit -m "feat(openrefine): preview, diff, adopt, and route to the runtime"
```

---

### Task 8: JSON-LD Mapping Preview and Export

**Files:**
- Create: `backend/app/services/contracts/jsonld.py`
- Modify: `backend/app/api/v1/contracts.py`
- Create: `backend/tests/test_contract_jsonld.py`
- Modify: `frontend/src/pages/ContractsPage.tsx`
- Modify: `frontend/src/components/ResultViewer.tsx`
- Create: `frontend/tests/jsonld_viewer.spec.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `ContractModelVersion.artifacts_json["linkml_yaml"]`, `canonical_model_json`; `DataSource.object_key`; `storage.get_bytes`.
- Produces: `jsonld_model(version) -> dict` with `classes`, `prefixes`, `base_uri`.
- Produces: `infer_jsonld_mapping(model, columns) -> dict` with `class_name`, `id_slot`, `columns`.
- Produces: `build_jsonld(model, rows, mapping, *, limit=5000) -> dict` with `context`, `graph`, `count`, `truncated`.
- Produces API:
  - `GET /api/v1/projects/{project_id}/contracts/versions/{version_id}/jsonld/preview?data_source_id=...` → `{model, inferred_mapping}`.
  - `POST /api/v1/projects/{project_id}/contracts/versions/{version_id}/jsonld` `{data_source_id, class_name, id_slot, columns}` → `{context, graph, count, truncated}`.

- [x] **Step 1: Write failing JSON-LD tests**

```python
def test_infers_class_id_and_slot_mapping_from_columns(contract_version):
    model = jsonld_model(contract_version)
    mapping = infer_jsonld_mapping(model, ["company_name", "tax_id", "city"])
    assert mapping["class_name"].startswith("Supplier")
    assert mapping["id_slot"] == "tax_id"
    assert mapping["columns"]["company_name"] == "company_name"

def test_builds_context_and_graph_with_overrides(contract_version, rows):
    model = jsonld_model(contract_version)
    payload = build_jsonld(model, rows, {"class_name": "Supplier", "id_slot": "tax_id",
        "columns": {"company_name": "legal_name", "tax_id": "tax_id"}})
    assert payload["context"]["@vocab"].endswith("/")
    assert payload["graph"][0]["@type"] == "Supplier"
    assert payload["graph"][0]["@id"].endswith("91310000TEST")
    assert payload["graph"][0]["legal_name"] == "Acme"

def test_rejects_unknown_class_or_missing_id_slot(contract_version, rows):
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(jsonld_model(contract_version), rows, {"class_name": "Nope", "id_slot": "tax_id", "columns": {}})
    assert exc.value.code == "UNKNOWN_CLASS"
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(jsonld_model(contract_version), rows, {"class_name": "Supplier", "id_slot": "", "columns": {}})
    assert exc.value.code == "MISSING_ID_SLOT"

def test_missing_contract_version_returns_404(client, project):
    response = client.post(f"/api/v1/projects/{project}/contracts/versions/nope/jsonld",
                           json={"data_source_id": "ds-1", "class_name": "Supplier", "id_slot": "tax_id", "columns": {}})
    assert response.status_code == 404
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_contract_jsonld.py -q`
Expected: FAIL with missing module.

- [x] **Step 3: Implement the JSON-LD service**

Parse `canonical_model_json` for classes and attributes; parse the LinkML YAML only for `id`, `prefixes`, and `default_prefix`. `base_uri` = `document["id"]` if it ends with `/`, else `https://w3id.org/linkml/` fallback.
- `jsonld_model` returns classes with attributes (`range`, `identifier`, `required`), identifier slot per class, prefixes, base URI.
- `infer_jsonld_mapping` chooses the class with an identifier attribute (first class name sorted if none), picks the identifier slot, and maps each slot to a column by casefold/underscore-insensitive equality; no fuzzy guessing.
- `build_jsonld` validates class, slots, ID slot, and that a column maps to at most one slot. Context = `{"@vocab": base_uri, "@base": base_uri}` plus one term per mapped slot (`base_uri + slot`). Each graph node uses `@id` `f"urn:osu:{value}"` when the ID value exists, otherwise `f"urn:osu:row-{index}"`; `@type` class name; values coerced for `integer`/`float`/`double`/`boolean`, skipped when empty. Limit 5000 rows, set `truncated`.
- Raise `JsonLdError` subclasses with codes `UNKNOWN_CLASS`, `UNKNOWN_SLOT`, `MISSING_ID_SLOT`, `DUPLICATE_COLUMN_MAPPING`, `NO_CONTRACT`.

- [x] **Step 4: Implement the endpoints**

Load the version with `get_contract_version`; 404 when missing. Load the DataSource and require CSV file; 404/422 otherwise. Read headers only for preview; read up to 5000 rows for generation. Return 422 with `{"code": ..., "message": ...}` on JSON-LD validation errors.

- [x] **Step 5: Run backend tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_contract_jsonld.py backend/tests/test_contracts_api.py -q`
Expected: PASS (adjust the existing test filename if it differs).

- [x] **Step 6: Add the contract JSON-LD UI**

In `ContractsPage.tsx`, each version row gets a `JSON-LD` action. It opens an inline panel:
- Data source select (file sources), class select, ID slot select.
- A slot table with one row per class slot: slot name, range, required, column select.
- `生成预览` calls the preview then the generation endpoint; `导出 JSON-LD` downloads the generated payload as `contract-v{version}.jsonld`.
- `ResultViewer` gains a `jsonld` kind that pretty-prints `@context`/`@graph` and lists record count.
- If no contract version exists, the panel shows `先上传并编译契约，再导出 JSON-LD`.

- [x] **Step 7: Add the Playwright JSON-LD spec**

Seed a contract source/version and a cleaned CSV; open `#/tasks/contract/{version_id}`; click `JSON-LD`; assert class/slot mapping; click `生成预览`; assert `@context` and one record appear; click `导出 JSON-LD` and assert a download with `.jsonld` is offered.

Run: `python3 frontend/tests/jsonld_viewer.spec.py`
Expected: exit 0.

- [x] **Step 8: Add the spec to CI and commit**

```bash
git add backend/app/services/contracts/jsonld.py backend/app/api/v1/contracts.py backend/tests/test_contract_jsonld.py frontend/src/pages/ContractsPage.tsx frontend/src/components/ResultViewer.tsx frontend/tests/jsonld_viewer.spec.py .github/workflows/ci.yml
git commit -m "feat(contracts): map and export JSON-LD instances"
```

---

### Task 9: Docker Integration, Documentation, and Full Verification

**Files:**
- Create: `backend/tests/integration/test_compose_openrefine.py`
- Modify: `.github/workflows/ci.yml`
- Create: `docs/how-to/openrefine.md`
- Modify: `README.md`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: an opt-in Compose test that runs a real OpenRefine recipe and adopts the result.

- [x] **Step 1: Write the opt-in Compose OpenRefine test**

Mirror `test_clean_compose_migrate_restart_async_publish_and_download`'s setup (free ports, generated secrets, isolated project name) but start only `postgres`, `redis`, `minio`, `openrefine`, `backend`, and `worker`; the frontend image is already covered by the existing test. Steps:
1. Wait for `/health` and `openrefine` health.
2. Log in as the generated admin, create a project.
3. Upload a 5-row dirty CSV via multipart to the existing upload endpoint.
4. `POST /openrefine/previews`; assert `engine == "openrefine"`, `diff.changed_rows >= 1`, and the artifact id exists.
5. `POST .../adopt`; assert a new DataSource and that the original CSV bytes are unchanged by re-reading the original source.
6. Cancel a second preview and assert its OpenRefine project is gone (`GET /command/core/get-all-project-metadata` no longer lists it).
7. Tear down with `docker compose down --volumes --remove-orphans`.

Skip unless `RUN_COMPOSE_RELEASE_TESTS=true`.

- [x] **Step 2: Run the Compose integration test**

Run: `RUN_COMPOSE_RELEASE_TESTS=true python3 -m pytest backend/tests/integration/test_compose_openrefine.py -q`
Expected: PASS. Record the runtime and any image pull/build time.

- [x] **Step 3: Add the test to CI**

The compose job already runs with `RUN_COMPOSE_RELEASE_TESTS=true`; append the new test file to its pytest command.

- [x] **Step 4: Write documentation**

`docs/how-to/openrefine.md` covers: enabling the sidecar, the built-in recipe list, limits and fallbacks, preview/adopt/cancel semantics, JSON-LD mapping, and how to verify with the Compose test. `README.md` links it under Guides and states that OpenRefine is an internal sidecar, not an external service.

- [x] **Step 5: Run all verification**

Run:
```bash
python3 -m pytest backend/tests -q
python3 -m pytest tests/test_decision_model_holdout.py -q
python3 tests/formal/check_decision_dispatch_boundary.py
python3 scripts/evaluate_decision_models.py --validate-only
npm --prefix frontend run build
for spec in unification_workbench contracts_workbench decision_models_workbench menu_shell deep_link guided_assistant review_readability result_viewer method_recommendation openrefine_diff jsonld_viewer; do
  python3 frontend/tests/$spec.spec.py || exit 1
done
```
Expected: backend all pass; holdout 7 passed; formal PASS; manifest VALID; build succeeds; every spec exits 0.

- [x] **Step 6: Commit**

```bash
git add backend/tests/integration/test_compose_openrefine.py .github/workflows/ci.yml docs/how-to/openrefine.md README.md docker-compose.yml
git commit -m "test(openrefine): prove the runtime, adoption, and W3C export end to end"
```
