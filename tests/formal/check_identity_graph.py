"""Exhaustive bounded check of the persistent identity graph.

Property: every sequence of committed identity changes up to the depth bound
leaves a well-formed graph. Each source record belongs to an ACTIVE entity in
its project, every ACTIVE entity owns at least one record, every MERGED entity
redirects to an ACTIVE entity, redirect chains have no cycle, no active
KEEP_DISTINCT decision names two entities that resolve into one, and a
successful undo restores the affected records to the entity they held before
the undone change.

The check drives the real ``identity.py`` functions against a temporary
SQLite database. It enumerates every operation sequence, not a hand model:
human MERGE and KEEP_DISTINCT decisions, split by every non-empty proper
subset of an active entity's records, and undo of every earlier change. A
refused operation is expected when the current state forbids it and is not
explored. ``merge_entities`` is reached through ``apply_decision``, which is
the only production merge path; a direct call without a decision pair is out
of scope.

Run: python3 tests/formal/check_identity_graph.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

_BOOTSTRAP = tempfile.mkdtemp(prefix="osu-identity-formal-boot-")
os.environ["DATABASE_URL"] = f"sqlite:///{_BOOTSTRAP}/bootstrap.db"
os.environ.setdefault("CELERY_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.entities import Project, Workspace
from app.models.unification import (
    IdentityChangeRecord,
    IdentityDecisionRecord,
    SourceRecord,
    UnifiedEntity,
)
from app.services.unification.identity import (
    DecisionInput,
    IdentityConflict,
    apply_decision,
    resolve_entity_id,
    split_entity,
    undo_identity_change,
)

PROJECT = "project"
DEPTH_DEFAULT = 3
NODE_BUDGET = 60_000
ACTOR = "formal-check"


class Failure(RuntimeError):
    pass


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


@dataclass(frozen=True)
class Op:
    kind: str
    target: str | None = None
    sources: tuple[str, ...] = ()
    records: tuple[str, ...] = ()
    change_id: str | None = None

    def label(self) -> str:
        if self.kind == "DECIDE_MERGE":
            return f"DECIDE_MERGE {self.target} / {self.sources[0]}"
        if self.kind == "KEEP_DISTINCT":
            return f"KEEP_DISTINCT {self.target} / {self.sources[0]}"
        if self.kind == "SPLIT":
            return f"SPLIT {self.target} off {list(self.records)}"
        return f"UNDO change {self.change_id}"


def _open(path: Path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed(path: Path) -> None:
    engine, maker = _open(path)
    Base.metadata.create_all(engine)
    with maker() as db:
        db.add(Workspace(id="workspace", name="Workspace", slug="workspace"))
        db.flush()
        db.add(Project(id=PROJECT, workspace_id="workspace", name="Project", slug=PROJECT))
        db.flush()
        db.add_all([
            UnifiedEntity(id="ent-a", project_id=PROJECT, entity_type="supplier", status="ACTIVE"),
            UnifiedEntity(id="ent-b", project_id=PROJECT, entity_type="supplier", status="ACTIVE"),
            UnifiedEntity(id="ent-c", project_id=PROJECT, entity_type="supplier", status="ACTIVE"),
        ])
        db.flush()
        db.add_all([
            SourceRecord(id="rec-a1", project_id=PROJECT, source_system="erp", dataset="suppliers",
                         source_record_id="a1", current_entity_id="ent-a"),
            SourceRecord(id="rec-a2", project_id=PROJECT, source_system="erp", dataset="suppliers",
                         source_record_id="a2", current_entity_id="ent-a"),
            SourceRecord(id="rec-b1", project_id=PROJECT, source_system="crm", dataset="suppliers",
                         source_record_id="b1", current_entity_id="ent-b"),
            SourceRecord(id="rec-c1", project_id=PROJECT, source_system="wms", dataset="suppliers",
                         source_record_id="c1", current_entity_id="ent-c"),
        ])
        db.commit()
    engine.dispose()


def _current_revision(db) -> int:
    return db.scalar(select(func.max(IdentityChangeRecord.revision)).where(
        IdentityChangeRecord.project_id == PROJECT
    )) or 1


def _check_graph(db) -> None:
    entities = {entity.id: entity for entity in db.scalars(select(UnifiedEntity))}
    records = list(db.scalars(select(SourceRecord)))
    _assert(records, "the fixture lost every source record")
    members = Counter()
    for record in records:
        entity = entities.get(record.current_entity_id)
        _assert(entity is not None, f"record {record.id} points at missing entity {record.current_entity_id}")
        _assert(entity.project_id == PROJECT, f"record {record.id} points outside project {PROJECT}")
        _assert(entity.status == "ACTIVE", f"record {record.id} belongs to {entity.id}, which is {entity.status}")
        members[entity.id] += 1
    for entity in entities.values():
        _assert(entity.status in ("ACTIVE", "MERGED"), f"entity {entity.id} has unknown status {entity.status}")
        if entity.status == "ACTIVE":
            _assert(entity.redirect_to_id is None, f"active entity {entity.id} still redirects to {entity.redirect_to_id}")
            _assert(members[entity.id] > 0, f"active entity {entity.id} owns no source record")
        else:
            _assert(entity.redirect_to_id is not None, f"merged entity {entity.id} has no redirect")
    for entity_id in entities:
        resolve_entity_id(db, entity_id)
    for decision in db.scalars(select(IdentityDecisionRecord).where(
        IdentityDecisionRecord.project_id == PROJECT,
        IdentityDecisionRecord.action == "KEEP_DISTINCT",
    )):
        left = resolve_entity_id(db, decision.left_entity_id)
        right = resolve_entity_id(db, decision.right_entity_id)
        _assert(
            left != right,
            f"active KEEP_DISTINCT {decision.left_entity_id}/{decision.right_entity_id} resolves into {left}",
        )
    revisions = sorted(change.revision for change in db.scalars(select(IdentityChangeRecord).where(
        IdentityChangeRecord.project_id == PROJECT,
    )))
    _assert(
        revisions == list(range(2, len(revisions) + 2)),
        f"identity revisions are not consecutive from 2: {revisions}",
    )


def _affected_records(db, op: Op) -> dict[str, str]:
    if op.kind == "DECIDE_MERGE":
        entity_ids = [op.target, *op.sources]
        rows = db.scalars(select(SourceRecord).where(SourceRecord.current_entity_id.in_(entity_ids))).all()
        return {row.id: row.current_entity_id for row in rows}
    if op.kind == "KEEP_DISTINCT":
        return {}
    if op.kind == "SPLIT":
        return {record_id: op.target for record_id in op.records}
    return {}


def _apply(db, op: Op, revision: int):
    if op.kind == "DECIDE_MERGE":
        return apply_decision(db, DecisionInput(
            project_id=PROJECT, left_entity_id=op.target, right_entity_id=op.sources[0],
            action="MERGE", reason="formal merge decision",
        ), expected_revision=revision, actor_id=ACTOR)
    if op.kind == "KEEP_DISTINCT":
        return apply_decision(db, DecisionInput(
            project_id=PROJECT, left_entity_id=op.target, right_entity_id=op.sources[0],
            action="KEEP_DISTINCT", reason="formal cannot-link decision",
        ), expected_revision=revision, actor_id=ACTOR)
    if op.kind == "SPLIT":
        return split_entity(db, project_id=PROJECT, entity_id=op.target,
                            source_record_ids=list(op.records), actor_id=ACTOR,
                            reason="formal split", expected_revision=revision)
    return undo_identity_change(db, op.change_id, expected_revision=revision, actor_id=ACTOR,
                                reason="formal undo")


def _operations(db, history: list[dict]) -> list[Op]:
    active = sorted(
        entity.id for entity in db.scalars(select(UnifiedEntity).where(
            UnifiedEntity.project_id == PROJECT, UnifiedEntity.status == "ACTIVE",
        ))
    )
    records_by_entity: dict[str, list[str]] = defaultdict(list)
    for record in db.scalars(select(SourceRecord).where(SourceRecord.project_id == PROJECT)):
        records_by_entity[record.current_entity_id].append(record.id)
    ops: list[Op] = []
    for left, right in combinations(active, 2):
        ops.append(Op("DECIDE_MERGE", left, (right,)))
        ops.append(Op("KEEP_DISTINCT", left, (right,)))
    for entity_id in active:
        records = sorted(records_by_entity[entity_id])
        for size in range(1, len(records)):
            for subset in combinations(records, size):
                ops.append(Op("SPLIT", entity_id, records=subset))
    for entry in history:
        if entry["kind"] != "UNDO":
            ops.append(Op("UNDO", change_id=entry["change_id"]))
    return ops


def _active_keep_pairs(history: list[dict]) -> dict[frozenset[str], dict]:
    active: dict[frozenset[str], dict] = {}
    for entry in history:
        if entry["kind"] == "KEEP_DISTINCT":
            active[frozenset((entry["target"], entry["sources"][0]))] = entry
        elif entry["kind"] == "UNDO":
            for pair, keep in list(active.items()):
                if keep["change_id"] == entry.get("undoes"):
                    del active[pair]
    return active


class Explorer:
    def __init__(self, root: Path, depth: int):
        self.root = root
        self.depth = depth
        self.counter = 0
        self.states = 0
        self.committed = 0
        self.refused = 0
        self.noops = 0

    def _child_path(self) -> Path:
        self.counter += 1
        return self.root / f"state-{self.counter:06d}.db"

    def visit(self, path: Path, remaining: int, history: list[dict]) -> None:
        self.states += 1
        _assert(self.states <= NODE_BUDGET,
                f"explored more than {NODE_BUDGET} states at depth {self.depth}; lower the depth")
        engine, maker = _open(path)
        try:
            with maker() as db:
                _check_graph(db)
                ops = _operations(db, history)
        finally:
            engine.dispose()
        if remaining == 0:
            return
        keep_pairs = _active_keep_pairs(history)
        for op in ops:
            pair = frozenset((op.target, op.sources[0])) if op.kind == "DECIDE_MERGE" else None
            expected_refusal = pair in keep_pairs
            child = self._child_path()
            shutil.copyfile(path, child)
            engine, maker = _open(child)
            try:
                with maker() as db:
                    revision = _current_revision(db)
                    before_ids = {change.id for change in db.scalars(select(IdentityChangeRecord))}
                    before_members = _affected_records(db, op)
                with maker() as db:
                    try:
                        change = _apply(db, op, revision)
                    except IdentityConflict:
                        self.refused += 1
                        child.unlink()
                        continue
                    if expected_refusal:
                        raise Failure(
                            f"{op.label()} was allowed despite active {keep_pairs[pair]['label']}"
                        )
                    new_revision = _current_revision(db)
                    if change.id in before_ids:
                        self.noops += 1
                        child.unlink()
                        continue
                    _assert(change.revision == revision + 1,
                            f"committed {op.label()} but revision moved {revision} -> {change.revision}")
                    _assert(new_revision == change.revision,
                            f"committed {op.label()} but max revision is {new_revision}")
                    _check_graph(db)
                    if op.kind == "UNDO":
                        original = next(entry for entry in history if entry["change_id"] == op.change_id)
                        current = {row.id: row.current_entity_id for row in db.scalars(select(SourceRecord))}
                        for record_id, entity_id in original["before"].items():
                            _assert(
                                current.get(record_id) == entity_id,
                                f"undo of {original['label']} left record {record_id} in "
                                f"{current.get(record_id)}, expected {entity_id}",
                            )
            except Failure as exc:
                raise Failure(f"after [{'; '.join(entry['label'] for entry in history)}] "
                              f"+ {op.label()}: {exc}") from exc
            finally:
                engine.dispose()
            self.committed += 1
            entry = {
                "change_id": change.id,
                "kind": op.kind,
                "label": op.label(),
                "target": op.target,
                "sources": op.sources,
                "before": before_members,
                "undoes": op.change_id if op.kind == "UNDO" else None,
            }
            self.visit(child, remaining - 1, [*history, entry])


def main() -> int:
    depth = int(sys.argv[1]) if len(sys.argv) > 1 else DEPTH_DEFAULT
    root = Path(tempfile.mkdtemp(prefix="osu-identity-formal-"))
    try:
        initial = root / "state-000000.db"
        _seed(initial)
        explorer = Explorer(root, depth)
        explorer.visit(initial, depth, [])
        print(
            f"PASS: {explorer.states} reachable states and {explorer.committed} committed changes "
            f"across every identity operation sequence up to depth {depth}; "
            f"{explorer.refused} state-forbidden operations refused."
        )
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(_BOOTSTRAP, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
