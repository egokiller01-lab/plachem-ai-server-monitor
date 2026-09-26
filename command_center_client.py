from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any


COMMAND_CENTER_MODE = "command_center"
LEGACY_MODE = "legacy"
_FORBIDDEN_FIELDS = {
    "model", "provider", "endpoint", "api_key", "secret", "credential", "context_size",
}


class CommandCenterAPIError(RuntimeError):
    def __init__(self, status: int, body: Mapping[str, Any] | None = None):
        self.status = int(status)
        self.body = dict(body or {})
        code = self.body.get("error", "INTERNAL_ERROR")
        super().__init__(f"Command Center API error {self.status}: {code}")


class CommandCenterClient:
    """Server-side client for the Phase #19 Command Center transport contract."""

    def __init__(self, base_url: str, integration_secret: str, actor_id: str, *, timeout: float = 10.0, opener: Callable[..., Any] | None = None):
        if not base_url or not integration_secret or not actor_id:
            raise ValueError("base_url, integration_secret and actor_id are required")
        self.base_url = base_url.rstrip("/")
        self._integration_secret = integration_secret
        self.actor_id = actor_id
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def submit(self, payload: Mapping[str, Any], idempotency_key: str, *, mode: str = COMMAND_CENTER_MODE) -> dict[str, Any]:
        self._require_command_center(mode)
        self._validate_payload(payload)
        return self._write("/api/command-center/war-room/tasks", payload, idempotency_key)

    def status(self, project_id: str, war_task_id: str) -> dict[str, Any]:
        return self._read(f"/api/command-center/war-room/projects/{_segment(project_id)}/tasks/{_segment(war_task_id)}")

    def candidates(self, project_id: str, war_task_id: str) -> dict[str, Any]:
        return self._read(f"/api/command-center/war-room/projects/{_segment(project_id)}/tasks/{_segment(war_task_id)}/candidates")

    def dispatch(self, task_id: str, idempotency_key: str, *, mode: str = COMMAND_CENTER_MODE) -> dict[str, Any]:
        self._require_command_center(mode)
        if not task_id:
            raise ValueError("task_id is required")
        return self._write(f"/api/command-center/tasks/{_segment(task_id)}/dispatch", {}, idempotency_key)

    def next_ready(self, project_id: str, war_task_id: str) -> dict[str, Any]:
        return self._read(f"/api/command-center/war-room/projects/{_segment(project_id)}/tasks/{_segment(war_task_id)}/next-ready")

    def summary(self, project_id: str, war_task_id: str) -> dict[str, Any]:
        return self._read(f"/api/command-center/war-room/projects/{_segment(project_id)}/tasks/{_segment(war_task_id)}/summary")

    def _read(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _write(self, path: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Idempotency-Key is required")
        self._validate_payload(payload)
        return self._request("POST", path, payload=payload, idempotency_key=idempotency_key)

    def _request(self, method: str, path: str, *, payload: Mapping[str, Any] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "X-War-Room-Integration-Secret": self._integration_secret,
            "X-War-Room-Actor-ID": self.actor_id,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise CommandCenterAPIError(503, {"error": "INTERNAL_ERROR"}) from exc
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CommandCenterAPIError(502, {"error": "INTERNAL_ERROR"}) from exc
        if not isinstance(body, dict):
            raise CommandCenterAPIError(502, {"error": "INTERNAL_ERROR"})
        if status >= 400:
            raise CommandCenterAPIError(status, body)
        return body

    @staticmethod
    def _require_command_center(mode: str) -> None:
        if mode != COMMAND_CENTER_MODE:
            raise ValueError("Command Center client does not execute legacy mode")

    @staticmethod
    def _validate_payload(payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        if _contains_forbidden(payload):
            raise ValueError("forbidden runtime field")


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(key).lower() in _FORBIDDEN_FIELDS or _contains_forbidden(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("invalid path identity")
    return value
