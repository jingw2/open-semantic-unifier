---
name: verify-identity-graph
description: "Prove that every API-reachable identity merge, cannot-link decision, split, and undo leaves a well-formed persistent entity graph. Use when changing identity.py, identity decisions, split or undo, entity redirects, or the unification identity API."
---

# Verify the persistent identity graph

**Property.** After every committed sequence of an API-reachable identity change, every source record belongs to an ACTIVE entity in its project, every ACTIVE entity owns at least one record, every MERGED entity redirects to an ACTIVE entity, redirect chains have no cycle, no active `KEEP_DISTINCT` decision names two entities that resolve into one, revisions stay consecutive, and a successful undo restores the records it moved to the entity they held before the undone change. A merge of a pair under an active `KEEP_DISTINCT` is refused.

**Harm.** A broken graph silently moves source records onto retired entities or lets a merge override a reviewer's cannot-link decision. Contracts, snapshots, and exports then describe an entity that no longer owns the records claimed for it.

## Run

```bash
python3 tests/formal/check_identity_graph.py
```

A healthy tree prints:

```
PASS: 626 reachable states and 625 committed changes across every identity operation sequence up to depth 3; 128 state-forbidden operations refused.
```

The check is deterministic for a given SQLAlchemy and SQLite version. The counts can move when `identity.py` gains or loses an operation; the invariants must still hold.

## What it checks

The fixture is one project with three supplier entities and four source records (`ent-a` owns `rec-a1` and `rec-a2`; `ent-b` owns `rec-b1`; `ent-c` owns `rec-c1`). The checker walks every reachable state up to three committed operations by copying the SQLite file at each step and running the real service:

- **MERGE decision.** `apply_decision(..., action="MERGE")` for every pair of active entities.
- **KEEP_DISTINCT decision.** `apply_decision(..., action="KEEP_DISTINCT")` for every pair of active entities.
- **SPLIT.** `split_entity` for every non-empty proper subset of every active entity's records. The fixture never holds more than four records in one entity, so this is every subset.
- **UNDO.** `undo_identity_change` for every earlier committed change, including changes that are not the latest.
- **Refusals.** An operation the current state forbids must raise `IdentityConflict`. The check records it and does not explore it. If a MERGE succeeds while an active `KEEP_DISTINCT` covers the same pair, the check fails.

After every committed operation the checker re-reads the database and asserts the property above. It also checks that the revision is exactly one higher than before and that a successful undo restores every record moved by the undone change.

## Why this tool

The service is Python and SQLAlchemy over SQLite, PostgreSQL, or MySQL. No verifier runs on that stack's transaction and ORM semantics. A TLA+ model would restate `identity.py`, `lock_project_identity_state`, and the SQL queries, then drift from them. The property is finite over a small fixture and a depth bound, so the checker explores the real code instead: every reachable operation sequence, with the database as the state.

## Coverage

| File | Functions | Last changed |
| --- | --- | --- |
| `backend/app/services/unification/identity.py` | `apply_decision`, `merge_entities` (through `apply_decision`), `split_entity`, `undo_identity_change`, `resolve_entity_id`, `_assert_no_cannot_link` | `9de8009` |
| `backend/app/services/unification/locking.py` | `lock_project_identity_state` (SQLite path) | `9de8009` |
| `backend/app/models/unification.py` | `UnifiedEntity`, `SourceRecord`, `IdentityChangeRecord`, `IdentityDecisionRecord` | `9de8009` |
| `backend/app/api/v1/unification.py` | `create_identity_decision`, `split_unified_entity`, `undo_identity_decision` (the service calls they make) | `9de8009` |

Companion tests in `backend/tests/test_unification_identity.py` cover the named decision paths, scheme-conflict merges, and stale revisions. The formal check replaces none of them; it explores combinations the tests do not enumerate.

Sync commit: `9de8009`. Move it forward only after you confirm these functions still match the check at the new commit.

## What it does not cover

- **Direct `merge_entities` without a decision pair.** The API reaches `merge_entities` only through `apply_decision`, which is the modelled merge. A direct service call with `decision_pair=None` is outside the check. A scratch probe showed that `SPLIT ent-a off rec-a1; merge_entities(target=ent-b, sources=[ent-a]); undo the split` leaves `rec-a1` on the MERGED `ent-a`, because undo only looks for later `IdentityDecisionRecord` rows. Treat that as a known boundary until a production caller appears or the service is tightened.
- **Multi-source merges.** `apply_decision` merges one source at a time. A direct merge of several sources is not exercised.
- **Concurrency.** The checker runs one session at a time. It does not exercise the PostgreSQL row lock, SQLite `BEGIN IMMEDIATE` contention, or a stale-revision race between two live requests.
- **Attribute overrides, identity conflicts, snapshots, and releases.** Only the graph-changing decisions are modelled.
- **Depth beyond three operations and fixtures beyond three entities and four records.** The bound is the guarantee; a bug that needs four operations is not excluded, only not proven here.

## If it fails

The message gives the sequence and the broken invariant, for example:

```
FAIL: after [DECIDE_MERGE ent-a / ent-b] + SPLIT ent-a off ['rec-a1']: active entity ent-b owns no source record
```

That is a real identity-state bug, not a tool error. Fix `identity.py`; do not relax the check. If the sequence names a direct service call the API never makes, see the boundary above before calling it a user-facing bug.

## Prove the check can fail

Break one guard at a time and rerun:

- Comment out `_assert_no_cannot_link(db, project_id, members)` in `merge_entities`. The check fails with `DECIDE_MERGE ... was allowed despite active KEEP_DISTINCT ...`.
- Comment out `entity.redirect_to_id = target_id` for merged sources. The check fails with `merged entity ent-b has no redirect`.

Restore both before continuing.

## After a change

Rerun the check. If `identity.py`, the identity models, or the identity API endpoints changed, read the diff and confirm the check still models the new operations and invariants. Move the sync commit and the coverage dates forward only after that confirmation. Use `/pstack:maintain-formal-verification-skill` when the identity lifecycle changes materially.
