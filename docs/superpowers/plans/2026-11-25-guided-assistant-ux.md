# Guided Assistant and Readability UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent assistant, four top-level menus, plain-language entity review, online result viewing, and method guidance to the semantic unification workbench.

**Architecture:** The backend adds method metadata to the profiler, a review explanation payload, an assistant planning endpoint, and an idempotent entity demo run. The frontend adds a hash-routed four-menu shell, a task aggregation page, an assistant panel that drives existing task APIs, a shared review card, and a result viewer. Delivery two (OpenRefine runtime and W3C export) is a separate plan and is not implemented here.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, pytest, React 19, TypeScript, Vite, Tailwind CSS, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-25-guided-assistant-design.md`

## Global Constraints

- Four top-level menus only: 数据 / 任务 / 复核 / 设置. The assistant is a persistent right panel, not a menu.
- First delivery labels the existing L1 path `规则清洗 + W3C 契约`. Never label it OpenRefine before the runtime exists.
- The assistant asks for one confirmation, then may auto-run. It must never auto-publish a contract, publish a snapshot, merge entities, or decide a review.
- The assistant LLM uses the default `openai_compatible` provider. It sends only the user message, current page, and a data summary of counts and field names. It never sends record values. Failures fall back to deterministic rules.
- `GET /unification/reviews` keeps `items` limited to `action == "REVIEW"` and adds a separate `auto_separated` list of meaningful `DISTINCT` pairs, capped at 20.
- Keep the existing review strings `模型建议 · SAME`, confidence, distribution, model version, provider kind/key, and latency visible. Move only evidence codes and raw JSON behind a disclosure.
- One nullable `unification_runs.input_source_system` column migration is allowed for the demo scope, with no backfill. No other database migration in this plan; use existing tables and file-backed constants.
- Every new backend test module starts by inserting `backend` into `sys.path`, matching the existing tests. Every new Playwright spec starts its own isolated backend and uses the existing `open_route` helper.
- Reuse existing page components. Do not rewrite the unification, contracts, or workbench business logic.
- All primary actions use the existing neutral theme. No new gradients, emoji, or decorative dark headers.
- Backend tests use `python3 -m pytest`. Frontend tests are standalone Playwright scripts under `frontend/tests/`. The three existing specs must stay green.

## Review Focus

1. Assistant auto-run when the user has no data and demo loading or job submission fails. The panel must stop, show the error, and link to the manual page instead of leaving a half-created task.
2. Assistant free text when the default provider is missing, returns invalid JSON, or returns low confidence. The panel must fall back to deterministic suggestions and stay usable.
3. Auto-separated explanations when there are zero qualifying pairs or more than 20. The UI must show an empty state or the capped list plus the total.
4. CSV viewing with quoted commas, a UTF-8 BOM, and a single-column file. The table must stay aligned and never crash.
5. Deep links to a job, run, or contract version that no longer exists. The task detail must show a clear not-found state with a back link.

---

### Task 1: Method Recommendation and LinkML Detection

**Files:**
- Modify: `backend/app/services/profiler.py`
- Modify: `backend/app/api/v1/projects_sources.py` (`list_datasources`)
- Create: `backend/tests/test_profiler.py`

**Interfaces:**
- Consumes: existing `profile_items(items: list[dict]) -> dict` and `recommend_pipeline(profile: dict, user_goal: str | None = None) -> dict`.
- Produces: `profile["has_linkml_contract"]: bool`; `routing["method"]` with keys `key`, `label`, `stage`, `description`, `reason`, `available`; `method_for_routing(routing: dict | None) -> dict` for legacy rows.

- [ ] **Step 1: Write failing tests for the four method routes and LinkML detection**

```python
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.profiler import profile_items, recommend_pipeline


def test_small_dirty_table_uses_rule_clean():
    routing = recommend_pipeline({
        "total_rows": 250, "table_count": 1, "has_whitespace_noise": True,
        "has_metric_yaml": False, "has_schemasheet_tsv": False,
        "has_jargon": False, "detected_keys": {"name_col": "supplier_raw_name"},
    })
    assert routing["method"] == {
        "key": "rule_clean_w3c",
        "label": "规则清洗 + W3C 契约",
        "stage": "L1",
        "description": "先清洗小表，再用 LinkML 契约固化标准字段。",
        "reason": "检测到 1 张 250 行的小表，字段有空格和大小写噪声。",
        "available": True,
    }


def test_two_tables_use_splink_duckdb():
    routing = recommend_pipeline({"total_rows": 35000, "table_count": 2, "detected_keys": {}})
    assert routing["method"]["key"] == "splink_duckdb"
    assert routing["method"]["stage"] == "L2"
    assert "Splink" in routing["method"]["label"]


def test_metric_yaml_uses_metrics_method():
    routing = recommend_pipeline({"total_rows": 0, "table_count": 0, "has_metric_yaml": True, "detected_keys": {}})
    assert routing["method"]["key"] == "metrics_yaml"
    assert routing["method"]["stage"] == "L4"


def test_linkml_yaml_is_detected_and_routes_to_contract():
    text = "id: https://example.org/supplier\nclasses:\n  Supplier:\n    attributes:\n      name:\n        required: true\ndefault_range: string\n"
    profile = profile_items([{"kind": "yaml", "name": "supplier.yaml", "text": text}])
    assert profile["has_linkml_contract"] is True
    routing = recommend_pipeline(profile)
    assert routing["method"]["key"] == "contract_w3c"
    assert routing["method"]["stage"] == "L3"


