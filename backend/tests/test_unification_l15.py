"""L15 access-control lifecycle with real bearer and API key authentication."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.api.deps import get_current_user
from app.core.security import create_access_token
from app.main import app
from app.models.entities import Job, ProjectApiKey, User, WorkspaceMember
from app.models.unification import IdentityChangeRecord, IdentityDecisionRecord, SourceRecord, UnificationSchemeVersion, UnificationSnapshot
from test_unification_public_api import public_api


def test_l15_authorization_hides_resources_and_preserves_state(public_api, monkeypatch):
    import app.api.v1.unification as api
    from app.services.unification.releases import execute_unification_run

    client, maker, _ = public_api
    with maker() as db:
        for user_id, workspace, role in (("owner", "ws-one", "editor"), ("viewer", "ws-one", "viewer"), ("outsider", "ws-two", "editor")):
            db.add(User(id=user_id, email=f"{user_id}@example.test", password_hash="unused", display_name=user_id))
            db.add(WorkspaceMember(user_id=user_id, workspace_id=workspace, role=role))
        db.add(Job(id="private-job", project_id="project-one", stage="L1", status="SUCCESS"))
        db.commit()
    app.dependency_overrides.pop(get_current_user)

    def headers(user, workspace, role):
        return {"Authorization": "Bearer " + create_access_token(user, f"{user}@example.test", role, workspace)}

    owner = headers("owner", "ws-one", "editor")
    viewer = headers("viewer", "ws-one", "viewer")
    outsider = headers("outsider", "ws-two", "editor")
    prefix = "/api/v1/projects/project-one/unification"
    root = Path(__file__).resolve().parents[2] / "tests/golden/v1/supplier"
    scheme = json.loads((root / "scheme.json").read_text())
    records = [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in json.loads((root / "records.json").read_text()) if row["record_ref"] in {"supplier:erp:02", "supplier:secondary:02"}]

    def run_inline(run_id):
        with maker() as db:
            execute_unification_run(db, run_id)

    monkeypatch.setattr(api, "dispatch_unification_run", run_inline)
    assert client.put(prefix + "/schemes/supplier", headers=owner, json=scheme).status_code == 200
    assert client.post(prefix + "/batches", headers=owner, json={"input_version": "l15", "records": records}).status_code == 201
    run = client.post(prefix + "/runs", headers={**owner, "Idempotency-Key": "l15"}, json={"scheme_key": "supplier", "input_version": "l15"})
    assert run.status_code == 202, run.text
    run_id = run.json()["run_id"]
    reviews = client.get(prefix + "/reviews", headers=owner, params={"run_id": run_id})
    assert reviews.status_code == 200 and len(reviews.json()["items"]) == 1
    pair = reviews.json()["items"][0]
    decision = {"run_id": run_id, "left_source_revision_id": pair["left_source_revision_id"], "right_source_revision_id": pair["right_source_revision_id"], "action": "MERGE", "reason": "Reviewed", "expected_revision": 1}
    published = client.post(prefix + "/snapshots", headers=owner, json={"run_id": run_id, "expected_current_version": 0})
    assert published.status_code == 201, published.text
    snapshot = published.json()["snapshot_id"]
    key = client.post("/api/v1/projects/project-one/api-keys", headers=owner, json={"name": "l15"})
    assert key.status_code == 200
    secret = key.json()["api_key_once"]
    key_id = key.json()["id"]
    owner_keys = client.get("/api/v1/projects/project-one/api-keys", headers=owner)
    assert owner_keys.status_code == 200 and secret not in owner_keys.text
    served = client.post("/api/v1/serve/project-one/unification/resolve", headers={"X-API-Key": secret}, json={"snapshot_id": snapshot, **{k: records[0][k] for k in ("source_system", "dataset", "source_record_id")}})
    assert served.status_code == 200 and served.json()["matched"]

    def persisted_state():
        with maker() as db:
            counts = tuple(db.scalar(select(func.count()).select_from(model)) for model in (IdentityChangeRecord, IdentityDecisionRecord, SourceRecord, UnificationSchemeVersion, UnificationSnapshot, ProjectApiKey))
            owners = sorted((row.id, row.current_entity_id, row.current_revision_id) for row in db.scalars(select(SourceRecord)).all())
            key_states = sorted((row.id, row.key_hash, row.is_active) for row in db.scalars(select(ProjectApiKey)).all())
            return counts, owners, key_states

    before = persisted_state()
    responses = [
        client.get(prefix + f"/snapshots/{snapshot}/export?kind=entities"),
        client.get("/api/v1/jobs/private-job/stream"),
        client.get("/api/v1/artifacts/private-artifact/content"),
        client.get(prefix + "/schemes/supplier", headers=outsider),
        client.get(prefix + f"/snapshots/{snapshot}/export?kind=entities", headers=outsider),
        client.put(prefix + "/schemes/supplier", headers=outsider, json=scheme),
        client.get("/api/v1/projects/project-one/api-keys", headers=outsider),
        client.post("/api/v1/projects/project-one/api-keys", headers=outsider, json={"name": "foreign"}),
        client.delete(f"/api/v1/api-keys/{key_id}", headers=outsider),
        client.post(prefix + "/reviews", headers=viewer, json=decision),
    ]
    assert [response.status_code for response in responses] == [401, 401, 401, 404, 404, 404, 404, 404, 404, 403]
    credentials = [secret, owner["Authorization"].removeprefix("Bearer "), viewer["Authorization"].removeprefix("Bearer "), outsider["Authorization"].removeprefix("Bearer ")]
    assert all(credential not in response.text for response in responses + [owner_keys, served] for credential in credentials)
    assert persisted_state() == before
    assert client.get(prefix + "/reviews", headers=owner, params={"run_id": run_id}).json()["items"] == reviews.json()["items"]
    assert client.post(prefix + "/reviews", headers=owner, json=decision).status_code == 201
