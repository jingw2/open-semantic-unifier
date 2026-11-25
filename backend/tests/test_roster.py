"""Standard roster detection, config API, catalog build, and L1 alignment."""
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_unification_public_api import public_api  # noqa: F401

from app.models.entities import DataSource, Job
from app.services.engines import execute_l1
from app.services.roster import (
    RosterError,
    detect_roster_columns,
    normalize_roster_config,
    roster_catalog_for_project,
)
from app.services.storage import storage
from app.workers.tasks import run_job_sync

ROSTER_CSV = b"\xef\xbb\xbfcompany_name,aliases,tax_id,phone\nAcme,Acme Ltd|ACME;Acme Corp,9144,13800000000\nBeta,,9145,13900000000\n"
ROSTER_TSV = "company_name\taliases\ttax_id\tphone\nAcme\tAcme Ltd|ACME\t9144\t13800000000\nBeta\t\t9145\t13900000000\n".encode("utf-8")
DIRTY_CSV = b"supplier_raw_name\nAcme Ltd\nBeta\n"


def _profile(columns, name="suppliers.csv"):
    return {
        "total_rows": 2,
        "table_count": 1,
        "tables": [{"name": name, "rows": 2, "cols": len(columns), "columns": columns, "preview": []}],
    }


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    monkeypatch.setattr(storage, "local_root", str(root))
    return root


def _seed_source(maker, *, project_id="project-one", name="suppliers.csv", object_key="projects/project-one/uploads/suppliers.csv",
                 columns=("supplier_id", "company_name", "aliases", "tax_id", "phone"), routing=None, content=ROSTER_CSV):
    storage.put_bytes(object_key, content)
    with maker() as db:
        source = DataSource(
            project_id=project_id, name=name, source_type="file",
            object_key=object_key, profile_json=_profile(list(columns), name=name),
            routing_json=routing or {},
        )
        db.add(source)
        db.commit()
        return source.id


def _seed_roster_source(maker, *, columns=("company_name", "aliases", "tax_id", "phone"),
                        roster=None, name="suppliers.csv", content=ROSTER_CSV, project_id="project-one"):
    routing = {"role": "standard_roster", "roster": roster or {
        "id_column": "",
        "name_column": "company_name",
        "alias_columns": ["aliases"],
        "tax_id_column": "tax_id",
        "phone_column": "phone",
    }}
    return _seed_source(maker, project_id=project_id, name=name, columns=columns, routing=routing, content=content)


def test_detect_roster_columns_english_terms():
    detected = detect_roster_columns(["supplier_id", "company_name", "aliases", "tax_id", "phone"])
    assert detected["id_column"] == "supplier_id"
    assert detected["name_column"] == "company_name"
    assert detected["alias_columns"] == ["aliases"]
    assert detected["tax_id_column"] == "tax_id"
    assert detected["phone_column"] == "phone"


def test_detect_roster_columns_chinese_terms():
    detected = detect_roster_columns(["编号", "公司名称", "曾用名", "税号", "手机"])
    assert detected["id_column"] == "编号"
    assert detected["name_column"] == "公司名称"
    assert detected["alias_columns"] == ["曾用名"]
    assert detected["tax_id_column"] == "税号"
    assert detected["phone_column"] == "手机"


def test_detect_roster_columns_matches_substrings():
    detected = detect_roster_columns(["供应商编号", "供应商名称", "别名1", "统一社会信用代码", "联系电话"])
    assert detected["id_column"] == "供应商编号"
    assert detected["name_column"] == "供应商名称"
    assert detected["alias_columns"] == ["别名1"]
    assert detected["tax_id_column"] == "统一社会信用代码"
    assert detected["phone_column"] == "联系电话"


def test_detect_roster_columns_requires_a_name_term():
    detected = detect_roster_columns(["amount", "city"])
    assert detected["name_column"] == ""
    with pytest.raises(RosterError) as exc:
        normalize_roster_config(None, ["amount", "city"])
    assert exc.value.code == "ROSTER_NAME_COLUMN_MISSING"