def test_method_for_routing_backfills_legacy_rows():
    from app.services.profiler import method_for_routing
    assert method_for_routing({"recommended_stage": "L2"})["key"] == "splink_duckdb"
    existing = {"recommended_stage": "L1", "method": {"key": "custom", "label": "Custom"}}
    assert method_for_routing(existing)["key"] == "custom"
    assert method_for_routing(None)["key"] == "rule_clean_w3c"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_profiler.py -q`
Expected: FAIL with `KeyError: 'method'` and `KeyError: 'has_linkml_contract'`.

- [ ] **Step 3: Implement the method metadata**

In `profiler.py`, add a module constant `METHOD_META` with the four entries. In `profile_items`, set `has_linkml_contract` true when a YAML item contains `classes:` and any of `prefixes:`, `default_range:`, `imports:`. Add an explicit branch to the stage chain: `elif profile.get("has_linkml_contract"): stage, conf = "L3", 0.98`, before the table/row-count branch. Then set `method` from `METHOD_META` after the stage is chosen. Add `method_for_routing(routing)` that returns `routing["method"]` when present, otherwise maps `routing["recommended_stage"]` to `METHOD_META`, defaulting to `rule_clean_w3c`. In `list_datasources`, return `"routing": {**d.routing_json, "method": method_for_routing(d.routing_json)}` so legacy rows do not break the UI. Build `reason` in one plain sentence from the stage:

- L1: `检测到 {table_count} 张 {total_rows} 行的小表，字段有空格和大小写噪声。`
- L2: `检测到 {table_count} 张表共 {total_rows} 行，需要跨库匹配。`
- L3: `检测到 LinkML 契约结构。`
- L4: `检测到指标 YAML。`

Keep every existing routing field unchanged. Set `available` to `True`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_profiler.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/profiler.py backend/app/api/v1/projects_sources.py backend/tests/test_profiler.py
git commit -m "feat(profiler): expose method recommendations for clean, match, contract, and metrics"
```

---

### Task 2: Review Explanation Payload and Auto-Separated List

**Files:**
- Create: `backend/app/services/unification/reviews.py`
- Modify: `backend/app/api/v1/unification.py` (`list_unification_reviews`)
- Create: `backend/tests/test_unification_reviews.py`

**Interfaces:**
- Produces: `record_payload(revision, scheme) -> dict`, `compare_attributes(left_attributes, right_attributes, scheme) -> list[dict]`, `explain_review(evidence_codes: tuple[str, ...]) -> str`, `suggest_action(verdict: str | None, evidence_codes: tuple[str, ...]) -> str`, `auto_separated_items(run_result: dict, revisions_by_id: dict, scheme, cap: int = 20) -> list[dict]`.
- Response: `{"items": [...], "auto_separated": [...], "total": int, "auto_separated_total": int, "run_id": str}`.

- [ ] **Step 1: Write failing unit tests for the explanation and comparison helpers**

```python
from app.services.unification.reviews import compare_attributes, explain_review, suggest_action


def test_missing_strong_identifier_explains_why_review_is_needed():
    assert "缺少可核对的强标识" in explain_review(("CANDIDATE", "STRONG_IDENTIFIER_MISSING", "WEAK_EVIDENCE_ONLY"))


def test_conflict_explains_why_pair_was_separated():
    assert "强标识不一致" in explain_review(("CANDIDATE", "STRONG_IDENTIFIER_CONFLICT"))


def test_comparison_marks_match_different_and_missing():
    scheme = type("Scheme", (), {"field_map": {"name": "name", "tax_id": "tax_id"}, "normalizers": []})()
    rows = compare_attributes({"name": "Acme Ltd", "tax_id": None}, {"name": "acme ltd", "tax_id": "T-1"}, scheme)
    assert [(row["field"], row["status"]) for row in rows] == [("name", "DIFFERENT"), ("tax_id", "MISSING")]


def test_suggestion_is_never_a_merge_for_unknown():
    assert suggest_action("UNKNOWN", ("CANDIDATE",)) == "建议分离"
    assert suggest_action("SAME", ("CANDIDATE", "WEAK_EVIDENCE_ONLY", "STRONG_IDENTIFIER_MISSING")) == "建议分离"
    assert suggest_action("SAME", ("CANDIDATE", "STRONG_IDENTIFIER_MATCH")) == "建议合并"
    assert suggest_action("DIFFERENT", ("CANDIDATE",)) == "建议分离"


def test_auto_separated_includes_human_cannot_link():
    class Revision:
        def __init__(self, revision_id):
            self.id = revision_id
            self.attributes = {}
            self.source_record = type("SourceRecord", (), {
                "source_system": "s", "dataset": "d", "source_record_id": revision_id,
            })()

    scheme = type("Scheme", (), {"field_map": {}, "normalizers": []})()
    pair = {"action": "DISTINCT", "evidence_codes": ["CANDIDATE", "CANNOT_LINK"], "left_source_revision_id": "L", "right_source_revision_id": "R"}
    revisions = {"L": Revision("L"), "R": Revision("R")}
    items = auto_separated_items({"pairs": [pair]}, revisions, scheme)
    assert len(items) == 1 and "CANNOT_LINK" in items[0]["evidence_codes"]


def test_auto_separated_is_empty_safe_and_capped():
    assert auto_separated_items({}, {}, None) == []

    class Revision:
        def __init__(self, revision_id):
            self.id = revision_id
            self.attributes = {}
            self.source_record = type("SourceRecord", (), {
                "source_system": "s", "dataset": "d", "source_record_id": revision_id,
            })()

    scheme = type("Scheme", (), {"field_map": {}, "normalizers": []})()
    pairs = [
        {"action": "DISTINCT", "evidence_codes": ["CONFLICT_CONSTRAINT"], "left_source_revision_id": f"L{i}", "right_source_revision_id": f"R{i}"}
        for i in range(25)
    ]
    revisions = {f"L{i}": Revision(f"L{i}") for i in range(25)} | {f"R{i}": Revision(f"R{i}") for i in range(25)}
    assert len(auto_separated_items({"pairs": pairs}, revisions, scheme, cap=20)) == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_unification_reviews.py -q`
Expected: FAIL with `ModuleNotFoundError` or missing symbols.

- [ ] **Step 3: Implement the helper module**

