"""Opt-in Compose proof: real OpenRefine preview, adoption, and cancellation."""
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(
    os.getenv("RUN_COMPOSE_RELEASE_TESTS") != "true",
    reason="requires Docker image builds and isolated services",
)
def test_real_openrefine_preview_adopt_and_cancel(tmp_path):
    ports = {key: _free_port() for key in ("WEB_PORT", "API_PORT", "MINIO_PORT", "MINIO_CONSOLE_PORT")}
    secrets_map = {
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "APP_ENCRYPTION_KEY": __import__("base64").urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "INIT_ADMIN_EMAIL": "openrefine-release@example.invalid",
        "INIT_ADMIN_PASSWORD": secrets.token_hex(24),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "MINIO_SECRET_KEY": secrets.token_hex(24),
    }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in {**secrets_map, **ports, "APP_ENV_FILE": str(env_file)}.items()) + "\n"
    )
    project_name = f"osu-openrefine-{secrets.token_hex(5)}"
    compose = [
        "docker", "compose", "--project-name", project_name,
        "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml"),
    ]
    api = f"http://127.0.0.1:{ports['API_PORT']}"

    def run_compose(*args, timeout=1200):
        return subprocess.run(compose + list(args), cwd=ROOT, text=True, capture_output=True, timeout=timeout)

    def openrefine_projects() -> dict:
        result = run_compose("exec", "-T", "openrefine", "curl", "-fsS",
                             "http://127.0.0.1:3333/command/core/get-all-project-metadata")
        assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        return json.loads(result.stdout).get("projects", {})

    try:
        result = run_compose("up", "-d", "--build", "postgres", "redis", "minio", "openrefine", "backend", "worker")
        assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]

        client = httpx.Client(base_url=api, timeout=30)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise AssertionError("Compose API did not become healthy in five minutes")

        login = client.post("/api/v1/auth/login", json={
            "email": secrets_map["INIT_ADMIN_EMAIL"],
            "password": secrets_map["INIT_ADMIN_PASSWORD"],
        })
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        projects = client.get("/api/v1/projects").json()
        if projects:
            project_id = projects[0]["id"]
        else:
            created = client.post("/api/v1/projects", json={
                "name": "OpenRefine release project", "slug": f"openrefine-{secrets.token_hex(4)}", "description": "",
            })
            assert created.status_code in (200, 201), created.text
            project_id = created.json()["id"]

        dirty = 'id,company_name,city\n1,"  Acme, Ltd ",Shenzhen\n2,Beta,Shanghai\n'.encode()
        upload = client.post(f"/api/v1/projects/{project_id}/datasources/upload",
                             files={"files": ("dirty.csv", dirty, "text/csv")})
        assert upload.status_code == 200, upload.text
        source_id = upload.json()["datasource_ids"][0]
        before_ids = {item["id"] for item in client.get(f"/api/v1/projects/{project_id}/datasources").json()}

        preview = client.post(f"/api/v1/projects/{project_id}/openrefine/previews",
                              json={"data_source_id": source_id})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["engine"] == "openrefine"
        assert body["diff"]["changed_rows"] >= 1, body
        assert body["artifact_id"]
        first_preview = body["preview_id"]

        adopted = client.post(f"/api/v1/projects/{project_id}/openrefine/previews/{first_preview}/adopt")
        assert adopted.status_code == 200, adopted.text
        after = client.get(f"/api/v1/projects/{project_id}/datasources").json()
        after_ids = {item["id"] for item in after}
        assert adopted.json()["data_source_id"] in after_ids
        assert before_ids <= after_ids
        assert len(after_ids) == len(before_ids) + 1

        second = client.post(f"/api/v1/projects/{project_id}/openrefine/previews",
                             json={"data_source_id": source_id})
        assert second.status_code == 200, second.text
        second_preview = second.json()["preview_id"]
        assert second_preview != first_preview
        cancelled = client.delete(f"/api/v1/projects/{project_id}/openrefine/previews/{second_preview}")
        assert cancelled.status_code == 200, cancelled.text
        job = client.get(f"/api/v1/jobs/{second_preview}")
        assert job.status_code == 200, job.text
        assert job.json()["status"] == "CANCELLED"

        for _ in range(20):
            if not any(name.startswith("osu-") for name in openrefine_projects().values()):
                break
            time.sleep(0.5)
        metadata = openrefine_projects()
        names = [value.get("name") if isinstance(value, dict) else value for value in metadata.values()]
        assert not any(isinstance(name, str) and name.startswith("osu-") for name in names), names
    finally:
        run_compose("down", "--volumes", "--remove-orphans", timeout=300)
