"""Production authorization grants for Fast Gateway.

This store owns authorization metadata only.  Provider credentials remain in
OpenConnector and execution/session data remain in their existing stores.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


ERROR_CODES = frozenset({
    "AUTH_REQUIRED", "INVALID_TOKEN", "NOT_FOUND", "EXPIRED", "REVOKED",
    "ALREADY_CONSUMED", "BINDING_MISMATCH", "TASK_DIGEST_MISMATCH", "CONFLICT",
    "STORE_BUSY", "STORE_CORRUPT", "AUDIT_INTEGRITY_FAILED", "SCHEMA_UNSUPPORTED",
    "FORBIDDEN_FIELD",
})
_SCHEMA_VERSION = 1
_SENSITIVE = frozenset({
    "model", "provider", "endpoint", "token", "credential", "credentials", "secret",
    "password", "api_key", "apikey", "message", "task",
})
_DETAIL_KEYS = frozenset({"reason", "key_id", "schema_version"})
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")


class AuthBrokerError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError("UNKNOWN_AUTH_ERROR")
        self.code = code
        super().__init__(code)


class SecretRef(Protocol):
    def get(self, key_id: str) -> bytes: ...


@dataclass(frozen=True)
class EnvironmentPepperRef:
    prefix: str = "PLACHEM_AUTH_BROKER_PEPPER_"

    def get(self, key_id: str) -> bytes:
        value = os.environ.get(self.prefix + key_id.upper().replace("-", "_"))
        if not value:
            raise AuthBrokerError("AUTH_REQUIRED")
        return value.encode("utf-8")


@dataclass(frozen=True)
class FilePepperRef:
    paths: Mapping[str, str | Path]

    def get(self, key_id: str) -> bytes:
        path = self.paths.get(key_id)
        if path is None:
            raise AuthBrokerError("AUTH_REQUIRED")
        try:
            value = Path(path).read_bytes().strip()
        except OSError as exc:
            raise AuthBrokerError("AUTH_REQUIRED") from exc
        if not value:
            raise AuthBrokerError("AUTH_REQUIRED")
        return value


@dataclass(frozen=True)
class AuthScope:
    agent_id: str
    action: str
    workspace_id: str
    project_id: str
    task_contract: Mapping[str, Any]


@dataclass(frozen=True)
class IssuedGrant:
    grant_id: str
    token: str
    token_id: str
    task_digest: str
    expires_at_ms: int


@dataclass(frozen=True)
class ConsumedGrant:
    grant_id: str
    run_id: str
    agent_id: str
    action: str
    workspace_id: str
    project_id: str
    task_digest: str


def canonical_task_digest(scope: AuthScope) -> str:
    if not isinstance(scope.task_contract, Mapping):
        raise ValueError("INVALID_TASK_CONTRACT")
    envelope = {
        "version": 1,
        "agent_id": _bound(scope.agent_id),
        "action": _bound(scope.action),
        "workspace_id": _bound(scope.workspace_id),
        "project_id": _bound(scope.project_id),
        "task_goal_contract": _canonical_value(scope.task_contract),
    }
    raw = json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "v1:" + hashlib.sha256(raw.encode()).hexdigest()


def _bound(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("INVALID_BINDING")
    return value.strip()


def _identity_digest(value: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError("INVALID_IDENTITY")
    return "id-v1:" + hashlib.sha256(value.encode()).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_") if isinstance(key, str) else ""
            if not isinstance(key, str) or normalized in _SENSITIVE:
                raise AuthBrokerError("FORBIDDEN_FIELD")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("INVALID_TASK_CONTRACT")


class SQLiteAuthBroker:
    """One-time bearer grant broker with an authenticated append-only audit."""

    def __init__(self, path: str | Path, secrets_ref: SecretRef, *, key_id: str,
                 busy_retries: int = 3, busy_delay: float = .01) -> None:
        self.path = Path(path)
        self.secrets = secrets_ref
        self.key_id = _bound(key_id)
        self.busy_retries = max(0, busy_retries)
        self.busy_delay = max(0.0, busy_delay)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.verify_audit()

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db
        except sqlite3.DatabaseError as exc:
            raise AuthBrokerError("STORE_CORRUPT") from exc

    def _initialize(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS schema_migrations(
          version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL, checksum TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_grants(
          grant_id TEXT PRIMARY KEY, token_id TEXT NOT NULL UNIQUE, token_mac BLOB NOT NULL UNIQUE,
          key_id TEXT NOT NULL, task_digest TEXT NOT NULL, agent_id TEXT NOT NULL,
          action TEXT NOT NULL, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
          issued_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
          revoked_at_ms INTEGER, consumed_at_ms INTEGER, consumed_run_id TEXT,
          created_by TEXT NOT NULL,
          CHECK(expires_at_ms > issued_at_ms),
          CHECK((consumed_at_ms IS NULL) = (consumed_run_id IS NULL)));
        CREATE INDEX IF NOT EXISTS auth_grants_live_idx
          ON auth_grants(token_id, expires_at_ms, revoked_at_ms, consumed_at_ms);
        CREATE TABLE IF NOT EXISTS auth_audit(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
          grant_id TEXT, event_type TEXT NOT NULL, outcome TEXT NOT NULL, error_code TEXT,
          run_id TEXT, agent_id TEXT, action TEXT, workspace_id TEXT, project_id TEXT,
          task_digest TEXT, occurred_at_ms INTEGER NOT NULL, prev_hash TEXT NOT NULL,
          event_hash TEXT NOT NULL UNIQUE, key_id TEXT NOT NULL, event_mac BLOB NOT NULL,
          details_json TEXT NOT NULL,
          FOREIGN KEY(grant_id) REFERENCES auth_grants(grant_id));
        CREATE TRIGGER IF NOT EXISTS auth_audit_no_update BEFORE UPDATE ON auth_audit
          BEGIN SELECT RAISE(ABORT, 'AUTH_AUDIT_APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS auth_audit_no_delete BEFORE DELETE ON auth_audit
          BEGIN SELECT RAISE(ABORT, 'AUTH_AUDIT_APPEND_ONLY'); END;
        """
        checksum = hashlib.sha256(ddl.encode()).hexdigest()
        try:
            with self._connect() as db:
                objects = db.execute("""SELECT name FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%' ORDER BY name""").fetchall()
                names = {row["name"] for row in objects}
                if names and "schema_migrations" not in names:
                    raise AuthBrokerError("SCHEMA_UNSUPPORTED")
                if "schema_migrations" in names:
                    rows = db.execute("SELECT version,checksum FROM schema_migrations").fetchall()
                    if any(row["version"] > _SCHEMA_VERSION for row in rows):
                        raise AuthBrokerError("SCHEMA_UNSUPPORTED")
                else:
                    rows = []
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(ddl)
                row = next((r for r in rows if r["version"] == _SCHEMA_VERSION), None)
                if row and row["checksum"] != checksum:
                    raise AuthBrokerError("SCHEMA_UNSUPPORTED")
                if not row:
                    db.execute("INSERT INTO schema_migrations VALUES(?,?,?)",
                               (_SCHEMA_VERSION, _now_ms(), checksum))
        except AuthBrokerError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AuthBrokerError("STORE_CORRUPT") from exc

    def issue(self, scope: AuthScope, *, ttl_seconds: float, created_by: str,
              now_ms: int | None = None) -> IssuedGrant:
        if ttl_seconds <= 0:
            raise ValueError("INVALID_TTL")
        now = _now_ms() if now_ms is None else now_ms
        token = secrets.token_urlsafe(32)
        token_id = hashlib.sha256(token.encode()).hexdigest()
        mac = self._token_mac(token, self.key_id)
        digest = canonical_task_digest(scope)
        grant_id = str(uuid.uuid4())
        expires = now + int(ttl_seconds * 1000)
        with self._transaction() as db:
            try:
                db.execute("""INSERT INTO auth_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    grant_id, token_id, mac, self.key_id, digest, _bound(scope.agent_id),
                    _bound(scope.action), _bound(scope.workspace_id), _bound(scope.project_id),
                    now, expires, None, None, None, _identity_digest(created_by),
                ))
            except sqlite3.IntegrityError as exc:
                raise AuthBrokerError("CONFLICT") from exc
            self._audit(db, grant_id, "ISSUED", "ALLOW", None, None, scope, digest,
                        {"key_id": self.key_id})
        return IssuedGrant(grant_id, token, token_id, digest, expires)

    def verify_and_consume(self, token: str | None, scope: AuthScope, *, run_id: str,
                           now_ms: int | None = None) -> ConsumedGrant:
        expected_digest = canonical_task_digest(scope)
        if not token:
            with self._transaction() as db:
                self._audit(db, None, "CONSUME", "DENY", "AUTH_REQUIRED", run_id,
                            scope, expected_digest, {"reason": "AUTH_REQUIRED"})
            raise AuthBrokerError("AUTH_REQUIRED")
        now = _now_ms() if now_ms is None else now_ms
        token_id = hashlib.sha256(token.encode()).hexdigest()
        denied: str | None = None
        result: ConsumedGrant | None = None
        with self._transaction() as db:
            row = db.execute("SELECT * FROM auth_grants WHERE token_id=?", (token_id,)).fetchone()
            if row is None:
                self._audit(db, None, "CONSUME", "DENY", "INVALID_TOKEN", run_id,
                            scope, expected_digest, {"reason": "INVALID_TOKEN"})
                denied = "INVALID_TOKEN"
            elif not hmac.compare_digest(bytes(row["token_mac"]), self._token_mac(token, row["key_id"])):
                self._audit(db, row["grant_id"], "CONSUME", "DENY", "INVALID_TOKEN", run_id,
                            scope, expected_digest, {"reason": "INVALID_TOKEN"})
                denied = "INVALID_TOKEN"
            code = None if denied else self._deny_reason(row, scope, expected_digest, now)
            if denied:
                pass
            elif code:
                self._audit(db, row["grant_id"], "CONSUME", "DENY", code, run_id,
                            scope, expected_digest, {"reason": code})
                denied = code
            else:
                changed = db.execute("""UPDATE auth_grants SET consumed_at_ms=?,consumed_run_id=?
                    WHERE grant_id=? AND consumed_at_ms IS NULL AND revoked_at_ms IS NULL
                    AND expires_at_ms>?""", (now, _bound(run_id), row["grant_id"], now)).rowcount
                if changed != 1:
                    raise AuthBrokerError("CONFLICT")
                self._audit(db, row["grant_id"], "CONSUME", "ALLOW", None, run_id,
                            scope, expected_digest, {})
                result = ConsumedGrant(row["grant_id"], run_id, scope.agent_id, scope.action,
                                       scope.workspace_id, scope.project_id, expected_digest)
        if denied:
            raise AuthBrokerError(denied)
        assert result is not None
        return result

    def revoke(self, grant_id: str, *, revoked_by: str, now_ms: int | None = None) -> None:
        now = _now_ms() if now_ms is None else now_ms
        denied = False
        with self._transaction() as db:
            row = db.execute("SELECT * FROM auth_grants WHERE grant_id=?", (_bound(grant_id),)).fetchone()
            if row is None:
                raise AuthBrokerError("NOT_FOUND")
            if row["revoked_at_ms"] is not None:
                raise AuthBrokerError("REVOKED")
            scope = AuthScope(row["agent_id"], row["action"], row["workspace_id"], row["project_id"], {})
            if row["consumed_at_ms"] is not None:
                self._audit(db, grant_id, "REVOKE", "DENY", "ALREADY_CONSUMED", None,
                            scope, row["task_digest"], {"reason": "ALREADY_CONSUMED"})
                denied = True
            else:
                changed = db.execute("""UPDATE auth_grants SET revoked_at_ms=?
                    WHERE grant_id=? AND revoked_at_ms IS NULL AND consumed_at_ms IS NULL""",
                    (now, grant_id)).rowcount
                if changed != 1:
                    raise AuthBrokerError("CONFLICT")
                self._audit(db, grant_id, "REVOKED", "ALLOW", None, None, scope,
                            row["task_digest"], {"reason": _identity_digest(revoked_by)})
        if denied:
            raise AuthBrokerError("ALREADY_CONSUMED")

    def verify_audit(self) -> None:
        try:
            with self._connect() as db:
                previous = "0" * 64
                rows = db.execute("SELECT * FROM auth_audit ORDER BY seq").fetchall()
                for expected_seq, row in enumerate(rows, 1):
                    if row["seq"] != expected_seq or row["prev_hash"] != previous:
                        raise AuthBrokerError("AUDIT_INTEGRITY_FAILED")
                    body = self._audit_body(row)
                    event_hash = hashlib.sha256(body).hexdigest()
                    if not hmac.compare_digest(event_hash, row["event_hash"]):
                        raise AuthBrokerError("AUDIT_INTEGRITY_FAILED")
                    expected_mac = hmac.new(self.secrets.get(row["key_id"]), body, hashlib.sha256).digest()
                    if not hmac.compare_digest(expected_mac, bytes(row["event_mac"])):
                        raise AuthBrokerError("AUDIT_INTEGRITY_FAILED")
                    previous = event_hash
        except AuthBrokerError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AuthBrokerError("STORE_CORRUPT") from exc

    def list_audit(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as db:
            return tuple(dict(row) for row in db.execute("SELECT * FROM auth_audit ORDER BY seq"))

    def _deny_reason(self, row: sqlite3.Row, scope: AuthScope, digest: str, now: int) -> str | None:
        if row["revoked_at_ms"] is not None: return "REVOKED"
        if row["consumed_at_ms"] is not None: return "ALREADY_CONSUMED"
        if row["expires_at_ms"] <= now: return "EXPIRED"
        bindings = ("agent_id", "action", "workspace_id", "project_id")
        if any(not hmac.compare_digest(str(row[k]), str(getattr(scope, k))) for k in bindings):
            return "BINDING_MISMATCH"
        if not hmac.compare_digest(row["task_digest"], digest):
            return "TASK_DIGEST_MISMATCH"
        return None

    def _token_mac(self, token: str, key_id: str) -> bytes:
        return hmac.new(self.secrets.get(key_id), token.encode(), hashlib.sha256).digest()

    def _transaction(self):
        return _ImmediateTransaction(self, self.busy_retries, self.busy_delay)

    def _audit(self, db: sqlite3.Connection, grant_id: str | None, event_type: str, outcome: str,
               error: str | None, run_id: str | None, scope: AuthScope, digest: str,
               details: Mapping[str, Any]) -> None:
        if set(details) - _DETAIL_KEYS:
            raise ValueError("UNSAFE_AUDIT_DETAILS")
        prev = db.execute("SELECT event_hash FROM auth_audit ORDER BY seq DESC LIMIT 1").fetchone()
        values = {
            "event_id": str(uuid.uuid4()), "grant_id": grant_id, "event_type": event_type,
            "outcome": outcome, "error_code": error, "run_id": run_id,
            "agent_id": scope.agent_id, "action": scope.action,
            "workspace_id": scope.workspace_id, "project_id": scope.project_id,
            "task_digest": digest, "occurred_at_ms": _now_ms(),
            "prev_hash": prev[0] if prev else "0" * 64, "key_id": self.key_id,
            "details_json": json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
        }
        body = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        event_hash = hashlib.sha256(body).hexdigest()
        event_mac = hmac.new(self.secrets.get(self.key_id), body, hashlib.sha256).digest()
        db.execute("""INSERT INTO auth_audit(event_id,grant_id,event_type,outcome,error_code,run_id,
          agent_id,action,workspace_id,project_id,task_digest,occurred_at_ms,prev_hash,event_hash,key_id,event_mac,details_json)
          VALUES(:event_id,:grant_id,:event_type,:outcome,:error_code,:run_id,:agent_id,:action,
          :workspace_id,:project_id,:task_digest,:occurred_at_ms,:prev_hash,:event_hash,:key_id,:event_mac,:details_json)""", values | {"event_hash": event_hash, "event_mac": event_mac})

    @staticmethod
    def _audit_body(row: sqlite3.Row) -> bytes:
        fields = ("event_id", "grant_id", "event_type", "outcome", "error_code", "run_id",
                  "agent_id", "action", "workspace_id", "project_id", "task_digest",
                  "occurred_at_ms", "prev_hash", "key_id", "details_json")
        return json.dumps({k: row[k] for k in fields}, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")).encode()


class _ImmediateTransaction:
    def __init__(self, broker: SQLiteAuthBroker, retries: int, delay: float) -> None:
        self.broker, self.retries, self.delay = broker, retries, delay
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.db = self.broker._connect()
        for attempt in range(self.retries + 1):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                return self.db
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == self.retries:
                    self.db.close()
                    raise AuthBrokerError("STORE_BUSY") from exc
                time.sleep(self.delay * (attempt + 1))
        raise AuthBrokerError("STORE_BUSY")

    def __exit__(self, typ, value, traceback) -> bool:
        assert self.db is not None
        try:
            self.db.execute("COMMIT" if typ is None else "ROLLBACK")
        finally:
            self.db.close()
        return False


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def create_production_auth_broker() -> SQLiteAuthBroker:
    """Compose a fail-closed broker; no mock or in-memory fallback exists."""
    path = os.environ.get("PLACHEM_AUTH_BROKER_DB")
    key_id = os.environ.get("PLACHEM_AUTH_BROKER_KEY_ID")
    if not path or not key_id:
        raise AuthBrokerError("AUTH_REQUIRED")
    secret_ref = EnvironmentPepperRef()
    secret_ref.get(key_id)  # fail before opening/creating the production store
    return SQLiteAuthBroker(path, secret_ref, key_id=key_id)