Use the scheme's `field_map` and `normalizers`. `compare_attributes` compares normalized values and returns `MATCH`, `DIFFERENT`, or `MISSING`; it displays the raw values. `record_payload` returns `record_ref`, `source_system`, `dataset`, `source_record_id`, and the canonical `attributes` subset. `explain_review` returns the four sentence families from the spec. `suggest_action(verdict, evidence_codes)` returns `建议合并` only when the verdict is `SAME` and `STRONG_IDENTIFIER_MATCH` is in the evidence codes; every other case returns `建议分离`. `auto_separated_items` keeps only `action == "DISTINCT"` pairs whose evidence codes intersect `{"CONFLICT_CONSTRAINT", "STRONG_IDENTIFIER_CONFLICT", "CANNOT_LINK", "TRANSITIVE_CONFLICT"}`, builds the same payload with `why`, sorts by record refs, and slices to `cap`. `human_cannot_link` is an evidence `rule`, not an evidence code.

- [ ] **Step 4: Wire the endpoint and add the endpoint test**

In `list_unification_reviews`, load the scheme from `UnificationSchemeVersion` and build `items` by keeping every existing pair field (`left_source_revision_id`, `right_source_revision_id`, `candidate`, `action`, `score`, `evidence_codes`, `evidence`, `recommendation`, `model_error`) and adding `left`, `right`, `comparison`, `why`, `suggestion`, and `consequence`. Do not replace the pair dict; the existing unification spec reads the raw revision ids from this response. Build `auto_separated` from the full run result. The revision query must include every revision referenced by both the REVIEW pairs and the DISTINCT pairs that can enter `auto_separated`; do not build `revisions_by_id` from REVIEW pairs only. Keep the existing pending-decision filtering for `items`.

Add `from test_unification_public_api import public_api  # noqa: F401` at the top of the test module. Seed a project, scheme version, four source records/revisions, and a `UnificationRun` whose `result_json` contains one REVIEW pair with `STRONG_IDENTIFIER_MISSING` using revisions `rev-review-left/right`, and one DISTINCT pair with `CANNOT_LINK` using different revisions `rev-distinct-left/right`. Assert `items[0]["left_source_revision_id"] == "rev-review-left"` and `items[0]["right_source_revision_id"] == "rev-review-right"` (raw fields are preserved), `items[0]["why"]` mentions the missing strong identifier, `items[0]["comparison"]` has the expected fields, and `auto_separated[0]["left"]["attributes"]` is non-empty, proving the DISTINCT revisions were loaded.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_unification_reviews.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/unification/reviews.py backend/app/api/v1/unification.py backend/tests/test_unification_reviews.py
git commit -m "feat(reviews): explain why a pair needs review and list auto-separated pairs"
```

---

### Task 3: Assistant Planning Endpoint

**Files:**
- Create: `backend/app/services/assistant.py`
- Create: `backend/app/services/provider_selection.py`
- Create: `backend/app/api/v1/assistant.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_assistant.py`

**Interfaces:**
- Produces: `POST /api/v1/projects/{project_id}/assistant/plan`, body `{"message": str, "page": str}`, response `{"intent": str, "confidence": float, "explanation": str, "suggested_task": str, "source": "model" | "rules"}`. Intents: `clean`, `match`, `metrics`, `contract`, `review`, `unknown`; `select_openai_provider(db, workspace_id) -> LLMProvider | None`; `select_decision_provider(db, workspace_id) -> LLMProvider | None`.
- Consumes: `LLMProvider`, `DataSource.profile_json`, `llm_client.call_llm_json`.

- [ ] **Step 1: Write failing tests with a controlled OpenAI-compatible server and a no-provider fallback**

Use the `_OpenAIHandler` pattern from `backend/tests/test_unification_llm.py`. Seed a workspace, project, default `openai_compatible` provider pointing at the controlled server, and a data source with `profile_json` containing `tables[0].preview[0].name = "SECRET-RECORD-VALUE"`. Call the endpoint with `{"message":"帮我把客户名单去重","page":"data"}`. Assert:

- response `source == "model"`
- response `intent == "match"`
- the controlled server's recorded prompt contains `"match"` and `"field_names"` or a column name, and does not contain `SECRET-RECORD-VALUE`.

Then delete the provider and call again; assert `source == "rules"` and `intent == "match"`. Add a third case with the server returning `not json`; assert the response still succeeds with `source == "rules"`.

Add a fourth case: make a `typesafe_jev` provider `is_default=True` and leave the controlled `openai_compatible` provider `is_default=False`. Call the endpoint and assert it still uses the openai provider (`source == "model"`). This proves the two provider roles are selected independently by kind.

Add a fifth case: insert an `offline` provider (`provider_key="offline"`, `provider_kind="openai_compatible"`, empty base URL and API key) with an earlier `created_at` than the controlled provider, and make it `is_default=False`. Call the endpoint and assert it still uses the controlled provider, not `offline`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest backend/tests/test_assistant.py -q`
Expected: FAIL with 404 or import errors.

- [ ] **Step 3: Implement the service and route**

`data_summary(db, project_id)` returns `{"source_count", "total_rows", "table_count", "field_names": [...]}` from `profile_json`, never `preview`. `provider_selection.py` defines `select_openai_provider(db, workspace_id)` and `select_decision_provider(db, workspace_id)`. Each filters its kind (`openai_compatible` or `perplexity_decisions/typesafe_jev`), orders `is_default` first then `created_at`, and returns the first usable row. `select_openai_provider` skips `provider_key == "offline"` and rows with an empty base URL and an empty encrypted key; a decision provider being default must not hide the openai provider. `classify_assistant_message` uses `select_openai_provider`, decrypts the key, and calls `call_llm_json` with a prompt that lists the allowed intents and asks for `{"intent","confidence","explanation"}`. Validate the result; on any error or confidence below `0.55`, call `deterministic_intent(message)`. `deterministic_intent` matches the keyword lists: clean, match, metrics, contract, review; unknown otherwise. Register the router in `main.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest backend/tests/test_assistant.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/assistant.py backend/app/services/provider_selection.py backend/app/api/v1/assistant.py backend/app/main.py backend/tests/test_assistant.py
git commit -m "feat(assistant): classify goals with the default provider and a deterministic fallback"
```

