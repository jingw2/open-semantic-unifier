"""File imports share source identity and batch atomicity contracts."""
import io
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.unification import SourceRecord, SourceRevision, UnifiedEntity
from test_unification_public_api import public_api


ENDPOINT = "/api/v1/projects/project-one/unification/batches/file"
FORM = {"input_version": "file-v1", "source_system": "erp", "dataset": "supplier", "source_key_column": "id", "mode": "sync"}


def parquet(rows):
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("filename,content", [
    ("records.csv", b"id,name\n001,Acme\n002,Beta\n"),
    ("records.parquet", parquet([{"id": "001", "name": "Acme"}, {"id": "002", "name": "Beta"}])),
])
def test_file_import_preserves_text_keys_and_reuses_identities(public_api, filename, content):
    client, maker, _ = public_api
    def upload():
        return client.post(ENDPOINT, data=FORM, files={"file": (filename, content, "application/octet-stream")})
    first = upload()
    assert first.status_code == 201, first.text
    assert {item["source_key"][-1] for item in first.json()["identities"]} == {"001", "002"}
    second = upload()
    assert second.status_code == 201
    assert second.json()["created_records"] == second.json()["created_revisions"] == 0
    assert second.json()["identities"] == first.json()["identities"]
    with maker() as db:
        attrs = {source.source_record_id: db.get(SourceRevision, source.current_revision_id).attributes for source in db.scalars(select(SourceRecord)).all()}
    assert attrs == {"001": {"name": "Acme"}, "002": {"name": "Beta"}}


@pytest.mark.parametrize("filename,content", [
    ("missing.csv", b"id,name\n001,Acme\n,Beta\n"),
    ("duplicate.csv", b"id,name\n001,Acme\n001,Beta\n"),
    ("missing.parquet", parquet([{"id": "001", "name": "Acme"}, {"id": None, "name": "Beta"}])),
    ("duplicate.parquet", parquet([{"id": "001", "name": "Acme"}, {"id": "001", "name": "Beta"}])),
    ("missing-column.csv", b"name\nAcme\n"),
    ("empty.csv", b"id,name\n"),
    ("bad.csv", b'\xff'),
    ("bad.parquet", b"not parquet"),
    ("numbers.parquet", parquet([{"id": 1, "name": "Acme"}])),
    ("records.txt", b"id,name\n001,Acme\n"),
])
def test_invalid_file_is_rejected_without_writes(public_api, filename, content):
    client, maker, _ = public_api
    response = client.post(ENDPOINT, data=FORM, files={"file": (filename, content, "application/octet-stream")})
    assert response.status_code in (415, 422), response.text
    with maker() as db:
        assert all(db.scalar(select(func.count()).select_from(model)) == 0 for model in (SourceRecord, SourceRevision, UnifiedEntity))


def test_file_import_respects_project_and_role_permissions(public_api):
    client, maker, identity = public_api
    upload = {"file": ("records.csv", b"id,name\n001,Acme\n", "text/csv")}
    identity["role"] = "viewer"
    assert client.post(ENDPOINT, data=FORM, files=upload).status_code == 403
    identity["role"] = "editor"
    assert client.post(ENDPOINT.replace("project-one", "project-two"), data=FORM, files=upload).status_code == 404
    with maker() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecord)) == 0


@pytest.mark.parametrize("filename,content,code", [
    ("missing.csv", b"id,name\n001,Acme\n,Beta\n", "SOURCE_ID_REQUIRED"),
    ("duplicate.csv", b"id,name\n001,Acme\n001,Beta\n", "DUPLICATE_SOURCE_KEY"),
    ("missing.parquet", parquet([{"id": "001", "name": "Acme"}, {"id": None, "name": "Beta"}]), "SOURCE_ID_REQUIRED"),
    ("duplicate.parquet", parquet([{"id": "001", "name": "Acme"}, {"id": "001", "name": "Beta"}]), "DUPLICATE_SOURCE_KEY"),
])
def test_file_row_errors_match_json_batch_contract(public_api, filename, content, code):
    client, _, _ = public_api
    response = client.post(ENDPOINT, data=FORM, files={"file": (filename, content, "application/octet-stream")})
    assert response.status_code == 422
    assert any(item["index"] == 1 and item["code"] == code for item in response.json()["detail"])


def test_file_exploration_declares_single_import_identity(public_api):
    client, _, _ = public_api
    response = client.post(ENDPOINT, data={**FORM, "mode": "exploration"}, files={"file": ("records.csv", b"id,name\n,Acme\n", "text/csv")})
    assert response.status_code == 201, response.text
    assert response.json()["identity_guarantee"] == "single_import"
    assert "repeated imports create new identities" in response.json()["limitations"][0]


@pytest.mark.parametrize("filename,content,byte_limit,row_limit,expanded_limit", [
    ("large.csv", b"id,name\n001,Acme\n", 8, 100, 10000),
    ("rows.csv", b"id,name\n001,Acme\n002,Beta\n", 10000, 1, 10000),
    ("rows.parquet", parquet([{"id": "001", "name": "Acme"}, {"id": "002", "name": "Beta"}]), 10000, 1, 10000),
    ("expanded.parquet", parquet([{"id": "001", "name": "x" * 4000}]), 10000, 100, 1000),
])
def test_file_limits_reject_before_writes(public_api, monkeypatch, filename, content, byte_limit, row_limit, expanded_limit):
    import app.api.v1.unification as api
    monkeypatch.setattr(api, "FILE_IMPORT_MAX_BYTES", byte_limit, raising=False)
    monkeypatch.setattr(api, "FILE_IMPORT_MAX_ROWS", row_limit, raising=False)
    monkeypatch.setattr(api, "FILE_IMPORT_MAX_EXPANDED_BYTES", expanded_limit, raising=False)
    client, maker, _ = public_api
    response = client.post(ENDPOINT, data=FORM, files={"file": (filename, content, "application/octet-stream")})
    assert response.status_code == 413
    with maker() as db:
        assert all(db.scalar(select(func.count()).select_from(model)) == 0 for model in (SourceRecord, SourceRevision, UnifiedEntity))


def test_parquet_nonfinite_attributes_are_row_errors_and_atomic(public_api):
    client, maker, _ = public_api
    data = parquet([{"id": "001", "score": 1.0}, {"id": "002", "score": float("nan")}])
    response = client.post(ENDPOINT, data=FORM, files={"file": ("nan.parquet", data, "application/octet-stream")})
    assert response.status_code == 422
    assert any(item["index"] == 1 and item["code"] == "ATTRIBUTES_INVALID" for item in response.json()["detail"])
    with maker() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecord)) == 0


def test_too_many_columns_is_rejected(public_api, monkeypatch):
    import app.api.v1.unification as api
    monkeypatch.setattr(api, "FILE_IMPORT_MAX_COLUMNS", 2, raising=False)
    client, maker, _ = public_api
    response = client.post(ENDPOINT, data=FORM, files={"file": ("wide.csv", b"id,name,extra\n001,A,B\n", "text/csv")})
    assert response.status_code == 413
    with maker() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecord)) == 0
