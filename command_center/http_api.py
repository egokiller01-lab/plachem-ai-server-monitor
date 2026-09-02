from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable


FEATURE_ENV = "PLACHEM_COMMAND_CENTER_WAR_ROOM_API"
_FORBIDDEN_FIELDS = {
    "model", "provider", "endpoint", "api_key", "secret", "credential",
    "context_size", "project_root", "workspace_root", "workspace_path", "path",
}
_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "UNKNOWN_AGENT": 422,
    "UNKNOWN_WORKSPACE": 422,
    "DUPLICATE_EXTERNAL_REFERENCE": 409,
    "REVISION_REQUIRED": 409,
    "TASK_NOT_FOUND": 404,
    "TASK_NOT_READY": 409,
    "TASK_ALREADY_ACTIVE": 409,
    "TASK_ALREADY_TERMINAL": 409,
    "NO_AVAILABLE_WORKER": 409,
    "AUTHORIZATION_REQUIRED": 403,
    "DISPATCH_REJECTED": 409,
    "INTERNAL_ERROR": 500,
}

_current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("command_center_actor", default=None)


def current_actor() -> str | None:
    """Return the authenticated actor for the current transport call."""
    return _current_actor.get()


class FileIdempotencyStore:
    """Small repository-local idempotency port backed by an atomic JSON file."""

    _lock = threading.RLock()

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def lookup(self, identity: str) -> dict[str, Any] | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("idempotency storage is unreadable") from exc
            record = data.get(identity)
            return dict(record) if isinstance(record, dict) else None

    def reserve(self, identity: str, request_hash: str, status: int, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {}
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise ValueError
                    data = loaded
                except (OSError, ValueError) as exc:
                    raise RuntimeError("idempotency storage is unreadable") from exc
            existing = data.get(identity)
            if isinstance(existing, dict):
                return existing
            data[identity] = {"request_hash": request_hash, "status": status, "body": body}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".idempotency-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return data[identity]


class CommandCenterWarRoomAPI:
    _write_lock = threading.RLock()

    def __init__(self, orchestrator: Any, *, idempotency_path: str | Path, integration_secret: str | None = None, enabled: bool | None = None):
        self.orchestrator = orchestrator
        self.idempotency = FileIdempotencyStore(idempotency_path)
        self.integration_secret = integration_secret if integration_secret is not None else os.environ.get("PLACHEM_WAR_ROOM_INTEGRATION_SECRET")
        self.enabled = enabled if enabled is not None else os.environ.get(FEATURE_ENV) == "1"

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]):
        status, body = self.handle(environ)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        start_response(f"{status} {_reason(status)}", [("Content-Type", "application/json"), ("Content-Length", str(len(raw)))])
        return [raw]

    def handle(self, environ: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "")
        if not self.enabled and method == "POST" and (path.startswith("/api/command-center/war-room/") or path.startswith("/api/command-center/tasks/")):
            return 404, {"error": "INTEGRATION_DISABLED"}
        if not self._authenticated(environ):
            return 401, {"error": "AUTHENTICATION_REQUIRED"}
        token = _current_actor.set(environ.get("HTTP_X_WAR_ROOM_ACTOR_ID"))
        try:
            return self._route(method, path, environ)
        finally:
            _current_actor.reset(token)

    def _authenticated(self, environ: dict[str, Any]) -> bool:
        supplied = environ.get("HTTP_X_WAR_ROOM_INTEGRATION_SECRET")
        actor = environ.get("HTTP_X_WAR_ROOM_ACTOR_ID")
        return bool(self.integration_secret and supplied and hmac.compare_digest(supplied, self.integration_secret) and actor)

    def _route(self, method: str, path: str, environ: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        project_task = re.fullmatch(r"/api/command-center/war-room/projects/([^/]+)/tasks/([^/]+)", path)
        project_suffix = re.fullmatch(r"/api/command-center/war-room/projects/([^/]+)/tasks/([^/]+)/(candidates|next-ready|summary)", path)
        dispatch = re.fullmatch(r"/api/command-center/tasks/([^/]+)/dispatch", path)
        if method == "POST" and path == "/api/command-center/war-room/tasks":
            return self._write("submit", environ, lambda payload: self.orchestrator.submit(payload))
        if method == "POST" and dispatch:
            task_id = dispatch.group(1)
            return self._write("dispatch:" + task_id, environ, lambda _payload: self.orchestrator.dispatch(task_id), dispatch_task=task_id)
        if method == "GET" and project_task:
            return self._read(lambda: self.orchestrator.status(project_task.group(1), project_task.group(2)))
        if method == "GET" and project_suffix:
            project, task, operation = project_suffix.groups()
            calls = {"candidates": self.orchestrator.candidates, "next-ready": self.orchestrator.next_ready, "summary": self.orchestrator.summary}
            return self._read(lambda: calls[operation](project, task))
        return 404, {"error": "TASK_NOT_FOUND"}

    def _write(self, scope: str, environ: dict[str, Any], operation: Callable[[dict[str, Any]], Any], dispatch_task: str | None = None) -> tuple[int, dict[str, Any]]:
        # Serialize the lookup/execute/reserve sequence in this process so a
        # repeated key cannot execute twice. The file is the durable port;
        # deployment-wide locking belongs to the approved integration store.
        with self._write_lock:
            return self._write_locked(scope, environ, operation, dispatch_task)

    def _write_locked(self, scope: str, environ: dict[str, Any], operation: Callable[[dict[str, Any]], Any], dispatch_task: str | None = None) -> tuple[int, dict[str, Any]]:
        key = environ.get("HTTP_IDEMPOTENCY_KEY")
        if not key:
            return 400, {"error": "INVALID_REQUEST", "message": "Idempotency-Key is required"}
        try:
            payload = _json_body(environ)
            if dispatch_task is not None:
                if payload is None:
                    payload = {}
            elif not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            if _contains_forbidden(payload):
                return 400, {"error": "INVALID_REQUEST"}
            principal = environ["HTTP_X_WAR_ROOM_ACTOR_ID"]
            request_hash = _hash({"scope": scope, "payload": payload})
            identity = _hash({"principal": principal, "scope": scope, "key": key})
            old = self.idempotency.lookup(identity)
            if old:
                if old.get("request_hash") != request_hash:
                    return 409, {"error": "IDEMPOTENCY_CONFLICT"}
                return int(old["status"]), _redact(old["body"])
            result = operation(payload)
            body = _jsonable(result)
            stored = self.idempotency.reserve(identity, request_hash, 200, body)
            if stored.get("request_hash") != request_hash:
                return 409, {"error": "IDEMPOTENCY_CONFLICT"}
            return 200, _redact(body)
        except json.JSONDecodeError:
            return 400, {"error": "INVALID_REQUEST"}
        except ValueError as exc:
            return _mapped_error(str(exc))
        except (TypeError, KeyError):
            return 400, {"error": "INVALID_REQUEST"}
        except Exception:
            return 500, {"error": "INTERNAL_ERROR"}

    def _read(self, operation: Callable[[], Any]) -> tuple[int, dict[str, Any]]:
        try:
            return 200, _redact(_jsonable(operation()))
        except ValueError as exc:
            return _mapped_error(str(exc))
        except Exception:
            return 500, {"error": "INTERNAL_ERROR"}


def create_app(orchestrator: Any, *, idempotency_path: str | Path, integration_secret: str | None = None, enabled: bool | None = None) -> CommandCenterWarRoomAPI:
    return CommandCenterWarRoomAPI(orchestrator, idempotency_path=idempotency_path, integration_secret=integration_secret, enabled=enabled)


def _json_body(environ: dict[str, Any]) -> Any:
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ.get("wsgi.input").read(length) if length else b""
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _FORBIDDEN_FIELDS or _contains_forbidden(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _jsonable(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"result": value}
    return json.loads(json.dumps(value, ensure_ascii=False))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if str(key).lower() not in {"api_key", "token", "secret", "credential", "provider_credential", "authorization", "environment", "env"} and "path" not in str(key).lower()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _mapped_error(message: str) -> tuple[int, dict[str, Any]]:
    code = message.split(":", 1)[0]
    aliases = {"COMPILATION_REVISION_REQUIRED": "REVISION_REQUIRED", "DUPLICATE_EXTERNAL_REFERENCE": "DUPLICATE_EXTERNAL_REFERENCE", "UNKNOWN_TASK": "TASK_NOT_FOUND", "UNKNOWN_WORKFLOW": "TASK_NOT_FOUND", "NO_AVAILABLE_WORKER": "NO_AVAILABLE_WORKER", "AUTH_REQUIRED_ACTION": "AUTHORIZATION_REQUIRED", "AUTH_FAILED": "AUTHORIZATION_REQUIRED"}
    code = aliases.get(code, code if code in _ERROR_STATUS else "DISPATCH_REJECTED")
    return _ERROR_STATUS.get(code, 409), {"error": code}


def _reason(status: int) -> str:
    return {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict", 422: "Unprocessable Entity", 500: "Internal Server Error"}.get(status, "Error")