---

### Task 4: Entity Demo Run Endpoint

**Files:**
- Create: `backend/app/demo/entity_demo.py`
- Modify: `backend/app/api/v1/unification.py`
- Modify: `backend/app/models/unification.py` (`UnificationRun.input_source_system`)
- Create: `backend/alembic/versions/<new>_add_run_input_source_system.py`
- Modify: `backend/app/services/unification/releases.py` (`submit_run`, `_current_input_manifest`, `_execute_unification_run`, `_snapshot_result_at_publication`)
- Create: `backend/tests/test_unification_demo.py`

**Interfaces:**
- Produces: `POST /api/v1/projects/{project_id}/unification/demo`, response `{"run_id": str, "scheme_key": "assistant_entity_demo", "revision": int}`; `select_decision_provider(db, workspace_id) -> LLMProvider | None`.
- Changes: `UnificationRun.input_source_system: str | None`; `submit_run(..., input_source_system: str | None = None)`; `_current_input_manifest(db, project_id, input_source_system=None)` filters `SourceRecord.source_system` when the argument is set. Existing callers keep passing `None`.
- Consumes: `save_scheme`, `ingest_batch`, `submit_run_service`, `dispatch_unification_run`, `select_decision_provider` from `app.services.provider_selection`.

- [ ] **Step 1: Write the failing endpoint test**

Add `from test_unification_public_api import public_api  # noqa: F401` and use the fixture. Seed a pre-existing `SourceRecord` with `source_system="erp"` whose tax id matches one of the demo records. Patch `app.api.v1.unification.dispatch_unification_run` to a function that opens the fixture maker and calls `execute_unification_run`, so the run does not use the global `SessionLocal`. Call the endpoint. Poll `GET /projects/{id}/unification/runs/{run_id}` until terminal. Assert the run is `SUCCEEDED`, the result has 3 pairs, the input manifest contains only the six `assistant_demo` records and not the `erp` record, at least one pair has `action == "REVIEW"` and at least one has `action == "DISTINCT"`, no pair has `action == "AUTO_MATCH"`, no `IdentityDecisionRecord` with action `MERGE` exists for the project, and no snapshot exists. Call the endpoint again and assert it returns the same `run_id`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest backend/tests/test_unification_demo.py -q`
Expected: FAIL with 404.

- [ ] **Step 3: Implement the demo records and endpoint**

`entity_demo.py` defines six records, all with `source_system="assistant_demo"`. None may form an `AUTO_MATCH` pair:

- `acme-erp` / `acme-crm`: similar names, no tax id -> REVIEW.
- `bluefin-a` / `bluefin-b`: similar names, no tax id -> REVIEW.
- `cedar-a` / `cedar-b`: same name, different tax ids -> DISTINCT.

The endpoint saves the scheme `assistant_entity_demo` with `entity_type="Supplier"`, `identity_granularity="legal_entity"`, `normalizers=["unicode_nfkc","trim","casefold","strip_punctuation"]`, field_map `{"name":"name","tax_id":"tax_id","phone":"phone"}`, strong identifier `tax_id`, conflict constraint on `tax_id`, candidate rules `exact tax_id` and `name_similarity name 0.82`, and failure policy `REVIEW`. It calls `select_decision_provider(db, workspace_id)`; when a provider exists, set `model_judgment_enabled=True` and `model_provider_id=provider.id`; otherwise disable model judgment. It ingests with `input_version="assistant-entity-demo-v1"`, submits with idempotency key `assistant-entity-demo-v1` and `input_source_system="assistant_demo"`, and returns the run. On `RunConflict`, query the existing run by `input_version` and return it. Do not publish a snapshot.

In `releases.py`, `_current_input_manifest` adds `.where(SourceRecord.source_system == input_source_system)` when the filter is set, and `submit_run` forwards the optional argument. Persist it on the run with a nullable `input_source_system` column and migration. `_execute_unification_run` must pass `run.input_source_system` at both manifest re-reads (`releases.py:357` and `:510`), and `_snapshot_result_at_publication` must do the same at `:599`; filtering only at submit makes the run fail with `RunConflict` whenever any non-demo record exists. Include the filter in the demo endpoint's payload hash so an idempotency key cannot collide with an unfiltered run.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest backend/tests/test_unification_demo.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/demo/entity_demo.py backend/app/api/v1/unification.py backend/app/models/unification.py backend/alembic/versions backend/app/services/unification/releases.py backend/tests/test_unification_demo.py
git commit -m "feat(unification): add an idempotent entity demo run for the assistant"
```

---

### Task 5: Four-Menu Shell, Task Page, Settings, and Existing Spec Updates

**Files:**
- Create: `frontend/src/lib/router.ts`
- Create: `frontend/src/lib/tasks.ts`
- Create: `frontend/src/pages/TasksPage.tsx`
- Create: `frontend/src/pages/TaskDetailPage.tsx`
- Create: `frontend/src/pages/SettingsPage.tsx`
- Create: `frontend/src/pages/ReviewHomePage.tsx`
- Create: `frontend/src/components/ApiKeysPanel.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/WorkbenchPage.tsx`
- Modify: `frontend/src/pages/UnificationPage.tsx`
- Modify: `frontend/src/pages/ContractsPage.tsx`
- Modify: `frontend/src/pages/ServingReleasePage.tsx`
- Create: `frontend/tests/ui_helpers.py`
- Create: `frontend/tests/menu_shell.spec.py`
- Create: `frontend/tests/deep_link.spec.py`
- Modify: `frontend/tests/unification_workbench.spec.py`
- Modify: `frontend/tests/contracts_workbench.spec.py`
- Modify: `frontend/tests/decision_models_workbench.spec.py`

