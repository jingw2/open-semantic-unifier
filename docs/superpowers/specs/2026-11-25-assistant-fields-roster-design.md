# Assistant, Arbitrary Fields, and Standard Roster Design

Date: 2026-11-25
Status: approved for implementation by the user's request; no new database migration.

## Intent

The assistant currently classifies one message into one of five fixed tasks. The model does not choose a source, fields, or a recipe, and the panel has no conversation. OpenRefine recipes are tied to a few detected columns, and L1 can only align against the built-in demo supplier catalog. This change makes the assistant plan from project context, lets OpenRefine clean arbitrary selected fields, and adds an uploaded standard roster that L1 uses for alignment.

## Approaches

### Assistant
- A. Keep one-shot classification and add more keyword rules. Rejected. It stays rule-shaped and cannot answer questions.
- B. Stateless structured plan. Send the page, source summaries, and the last six turns. The model returns an intent, explanation, optional clarifying question, optional source id, optional allow-listed params, and an optional recipe id. The server validates everything against project data. Chosen.
- C. Server-side agent loop that calls tools itself. Rejected. It would own execution and state, widening the safety boundary.

### OpenRefine fields
- A. Add more hardcoded role placeholders and hope they cover the data. Rejected. Arbitrary lists still fail.
- B. Keep role placeholders for recipes such as split/merge, and add a per-column recipe mode that expands one operation over user-selected columns. The preview request carries `field_map` and `columns`, and the Job records them. Chosen.
- C. Let users upload OpenRefine recipes. Rejected by the existing product constraint.

### Standard roster
- A. Add a new roster table. Rejected. One nullable migration was the ceiling for delivery two; this needs none.
- B. Store the roster as a normal file DataSource with a role and field map in `routing_json`, and build its catalog at run time. Chosen.
- C. Replace the built-in demo catalog file. Rejected. Demo behavior must stay reproducible.

## Data shapes

### Assistant

```python
AssistantTurn = {"role": "user" | "assistant", "content": str}

AssistantPlanRequest = {
    "message": str,          # 1..2000
    "page": str,             # 1..80
    "history": list[AssistantTurn],  # last 6, each content <= 1000
}

AssistantPlan = {
    "intent": "clean" | "match" | "entity" | "metrics" | "contract" | "review" | "answer" | "unknown",
    "confidence": float,
    "explanation": str,
    "source": "model" | "rules",
    "suggested_task": str | None,
    "needs_clarification": bool,
    "question": str | None,
    "options": [{"label": str, "message": str}],  # max 3
    "datasource_id": str | None,
    "params": dict | None,
    "recipe_id": str | None,
}
```

Allowed params by intent:
- clean: `target_col`, `amount_col`
- match: `name_col`, `tax_col`, `phone_col`, `city_col`
- metrics: `metric_name`, `group_by`
- others: none

The server drops a source id outside the project and any param key or value not present in the project summary. The prompt contains source names, roles, row counts, and field names only. It never contains record values.

### OpenRefine

```python
Recipe.mode: "fields" | "per_column"

# fields mode
field_map: dict[str, str]  # e.g. {"name_col": "公司名", "contact_col": "联系方式"}

# per_column mode
columns: list[str]         # selected from the source profile, at least one
```

`normalize_selected_columns` is the first per-column recipe. It applies trim, whitespace collapse, punctuation cleanup, and lowercase to every selected column. Template recipes declare their required fields, detected columns, and options in the recipe list response.

The preview request adds `field_map` and `columns`; the Job params record both for audit.

### Standard roster

```python
RosterConfig = {
    "id_column": str,
    "name_column": str,
    "alias_columns": list[str],
    "tax_id_column": str | None,
    "phone_column": str | None,
}
```

Stored at `datasource.routing_json["roster"]` with `routing_json["role"] == "standard_roster"`. No table change.

`PUT /projects/{id}/datasources/{ds_id}/roster` validates every column against the stored profile and saves the config. `DELETE` removes the role. The latest roster in a project supplies the L1 catalog:

```python
CatalogEntry = {"id": str, "name": str, "aliases": list[str]}
```

Alias columns, tax id, and phone values become aliases. If no roster exists, L1 keeps today's demo-catalog behavior, and the result reports which source was used.

## UI

- The assistant becomes a scrollable conversation with a bottom composer. Quick tasks stay as chips above the composer. Assistant turns can carry an explanation, a clarifying question with up to three options, or a plan card. The plan card shows source, method, mapped fields, and one confirm button. The page explanation is model-answered when a provider exists, with the current static text as fallback.
- The OpenRefine method page gains a field-setting card. Template recipes show one select per required field. The per-column recipe shows a column multi-select. Defaults come from the data profile. The preview sends the mapping.
- The data page source card gains a `设为标准花名册` action and, once configured, a `标准花名册` badge plus an edit panel for ID, name, aliases, tax id, and phone. Contract JSON-LD source options label roster sources.
- The workbench L1 result states whether it aligned against a standard roster or the demo catalog.

## Constraints

- No auto-merge, auto-publish, auto-adopt, or auto-review decision.
- No new database migration.
- Assistant requests never include record values; tests assert this on the prompt.
- Per-column recipes are built-in and read-only.
- Existing deterministic fallback and existing Playwright specs stay green.
- New Playwright specs start an isolated backend on 127.0.0.1:8765 and use `open_route`.
