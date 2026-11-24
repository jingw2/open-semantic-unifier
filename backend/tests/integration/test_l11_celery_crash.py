"""Opt-in real Celery prefork crash at an uncommitted staged-result boundary."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(os.getenv("RUN_L11_CELERY_INTEGRATION") != "true", reason="requires Docker Compose, image builds, PostgreSQL and Redis")
def test_l11_worker_loss_during_uncommitted_stage_is_redelivered(tmp_path):
    # This wrapper exists only in the test container. It writes the same result row
    # inside the existing transaction and pauses the first child before commit.
    worker = tmp_path / "fault_worker.py"
    worker.write_text('''import os, time\nimport redis\nfrom sqlalchemy import update\nfrom app.models.unification import UnificationRun\nfrom app.services.unification import releases\nfrom app.workers.celery_app import celery_app\noriginal = releases.complete_run\nqueue = redis.Redis.from_url(os.environ["REDIS_URL"])\ndef pause_once(db, run_id, result, *, expected_attempt=None):\n    if queue.set("l11-fault-once", str(os.getpid()), nx=True):\n        db.execute(update(UnificationRun).where(UnificationRun.id == run_id).values(result_json=result))\n        db.flush()\n        queue.set("l11-staged-pid", str(os.getpid()))\n        while True: time.sleep(1)\n    return original(db, run_id, result, expected_attempt=expected_attempt)\nreleases.complete_run = pause_once\ncelery_app.worker_main(["worker", "--loglevel=info", "--concurrency=1", "--pool=prefork"])\n''')
    override = tmp_path / "override.yml"
    override.write_text(json.dumps({"services": {"worker": {"command": ["python", "-c", worker.read_text()], "environment": {"PYTHONPATH": "/app"}}}}))
    def port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    env_file = tmp_path / ".env"
    values = {"JWT_SECRET_KEY": secrets.token_hex(32), "INIT_ADMIN_EMAIL": "l11@example.invalid", "INIT_ADMIN_PASSWORD": secrets.token_hex(24), "POSTGRES_PASSWORD": secrets.token_hex(24), "MINIO_SECRET_KEY": secrets.token_hex(24), "APP_ENV_FILE": str(env_file), "API_PORT": str(port()), "MINIO_PORT": str(port()), "MINIO_CONSOLE_PORT": str(port())}
    env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    command = ["docker", "compose", "--project-name", f"osu-l11-{secrets.token_hex(5)}", "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml"), "-f", str(override)]
    def compose(*args):
        result = subprocess.run(command + list(args), cwd=ROOT, capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
        return result.stdout.strip()
    api = f"http://127.0.0.1:{values['API_PORT']}"
    def request(path, token=None, payload=None, method=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if path.endswith("/runs") and method == "POST":
            headers["Idempotency-Key"] = "l11-run"
        req = Request(api + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
        with urlopen(req, timeout=10) as response:
            return json.load(response)
    try:
        compose("up", "-d", "--build", "postgres", "redis", "minio", "backend", "worker")
        deadline = time.monotonic() + 180
        while True:
            try:
                request("/health")
                break
            except (OSError, URLError):
                assert time.monotonic() < deadline, "API startup timed out"
                time.sleep(1)
        token = request("/api/v1/auth/login", payload={"email": values["INIT_ADMIN_EMAIL"], "password": values["INIT_ADMIN_PASSWORD"]}, method="POST")["access_token"]
        project = request("/api/v1/projects", token)[0]["id"]
        base = f"/api/v1/projects/{project}/unification"
        scheme = {"entity_type": "supplier", "identity_granularity": "legal_entity", "field_map": {"name": "name", "tax_id": "tax_id"}, "strong_identifiers": ["tax_id"], "candidate_rules": [{"kind": "exact", "fields": ["tax_id"]}]}
        request(base + "/schemes/l11", token, scheme, "PUT")
        records = [{"source_system": source, "dataset": "supplier", "source_record_id": source_id, "attributes": {"name": "L11 Supplier", "tax_id": "TEST-L11"}} for source, source_id in (("erp", "1"), ("crm", "2"))]
        batch = request(base + "/batches", token, {"input_version": "l11", "records": records}, "POST")
        identities = {tuple(item["source_key"]): item for item in batch["identities"]}
        original_entities = {item["entity_id"] for item in identities.values()}
        assert len(identities) == len(original_entities) == 2
        run_id = request(base + "/runs", token, {"scheme_key": "l11", "input_version": "l11"}, "POST")["run_id"]
        deadline = time.monotonic() + 90
        while True:
            pid = compose("exec", "-T", "redis", "redis-cli", "GET", "l11-staged-pid")
            if pid:
                break
            assert time.monotonic() < deadline, "worker never reached staged-result boundary"
            time.sleep(0.5)
        during = request(base + "/runs/" + run_id, token)
        assert during["status"] == "RUNNING" and during["attempt_count"] == 1 and during["result"] is None
        # The worker has staged its merge and result; another transaction still sees
        # the two original owners and no audit until the worker commits.
        query = f"SELECT source_record_id || ':' || current_entity_id FROM unified_source_records WHERE project_id = '{project}' ORDER BY source_record_id"
        owners = compose("exec", "-T", "postgres", "psql", "-U", "su_admin", "-d", "semantic_unifier", "-At", "-c", query).splitlines()
        assert set(owners) == {key[-1] + ":" + item["entity_id"] for key, item in identities.items()}
        assert request(base + "/identity-changes", token)["items"] == []
        snapshots = request(base + "/snapshots", token)
        assert snapshots["total"] == 0 and snapshots["current_snapshot_id"] is None
        compose("exec", "-T", "worker", "python", "-c", "import os, signal, sys; os.kill(int(sys.argv[1]), signal.SIGKILL)", pid)
        deadline = time.monotonic() + 90
        while True:
            run = request(base + "/runs/" + run_id, token)
            if run["status"] in {"SUCCEEDED", "FAILED"}:
                break
            assert time.monotonic() < deadline, run
            time.sleep(0.5)
        assert run["status"] == "SUCCEEDED" and run["attempt_count"] == 2, run
        snapshot = request(base + "/snapshots", token, {"run_id": run_id, "expected_current_version": 0}, "POST")
        mappings = request(base + "/snapshots/" + snapshot["snapshot_id"] + "/export?kind=mappings", token)
        assert {tuple(item["source_key"]) for item in mappings["mappings"]} == set(identities)
        assert {tuple(item["source_key"]): item["source_revision_id"] for item in mappings["mappings"]} == {key: value["source_revision_id"] for key, value in identities.items()}
        merged_entities = {item["entity_id"] for item in mappings["mappings"]}
        assert len(merged_entities) == 1 and merged_entities <= original_entities
        merged_id = merged_entities.pop()
        entities = request(base + "/snapshots/" + snapshot["snapshot_id"] + "/export?kind=entities", token)["entities"]
        assert len(entities) == 1 and entities[0]["entity_id"] == merged_id
        assert entities[0]["attributes"] == {"name": "L11 Supplier", "tax_id": "TEST-L11"}
        assert {tuple(item["source_key"]): item["source_revision_id"] for item in entities[0]["members"]} == {key: value["source_revision_id"] for key, value in identities.items()}
        assert request(base + "/snapshots", token)["total"] == 1
        repeat = request(base + "/runs", token, {"scheme_key": "l11", "input_version": "l11"}, "POST")
        assert repeat["run_id"] == run_id
        changes = request(base + "/identity-changes", token)["items"]
        assert len(changes) == 1 and changes[0]["change_type"] == "MERGE"
        assert changes[0]["payload"]["target_id"] == merged_id
        assert set(changes[0]["payload"]["source_entity_ids"]) == original_entities - {merged_id}
        assert set(changes[0]["payload"]["record_ids"] + changes[0]["payload"]["target_record_ids"]) == {item["source_record_id"] for item in identities.values()}
        assert run["error_message"] is None
        assert run["last_interruption"] == "previous worker was lost; run reclaimed for retry"
    finally:
        compose("down", "--volumes", "--remove-orphans")