**Interfaces:**
- `Route` union and `parseRoute(hash: string): Route`, `routeHash(route: Route): string`.
- `TaskItem` and `loadTasks(projectId: string): Promise<TaskItem[]>`.

```ts
export type Route =
  | { view: 'data' }
  | { view: 'tasks' }
  | { view: 'tasks-new'; kind: 'clean' | 'match' | 'metrics' | 'contract' | 'entity' }
  | { view: 'tasks-job'; jobId: string }
  | { view: 'tasks-run'; runId: string }
  | { view: 'tasks-contract'; versionId: string }
  | { view: 'tasks-publish' }
  | { view: 'reviews' }
  | { view: 'settings'; tab: 'providers' | 'team' | 'api-keys' }

export type TaskItem = {
  id: string
  type: 'L1' | 'L2' | 'L3' | 'L4' | 'ENTITY'
  title: string
  status: string
  createdAt: string
  detail:
    | { kind: 'job'; jobId: string; stage: 'L1' | 'L2' | 'L4' }
    | { kind: 'run'; runId: string }
    | { kind: 'contract'; versionId: string }
}
```

- `WorkbenchPage` props add `initialJobId?: string`, `initialStage?: 'L1' | 'L2' | 'L4'`; when `activeRouting` is null it fetches `/datasources` and uses the latest source routing locally.
- `UnificationPage` props add `initialRunId?: string`; it keeps `canEdit`.
- `TaskDetailPage` props: `{project: ProjectItem, canEdit: boolean, activeDsIds: string[], activeProfile: any, activeRouting: any, jobId?: string, runId?: string, versionId?: string, newKind?: TaskType, onGoDataSources: () => void, onGoContracts: () => void}`; use `project.id` for the embedded pages.
- `ContractsPage` props add `initialVersionId?: string`; when set, open the `versions` tab and highlight that row. If the id is not in the first 50 versions, fetch `GET /contracts/versions/{id}` and prepend it.
- `UnificationPage` props add `initialRunId?: string`; if the id is not in the first 20 runs, fetch `GET /unification/runs/{id}` and prepend it.
- `SettingsPage` props `{projectId: string, currentUser: UserInfo, canEdit: boolean}` with tabs `providers`, `team`, `api-keys`.
- `ReviewHomePage` props `{projectId: string}`.

- [ ] **Step 1: Write the failing menu shell spec**

Create `frontend/tests/menu_shell.spec.py` using the isolated backend pattern from `unification_workbench.spec.py`. Seed one workspace and project. After login, assert exactly four menu buttons: 数据, 任务, 复核, 设置. Click 任务 and assert a heading `任务`. Click 复核 and assert `人工复核` and `实体配对` text. Click 设置 and assert tabs `模型 Provider`, `团队`, `API Key`. Visit `http://127.0.0.1:5173/#/tasks/publish` and assert the `发布中心` heading and the empty-state copy `还没有已生成的版本`; the publish button is asserted in Task 8 after seeding a releasable artifact. Visit `http://127.0.0.1:5173/#/tasks/new/entity` and assert the `实体统一工作台` heading. Visit `http://127.0.0.1:5173/#/tasks/job/missing-job` and assert `任务不存在` and a `返回任务` link.

Create `frontend/tests/deep_link.spec.py`: seed 21 unification runs and 51 contract versions directly in the fixture database, then open the oldest run via `#/tasks/run/{run_id}` and the first version via `#/tasks/contract/{version_id}`. Assert the ids render, proving the single-resource fallback outside the first list page.

- [ ] **Step 2: Run the spec to verify it fails**

Run: `python3 frontend/tests/menu_shell.spec.py` and `python3 frontend/tests/deep_link.spec.py`
Expected: FAIL because the menu buttons and hash routes do not exist.

- [ ] **Step 3: Implement the router, task aggregation, and shell**

`router.ts` parses and serializes the routes `#/data`, `#/tasks`, `#/tasks/new/{clean|match|metrics|contract|entity}`, `#/tasks/job/{jobId}`, `#/tasks/run/{runId}`, `#/tasks/contract/{versionId}`, `#/reviews`, and `#/settings/{providers|team|api-keys}`. `App.tsx` listens to `hashchange`, falls back to `#/data`, and renders the four menus. `loadTasks` fetches jobs, unification runs, and contract versions, then normalizes them into `TaskItem[]`.

`TasksPage` renders the task list, five "新建任务" buttons that set the hash, and a `产物与发布` button that routes to `#/tasks/publish`. `App.tsx` renders `ServingReleasePage project={activeProject}` for `tasks-publish`, so artifact publishing and rollback remain reachable after the old `serving` menu is removed. `App.tsx` parses `tasks-job`, `tasks-run`, and `tasks-contract` into `jobId`, `runId`, and `versionId` and passes them to `TaskDetailPage`; it does not depend on a `TaskItem` being present. `TaskDetailPage` renders `WorkbenchPage` for jobs, `UnificationPage` for runs, or `ContractsPage` for contract versions; pass `project.id`, `canEdit`, and the active data-source context. For a job, pass `activeDsIds`, `activeProfile`, `activeRouting`, `initialJobId={jobId}`, `initialStage` derived from `newKind`, and `onGoDataSources`/`onGoContracts` callbacks that route to `#/data` and `#/tasks/new/contract`. For a run, pass `initialRunId={runId}`. For a contract, pass `initialVersionId={versionId}`. Its not-found state uses the fixed `任务不存在` copy and a `返回任务` link, not the API's `资源不存在` text; it is shown only when the single-resource fetch fails. `SettingsPage` renders `ProvidersPage`, `TeamAdminPage currentUser={currentUser}`, and the extracted `ApiKeysPanel`. `ReviewHomePage` renders the existing `ReviewQueuePage` plus a card linking to `#/tasks/new/entity`.

