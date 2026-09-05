"""Production OpenClaw transport adapter for the PLACHEM Fast Gateway.

This module deliberately speaks only the verified public WebSocket RPC
contract.  It does not import OpenClaw's hashed ``dist`` modules and it does
not allow callers to select a model, provider, endpoint, credential, working
directory, delivery channel, or arbitrary session id.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


PROTOCOL_VERSION = 4
DEFAULT_GATEWAY_URL = "ws://127.0.0.1:18789"
REQUIRED_SCOPES = ("operator.read", "operator.write")
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_ALLOWED_SUBMIT_FIELDS = {
    "message",
    "agentId",
    "idempotencyKey",
    "timeout",
    "sessionKey",
}
_FORBIDDEN_SUBMIT_FIELDS = {
    "model",
    "provider",
    "endpoint",
    "base_url",
    "baseUrl",
    "credential",
    "credentials",
    "token",
    "api_key",
    "apiKey",
    "workspace",
    "workspace_id",
    "workspaceId",
    "cwd",
    "sessionId",
    "delivery",
    "channel",
    "accountId",
}


class CoreRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class AdapterError(RuntimeError):
    """Base class for fail-closed adapter errors."""


class TransportError(AdapterError):
    pass


class GatewayContractError(AdapterError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class SessionBindingError(AdapterError):
    pass


class IdempotencyConflict(AdapterError):
    pass


class SecretRef(Protocol):
    """A reference resolved by the trusted service boundary, never a caller."""

    def resolve(self) -> str: ...


@dataclass(frozen=True)
class EnvironmentSecretRef:
    """Reference to a SecretRef-backed environment injection.

    Production service configuration is responsible for resolving the real
    OpenClaw SecretRef into this fixed environment slot.  A raw token cannot
    be passed to :class:`OpenClawAdapter`.
    """

    name: str = "OPENCLAW_GATEWAY_TOKEN"

    def __post_init__(self) -> None:
        if self.name != "OPENCLAW_GATEWAY_TOKEN":
            raise ValueError("only the OpenClaw Gateway SecretRef slot is allowed")

    def resolve(self) -> str:
        value = os.environ.get(self.name, "")
        if not value:
            raise AdapterError("OpenClaw Gateway SecretRef is unavailable")
        return value


class GatewaySocket(Protocol):
    def send(self, data: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


SocketFactory = Callable[[str, float], GatewaySocket]


def _default_socket_factory(url: str, timeout: float) -> GatewaySocket:
    # websockets is a normal declared runtime dependency.  No OpenClaw
    # implementation module is imported here.
    from websockets.sync.client import connect

    return connect(
        url,
        open_timeout=timeout,
        close_timeout=min(timeout, 5.0),
        # OpenClaw's long-running RPCs can legitimately occupy the connection
        # beyond the websocket client's ping window.  RPC recv timeouts remain
        # bounded, so a second keepalive timeout only tears down a healthy
        # owner connection and prevents an authorized chat.abort later.
        ping_interval=None,
    )


class GatewayRPCClient:
    def __init__(
        self,
        secret_ref: SecretRef,
        *,
        url: str = DEFAULT_GATEWAY_URL,
        socket_factory: SocketFactory = _default_socket_factory,
        connect_timeout: float = 10.0,
        client_id: str = "gateway-client",
        client_version: str = "1",
        stale_after_seconds: float = 60.0,
    ) -> None:
        if url != DEFAULT_GATEWAY_URL:
            raise ValueError("OpenClaw endpoint override is not allowed")
        if isinstance(secret_ref, (str, bytes)) or not callable(getattr(secret_ref, "resolve", None)):
            raise TypeError("secret_ref must be a trusted SecretRef, not a raw token")
        self._secret_ref = secret_ref
        self._url = url
        self._socket_factory = socket_factory
        self._connect_timeout = connect_timeout
        self._client_id = client_id
        self._client_version = client_version
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._stale_after_seconds = stale_after_seconds
        self._socket: GatewaySocket | None = None
        self._last_activity = 0.0
        self._lock = threading.RLock()
        self._methods: set[str] = set()
        self.role = ""
        self.scopes: tuple[str, ...] = ()

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self._methods)

    def connect(self) -> Mapping[str, Any]:
        with self._lock:
            if self._socket is not None:
                if self._is_stale_locked():
                    self._disconnect_locked()
                else:
                    return {
                        "type": "hello-ok",
                        "role": self.role,
                        "scopes": list(self.scopes),
                        "methods": sorted(self._methods),
                    }
            token = self._secret_ref.resolve()
            if not isinstance(token, str) or not token:
                raise AdapterError("OpenClaw Gateway SecretRef resolved empty")
            try:
                socket = self._socket_factory(self._url, self._connect_timeout)
                self._socket = socket
                self._last_activity = time.monotonic()
                self._receive_challenge_locked(timeout=self._connect_timeout)
                payload = self._request_locked(
                    "connect",
                    {
                        "minProtocol": PROTOCOL_VERSION,
                        "maxProtocol": PROTOCOL_VERSION,
                        "client": {
                            "id": self._client_id,
                            "version": self._client_version,
                            "platform": "linux",
                            "mode": "backend",
                        },
                        "role": "operator",
                        "scopes": list(REQUIRED_SCOPES),
                        "auth": {"token": token},
                    },
                    timeout=self._connect_timeout,
                )
            except Exception as exc:
                self.close()
                if isinstance(exc, AdapterError):
                    raise
                raise TransportError("OpenClaw Gateway connect failed") from exc
            finally:
                # Do not retain an extra token copy after the connect frame.
                token = ""

            hello = self._hello_payload(payload)
            if hello.get("type") != "hello-ok":
                self.close()
                raise GatewayContractError("OpenClaw hello-ok was not returned")
            self.role, raw_scopes = self._extract_negotiated_auth(hello)
            self.scopes = tuple(str(item) for item in raw_scopes if isinstance(item, str))
            self._methods = self._extract_methods(hello)
            required_methods = {"agent", "agent.wait", "chat.history", "sessions.abort"}
            missing_methods = sorted(required_methods - self._methods)
            if missing_methods:
                self.close()
                raise GatewayContractError(
                    f"OpenClaw required method is unavailable: {missing_methods[0]}"
                )
            if self.role != "operator":
                self.close()
                raise GatewayContractError("OpenClaw operator role was not negotiated")
            if "operator.admin" not in self.scopes and not set(REQUIRED_SCOPES).issubset(self.scopes):
                self.close()
                raise GatewayContractError("OpenClaw operator.read/write scopes were not negotiated")
            return hello

    def _receive_challenge_locked(self, *, timeout: float) -> Mapping[str, Any]:
        if self._socket is None:
            raise TransportError("OpenClaw Gateway is not connected")
        try:
            decoded = json.loads(self._socket.recv(timeout=timeout))
        except TimeoutError:
            raise
        except Exception as exc:
            raise TransportError("OpenClaw Gateway challenge failed") from exc
        if not isinstance(decoded, Mapping):
            raise GatewayContractError("OpenClaw challenge is not an object")
        payload = decoded.get("payload")
        nonce = payload.get("nonce") if isinstance(payload, Mapping) else None
        if (
            decoded.get("type") != "event"
            or decoded.get("event") != "connect.challenge"
            or not isinstance(nonce, str)
            or not nonce
        ):
            raise GatewayContractError("OpenClaw connect.challenge was not returned")
        self._last_activity = time.monotonic()
        return decoded

    @staticmethod
    def _hello_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nested = payload.get("hello")
        if isinstance(nested, Mapping):
            return nested
        return payload

    @staticmethod
    def _extract_negotiated_auth(hello: Mapping[str, Any]) -> tuple[str, list[Any]]:
        """Extract auth from the current HelloOkSchema or older live shape.

        OpenClaw 2026.7.1-2 documents negotiated authorization at
        ``hello-ok.auth.{role,scopes}``.  Top-level role/scopes are accepted
        only as a compatibility shape; either shape must still explicitly
        prove the operator role and write/admin scope.
        """

        auth = hello.get("auth")
        if isinstance(auth, Mapping):
            role = auth.get("role") or auth.get("negotiatedRole")
            scopes = auth.get("scopes") or auth.get("negotiatedScopes")
        else:
            role = hello.get("role") or hello.get("negotiatedRole")
            scopes = hello.get("scopes") or hello.get("negotiatedScopes")
        if not isinstance(role, str):
            return "", []
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            return role, []
        return role, list(scopes)

    @staticmethod
    def _extract_methods(hello: Mapping[str, Any]) -> set[str]:
        candidates: list[Any] = [hello.get("methods")]
        features = hello.get("features")
        if isinstance(features, Mapping):
            candidates.append(features.get("methods"))
        snapshot = hello.get("snapshot")
        if isinstance(snapshot, Mapping):
            candidates.append(snapshot.get("methods"))
        for candidate in candidates:
            if isinstance(candidate, list):
                return {str(item) for item in candidate if isinstance(item, str)}
            if isinstance(candidate, Mapping):
                return {str(item) for item, enabled in candidate.items() if enabled}
        return set()

    def request(self, method: str, params: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
        with self._lock:
            # A persistent socket can be silently killed by an intermediary or
            # by the gateway's keepalive watchdog.  Discard old idle sockets
            # before sending, then allow exactly one fresh-connection retry for
            # an actual transport failure.  The original params (including
            # runId/sessionKey) are deliberately reused unchanged.
            for attempt in range(2):
                try:
                    if self._socket is not None and self._is_stale_locked():
                        self._disconnect_locked()
                    if self._socket is None:
                        self.connect()
                    return self._request_locked(method, params, timeout=timeout)
                except (TransportError, TimeoutError) as exc:
                    self._disconnect_locked()
                    if attempt == 1:
                        if isinstance(exc, TransportError):
                            raise
                        raise TransportError("OpenClaw Gateway transport failed") from exc
            raise AssertionError("unreachable")

    def request_fresh(self, method: str, params: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
        """Run one RPC on a private connection.

        A blocking agent.wait must never hold the persistent client's lock or
        socket hostage from unrelated dispatch/cancel operations.
        """
        client = GatewayRPCClient(
            self._secret_ref,
            url=self._url,
            socket_factory=self._socket_factory,
            connect_timeout=self._connect_timeout,
            client_id=self._client_id,
            client_version=self._client_version,
            stale_after_seconds=self._stale_after_seconds,
        )
        try:
            return client.request(method, params, timeout=timeout)
        finally:
            client.close()

    def request_on_owner_connection(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        """Run an ownership-sensitive RPC on the original connection.

        OpenClaw binds an active agent run to the connection that submitted
        it.  Reconnect is intentionally forbidden here: a new connection has
        no authority to abort that run even when it has operator.write.
        """
        with self._lock:
            if self._socket is None:
                raise TransportError("OpenClaw Gateway owner connection is unavailable")
            return self._request_locked(method, params, timeout=timeout)

    def _is_stale_locked(self) -> bool:
        return (
            self._socket is not None
            and self._last_activity > 0
            and time.monotonic() - self._last_activity >= self._stale_after_seconds
        )

    def _disconnect_locked(self) -> None:
        socket, self._socket = self._socket, None
        self._last_activity = 0.0
        self._methods.clear()
        self.role = ""
        self.scopes = ()
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _request_locked(self, method: str, params: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
        if self._socket is None:
            raise TransportError("OpenClaw Gateway is not connected")
        request_id = uuid.uuid4().hex
        frame = {"type": "req", "id": request_id, "method": method, "params": dict(params)}
        try:
            self._socket.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
            while True:
                raw = self._socket.recv(timeout=timeout)
                decoded = json.loads(raw)
                if not isinstance(decoded, Mapping):
                    raise GatewayContractError("OpenClaw returned a non-object frame")
                if decoded.get("type") == "event":
                    continue
                if decoded.get("type") != "res" or decoded.get("id") != request_id:
                    continue
                if decoded.get("ok") is not True:
                    error = decoded.get("error")
                    if isinstance(error, Mapping):
                        code = str(error.get("code") or "GATEWAY_REJECTED")
                    else:
                        code = "GATEWAY_REJECTED"
                    raise GatewayContractError(f"OpenClaw RPC rejected: {code}", code=code)
                payload = decoded.get("payload")
                if not isinstance(payload, Mapping):
                    raise GatewayContractError("OpenClaw RPC payload is not an object")
                self._last_activity = time.monotonic()
                return payload
        except (AdapterError, TimeoutError):
            raise
        except Exception as exc:
            raise TransportError("OpenClaw Gateway transport failed") from exc

    def close(self) -> None:
        with self._lock:
            self._disconnect_locked()


@dataclass(frozen=True)
class RunBinding:
    core_run_id: str
    openclaw_run_id: str
    agent_id: str
    session_key: str
    session_id: str | None
    idempotency_key: str
    status: CoreRunStatus


class RunBindingStore(Protocol):
    def get(self, core_run_id: str) -> RunBinding | None: ...

    def put(self, binding: RunBinding) -> None: ...

    def get_preparation(self, core_run_id: str) -> "RunPreparation | None": ...

    def put_preparation(self, preparation: "RunPreparation") -> None: ...


@dataclass(frozen=True)
class RunPreparation:
    core_run_id: str
    agent_id: str
    session_key: str
    idempotency_key: str
    watermark_seq: int | None
    watermark_message_id: str | None
    captured_at_ms: int
    history_was_empty: bool


class MemoryRunBindingStore:
    def __init__(self) -> None:
        self._items: dict[str, RunBinding] = {}
        self._preparations: dict[str, RunPreparation] = {}

    def get(self, core_run_id: str) -> RunBinding | None:
        return self._items.get(core_run_id)

    def put(self, binding: RunBinding) -> None:
        self._items[binding.core_run_id] = binding

    def get_preparation(self, core_run_id: str) -> RunPreparation | None:
        return self._preparations.get(core_run_id)

    def put_preparation(self, preparation: RunPreparation) -> None:
        existing = self._preparations.get(preparation.core_run_id)
        if existing is not None and existing != preparation:
            raise IdempotencyConflict("run preparation conflicts")
        if any(
            item.idempotency_key == preparation.idempotency_key
            and item.core_run_id != preparation.core_run_id
            for item in self._preparations.values()
        ):
            raise IdempotencyConflict("run preparation idempotency conflicts")
        self._preparations[preparation.core_run_id] = preparation


class SQLiteRunBindingStore:
    """Durable binding store separate from the legacy War Room database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS openclaw_run_bindings (
                       core_run_id TEXT PRIMARY KEY,
                       openclaw_run_id TEXT NOT NULL UNIQUE,
                       agent_id TEXT NOT NULL,
                       session_key TEXT NOT NULL,
                       session_id TEXT,
                       idempotency_key TEXT NOT NULL UNIQUE,
                       status TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS openclaw_run_preparations (
                       core_run_id TEXT PRIMARY KEY,
                       agent_id TEXT NOT NULL,
                       session_key TEXT NOT NULL,
                       idempotency_key TEXT NOT NULL UNIQUE,
                       watermark_seq INTEGER,
                       watermark_message_id TEXT,
                       captured_at_ms INTEGER NOT NULL,
                       history_was_empty INTEGER NOT NULL CHECK(history_was_empty IN (0, 1))
                   )"""
            )

    def get(self, core_run_id: str) -> RunBinding | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT core_run_id, openclaw_run_id, agent_id, session_key,
                          session_id, idempotency_key, status
                     FROM openclaw_run_bindings WHERE core_run_id = ?""",
                (core_run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunBinding(*row[:-1], CoreRunStatus(row[-1]))

    def put(self, binding: RunBinding) -> None:
        values = asdict(binding)
        values["status"] = binding.status.value
        try:
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    """INSERT INTO openclaw_run_bindings
                           (core_run_id, openclaw_run_id, agent_id, session_key,
                            session_id, idempotency_key, status)
                       VALUES (:core_run_id, :openclaw_run_id, :agent_id, :session_key,
                               :session_id, :idempotency_key, :status)
                       ON CONFLICT(core_run_id) DO UPDATE SET
                           openclaw_run_id=excluded.openclaw_run_id,
                           agent_id=excluded.agent_id,
                           session_key=excluded.session_key,
                           session_id=excluded.session_id,
                           idempotency_key=excluded.idempotency_key,
                           status=excluded.status""",
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflict("run or idempotency binding conflicts") from exc

    def get_preparation(self, core_run_id: str) -> RunPreparation | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """SELECT core_run_id, agent_id, session_key, idempotency_key,
                          watermark_seq, watermark_message_id, captured_at_ms, history_was_empty
                     FROM openclaw_run_preparations WHERE core_run_id = ?""",
                (core_run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunPreparation(*row[:-1], bool(row[-1]))

    def put_preparation(self, preparation: RunPreparation) -> None:
        values = asdict(preparation)
        values["history_was_empty"] = int(preparation.history_was_empty)
        try:
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    """INSERT INTO openclaw_run_preparations
                           (core_run_id, agent_id, session_key, idempotency_key,
                            watermark_seq, watermark_message_id, captured_at_ms, history_was_empty)
                       VALUES (:core_run_id, :agent_id, :session_key, :idempotency_key,
                               :watermark_seq, :watermark_message_id, :captured_at_ms, :history_was_empty)
                       ON CONFLICT(core_run_id) DO NOTHING""",
                    values,
                )
                row = connection.execute(
                    """SELECT agent_id, session_key, idempotency_key, watermark_seq,
                              watermark_message_id, captured_at_ms, history_was_empty
                         FROM openclaw_run_preparations WHERE core_run_id = ?""",
                    (preparation.core_run_id,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflict("run preparation conflicts") from exc
        expected = (
            preparation.agent_id, preparation.session_key, preparation.idempotency_key,
            preparation.watermark_seq, preparation.watermark_message_id,
            preparation.captured_at_ms, int(preparation.history_was_empty),
        )
        if row != expected:
            raise IdempotencyConflict("run preparation conflicts")


@dataclass(frozen=True)
class ValidationDecision:
    status: CoreRunStatus
    reason: str = ""
    result: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in {CoreRunStatus.PASS, CoreRunStatus.FAIL, CoreRunStatus.BLOCKED}:
            raise ValueError("validator may return only PASS, FAIL, or BLOCKED")


class ResultValidator(Protocol):
    def __call__(self, payload: Mapping[str, Any]) -> ValidationDecision: ...


def reject_unvalidated_result(_: Mapping[str, Any]) -> ValidationDecision:
    return ValidationDecision(CoreRunStatus.FAIL, "RESULT_VALIDATION_REQUIRED")


@dataclass(frozen=True)
class AdapterOutcome:
    status: CoreRunStatus
    reason: str = ""
    result: Mapping[str, Any] | None = None


ValidationCheck = Callable[[Mapping[str, Any], Mapping[str, Any]], str | None]


class CompositeResultValidator:
    """Run Fast Gateway-owned schema/evidence/artifact/scope checks.

    Checks return ``None`` on success or a non-secret reason code on failure.
    They are configured by the Fast Gateway, not derived from worker output.
    """

    def __init__(
        self,
        *,
        result_schema: ValidationCheck,
        evidence: ValidationCheck,
        artifacts: ValidationCheck,
        scope: ValidationCheck,
    ) -> None:
        self._checks = (
            ("RESULT_SCHEMA", result_schema),
            ("EVIDENCE", evidence),
            ("ARTIFACT", artifacts),
            ("SCOPE", scope),
        )

    def __call__(self, payload: Mapping[str, Any]) -> ValidationDecision:
        result = self._extract_result(payload)
        if result is None:
            return ValidationDecision(CoreRunStatus.FAIL, "MISSING_RESULT")
        for name, check in self._checks:
            try:
                reason = check(result, payload)
            except Exception:
                return ValidationDecision(CoreRunStatus.FAIL, f"{name}_VALIDATION_ERROR")
            if reason:
                return ValidationDecision(CoreRunStatus.FAIL, f"{name}_VALIDATION_FAILED:{reason}")
        worker_status = str(result.get("status") or "").lower()
        if worker_status == "completed":
            return ValidationDecision(CoreRunStatus.PASS, result=result)
        if worker_status == "blocked":
            return ValidationDecision(CoreRunStatus.BLOCKED, str(result.get("reason") or "WORKER_BLOCKED"), result)
        return ValidationDecision(CoreRunStatus.FAIL, str(result.get("reason") or "WORKER_FAILED"), result)

    @classmethod
    def _extract_result(cls, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        result = payload.get("result")
        if isinstance(result, Mapping):
            return result
        history = payload.get("history")
        if not isinstance(history, Mapping):
            return None
        messages = history.get("messages")
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, Mapping):
                return content
            if isinstance(content, list):
                content = "".join(
                    item if isinstance(item, str) else str(item.get("text") or "")
                    for item in content
                    if isinstance(item, (str, Mapping))
                )
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, Mapping):
                    return parsed
        return None


class OpenClawAdapter:
    def __init__(
        self,
        secret_ref: SecretRef,
        binding_store: RunBindingStore,
        *,
        result_validator: ResultValidator = reject_unvalidated_result,
        socket_factory: SocketFactory = _default_socket_factory,
        history_limit: int = 20,
    ) -> None:
        if history_limit < 1 or history_limit > 100:
            raise ValueError("history_limit must be between 1 and 100")
        self.rpc = GatewayRPCClient(secret_ref, socket_factory=socket_factory)
        self.bindings = binding_store
        self.result_validator = result_validator
        self.history_limit = history_limit

    def connect(self) -> Mapping[str, Any]:
        return self.rpc.connect()

    @staticmethod
    def _validate_agent_id(agent_id: Any) -> str:
        if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
            raise GatewayContractError("INVALID_AGENT_ID")
        return agent_id

    @staticmethod
    def _validate_session_binding(session_key: Any, agent_id: str) -> str:
        if not isinstance(session_key, str) or not session_key:
            raise SessionBindingError("OpenClaw sessionKey is missing")
        prefix = f"agent:{agent_id}:"
        if not session_key.startswith(prefix):
            raise SessionBindingError("OpenClaw sessionKey does not match agentId")
        return session_key

    @staticmethod
    def _validate_submit(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise GatewayContractError("submit payload must be an object")
        keys = set(payload)
        forbidden = sorted(keys & _FORBIDDEN_SUBMIT_FIELDS)
        unknown = sorted(keys - _ALLOWED_SUBMIT_FIELDS)
        if forbidden:
            raise GatewayContractError(f"FORBIDDEN_RUNTIME_FIELD:{forbidden[0]}")
        if unknown:
            raise GatewayContractError(f"UNKNOWN_SUBMIT_FIELD:{unknown[0]}")
        required = {"message", "agentId", "idempotencyKey", "timeout"}
        missing = sorted(required - keys)
        if missing:
            raise GatewayContractError(f"MISSING_SUBMIT_FIELD:{missing[0]}")
        message = payload.get("message")
        key = payload.get("idempotencyKey")
        timeout = payload.get("timeout")
        if not isinstance(message, str) or not message.strip():
            raise GatewayContractError("message must be a non-empty string")
        if not isinstance(key, str) or not key or len(key) > 256:
            raise GatewayContractError("idempotencyKey is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise GatewayContractError("timeout must be positive")
        result = dict(payload)
        result["agentId"] = OpenClawAdapter._validate_agent_id(payload.get("agentId"))
        if "sessionKey" in result:
            OpenClawAdapter._validate_session_binding(result["sessionKey"], result["agentId"])
        return result

    def submit(self, core_run_id: str, payload: Mapping[str, Any]) -> RunBinding:
        if not isinstance(core_run_id, str) or not core_run_id:
            raise GatewayContractError("core_run_id is required")
        params = self._validate_submit(payload)
        agent_id = str(params["agentId"])
        requested_session = params.get("sessionKey")
        session_key = requested_session or f"agent:{agent_id}:fast-gateway-{core_run_id}"
        self._validate_session_binding(session_key, agent_id)
        params["sessionKey"] = session_key
        existing = self.bindings.get(core_run_id)
        if existing is not None:
            if existing.agent_id != params["agentId"] or existing.idempotency_key != params["idempotencyKey"]:
                raise IdempotencyConflict("core run binding does not match retry")
            if session_key != existing.session_key:
                raise SessionBindingError("core run sessionKey does not match retry binding")
            return existing
        preparation = self.bindings.get_preparation(core_run_id)
        if preparation is not None:
            if (
                preparation.agent_id != agent_id
                or preparation.session_key != session_key
                or preparation.idempotency_key != params["idempotencyKey"]
            ):
                raise IdempotencyConflict("core run preparation does not match retry")
        else:
            history = self.rpc.request(
                "chat.history", {"sessionKey": session_key, "limit": self.history_limit}, timeout=15.0,
            )
            watermark_seq, watermark_message_id, history_was_empty = self._capture_watermark(history)
            preparation = RunPreparation(
                core_run_id=core_run_id,
                agent_id=agent_id,
                session_key=session_key,
                idempotency_key=str(params["idempotencyKey"]),
                watermark_seq=watermark_seq,
                watermark_message_id=watermark_message_id,
                captured_at_ms=int(time.time() * 1000),
                history_was_empty=history_was_empty,
            )
            self.bindings.put_preparation(preparation)
        response = self.rpc.request("agent", params, timeout=float(params["timeout"]) + 5.0)
        status = str(response.get("status") or "")
        if status not in {"accepted", "in_flight", "ok"}:
            raise GatewayContractError("OpenClaw did not accept the agent run")
        run_id = response.get("runId")
        session_key = response.get("sessionKey")
        if not isinstance(run_id, str) or not run_id:
            raise GatewayContractError("OpenClaw runId is missing")
        session_key = self._validate_session_binding(session_key, agent_id)
        if session_key != preparation.session_key:
            raise SessionBindingError("OpenClaw response sessionKey does not match preparation")
        session_id = response.get("sessionId")
        if session_id is not None and not isinstance(session_id, str):
            raise GatewayContractError("OpenClaw sessionId is invalid")
        binding = RunBinding(
            core_run_id=core_run_id,
            openclaw_run_id=run_id,
            agent_id=agent_id,
            session_key=session_key,
            session_id=session_id,
            idempotency_key=str(params["idempotencyKey"]),
            status=CoreRunStatus.RUNNING,
        )
        self.bindings.put(binding)
        return binding

    @staticmethod
    def _message_cursor(message: Mapping[str, Any]) -> tuple[int | None, str | None]:
        metadata = message.get("__openclaw")
        seq = metadata.get("seq") if isinstance(metadata, Mapping) else None
        message_id = metadata.get("id") if isinstance(metadata, Mapping) else None
        if isinstance(seq, bool) or not isinstance(seq, int):
            seq = None
        if not isinstance(message_id, str) or not message_id:
            message_id = message.get("id") or message.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            message_id = None
        return seq, message_id

    @classmethod
    def _capture_watermark(cls, history: Mapping[str, Any]) -> tuple[int | None, str | None, bool]:
        messages = history.get("messages")
        if not isinstance(messages, list):
            raise GatewayContractError("HISTORY_WATERMARK_UNVERIFIABLE")
        assistants = [item for item in messages if isinstance(item, Mapping) and item.get("role") == "assistant"]
        if not assistants:
            return None, None, True
        seq, message_id = cls._message_cursor(assistants[-1])
        return seq, message_id, False

    @classmethod
    def _history_after_watermark(
        cls, history: Mapping[str, Any], preparation: RunPreparation,
    ) -> Mapping[str, Any]:
        messages = history.get("messages")
        if not isinstance(messages, list):
            raise GatewayContractError("HISTORY_WATERMARK_UNVERIFIABLE")
        if preparation.history_was_empty:
            candidates = list(messages)
        elif preparation.watermark_seq is not None:
            boundary_found = any(
                isinstance(message, Mapping)
                and cls._message_cursor(message)[0] == preparation.watermark_seq
                and (
                    preparation.watermark_message_id is None
                    or cls._message_cursor(message)[1] == preparation.watermark_message_id
                )
                for message in messages
            )
            if not boundary_found:
                raise GatewayContractError("HISTORY_WATERMARK_UNVERIFIABLE")
            candidates = []
            for message in messages:
                if not isinstance(message, Mapping):
                    continue
                seq, _ = cls._message_cursor(message)
                if seq is not None and seq > preparation.watermark_seq:
                    candidates.append(message)
        elif preparation.watermark_message_id is not None:
            boundary = next(
                (index for index, message in enumerate(messages)
                 if isinstance(message, Mapping)
                 and cls._message_cursor(message)[1] == preparation.watermark_message_id),
                None,
            )
            if boundary is None:
                raise GatewayContractError("HISTORY_WATERMARK_UNVERIFIABLE")
            candidates = messages[boundary + 1:]
        else:
            raise GatewayContractError("HISTORY_WATERMARK_UNVERIFIABLE")
        assistants = [item for item in candidates if isinstance(item, Mapping) and item.get("role") == "assistant"]
        if not assistants:
            raise GatewayContractError("MISSING_POST_SUBMIT_RESULT")
        return {**dict(history), "messages": assistants}

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> AdapterOutcome:
        binding = self._require_binding(core_run_id)
        if binding.status in {
            CoreRunStatus.PASS,
            CoreRunStatus.FAIL,
            CoreRunStatus.BLOCKED,
            CoreRunStatus.TIMEOUT,
            CoreRunStatus.CANCELLED,
        }:
            return AdapterOutcome(binding.status, binding.status.value)
        response = self.rpc.request_fresh(
            "agent.wait",
            {"runId": binding.openclaw_run_id, "timeoutMs": max(1, int(timeout_seconds * 1000))},
            timeout=timeout_seconds + 5.0,
        )
        observed = str(response.get("status") or "")
        response_run_id = response.get("runId")
        if response_run_id is not None and response_run_id != binding.openclaw_run_id:
            self._set_status(binding, CoreRunStatus.FAIL)
            return AdapterOutcome(CoreRunStatus.FAIL, "OPENCLAW_RUN_MISMATCH")
        if observed in {"pending", "accepted", "in_flight"}:
            self._set_status(binding, CoreRunStatus.RUNNING)
            return AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")
        if observed == "timeout":
            # agent.wait timed out; the agent run itself is still active.  Do
            # not poison the persisted run binding with a terminal state—the
            # Core owns the absolute Runtime Policy deadline.
            return AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")
        if observed == "error":
            error = str(response.get("error") or "")
            status = CoreRunStatus.CANCELLED if error.lower() == "aborted" else CoreRunStatus.FAIL
            self._set_status(binding, status)
            reason = "OPENCLAW_ABORTED" if status == CoreRunStatus.CANCELLED else "OPENCLAW_ERROR"
            return AdapterOutcome(status, reason)
        if observed != "ok":
            self._set_status(binding, CoreRunStatus.FAIL)
            return AdapterOutcome(CoreRunStatus.FAIL, "UNKNOWN_OPENCLAW_STATUS")

        # agent.wait is lifecycle metadata only. Embedded result material must
        # never bypass the history watermark for this exact submit.
        history = self.rpc.request_fresh(
            "chat.history",
            {"sessionKey": binding.session_key, "limit": self.history_limit},
            timeout=min(timeout_seconds + 5.0, 30.0),
        )
        preparation = self.bindings.get_preparation(core_run_id)
        if preparation is None:
            self._set_status(binding, CoreRunStatus.FAIL)
            return AdapterOutcome(CoreRunStatus.FAIL, "HISTORY_WATERMARK_UNVERIFIABLE")
        try:
            history = self._history_after_watermark(history, preparation)
        except GatewayContractError as exc:
            self._set_status(binding, CoreRunStatus.FAIL)
            return AdapterOutcome(CoreRunStatus.FAIL, str(exc))
        control_payload = {
            key: value for key, value in response.items()
            if key not in {"result", "evidence", "artifacts", "messages", "history"}
        }
        validation_payload: Mapping[str, Any] = {**control_payload, "history": history}
        decision = self.result_validator(validation_payload)
        self._set_status(binding, decision.status)
        return AdapterOutcome(decision.status, decision.reason, decision.result)

    @staticmethod
    def _contains_result(payload: Mapping[str, Any]) -> bool:
        return any(key in payload for key in ("result", "evidence", "artifacts", "messages"))

    def cancel(self, core_run_id: str) -> RunBinding:
        binding = self._require_binding(core_run_id)
        try:
            response = self.rpc.request_on_owner_connection(
                "sessions.abort",
                {
                    "key": binding.session_key,
                    "runId": binding.openclaw_run_id,
                    "agentId": binding.agent_id,
                },
                timeout=15.0,
            )
        except GatewayContractError as exc:
            # Abort is idempotent when the underlying run already completed.
            if exc.code not in {"RUN_NOT_FOUND", "ALREADY_FINISHED", "RUN_ALREADY_FINISHED", "NO_ACTIVE_RUN"}:
                raise
            return self._set_status(binding, CoreRunStatus.CANCELLED)
        confirmed = (
            response.get("aborted") is True
            or response.get("abortedRunId") == binding.openclaw_run_id
            or response.get("status") in {"aborted", "no-active-run"}
            or (response.get("ok") is True and response.get("runIds") == [])
        )
        if not confirmed:
            raise GatewayContractError("OpenClaw did not confirm abort")
        return self._set_status(binding, CoreRunStatus.CANCELLED)

    def _require_binding(self, core_run_id: str) -> RunBinding:
        binding = self.bindings.get(core_run_id)
        if binding is None:
            raise SessionBindingError("UNKNOWN_CORE_RUN")
        self._validate_agent_id(binding.agent_id)
        self._validate_session_binding(binding.session_key, binding.agent_id)
        return binding

    def _set_status(self, binding: RunBinding, status: CoreRunStatus) -> RunBinding:
        updated = RunBinding(
            core_run_id=binding.core_run_id,
            openclaw_run_id=binding.openclaw_run_id,
            agent_id=binding.agent_id,
            session_key=binding.session_key,
            session_id=binding.session_id,
            idempotency_key=binding.idempotency_key,
            status=status,
        )
        self.bindings.put(updated)
        return updated

    def close(self) -> None:
        self.rpc.close()


def redact_secrets(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively redact credential-shaped fields and known secret values."""

    sensitive = {"token", "credential", "credentials", "api_key", "apikey", "password", "secret"}
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in sensitive else redact_secrets(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item, secrets) for item in value)
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value