def test_put_roster_auto_detects(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    response = client.put(f"/api/v1/projects/project-one/datasources/{source_id}/roster", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data_source_id"] == source_id
    assert body["role"] == "standard_roster"
    assert body["roster"]["id_column"] == "supplier_id"
    assert body["roster"]["name_column"] == "company_name"
    assert body["roster"]["alias_columns"] == ["aliases"]
    assert body["roster"]["tax_id_column"] == "tax_id"
    assert body["roster"]["phone_column"] == "phone"
    with maker() as db:
        source = db.get(DataSource, source_id)
        assert source.routing_json["role"] == "standard_roster"
        assert source.routing_json["roster"]["name_column"] == "company_name"


def test_put_roster_custom_config(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    response = client.put(
        f"/api/v1/projects/project-one/datasources/{source_id}/roster",
        json={"id_column": "supplier_id", "name_column": "company_name", "alias_columns": ["aliases"],
              "tax_id_column": "tax_id", "phone_column": "phone"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["roster"] == {
        "id_column": "supplier_id",
        "name_column": "company_name",
        "alias_columns": ["aliases"],
        "tax_id_column": "tax_id",
        "phone_column": "phone",
    }


def test_put_roster_rejects_unknown_column(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    response = client.put(
        f"/api/v1/projects/project-one/datasources/{source_id}/roster",
        json={"name_column": "nope"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ROSTER_COLUMN_NOT_FOUND"


def test_put_roster_requires_name_column(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    response = client.put(
        f"/api/v1/projects/project-one/datasources/{source_id}/roster",
        json={"id_column": "supplier_id"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ROSTER_NAME_COLUMN_MISSING"


def test_put_roster_cross_project_is_404(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker, project_id="project-two")
    response = client.put(f"/api/v1/projects/project-one/datasources/{source_id}/roster", json={})
    assert response.status_code == 404


def test_delete_roster_clears_role(public_api, storage_root):
    client, maker, _identity = public_api
    source_id = _seed_source(maker)
    assert client.put(f"/api/v1/projects/project-one/datasources/{source_id}/roster", json={}).status_code == 200
    response = client.delete(f"/api/v1/projects/project-one/datasources/{source_id}/roster")
    assert response.status_code == 200, response.text
    assert response.json() == {"data_source_id": source_id, "role": None}
    with maker() as db:
        source = db.get(DataSource, source_id)
        assert "role" not in source.routing_json
        assert "roster" not in source.routing_json


def test_roster_catalog_parses_aliases_tax_phone_and_generates_ids(public_api, storage_root):
    client, maker, _identity = public_api
    _seed_roster_source(maker)
    with maker() as db:
        catalog, source = roster_catalog_for_project(db, "project-one")
    assert source is not None
    assert source.name == "suppliers.csv"
    assert len(catalog) == 2
    acme = catalog[0]
    assert acme["name"] == "Acme"
    assert acme["id"].startswith("ROSTER-")
    assert "Acme Ltd" in acme["aliases"]
    assert "ACME" in acme["aliases"]
    assert "Acme Corp" in acme["aliases"]
    assert "9144" in acme["aliases"]
    assert "13800000000" in acme["aliases"]
    assert "Acme" not in acme["aliases"]
    assert catalog[1]["name"] == "Beta"


def test_roster_catalog_parses_tsv(public_api, storage_root):
    client, maker, _identity = public_api
    _seed_roster_source(maker, name="suppliers.tsv", content=ROSTER_TSV)
    with maker() as db:
        catalog, _source = roster_catalog_for_project(db, "project-one")
    assert catalog[0]["name"] == "Acme"
    assert "Acme Ltd" in catalog[0]["aliases"]


def test_roster_catalog_returns_none_when_absent(public_api, storage_root):
    client, maker, _identity = public_api
    _seed_source(maker, routing={"recommended_stage": "L1"})
    with maker() as db:
        catalog, source = roster_catalog_for_project(db, "project-one")
    assert catalog is None
    assert source is None


def test_roster_catalog_raises_when_no_usable_rows(public_api, storage_root):
    client, maker, _identity = public_api
    _seed_roster_source(maker, content=b"\xef\xbb\xbfcompany_name,aliases,tax_id,phone\n,,,\n")
    with maker() as db:
        with pytest.raises(RosterError) as exc:
            roster_catalog_for_project(db, "project-one")
    assert exc.value.code == "ROSTER_EMPTY"


def test_l1_aligns_against_roster_and_reports_reference_catalog(public_api, storage_root, monkeypatch):
    client, maker, _identity = public_api
    _seed_roster_source(maker)
    dirty_id = _seed_source(maker, name="dirty.csv", object_key="projects/project-one/uploads/dirty.csv",
                            columns=("supplier_raw_name",), content=DIRTY_CSV)
    with maker() as db:
        job = Job(project_id="project-one", stage="L1", status="PENDING",
                  params_json={"datasource_ids": [dirty_id]})
        db.add(job)
        db.commit()
        job_id = job.id

    monkeypatch.setattr("app.workers.tasks.SessionLocal", maker)
    try:
        result = run_job_sync(job_id)
    finally:
        monkeypatch.undo()

    assert result["reference_catalog"]["kind"] == "roster"
    assert result["reference_catalog"]["name"] == "suppliers.csv"
    assert result["reference_catalog"]["source_id"]
    by_name = {row["canonical_name"]: row for row in result["after_top"]}
    assert "Acme" in by_name
    assert "Beta" in by_name


def test_l1_with_roster_uses_canonical_name_via_execute_l1(public_api, storage_root):
    client, maker, _identity = public_api
    _seed_roster_source(maker)
    with maker() as db:
        catalog, _source = roster_catalog_for_project(db, "project-one")
        df = pd.DataFrame({"supplier_raw_name": ["Acme Ltd", "Beta"]})
        result = execute_l1(db, "project-one", "job-roster-direct", df, target_col="supplier_raw_name", demo_catalog=catalog)
    by_name = {row["canonical_name"]: row for row in result["after_top"]}
    assert by_name["Acme"]["count"] == 1
    assert by_name["Beta"]["count"] == 1