Move the API key state and handlers from `ServingReleasePage` into `ApiKeysPanel`; render the panel from `SettingsPage` and remove the duplicate section from `ServingReleasePage`, which then only handles artifact publishing and rollback.

Add `initialJobId` and `initialStage` to `WorkbenchPage`; when `initialJobId` is set, fetch `/api/v1/jobs/{id}` on mount and set the job and stage. When `activeRouting` is null, fetch `/api/v1/projects/{id}/datasources`, pick the most recent item with a routing object, and use it for the stage, method card, reasons, and suggested params locally. Add `initialRunId` to `UnificationPage`; after loading runs, select it and set the run tab.

Update `DataSourcesPage`'s `onRouteReady` in `App.tsx`: store `activeDsIds`, `activeProfile`, and `activeRouting`, then route to `#/tasks/new/clean`, `#/tasks/new/match`, or `#/tasks/new/metrics` based on `routing.method?.stage || routing.recommended_stage`. L3 routes to `#/tasks/new/contract`.

- [ ] **Step 4: Update the three existing specs to the new shell**

Add `frontend/tests/ui_helpers.py` with:

```python
APP = "http://127.0.0.1:5173"

def open_route(page, route: str):
    if page.url.startswith(APP):
        page.evaluate("route => { window.location.hash = route }", route)
    else:
        page.goto(f"{APP}{route}")
    page.wait_for_load_state("networkidle")
```

The hash-only branch matters: the unification spec selects `project-customer` and then opens the entity page. A full `page.goto` reloads and resets the active project to `list[0]`; the later export assertions then read the wrong project.

Replace every `page.get_by_role("button", name="实体统一工作台").click()` with `open_route(page, "#/tasks/new/entity")`. Replace the `契约统一` nav click with `open_route(page, "#/tasks/new/contract")`. Replace the `模型 Provider` nav click with `open_route(page, "#/settings/providers")`. Keep all page-level assertions unchanged except the evidence-code assertion in `unification_workbench.spec.py`, which Task 7 updates to expand `查看原始证据` first.

- [ ] **Step 5: Run the shell spec and the updated specs**

Start Vite on port 5173 and run:

```bash
python3 frontend/tests/menu_shell.spec.py
python3 frontend/tests/deep_link.spec.py
python3 frontend/tests/unification_workbench.spec.py
python3 frontend/tests/contracts_workbench.spec.py
python3 frontend/tests/decision_models_workbench.spec.py
```

Expected: all exit 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/src frontend/tests
git commit -m "feat(ui): add the four-menu shell, task aggregation, and settings"
```

---

### Task 6: Assistant Panel, Auto-Run, and Page Explanations

**Files:**
- Create: `frontend/src/components/AssistantPanel.tsx`
- Create: `frontend/src/lib/pageHelp.ts`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/tests/guided_assistant.spec.py`

**Interfaces:**
- `AssistantPanel` props: `{projectId: string, currentPage: string, onRoute: (hash: string) => void}`.
- Internal `AssistantPlan`: `{task: 'clean'|'match'|'metrics'|'contract'|'entity', stage: 'L1'|'L2'|'L3'|'L4'|'ENTITY', datasourceIds: string[], profile?: any, routing?: any}`.

- [ ] **Step 1: Write the failing assistant spec**

Use the isolated backend pattern. In the spec setup, set `app.workers.tasks.SessionLocal = maker` so `run_job_sync` writes the job and result into the fixture database, and patch `app.api.v1.unification.dispatch_unification_run` to a function that runs `execute_unification_run` with the fixture maker. Without these two patches, `POST /jobs` and `POST /unification/demo` return 500 and leave PENDING rows. After login:

1. Assert the assistant panel is visible and contains `你想完成什么？`.
2. Click `清洗供应商名单`; assert `规则清洗 + W3C 契约` and a one-sentence reason are visible.
3. Intercept `POST /api/v1/projects/*/datasources/demo/L1` and abort it; click `开始`; assert an error message and a `去手动处理` link. Reload the panel and remove the interception.
4. Click `开始`; wait for `已完成` or `运行中` to finish; click `查看结果`; assert an L1 result label such as `总行数` or `清洗后`.
5. Click `解释当前页面`; assert an explanation containing `数据源` or `任务`.
6. Type `帮我把客户名单去重` in the free-text input; submit; assert the plan mentions `跨系统匹配客户` or `Splink`.
7. Click `实体匹配`; wait for completion; assert the entity page or a `复核` link is shown.

- [ ] **Step 2: Run the spec to verify it fails**

Run: `python3 frontend/tests/guided_assistant.spec.py`
Expected: FAIL because the panel does not exist.

- [ ] **Step 3: Implement the panel**

The panel fetches `GET /api/v1/projects/{id}/datasources` when opened. It renders five task chips: `清洗供应商名单`, `跨系统匹配客户`, `实体匹配`, `统一 GMV 口径`, `生成数据契约`, plus a free-text input. Free text calls `POST /api/v1/projects/{id}/assistant/plan`; on failure or `source == "rules"`, show a small `本地规则` note. The plan card shows the selected data source, the method label and reason, three steps, and `开始`.

`开始` runs the plan:

- If no data source is selected, call `POST /datasources/demo/{L1|L2|L4}` and report that one example data source was added.
- For L1/L2/L4: call `POST /projects/{id}/jobs`. Map the routing params exactly like `WorkbenchPage`: L1 sends `{target_col: sp.name_col, amount_col: sp.amount_col}`; L2 sends `{name_col, tax_col, phone_col, city_col, enable_hybrid_llm}`. Fetch `/providers`, pick the `is_default` `openai_compatible` provider, else the first `openai_compatible`; never pass a decision provider. Poll `GET /jobs/{id}` every 500ms, update the step list, then call `onRoute('#/tasks/job/' + jobId)`.
- For L3: fetch `/contracts/sources` and `/unification/snapshots`; if either is empty, show the missing prerequisite and route to `#/tasks/new/contract`; otherwise `POST /contracts/versions` and route to `#/tasks/contract/{id}`.
- For entity: `POST /unification/demo`, poll the returned run, then route to `#/tasks/run/{runId}`.

