"""Release checks for the Docker Compose deployment contract.

The live lifecycle test is opt-in because it builds images and starts isolated
database, queue, object store, API, worker and web services.
"""

import os
import json
import secrets
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


def test_compose_uses_real_env_and_requires_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JWT_SECRET_KEY=test-jwt-key-with-enough-entropy\n"
        "INIT_ADMIN_PASSWORD=test-admin-password-long-enough\n"
        "POSTGRES_PASSWORD=test-postgres-password-long-enough\n"
        "MINIO_SECRET_KEY=test-minio-password-long-enough\n",
        encoding="utf-8",
    )
    env_file.write_text(env_file.read_text(encoding="utf-8") + f"APP_ENV_FILE={env_file}\n", encoding="utf-8")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", "docker-compose.yml", "config", "--no-env-resolution", "--format", "json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    import json

    config = json.loads(result.stdout)
    backend = config["services"]["backend"]
    assert backend["environment"]["INIT_ADMIN_PASSWORD"] == "test-admin-password-long-enough"
    assert "${" not in backend["environment"]["DATABASE_URL"]
    assert backend["env_file"] == [{"path": str(env_file)}]
    assert all("@sha256:" in service.get("image", "") for service in config["services"].values() if "image" in service)


def test_production_settings_reject_template_credentials():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="INIT_ADMIN_PASSWORD"):
        Settings(
            app_env="production",
            jwt_secret_key="generate-with-scripts-init-env",
            init_admin_password="generate-with-scripts-init-env",
            s3_secret_key="generate-with-scripts-init-env",
        ).validate_for_deployment()


