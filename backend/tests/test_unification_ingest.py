from pathlib import Path
import sys

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.db import Base
from app.services.unification.contracts import IngestMode, SourceKey, SourceRecordInput
from app.services.unification.ingest import ingest_batch
from app.models.entities import Project, Workspace
from app.models.unification import SourceRecord, SourceRevision


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unification.db'}")
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Workspace(id="workspace-1", name="Workspace", slug="workspace-1"))
    session.flush()
    session.add(Project(id="project-1", workspace_id="workspace-1", name="Project", slug="project-1"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def record(system, dataset, source_id, attrs=None):
    return SourceRecordInput(project_id="project-1", source_system=system, dataset=dataset, source_record_id=source_id, attributes=attrs or {"name": source_id})




def test_empty_batch_is_rejected(db):
    result = ingest_batch(db, project_id="project-1", records=[], mode=IngestMode.SYNC, input_version="v1")
    assert result.accepted is False
    assert result.errors[0].code == "EMPTY_BATCH"


def test_sync_missing_source_id_returns_row_error_without_writes(db):
    item = SourceRecordInput(project_id="project-1", source_system="erp", dataset="suppliers", source_record_id="  ", attributes={"name": "A"})
    result = ingest_batch(db, project_id="project-1", records=[record("erp", "suppliers", "A"), item], mode=IngestMode.SYNC, input_version="v1")
    assert result.accepted is False
    assert result.errors[0].index == 1
    assert result.errors[0].code == "SOURCE_ID_REQUIRED"
    assert db.scalar(select(func.count()).select_from(SourceRecord)) == 0


def test_exploration_generates_one_time_source_keys_and_marks_limitations(db):
    item = SourceRecordInput(project_id="project-1", source_system="erp", dataset="suppliers", source_record_id="upstream-id", attributes={"name": "A"})
    first = ingest_batch(db, project_id="project-1", records=[item], mode=IngestMode.EXPLORATION, input_version="v1")
    db.commit()
    second = ingest_batch(db, project_id="project-1", records=[item], mode=IngestMode.EXPLORATION, input_version="v1")
    assert first.identities[0].source_key.source_record_id != second.identities[0].source_key.source_record_id
    assert first.identity_guarantee == "single_import"
    assert first.limitations
    assert second.created_records == 1

def test_source_key_is_namespaced_and_normalized():
    left = SourceKey("project-1", "erp", "suppliers", "001")
    other_source = SourceKey("project-1", "procurement", "suppliers", "001")
    other_dataset = SourceKey("project-1", "erp", "vendors", "001")
    assert len({left, other_source, other_dataset}) == 3
    assert SourceKey("project-1", " ERP ", " Suppliers ", " 001 ") == left


def test_rerun_keeps_source_and_entity_ids(db):
    batch = [record("erp", "suppliers", "A"), record("procurement", "suppliers", "B")]
    first = ingest_batch(db, project_id="project-1", records=batch, mode=IngestMode.SYNC, input_version="v1")
    db.commit()
    first_map = {str(identity.source_key): (identity.source_record_id, identity.entity_id) for identity in first.identities}
    second = ingest_batch(db, project_id="project-1", records=batch, mode=IngestMode.SYNC, input_version="v2")
    db.commit()
    second_map = {str(identity.source_key): (identity.source_record_id, identity.entity_id) for identity in second.identities}
    assert second_map == first_map
    assert second.created_records == 0
    assert second.created_entities == 0
    assert second.created_revisions == 0
    current_revision_ids = {row.id for row in db.query(SourceRevision).all()}
    assert {row.current_revision_id for row in db.query(SourceRecord).all()} <= current_revision_ids


def test_changed_source_record_creates_revision_without_changing_identity(db):
    original = record("erp", "suppliers", "A", {"name": "Old name"})
    first = ingest_batch(db, project_id="project-1", records=[original], mode=IngestMode.SYNC, input_version="v1")
    db.commit()
    changed = record("erp", "suppliers", "A", {"name": "New name"})
    second = ingest_batch(db, project_id="project-1", records=[changed], mode=IngestMode.SYNC, input_version="v2")
    db.commit()
    assert (first.identities[0].source_record_id, first.identities[0].entity_id) == (second.identities[0].source_record_id, second.identities[0].entity_id)
    assert second.created_records == 0
    assert second.created_entities == 0
    assert second.created_revisions == 1
    assert second.identities[0].source_revision_id
    revisions = db.query(SourceRevision).order_by(SourceRevision.revision_no).all()
    assert [(r.revision_no, r.input_version, r.attributes["name"]) for r in revisions] == [(1, "v1", "Old name"), (2, "v2", "New name")]


def test_same_source_id_in_other_system_creates_distinct_source_record(db):
    first = ingest_batch(db, project_id="project-1", records=[record("erp", "suppliers", "001")], mode=IngestMode.SYNC, input_version="v1")
    second = ingest_batch(db, project_id="project-1", records=[record("procurement", "suppliers", "001")], mode=IngestMode.SYNC, input_version="v1")
    assert first.identities[0].source_record_id != second.identities[0].source_record_id
    assert first.identities[0].entity_id != second.identities[0].entity_id


def test_reordered_input_keeps_identity(db):
    batch = [record("erp", "suppliers", "A"), record("procurement", "suppliers", "B")]
    first = ingest_batch(db, project_id="project-1", records=batch, mode=IngestMode.SYNC, input_version="v1")
    db.commit()
    second = ingest_batch(db, project_id="project-1", records=list(reversed(batch)), mode=IngestMode.SYNC, input_version="v2")
    assert {str(identity.source_key): identity.entity_id for identity in first.identities} == {str(identity.source_key): identity.entity_id for identity in second.identities}


def test_missing_or_conflicting_key_rejects_whole_batch(db):
    good = record("erp", "suppliers", "A")
    with pytest.raises(ValueError):
        SourceKey("project-1", "erp", "suppliers", "   ")
    conflict = record("erp", "suppliers", "A", {"name": "different"})
    result = ingest_batch(db, project_id="project-1", records=[good, conflict], mode=IngestMode.SYNC, input_version="v1")
    assert result.accepted is False
    assert result.errors
    assert db.scalar(select(func.count()).select_from(SourceRecord)) == 0


def test_current_revision_must_belong_to_the_same_source_record(db):
    first = ingest_batch(db, project_id="project-1", records=[record("erp", "suppliers", "A"), record("erp", "suppliers", "B")], mode=IngestMode.SYNC, input_version="v1")
    db.commit()
    source_a = db.scalar(select(SourceRecord).where(SourceRecord.source_record_id == "A"))
    source_b = db.scalar(select(SourceRecord).where(SourceRecord.source_record_id == "B"))
    revision_b = db.scalar(select(SourceRevision).where(SourceRevision.source_record_id == source_b.id))
    source_a.current_revision_id = revision_b.id
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert first.accepted