On success, render a done state: `已完成`, the result summary, and the controls `查看结果` (routes to `#/tasks/job/{jobId}` or `#/tasks/run/{runId}`), `去复核` when the run has review pairs, and `解释当前页面`. On failure, show the error text and a `去手动处理` button that routes to the relevant page. Do not publish, merge, or decide.

`pageHelp.ts` maps each current page key to two or three sentences explaining its purpose and the next action.

- [ ] **Step 4: Run the spec to verify it passes**

Run: `python3 frontend/tests/guided_assistant.spec.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AssistantPanel.tsx frontend/src/lib/pageHelp.ts frontend/src/App.tsx frontend/tests/guided_assistant.spec.py
git commit -m "feat(assistant): add the guided panel, auto-run, and page explanations"
```

---

### Task 7: Readable Entity Review and L2 Queue Copy

**Files:**
- Create: `frontend/src/components/EntityReviewCard.tsx`
- Modify: `frontend/src/pages/UnificationPage.tsx`
- Modify: `frontend/src/pages/ReviewQueuePage.tsx`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/tests/unification_workbench.spec.py`
- Create: `frontend/tests/review_readability.spec.py`

**Interfaces:**
- `ReviewPair` adds `why?: string`, `suggestion?: string`, `consequence?: string`, `left?: {record_ref: string; attributes: Record<string, unknown>}`, `right?: ...`, `comparison?: Array<{field: string; left: unknown; right: unknown; status: 'MATCH'|'DIFFERENT'|'MISSING'}>`.
- `EntityReviewCard` props `{pair: ReviewPair, busy: boolean, onDecide: (action: 'MERGE'|'KEEP_DISTINCT') => void}`.

- [ ] **Step 1: Write the failing review readability spec**

Start a controlled choice server and seed a default `typesafe_jev` provider pointing at it with `send_fields ["name","tax_id"]`, reusing the setup from `decision_models_workbench.spec.py`. In the spec setup, patch `app.api.v1.unification.dispatch_unification_run` to use the fixture maker; otherwise `POST /unification/demo` returns 500. Call `POST /unification/demo`, poll the run, open `#/tasks/run/{run_id}`, then click the `人工复核` tab. Assert:

- `这两条记录是不是同一实体？` is visible.
- The `why` sentence is visible.
- A comparison row shows `name` and `tax_id`.
- `模型建议 · UNKNOWN` or `模型建议 · SAME` is visible.
- `查看原始证据` is collapsed; click it and assert an evidence code such as `STRONG_IDENTIFIER_MISSING`.
- The `系统已自动保持分离` section contains the DISTINCT pair and its `why`.

Seed an L2 `ReviewItem` and open the review page; assert the model sentence and `L2 灰色边界样本`.

- [ ] **Step 2: Run the spec to verify it fails**

Run: `python3 frontend/tests/review_readability.spec.py`
Expected: FAIL because the new text is missing.

- [ ] **Step 3: Implement the review card and queue copy**

Extract the review article from `UnificationPage` into `EntityReviewCard`. Render the title, `why`, comparison table with `MATCH`/`DIFFERENT`/`MISSING` labels, model badge (keep `模型建议 · SAME` text and add the Chinese translation), confidence, distribution, model version, provider kind/key, latency, `suggestion`, `consequence`, and the two existing action buttons. Put evidence codes and raw JSON inside `<details><summary>查看原始证据</summary>...`. Render `auto_separated` in a sibling `<details>` with a count and the `why` for each item.

In `ReviewQueuePage`, render `llm_suggestion.reason` as `模型理由：...`, a `推入原因：L2 灰色边界样本` line, and keep the existing buttons.

Update `frontend/tests/unification_workbench.spec.py`: before the existing `CANDIDATE` assertion, click `查看原始证据`. This is required because the evidence codes are now collapsed by default.

- [ ] **Step 4: Run the spec to verify it passes**

Run: `python3 frontend/tests/review_readability.spec.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/EntityReviewCard.tsx frontend/src/pages/UnificationPage.tsx frontend/src/pages/ReviewQueuePage.tsx frontend/src/lib/api.ts frontend/tests/review_readability.spec.py frontend/tests/unification_workbench.spec.py
git commit -m "feat(review): explain reviews in plain language with field comparison"
```

---

### Task 8: Result Viewer for L1, L2, and L3

**Files:**
- Create: `frontend/src/components/ResultViewer.tsx`
- Create: `frontend/src/lib/csv.ts`
- Modify: `frontend/src/pages/WorkbenchPage.tsx`
- Modify: `frontend/src/pages/ContractsPage.tsx`
- Modify: `frontend/src/pages/ServingReleasePage.tsx`
- Create: `frontend/tests/result_viewer.spec.py`

**Interfaces:**
- `parseCsv(text: string, limit = 200): {headers: string[]; rows: string[][]}` handles quotes, escaped quotes, CRLF, and a UTF-8 BOM.
- `ResultViewer` source: `{kind: 'artifact', id: string, filename: string} | {kind: 'contract', projectId: string, versionId: string, filename: string}`.

- [ ] **Step 1: Write the failing result viewer spec**

Seed the `Job`, `ArtifactVersion`, contract source, contract version, and snapshot rows directly in the fixture database; do not call `POST /jobs` or `POST /unification/demo` in this spec, because their dispatch uses the global `SessionLocal`. Seed an artifact with CSV content containing a quoted comma and a BOM:

```
name,note
"Acme, Ltd.","line1, line2"
```