def test_generated_environment_configures_compose(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/init_env.py", tmp_path / "scripts/init_env.py")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    subprocess.run([sys.executable, str(tmp_path / "scripts/init_env.py")], check=True, capture_output=True)
    values = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert len(values["MINIO_SECRET_KEY"]) >= 24
    assert not values["INIT_ADMIN_PASSWORD"].startswith("generate-with-")
    result = subprocess.run(["docker", "compose", "--env-file", str(tmp_path / ".env"), "-f", str(ROOT / "docker-compose.yml"), "config", "--no-env-resolution", "--quiet"], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.skipif(os.getenv("RUN_COMPOSE_RELEASE_TESTS") != "true", reason="requires Docker and isolated PostgreSQL")
def test_postgres_upgrade_preserves_existing_application_data(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join([
        f"APP_ENV_FILE={env_file}", f"JWT_SECRET_KEY={secrets.token_hex(32)}",
        f"INIT_ADMIN_PASSWORD={secrets.token_hex(24)}", f"POSTGRES_PASSWORD={secrets.token_hex(24)}",
        f"MINIO_SECRET_KEY={secrets.token_hex(24)}",
    ]) + "\n")
    project = f"osu-migration-{secrets.token_hex(5)}"
    command = ["docker", "compose", "--project-name", project, "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml")]

    def compose(*args):
        result = subprocess.run(command + list(args), cwd=ROOT, capture_output=True, text=True, timeout=900)
        assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
        return result.stdout

    try:
        compose("up", "-d", "--wait", "postgres")
        compose("build", "backend")
        compose("run", "--rm", "--no-deps", "backend", "alembic", "upgrade", "3a5d3f7c8e11")
        sql = "INSERT INTO workspaces (id,name,slug,created_at) VALUES ('migration-w','Existing','migration-w',now()); INSERT INTO projects (id,workspace_id,name,slug,description,created_at) VALUES ('migration-p','migration-w','Preserved project','migration-p','',now());"
        compose("exec", "-T", "postgres", "psql", "-U", "su_admin", "-d", "semantic_unifier", "-v", "ON_ERROR_STOP=1", "-c", sql)
        compose("run", "--rm", "--no-deps", "backend", "alembic", "upgrade", "head")
        output = compose("exec", "-T", "postgres", "psql", "-U", "su_admin", "-d", "semantic_unifier", "-At", "-c", "SELECT name FROM projects WHERE id='migration-p'")
        assert output.strip() == "Preserved project"
        output = compose("exec", "-T", "postgres", "psql", "-U", "su_admin", "-d", "semantic_unifier", "-At", "-c", "SELECT to_regclass('public.unification_snapshots') IS NOT NULL")
        assert output.strip() == "t"
    finally:
        compose("down", "--volumes", "--remove-orphans")


@pytest.mark.skipif(os.getenv("RUN_COMPOSE_RELEASE_TESTS") != "true", reason="requires Docker image builds and isolated services")
def test_clean_compose_migrate_restart_async_publish_and_download(tmp_path):
    """Exercise real PostgreSQL, Redis/Celery, MinIO, API and frontend images."""
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    ports = {key: free_port() for key in ("WEB_PORT", "API_PORT", "MINIO_PORT", "MINIO_CONSOLE_PORT")}
    secrets_map = {
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "APP_ENCRYPTION_KEY": __import__("base64").urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "INIT_ADMIN_EMAIL": "release-test@example.invalid",
        "INIT_ADMIN_PASSWORD": secrets.token_hex(24),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "MINIO_SECRET_KEY": secrets.token_hex(24),
    }
    compose_env = tmp_path / ".env"
    compose_env.write_text("\n".join(f"{k}={v}" for k, v in {**secrets_map, **ports, "APP_ENV_FILE": str(compose_env)}.items()) + "\n")
    compose_project = f"osu-release-{secrets.token_hex(5)}"
    compose = ["docker", "compose", "--project-name", compose_project, "--env-file", str(compose_env), "-f", str(ROOT / "docker-compose.yml")]
    api_base = f"http://127.0.0.1:{ports['API_PORT']}"

    def run_compose(*args, timeout=900):
        return subprocess.run(compose + list(args), cwd=ROOT, text=True, capture_output=True, timeout=timeout)

    def request(path, *, token=None, payload=None, method=None, timeout=20):
        headers = {}
        body = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        if path.endswith("/unification/runs") and method == "POST":
            headers["Idempotency-Key"] = "compose-release-run-v1"
        req = Request(api_base + path, data=body, headers=headers, method=method)
        with urlopen(req, timeout=timeout) as response:
            return response.status, response.headers, response.read()

    try:
        result = run_compose("up", "-d", "--build")
        assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]
        deadline = monotonic() + 240
        while monotonic() < deadline:
            try:
                request("/health", timeout=2)
                break
            except (OSError, URLError):
                sleep(2)
        else:
            raise AssertionError("Compose API did not become healthy within four minutes")
        with urlopen(f"http://127.0.0.1:{ports['WEB_PORT']}/", timeout=10) as frontend:
            assert frontend.status == 200 and b'<div id="root">' in frontend.read()
        try:
            urlopen(f"http://127.0.0.1:{ports['WEB_PORT']}/api/v1/auth/me", timeout=10)
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Frontend proxy did not enforce API authentication")
        status, _, body = request("/api/v1/auth/login", payload={"email": secrets_map["INIT_ADMIN_EMAIL"], "password": secrets_map["INIT_ADMIN_PASSWORD"]}, method="POST")
        assert status == 200
        token = json.loads(body)["access_token"]
        status, _, body = request("/api/v1/projects", token=token)
        project_id = json.loads(body)[0]["id"]

        scheme = json.loads((ROOT / "tests/golden/v1/supplier/scheme.json").read_text())
        request(f"/api/v1/projects/{project_id}/unification/schemes/release-supplier", token=token, payload=scheme, method="PUT")
        records = json.loads((ROOT / "tests/golden/v1/supplier/records.json").read_text())[:4]
        batch = {
            "input_version": "compose-release-v1",
            "records": [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in records],
        }
        request(f"/api/v1/projects/{project_id}/unification/batches", token=token, payload=batch, method="POST")
        barrier = Barrier(2)
        def submit_same_run():
            barrier.wait(timeout=10)
            return request(f"/api/v1/projects/{project_id}/unification/runs", token=token,
                           payload={"scheme_key": "release-supplier", "input_version": "compose-release-v1"}, method="POST")
        with ThreadPoolExecutor(max_workers=2) as executor:
            submissions = list(executor.map(lambda _: submit_same_run(), range(2)))
        assert all(status == 202 for status, _, _ in submissions)
        run_ids = {json.loads(body)["run_id"] for _, _, body in submissions}
        assert len(run_ids) == 1
        run_id = run_ids.pop()
        try:
            request(f"/api/v1/projects/{project_id}/unification/runs", token=token,
                    payload={"scheme_key": "release-supplier", "input_version": "different-input"}, method="POST")
        except HTTPError as exc:
            assert exc.code == 409
        else:
            raise AssertionError("reusing an idempotency key with changed input must conflict")
        _, _, other_body = request("/api/v1/projects", token=token, payload={"name": "Other release project", "slug": "other-release-project", "description": ""}, method="POST")
        other_project = json.loads(other_body)["id"]
        product_scheme = json.loads((ROOT / "tests/golden/v1/product/scheme.json").read_text())
        product_records = json.loads((ROOT / "tests/golden/v1/product/records.json").read_text())
        product_batch = {"input_version": "compose-release-v1", "records": [{k: row[k] for k in ("source_system", "dataset", "source_record_id", "attributes")} for row in product_records]}
        request(f"/api/v1/projects/{other_project}/unification/schemes/release-product", token=token, payload=product_scheme, method="PUT")
        request(f"/api/v1/projects/{other_project}/unification/batches", token=token, payload=product_batch, method="POST")
        product_barrier = Barrier(2)
        def repeat_product_run():
            product_barrier.wait(timeout=10)
            return request(f"/api/v1/projects/{other_project}/unification/runs", token=token,
                           payload={"scheme_key": "release-product", "input_version": "compose-release-v1"}, method="POST")
        with ThreadPoolExecutor(max_workers=2) as executor:
            product_submissions = list(executor.map(lambda _: repeat_product_run(), range(2)))
        product_ids = {json.loads(body)["run_id"] for _, _, body in product_submissions}
        assert len(product_ids) == 1
        other_run = product_ids.pop()
        assert other_run != run_id
        try:
            request(f"/api/v1/projects/{other_project}/unification/runs", token=token,
                    payload={"scheme_key": "release-product", "input_version": "different-input"}, method="POST")
        except HTTPError as exc:
            assert exc.code == 409
        else:
            raise AssertionError("product idempotency key accepted different input")
        deadline = monotonic() + 120
        while monotonic() < deadline:
            _, _, product_body = request(f"/api/v1/projects/{other_project}/unification/runs/{other_run}", token=token)
            product_run = json.loads(product_body)
            if product_run["status"] in {"SUCCEEDED", "FAILED"}:
                break
            sleep(1)
        assert product_run["status"] == "SUCCEEDED", product_run
        request(f"/api/v1/projects/{other_project}/unification/snapshots", token=token,
                payload={"run_id": other_run, "expected_current_version": 0}, method="POST")
        _, _, product_snapshots = request(f"/api/v1/projects/{other_project}/unification/snapshots", token=token)
        assert json.loads(product_snapshots)["total"] == 1
        deadline = monotonic() + 120
        while monotonic() < deadline:
            _, _, run_body = request(f"/api/v1/projects/{project_id}/unification/runs/{run_id}", token=token)
            run = json.loads(run_body)
            if run["status"] in {"SUCCEEDED", "FAILED"}:
                break
            sleep(1)
        assert run["status"] == "SUCCEEDED", run

        _, _, review_body = request(f"/api/v1/projects/{project_id}/unification/reviews?run_id={run_id}", token=token)
        reviews = json.loads(review_body)["items"]
        assert reviews, "Expected a human-review candidate in the supplier fixture"
        review = reviews[0]
        _, _, previous_changes = request(f"/api/v1/projects/{project_id}/unification/identity-changes", token=token)
        revision = max((item["revision"] for item in json.loads(previous_changes)["items"]), default=1)
        _, _, decision_body = request(f"/api/v1/projects/{project_id}/unification/reviews", token=token, method="POST", payload={
            "run_id": run_id,
            "left_source_revision_id": review["left_source_revision_id"],
            "right_source_revision_id": review["right_source_revision_id"],
            "action": "KEEP_DISTINCT", "reason": "Compose persistence review", "expected_revision": revision,
        })
        change_id = json.loads(decision_body)["change_id"]

        import boto3
        s3 = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{ports['MINIO_PORT']}",
                          aws_access_key_id="semantic-unifier", aws_secret_access_key=secrets_map["MINIO_SECRET_KEY"])
        s3.create_bucket(Bucket="release-persistence")
        s3.put_object(Bucket="release-persistence", Key="sample.csv", Body=b"id,name\n1,example\n")

        status, _, body = request(
            f"/api/v1/projects/{project_id}/unification/snapshots",
            token=token,
            payload={"run_id": run_id, "expected_current_version": 0},
            method="POST",
        )
        assert status == 201
        snapshot_id = json.loads(body)["snapshot_id"]
        exports = {}
        for kind in ("entities", "mappings", "decisions", "delta"):
            _, headers, data = request(f"/api/v1/projects/{project_id}/unification/snapshots/{snapshot_id}/export?kind={kind}", token=token)
            assert "attachment" in headers.get("Content-Disposition", "")
            exports[kind] = json.loads(data)
            assert exports[kind]["snapshot_id"] == snapshot_id

        result = run_compose("restart", "postgres", "minio", "redis", "backend", "worker")
        assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        deadline = monotonic() + 120
        while monotonic() < deadline:
            try:
                _, _, persisted = request(f"/api/v1/projects/{project_id}/unification/snapshots", token=token, timeout=3)
                break
            except (OSError, URLError):
                sleep(2)
        else:
            raise AssertionError("API did not recover after service restart")
        snapshots = json.loads(persisted)
        assert any(item["snapshot_id"] == snapshot_id for item in snapshots["items"])
        _, _, persisted_export = request(f"/api/v1/projects/{project_id}/unification/snapshots/{snapshot_id}/export?kind=entities", token=token)
        assert json.loads(persisted_export) == exports["entities"]
        _, _, changes = request(f"/api/v1/projects/{project_id}/unification/identity-changes", token=token)
        assert any(item["change_id"] == change_id and item["reason"] == "Compose persistence review" for item in json.loads(changes)["items"])
        assert s3.get_object(Bucket="release-persistence", Key="sample.csv")["Body"].read() == b"id,name\n1,example\n"
        assert run_compose("exec", "-T", "backend", "alembic", "current").returncode == 0
    finally:
        result = run_compose("down", "--volumes", "--remove-orphans", timeout=180)
        if result.returncode:
            raise AssertionError((result.stdout + result.stderr)[-3000:])
