"""HTTP adapter for the internal OpenRefine sidecar."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, quote, urlparse

import httpx

from app.core.config import settings

_ENGINE = {"facets": [], "mode": "row-based"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class OpenRefineError(Exception):
    """OpenRefine returned a failure the caller can map to an API error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class OpenRefineUnavailable(OpenRefineError):
    """The sidecar cannot be reached."""

    def __init__(self, message: str = "OpenRefine is unavailable"):
        super().__init__("OPENREFINE_UNAVAILABLE", message)


class OpenRefineClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or settings.openrefine_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.openrefine_timeout_seconds

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout or self.timeout, follow_redirects=False)

    def _token(self, client: httpx.Client) -> str:
        try:
            response = client.get("/command/core/get-csrf-token")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OpenRefineUnavailable() from exc
        except httpx.TimeoutException as exc:
            raise OpenRefineUnavailable("OpenRefine did not answer the token request") from exc
        except httpx.HTTPError as exc:
            raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine token request failed") from exc
        if response.status_code != 200:
            raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine token request failed")
        try:
            token = response.json().get("token")
        except ValueError as exc:
            raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine token response is not JSON") from exc
        if not isinstance(token, str) or not token:
            raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine did not return a token")
        return token

    def health(self) -> bool:
        try:
            with self._client(timeout=min(self.timeout, 3)) as client:
                response = client.get("/command/core/get-version")
                if response.status_code != 200:
                    return False
                payload = response.json()
                return isinstance(payload, dict) and bool(payload.get("version"))
        except (httpx.HTTPError, ValueError, OSError):
            return False

    def create_project(self, name: str, file_bytes: bytes, format: str = "csv") -> str:
        safe = _SAFE_NAME.sub("-", name).strip("-") or "openrefine"
        with self._client() as client:
            token = self._token(client)
            try:
                response = client.post(
                    "/command/core/create-project-from-upload",
                    params={"csrf_token": token},
                    data={
                        "project-name": safe,
                        "format": format,
                        "separator": ",",
                        "encoding": "UTF-8",
                        "ignore-lines": "0",
                        "header-line": "1",
                    },
                    files={"project-file": (f"{safe}.{format}", file_bytes, "text/csv")},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise OpenRefineUnavailable() from exc
            except httpx.TimeoutException as exc:
                raise OpenRefineError("OPENREFINE_TIMEOUT", "OpenRefine project creation timed out") from exc
            except httpx.HTTPError as exc:
                raise OpenRefineError("OPENREFINE_CREATE_FAILED", "OpenRefine project creation failed") from exc

            if response.status_code in (301, 302, 303):
                location = response.headers.get("location", "")
                project_id = parse_qs(urlparse(location).query).get("project", [""])[0]
                if project_id:
                    return project_id
                raise OpenRefineError("OPENREFINE_CREATE_FAILED", "OpenRefine redirect had no project id")

            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRefineError("OPENREFINE_CREATE_FAILED", "OpenRefine did not return a project") from exc
            if response.status_code != 200:
                raise OpenRefineError("OPENREFINE_CREATE_FAILED", str(payload.get("message") or "OpenRefine project creation failed"))
            project_id = payload.get("project") or payload.get("projectID")
            if project_id:
                return str(project_id)
            job_id = payload.get("jobID")
            if payload.get("importing") and job_id:
                return self._wait_for_import(client, str(job_id))
            raise OpenRefineError("OPENREFINE_CREATE_FAILED", str(payload.get("message") or "OpenRefine did not return a project id"))

    def _wait_for_import(self, client: httpx.Client, job_id: str) -> str:
        for _ in range(30):
            time.sleep(0.5)
            try:
                response = client.get("/command/core/get-importing-job-status", params={"jobID": job_id})
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            project_id = payload.get("projectID") or payload.get("project")
            if project_id:
                return str(project_id)
            if payload.get("status") == "error":
                raise OpenRefineError("OPENREFINE_CREATE_FAILED", str(payload.get("message") or "OpenRefine import failed"))
        raise OpenRefineError("OPENREFINE_TIMEOUT", "OpenRefine import did not finish")

    def apply_recipe(self, project_id: str, operations: list[dict]) -> None:
        with self._client() as client:
            token = self._token(client)
            try:
                response = client.post(
                    "/command/core/apply-operations",
                    params={"project": project_id, "csrf_token": token},
                    data={"operations": json.dumps(operations, ensure_ascii=False, separators=(",", ":"))},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise OpenRefineUnavailable() from exc
            except httpx.TimeoutException as exc:
                raise OpenRefineError("OPENREFINE_TIMEOUT", "OpenRefine recipe did not finish in time") from exc
            except httpx.HTTPError as exc:
                raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine recipe request failed") from exc
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine returned a non-JSON recipe response")
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine recipe response is not JSON") from exc
            if payload.get("code") != "ok":
                raise OpenRefineError("OPENREFINE_RECIPE_FAILED", str(payload.get("message") or "OpenRefine recipe failed"))

    def export_csv(self, project_id: str, project_name: str) -> bytes:
        with self._client() as client:
            token = self._token(client)
            safe = quote(_SAFE_NAME.sub("-", project_name).strip("-") or "openrefine")
            try:
                response = client.post(
                    f"/command/core/export-rows/{safe}.csv",
                    params={"csrf_token": token},
                    data={
                        "project": project_id,
                        "format": "csv",
                        "quoteAll": "",
                        "engine": json.dumps(_ENGINE, separators=(",", ":")),
                    },
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise OpenRefineUnavailable() from exc
            except httpx.TimeoutException as exc:
                raise OpenRefineError("OPENREFINE_TIMEOUT", "OpenRefine export did not finish in time") from exc
            except httpx.HTTPError as exc:
                raise OpenRefineError("OPENREFINE_EXPORT_FAILED", "OpenRefine export failed") from exc
            content_type = response.headers.get("content-type", "")
            if "html" in content_type or response.content.lstrip().startswith(b"<"):
                raise OpenRefineError("OPENREFINE_BAD_RESPONSE", "OpenRefine returned an HTML error page")
            if response.status_code != 200:
                raise OpenRefineError("OPENREFINE_EXPORT_FAILED", "OpenRefine export failed")
            if not response.content:
                raise OpenRefineError("OPENREFINE_EXPORT_FAILED", "OpenRefine returned an empty export")
            return response.content

    def delete_project(self, project_id: str) -> None:
        with self._client() as client:
            token = self._token(client)
            try:
                response = client.post(
                    "/command/core/delete-project",
                    params={"csrf_token": token},
                    data={"project": project_id},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise OpenRefineUnavailable() from exc
            except httpx.TimeoutException as exc:
                raise OpenRefineError("OPENREFINE_TIMEOUT", "OpenRefine delete timed out") from exc
            except httpx.HTTPError as exc:
                raise OpenRefineError("OPENREFINE_DELETE_FAILED", "OpenRefine delete failed") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRefineError("OPENREFINE_DELETE_FAILED", "OpenRefine delete response is not JSON") from exc
            if payload.get("code") != "ok":
                raise OpenRefineError("OPENREFINE_DELETE_FAILED", str(payload.get("message") or "OpenRefine delete failed"))