Write the CSV bytes and the contract YAML to storage with `storage.put_bytes` under the artifact and source object keys. Put `artifacts: [{id, filename, version}]` in the job's `result_json` so the workbench renders the artifact button. Seed one `l4_metrics` artifact and open `#/tasks/publish`; assert a `发布到线上 API` button is visible. Open the task detail, click `查看`, and assert the table shows two columns and the value `Acme, Ltd.`. Then open `#/tasks/contract/{versionId}`; assert the `versions` tab is active and the seeded version row is visible, then click `查看模型` and assert class names `Supplier` and `Country` are visible.

- [ ] **Step 2: Run the spec to verify it fails**

Run: `python3 frontend/tests/result_viewer.spec.py`
Expected: FAIL because no view buttons or viewer exist.

- [ ] **Step 3: Implement the viewer and wire the buttons**

Fetch artifact content with `Authorization` from `getToken()`, read it as text, and render by filename: CSV table, JSON/YAML code, HTML iframe, SQL/text code. For contracts, fetch `/api/v1/projects/{projectId}/contracts/versions/{versionId}/export?kind=model` and render classes, attributes, enums, and relations, with a JSON toggle. Add `查看` buttons next to the existing download buttons in `WorkbenchPage`, `ContractsPage`, and `ServingReleasePage`.

- [ ] **Step 4: Run the spec to verify it passes**

Run: `python3 frontend/tests/result_viewer.spec.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ResultViewer.tsx frontend/src/lib/csv.ts frontend/src/pages/WorkbenchPage.tsx frontend/src/pages/ContractsPage.tsx frontend/src/pages/ServingReleasePage.tsx frontend/tests/result_viewer.spec.py
git commit -m "feat(results): view CSV, JSON, and contract models online"
```

---

### Task 9: Method UI, Entity Banner, and Full Regression

**Files:**
- Modify: `frontend/src/pages/DataSourcesPage.tsx`
- Modify: `frontend/src/pages/WorkbenchPage.tsx`
- Modify: `frontend/src/pages/UnificationPage.tsx`
- Modify: `frontend/src/App.tsx` (rename `路由工作台` to `处理方法`)
- Modify: `.github/workflows/ci.yml` (add the six new Playwright specs to the frontend job)
- Create: `frontend/tests/method_recommendation.spec.py`
- Modify: `README.md` (one line pointing to the design and plan)

**Interfaces:**
- Consumes: `profile.routing.method` from Task 1 and the latest data source from `GET /datasources`.

- [ ] **Step 1: Write the failing method recommendation spec**

Seed the legacy data source first (`recommended_stage: "L1"`, no `method`), then the L1 source, then the L2 source last, so the L2 source is the latest by `created_at`. Open 数据 and assert `规则清洗 + W3C 契约` and `Splink + DuckDB` labels, and that the legacy source still renders a method label instead of crashing. Open `#/tasks/new/match` directly and assert the workbench header `处理方法` and the method card, proving `WorkbenchPage` fetched the latest routing without the user clicking a data source. Open `#/tasks/new/entity` and assert the banner mention `Splink + DuckDB` and the override link `仍然直接匹配`.

- [ ] **Step 2: Run the spec to verify it fails**

Run: `python3 frontend/tests/method_recommendation.spec.py`
Expected: FAIL because the labels and banner are missing.

- [ ] **Step 3: Implement the labels and banner**

In `DataSourcesPage`, show `routing.method?.label` and fall back to a label derived from `routing.recommended_stage` when `method` is missing; change the button text to `进入处理方法`. In `WorkbenchPage`, use `activeRouting` when present, otherwise the latest routing fetched from `/datasources`; show the method label, reason, and an expandable list of `routing.reasons`; rename the page header to `处理方法`. In `UnificationPage`, fetch `/datasources`, pick the selected or latest source with routing, and render a dismissible banner with the method label, reason, and two actions: route to the recommended task or `仍然直接匹配`. Use `window.location.hash` for navigation. The backend's `method_for_routing` backfill is the primary source, but the frontend fallback keeps legacy or stale responses usable.

- [ ] **Step 4: Run all checks**

```bash
python3 -m pytest backend/tests -q
python3 -m pytest tests/test_decision_model_holdout.py -q
python3 tests/formal/check_decision_dispatch_boundary.py
cd frontend && npm run build
```

In `.github/workflows/ci.yml`, add the six new specs to the frontend job after the existing three:

```yaml
          python frontend/tests/menu_shell.spec.py
          python frontend/tests/deep_link.spec.py
          python frontend/tests/guided_assistant.spec.py
          python frontend/tests/review_readability.spec.py
          python frontend/tests/result_viewer.spec.py
          python frontend/tests/method_recommendation.spec.py
```

Then start Vite and run every Playwright spec:

```bash
python3 frontend/tests/menu_shell.spec.py
python3 frontend/tests/deep_link.spec.py
python3 frontend/tests/guided_assistant.spec.py
python3 frontend/tests/review_readability.spec.py
python3 frontend/tests/result_viewer.spec.py
python3 frontend/tests/method_recommendation.spec.py
python3 frontend/tests/unification_workbench.spec.py
python3 frontend/tests/contracts_workbench.spec.py
python3 frontend/tests/decision_models_workbench.spec.py
```

Expected: backend suite passes; build passes; every spec exits 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src frontend/tests .github/workflows/ci.yml README.md
git commit -m "feat(ui): show the recommended method and explain the processing page"
```

---

## Self-Review Notes

- Spec coverage: assistant (Tasks 3, 5, 6), four menus and task page (Task 5), review explanation and auto-separated (Tasks 2, 7), online results (Task 8), method recommendations and entity banner (Tasks 1, 9), entity auto-run (Task 4), acceptance (Tasks 5-9).
- Delivery two (OpenRefine runtime, recipes, diff, JSON-LD) is intentionally absent from this plan; it is the second plan and is recorded in the spec.
- No database migration is required.
- Existing model metadata strings remain visible; only evidence and raw JSON move behind a disclosure.
