"""OpenRefine HTTP adapter against a controlled fake server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import time
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.openrefine.client import (
    OpenRefineClient,
    OpenRefineError,
    OpenRefineUnavailable,
)


class FakeOpenRefine:
    def __init__(self):
        self.behavior = "ok"
        self.created: list[dict] = []
        self.applied: list[dict] = []
        self.deleted: list[str] = []
        self.timeout_seconds = 0.0
        self.export_bytes = b"id,name\n1,Acme\n"
        self.token = "test-token"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: bytes = b"", content_type: str = "application/json", headers: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, payload: dict, status: int = 200):
                self._send(status, json.dumps(payload).encode())

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/command/core/get-version":
                    self._json({"version": "3.10.1"})
                elif path == "/command/core/get-csrf-token":
                    self._json({"token": fake.token})
                else:
                    self._send(404, b"missing", "text/plain")

            def do_POST(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if fake.timeout_seconds:
                    time.sleep(fake.timeout_seconds)
                if parsed.path == "/command/core/create-project-from-upload":
                    if query.get("csrf_token") != [fake.token]:
                        self._json({"code": "error", "message": "Missing or invalid csrf_token parameter"})
                        return
                    fake.created.append({"query": query, "body": body, "content_type": self.headers.get("Content-Type", "")})
                    if fake.behavior == "html_error":
                        self._send(200, b"<html>error</html>", "text/html")
                    elif fake.behavior == "create_error":
                        self._json({"code": "error", "message": "cannot create"})
                    else:
                        self._send(302, b"", headers={"Location": f"http://{self.headers.get('Host')}/project?project=42"})
                elif parsed.path == "/command/core/apply-operations":
                    if query.get("csrf_token") != [fake.token]:
                        self._json({"code": "error", "message": "Missing or invalid csrf_token parameter"})
                        return
                    form = parse_qs(body.decode("utf-8", "replace"))
                    fake.applied.append({"project": query.get("project", [""])[0], "operations": form.get("operations", [""])[0]})
                    if fake.behavior == "html_error":
                        self._send(200, b"<html>error</html>", "text/html")
                    elif fake.behavior == "recipe_error":
                        self._json({"code": "error", "message": "Operation #1: unknown"})
                    else:
                        self._json({"code": "ok"})
                elif parsed.path.startswith("/command/core/export-rows/"):
                    if query.get("csrf_token") != [fake.token]:
                        self._json({"code": "error", "message": "Missing or invalid csrf_token parameter"})
                        return
                    if fake.behavior == "html_error":
                        self._send(200, b"<html>error</html>", "text/html")
                    elif fake.behavior == "empty_export":
                        self._send(200, b"", "text/csv")
                    else:
                        self._send(200, fake.export_bytes, "text/csv")
                elif parsed.path == "/command/core/delete-project":
                    form = parse_qs(body.decode("utf-8", "replace"))
                    fake.deleted.append(form.get("project", [""])[0])
                    self._json({"code": "ok"})
                else:
                    self._send(404, b"missing", "text/plain")

            def log_message(self, *_args):
                pass

        return Handler


@pytest.fixture
def fake_openrefine():
    fake = FakeOpenRefine().start()
    try:
        yield fake
    finally:
        fake.stop()


def test_create_apply_export_delete_round_trip(fake_openrefine):
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=2)
    assert client.health() is True
    project_id = client.create_project("probe", b"id,name\n1,  Acme  \n")
    assert project_id == "42"
    assert fake_openrefine.created[0]["query"]["csrf_token"] == ["test-token"]
    assert b"probe.csv" in fake_openrefine.created[0]["body"]
    client.apply_recipe(project_id, [{"op": "core/text-transform", "columnName": "name"}])
    assert json.loads(fake_openrefine.applied[0]["operations"])[0]["columnName"] == "name"
    assert client.export_csv(project_id, "probe") == fake_openrefine.export_bytes
    client.delete_project(project_id)
    assert fake_openrefine.deleted == ["42"]


def test_health_is_false_when_the_server_is_unreachable():
    client = OpenRefineClient(base_url="http://127.0.0.1:1", timeout=0.2)
    assert client.health() is False
    with pytest.raises(OpenRefineUnavailable) as exc:
        client.create_project("probe", b"id\n1\n")
    assert exc.value.code == "OPENREFINE_UNAVAILABLE"


def test_html_response_is_a_bad_response(fake_openrefine):
    fake_openrefine.behavior = "html_error"
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=2)
    with pytest.raises(OpenRefineError) as exc:
        client.apply_recipe("42", [])
    assert exc.value.code == "OPENREFINE_BAD_RESPONSE"
    with pytest.raises(OpenRefineError) as exc:
        client.export_csv("42", "probe")
    assert exc.value.code == "OPENREFINE_BAD_RESPONSE"


def test_recipe_error_carries_the_openrefine_message(fake_openrefine):
    fake_openrefine.behavior = "recipe_error"
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=2)
    with pytest.raises(OpenRefineError) as exc:
        client.apply_recipe("42", [])
    assert exc.value.code == "OPENREFINE_RECIPE_FAILED"
    assert "unknown" in exc.value.message


def test_create_failure_and_empty_export_have_stable_codes(fake_openrefine):
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=2)
    fake_openrefine.behavior = "create_error"
    with pytest.raises(OpenRefineError) as exc:
        client.create_project("probe", b"id\n1\n")
    assert exc.value.code == "OPENREFINE_CREATE_FAILED"
    fake_openrefine.behavior = "empty_export"
    with pytest.raises(OpenRefineError) as exc:
        client.export_csv("42", "probe")
    assert exc.value.code == "OPENREFINE_EXPORT_FAILED"


def test_timeout_maps_to_a_timeout_error(fake_openrefine):
    fake_openrefine.timeout_seconds = 1.0
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=0.2)
    with pytest.raises(OpenRefineError) as exc:
        client.apply_recipe("42", [])
    assert exc.value.code == "OPENREFINE_TIMEOUT"


def test_health_never_raises(fake_openrefine):
    fake_openrefine.behavior = "html_error"
    client = OpenRefineClient(base_url=fake_openrefine.url, timeout=1)
    assert isinstance(client.health(), bool)
