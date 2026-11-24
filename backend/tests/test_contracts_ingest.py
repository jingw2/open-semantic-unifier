"""Contract source ingestion selects stored file DataSources as L3 contract inputs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
from threading import Barrier, local

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import Base
from app.models.contracts import ContractSource
from app.models.entities import DataSource, Project, Workspace
from app.services.contracts import sources as contract_sources
from app.services.storage import StorageError, storage
from test_unification_public_api import public_api  # noqa: F401

SAMPLE_YAML = b"id: supplier\nname: Supplier\n"


def _add_data_source(maker, *, project_id, name, object_key, content, source_type="file"):
    if source_type == "file":
        storage.put_bytes(object_key, content)
    with maker() as db:
        source = DataSource(project_id=project_id, name=name, source_type=source_type, object_key=object_key)
        db.add(source)
        db.commit()
        return source.id


def _select(client, project_id, data_source_id):
    return client.post(
        f"/api/v1/projects/{project_id}/contracts/sources", json={"data_source_id": data_source_id},
    )


def test_selecting_a_file_source_creates_a_source_record_and_reuses_the_hash(public_api):
    client, maker, _identity = public_api
    data_source_id = _add_data_source(
        maker, project_id="project-one", name="contracts.yaml",
        object_key="projects/project-one/uploads/contracts.yaml", content=SAMPLE_YAML,
    )
    first = _select(client, "project-one", data_source_id)
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["project_id"] == "project-one"
    assert payload["data_source_id"] == data_source_id
    assert payload["filename"] == "contracts.yaml"
    assert payload["format"] == "linkml_yaml"
    assert payload["sha256"] == hashlib.sha256(SAMPLE_YAML).hexdigest()
    assert payload["size_bytes"] == len(SAMPLE_YAML)
    assert payload["created_by"] == "editor-one"
    assert len(payload["id"]) == 32
    datetime.fromisoformat(payload["created_at"])

    repeated = _select(client, "project-one", data_source_id)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json() == payload
    with maker() as db:
        assert db.query(ContractSource).count() == 1


def test_missing_or_foreign_data_source_is_404(public_api):
    client, maker, _identity = public_api
    assert _select(client, "project-one", "no-such-source").status_code == 404
    foreign_id = _add_data_source(
        maker, project_id="project-two", name="foreign.yaml",
        object_key="projects/project-two/uploads/foreign.yaml", content=SAMPLE_YAML,
    )
    assert _select(client, "project-one", foreign_id).status_code == 404
    assert _select(client, "project-two", foreign_id).status_code == 404


def test_non_file_data_source_is_422(public_api):
    client, maker, _identity = public_api
    with maker() as db:
        source = DataSource(project_id="project-one", name="orders.duckdb", source_type="duckdb", object_key="")
        db.add(source)
        db.commit()
        data_source_id = source.id
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 422
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_file_source_without_stored_content_is_422(public_api):
    client, maker, _identity = public_api
    data_source_id = _add_data_source(
        maker, project_id="project-one", name="empty.yaml",
        object_key="projects/project-one/uploads/empty.yaml", content=b"",
    )
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 422
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_unsupported_extension_is_422(public_api):
    client, maker, _identity = public_api
    data_source_id = _add_data_source(
        maker, project_id="project-one", name="contracts.txt",
        object_key="projects/project-one/uploads/contracts.txt", content=b"col1\tcol2\n",
    )
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 422
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_unreadable_object_is_422(public_api):
    client, maker, _identity = public_api
    with maker() as db:
        source = DataSource(
            project_id="project-one", name="missing.yaml", source_type="file",
            object_key="projects/project-one/uploads/does-not-exist.yaml",
        )
        db.add(source)
        db.commit()
        data_source_id = source.id
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 422
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_oversized_content_is_413_and_writes_nothing(public_api, monkeypatch):
    client, maker, _identity = public_api
    data_source_id = _add_data_source(
        maker, project_id="project-one", name="big.yaml",
        object_key="projects/project-one/uploads/big.yaml", content=SAMPLE_YAML * 64,
    )
    monkeypatch.setattr(contract_sources, "MAX_CONTRACT_SOURCE_BYTES", 100)
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 413
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_viewer_cannot_select_contract_sources(public_api):
    client, maker, identity = public_api
    data_source_id = _add_data_source(
        maker, project_id="project-one", name="viewer.yaml",
        object_key="projects/project-one/uploads/viewer.yaml", content=SAMPLE_YAML,
    )
    identity["role"] = "viewer"
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 403
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_listing_contract_sources_is_project_scoped_and_newest_first(public_api):
    client, maker, _identity = public_api
    first_id = _add_data_source(
        maker, project_id="project-one", name="first.yaml",
        object_key="projects/project-one/uploads/first.yaml", content=b"a: 1\n",
    )
    second_id = _add_data_source(
        maker, project_id="project-one", name="second.yaml",
        object_key="projects/project-one/uploads/second.yaml", content=b"b: 2\n",
    )
    first = _select(client, "project-one", first_id).json()
    second = _select(client, "project-one", second_id).json()
    foreign_id = _add_data_source(
        maker, project_id="project-two", name="other.yaml",
        object_key="projects/project-two/uploads/other.yaml", content=b"c: 3\n",
    )
    with maker() as db:
        rows = db.query(ContractSource).order_by(ContractSource.id).all()
        rows[0].created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows[1].created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        db.add(ContractSource(
            project_id="project-two", data_source_id=foreign_id, filename="other.yaml",
            format="linkml_yaml", sha256=hashlib.sha256(b"c: 3\n").hexdigest(),
            size_bytes=4, created_by="editor-two",
            created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        ))
        db.commit()
        newer_id = rows[1].id
        older_id = rows[0].id
    assert {first["id"], second["id"]} == {newer_id, older_id}

    listing = client.get("/api/v1/projects/project-one/contracts/sources")
    assert listing.status_code == 200, listing.text
    items = listing.json()
    assert [item["id"] for item in items] == [newer_id, older_id]
    assert {item["project_id"] for item in items} == {"project-one"}
    assert client.get("/api/v1/projects/project-two/contracts/sources").status_code == 404


def test_storage_failure_is_422(public_api, monkeypatch):
    client, maker, _identity = public_api
    data_source_id = _add_data_source(
        maker, project_id="project-one", name="unavailable.yaml",
        object_key="projects/project-one/uploads/unavailable.yaml", content=SAMPLE_YAML,
    )

    def fail(_object_key):
        raise StorageError("backend unavailable")

    monkeypatch.setattr(contract_sources.storage, "get_bytes", fail)
    response = _select(client, "project-one", data_source_id)
    assert response.status_code == 422
    with maker() as db:
        assert db.query(ContractSource).count() == 0


def test_concurrent_selection_reuses_one_row(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'contract-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    object_key = "projects/project-race/uploads/race.yaml"
    storage.put_bytes(object_key, SAMPLE_YAML)
    with maker.begin() as db:
        db.add(Workspace(id="workspace-race", name="Race", slug="workspace-race"))
        db.flush()
        db.add(Project(id="project-race", workspace_id="workspace-race", name="Race", slug="project-race"))
        db.flush()
        db.add(DataSource(
            id="source-race", project_id="project-race", name="race.yaml",
            source_type="file", object_key=object_key,
        ))
    barrier = Barrier(2)
    thread_state = local()

    @event.listens_for(engine, "before_cursor_execute")
    def synchronize_initial_reads(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM contract_sources" in statement and getattr(thread_state, "wait_for_peer", False):
            thread_state.wait_for_peer = False
            barrier.wait(timeout=5)

    def select_once():
        thread_state.wait_for_peer = True
        with maker() as db:
            source = contract_sources.create_contract_source(
                db, project_id="project-race", data_source_id="source-race", actor_id="race-user",
            )
            db.commit()
            return source.id

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            ids = list(workers.map(lambda _: select_once(), range(2)))
        with maker() as db:
            assert db.scalar(select(func.count()).select_from(ContractSource)) == 1
        assert len(set(ids)) == 1
    finally:
        engine.dispose()
