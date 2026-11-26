"""JSON-LD mapping and instance export from a compiled contract version."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_unification_public_api import public_api  # noqa: F401

from app.models.contracts import ContractModelVersion, ContractSource
from app.models.entities import DataSource
from app.models.unification import UnificationSnapshot
from app.services.contracts.jsonld import (
    JsonLdInvalid,
    build_jsonld,
    infer_jsonld_mapping,
    iter_instance_rows,
    jsonld_model,
)
from app.services.contracts.validation import validate_contract_model
from app.services.storage import storage

LINKML = """id: https://example.org/supplier
name: supplier_model
prefixes:
  ex: https://example.org/
default_prefix: ex
classes:
  Supplier:
    attributes:
      tax_id:
        identifier: true
        required: true
      company_name:
        required: true
      city:
        range: string
"""

ROWS = [
    {"tax_id": "91310000TEST", "company_name": "Acme", "city": "Shenzhen"},
    {"tax_id": "91310000BETA", "company_name": "Beta", "city": "Shanghai"},
]


def _model():
    report = validate_contract_model(linkml_yaml=LINKML, known_entity_types={"Supplier"})
    assert report.accepted, report.issues
    return SimpleNamespace(canonical_model_json=report.canonical_model_json, artifacts_json={"linkml_yaml": LINKML})


def test_infers_class_id_and_slot_mapping_from_columns():
    model = jsonld_model(_model())
    mapping = infer_jsonld_mapping(model, ["company_name", "tax_id", "city"])
    assert mapping["class_name"] == "Supplier"
    assert mapping["id_slot"] == "tax_id"
    assert mapping["columns"] == {"tax_id": "tax_id", "company_name": "company_name", "city": "city"}


def test_builds_context_and_graph_with_slot_overrides():
    model = jsonld_model(_model())
    payload = build_jsonld(model, ROWS, {
        "class_name": "Supplier",
        "id_slot": "tax_id",
        "columns": {"tax_id": "tax_id", "company_name": "company_name"},
    })
    assert payload["@context"]["@vocab"] == "https://example.org/"
    assert payload["@context"]["company_name"] == "https://example.org/company_name"
    assert payload["@graph"][0]["@type"] == "Supplier"
    assert payload["@graph"][0]["@id"] == "urn:osu:91310000TEST"
    assert payload["@graph"][0]["company_name"] == "Acme"
    assert payload["count"] == 2
    assert payload["truncated"] is False


def test_ids_are_percent_encoded_and_synthetic_ids_do_not_collide():
    model = jsonld_model(_model())
    rows = [
        {"tax_id": "Acme Inc. / (HQ)", "company_name": "Acme"},
        {"tax_id": "", "company_name": "Missing"},
        {"tax_id": "autogen:2", "company_name": "Also missing-ish"},
    ]
    payload = build_jsonld(model, rows, {
        "class_name": "Supplier", "id_slot": "tax_id",
        "columns": {"tax_id": "tax_id", "company_name": "company_name"},
    })
    assert payload["@graph"][0]["@id"] == "urn:osu:Acme%20Inc.%20%2F%20%28HQ%29"
    assert payload["@graph"][1]["@id"] == "urn:osu:autogen:2"
    assert payload["@graph"][2]["@id"] == "urn:osu:autogen%3A2"
    assert payload["@graph"][1]["@id"] != payload["@graph"][2]["@id"]


def test_instance_row_reader_stops_at_the_cap():
    text = "id,name\n" + "".join(f"{index},row-{index}\n" for index in range(10))
    header, rows, has_more = iter_instance_rows(text, max_rows=5)
    assert header == ["id", "name"]
    assert len(rows) == 5
    assert rows[-1]["id"] == "4"
    assert has_more is True
    header, rows, has_more = iter_instance_rows(text)
    assert len(rows) == 10
    assert has_more is False


def test_truncates_at_the_limit():
    payload = build_jsonld(jsonld_model(_model()), ROWS, {
        "class_name": "Supplier", "id_slot": "tax_id",
        "columns": {"tax_id": "tax_id", "company_name": "company_name"},
    }, limit=1)
    assert payload["count"] == 1
    assert payload["truncated"] is True


def test_rejects_unknown_class_slot_and_id_problems():
    model = jsonld_model(_model())
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(model, ROWS, {"class_name": "Nope", "id_slot": "tax_id", "columns": {"tax_id": "tax_id"}})
    assert exc.value.code == "UNKNOWN_CLASS"
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(model, ROWS, {"class_name": "Supplier", "id_slot": "", "columns": {}})
    assert exc.value.code == "MISSING_ID_SLOT"
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(model, ROWS, {"class_name": "Supplier", "id_slot": "tax_id", "columns": {}})
    assert exc.value.code == "MISSING_ID_COLUMN"
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(model, ROWS, {"class_name": "Supplier", "id_slot": "tax_id",
                                   "columns": {"tax_id": "company_name", "company_name": "company_name"}})
    assert exc.value.code == "DUPLICATE_COLUMN_MAPPING"
    with pytest.raises(JsonLdInvalid) as exc:
        build_jsonld(model, ROWS, {"class_name": "Supplier", "id_slot": "tax_id",
                                   "columns": {"tax_id": "tax_id", "nope": "city"}})
    assert exc.value.code == "UNKNOWN_SLOT"


def _seed_contract(maker, project_id: str = "project-one") -> tuple[str, str]:
    with maker() as db:
        db.add(DataSource(
            id="ds-instance", project_id=project_id, name="instances.csv", source_type="file",
            object_key=f"projects/{project_id}/uploads/instances.csv",
        ))
        db.add(DataSource(
            id="ds-contract", project_id=project_id, name="supplier.yaml", source_type="file",
            object_key=f"projects/{project_id}/uploads/supplier.yaml",
        ))
        snapshot = UnificationSnapshot(
            project_id=project_id, version=1,
            snapshot_json={"entities": {"entity-one": {"entity_type": "Supplier", "status": "ACTIVE"}}},
            actor_id="publisher",
        )
        db.add(snapshot)
        db.flush()
        contract_source = ContractSource(
            id="cs-one", project_id=project_id, data_source_id="ds-contract",
            filename="supplier.yaml", format="yaml", sha256="ab" * 32, size_bytes=len(LINKML),
            created_by="editor-one",
        )
        db.add(contract_source)
        db.flush()
        report = validate_contract_model(linkml_yaml=LINKML, known_entity_types={"Supplier"})
        version = ContractModelVersion(
            id="cv-one", project_id=project_id, source_id="cs-one", version=1, status="DRAFT",
            source_sha256="ab" * 32, canonical_model_json=report.canonical_model_json,
            artifacts_json={"linkml_yaml": LINKML, "sql_ddl": "", "json_schema": "{}", "er_diagram": ""},
            compiler_versions_json={"linkml": "1.0.0"},
            validation_json={"counts": {"classes": 1}, "issues": []},
            entity_snapshot_id=snapshot.id, snapshot_version=1, created_by="editor-one",
        )
        db.add(version)
        db.commit()
    return "cv-one", "ds-instance"


@pytest.fixture
def jsonld_api(public_api, tmp_path, monkeypatch):
    client, maker, identity = public_api
    storage.local_root = str(tmp_path / "jsonld-storage")
    storage.put_bytes(
        "projects/project-one/uploads/instances.csv",
        "tax_id,company_name,city\n91310000TEST,Acme,Shenzhen\n91310000BETA,Beta,Shanghai\n".encode(),
    )
    version_id, data_source_id = _seed_contract(maker)
    return client, maker, version_id, data_source_id


def test_preview_reports_inferred_mapping(jsonld_api):
    client, _maker, version_id, data_source_id = jsonld_api
    response = client.get(
        f"/api/v1/projects/project-one/contracts/versions/{version_id}/jsonld/preview",
        params={"data_source_id": data_source_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["inferred_mapping"]["class_name"] == "Supplier"
    assert payload["inferred_mapping"]["id_slot"] == "tax_id"


def test_generate_returns_context_and_graph(jsonld_api):
    client, _maker, version_id, data_source_id = jsonld_api
    response = client.post(
        f"/api/v1/projects/project-one/contracts/versions/{version_id}/jsonld",
        json={
            "data_source_id": data_source_id, "class_name": "Supplier", "id_slot": "tax_id",
            "columns": {"tax_id": "tax_id", "company_name": "company_name", "city": "city"},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 2
    assert payload["@graph"][0]["@id"] == "urn:osu:91310000TEST"
    assert payload["@graph"][1]["city"] == "Shanghai"


def test_generation_reads_at_most_5000_rows_and_reports_truncation(jsonld_api):
    client, _maker, version_id, data_source_id = jsonld_api
    rows = "tax_id,company_name,city\n" + "".join(
        f"91310000{index:04d},Company {index},Shenzhen\n" for index in range(5001)
    )
    storage.put_bytes("projects/project-one/uploads/instances.csv", rows.encode())
    response = client.post(
        f"/api/v1/projects/project-one/contracts/versions/{version_id}/jsonld",
        json={
            "data_source_id": data_source_id, "class_name": "Supplier", "id_slot": "tax_id",
            "columns": {"tax_id": "tax_id", "company_name": "company_name"},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 5000
    assert payload["truncated"] is True


def test_unknown_class_is_a_structured_422(jsonld_api):
    client, _maker, version_id, data_source_id = jsonld_api
    response = client.post(
        f"/api/v1/projects/project-one/contracts/versions/{version_id}/jsonld",
        json={"data_source_id": data_source_id, "class_name": "Nope", "id_slot": "tax_id", "columns": {"tax_id": "tax_id"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNKNOWN_CLASS"


def test_missing_contract_version_is_404(jsonld_api):
    client, _maker, _version_id, data_source_id = jsonld_api
    response = client.post(
        "/api/v1/projects/project-one/contracts/versions/nope/jsonld",
        json={"data_source_id": data_source_id, "class_name": "Supplier", "id_slot": "tax_id", "columns": {"tax_id": "tax_id"}},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NO_CONTRACT"


def test_rejects_a_source_over_the_size_limit_before_reading_it(jsonld_api, monkeypatch):
    client, _maker, version_id, data_source_id = jsonld_api
    monkeypatch.setattr(storage, "get_size", lambda _key: 50 * 1024 * 1024 + 1)
    response = client.get(
        f"/api/v1/projects/project-one/contracts/versions/{version_id}/jsonld/preview",
        params={"data_source_id": data_source_id},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "JSONLD_SOURCE_TOO_LARGE"


def test_missing_data_source_is_404(jsonld_api):
    client, _maker, version_id, _data_source_id = jsonld_api
    response = client.get(
        f"/api/v1/projects/project-one/contracts/versions/{version_id}/jsonld/preview",
        params={"data_source_id": "nope"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DATA_SOURCE_NOT_FOUND"
