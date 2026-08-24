from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request


router = APIRouter(prefix="/api/war-room", tags=["war-room"])

PROJECT_ID = "plachem-agent-war-room"
MANYFAST_PROJECT_ID = "1b2eeb07-03bb-4e98-8b52-2f0ae1f716d9"
ALLOWED_PROJECT_IDS = frozenset({PROJECT_ID})
ALLOWED_AGENT_IDS = frozenset({"main", "ERPcoder", "ERPmanager", "ERPqa"})
MESSAGE_TYPES = frozenset({"instruction", "opinion", "question", "decision", "result", "status", "system"})
DELIVERY_STATUSES = frozenset({"queued", "sent", "received", "responded", "failed", "timed_out", "stopped"})
SCHEMA_TABLES = frozenset(
    {
        "war_projects",
        "war_participants",
        "war_messages",
        "war_project_sessions",
    }
)

SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(token|secret|api[_-]?key|password|credential|authorization|cookie|oauth|private[_-]?key)"
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?ix)"
    r"(?:"
    r"\b(?:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:token|secret|credential|password|api[_-]?key|authorization|cookie|oauth|private[_-]?key)"
    r"\s*[:=]\s*[^\s,;]{4,}"
    r"|\b(?:sk|ghp|xox[baprs])[-_][A-Za-z0-9._-]{8,}"
    r"|\b[A-Za-z0-9]{4,}[-_](?:token|secret|credential|password)\b"
    r")"
)
# Values are treated as sensitive even when they arrive in an innocent field
# such as body/model.  This deliberately errs on the side of redaction for
# bearer-like, opaque credential-looking values.
OPAQUE_SECRET_PATTERN = re.compile(
    r"(?ix)(?:"
    r"\b(?:token|secret|credential|password|api[_-]?key|oauth|cookie)\b"
    r"\s*[-_=:/]\s*[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|ghp|xox[baprs])[-_][A-Za-z0-9._-]{8,}\b"
    r")"
)
MAX_PUBLIC_STRING_LENGTH = 4096
_OPERATIONS_LAST_GOOD: dict[tuple[str, str], dict[str, Any]] = {}


def _openclaw_home() -> Path:
    return Path(os.getenv("OPENCLAW_HOME", str(Path.home() / ".openclaw"))).expanduser()


def _db_path() -> Path:
    configured = os.getenv("PLACHEM_WAR_ROOM_DB")
    if configured:
        return Path(configured).expanduser()
    return _openclaw_home() / "war-room" / "war_room.sqlite3"


def _initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the read model during explicit provisioning only."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS war_projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            manyfast_project_id TEXT NOT NULL,
            manyfast_version TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS war_participants (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES war_projects(id),
            principal_type TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            role TEXT NOT NULL,
            can_read INTEGER NOT NULL DEFAULT 1,
            can_comment INTEGER NOT NULL DEFAULT 0,
            can_approve INTEGER NOT NULL DEFAULT 0,
            can_execute INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS war_messages (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES war_projects(id),
            message_type TEXT NOT NULL,
            author_type TEXT NOT NULL,
            author_id TEXT NOT NULL,
            body TEXT NOT NULL,
            source_session_id TEXT,
            source_message_id TEXT,
            created_at INTEGER NOT NULL,
            correlation_id TEXT,
            redaction_state TEXT NOT NULL DEFAULT 'clean'
        );
        CREATE INDEX IF NOT EXISTS idx_war_messages_project_created_id
            ON war_messages(project_id, created_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS war_project_sessions (
            project_id TEXT NOT NULL REFERENCES war_projects(id),
            agent_id TEXT NOT NULL,
            session_key TEXT,
            session_id TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(project_id, agent_id, session_key, session_id),
            CHECK (session_key IS NOT NULL OR session_id IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS idx_war_project_sessions_scope
            ON war_project_sessions(project_id, agent_id, enabled);
        """
    )


def provision_database(path: str | Path | None = None) -> Path:
    """Provision the War Room read model explicitly, outside every GET path."""
    target = Path(path).expanduser() if path is not None else _db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _initialize_schema(connection)
        now = int(time.time())
        connection.execute(
            """
            INSERT OR IGNORE INTO war_projects
            (id, name, status, manyfast_project_id, manyfast_version, created_at, updated_at)
            VALUES (?, ?, 'planning', ?, 'baseline-2026-08-23', ?, ?)
            """,
            (
                PROJECT_ID,
                "PLACHEM Agent War Room — 멀티에이전트 통합 작전실",
                MANYFAST_PROJECT_ID,
                now,
                now,
            ),
        )
        participants = (
            ("participant-main", "main", "project_manager", 1, 1, 1),
            ("participant-erpcoder", "ERPcoder", "developer", 1, 0, 0),
            ("participant-erpmanager", "ERPmanager", "observer", 1, 0, 0),
            ("participant-erpqa", "ERPqa", "qa", 1, 0, 0),
        )
        for row_id, agent_id, role, can_comment, can_approve, can_execute in participants:
            connection.execute(
                """
                INSERT OR IGNORE INTO war_participants
                (id, project_id, principal_type, principal_id, role,
                 can_read, can_comment, can_approve, can_execute)
                VALUES (?, ?, 'agent', ?, ?, 1, ?, ?, ?)
                """,
                (row_id, PROJECT_ID, agent_id, role, can_comment, can_approve, can_execute),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO war_messages
            (id, project_id, message_type, author_type, author_id, body, created_at,
             correlation_id, redaction_state)
            VALUES ('baseline-imported', ?, 'decision', 'system', 'manyfast', ?, ?,
                    'manyfast-baseline', 'clean')
            """,
            (
                PROJECT_ID,
                "ManyFast PRD·요구사항·기능명세·유저플로우 기준선을 연결했습니다. 실제 실행 기능은 비활성 상태입니다.",
                now,
            ),
        )
        connection.commit()
    # The controlled write model is provisioned explicitly with the read model;
    # request handlers never create schema or data.
    from war_room_actions import provision_action_schema
    provision_action_schema(str(target))
    return target


def _connect_readonly() -> sqlite3.Connection:
    """Open an existing database without creating directories, schema, or rows."""
    path = _db_path()
    if not path.is_file():
        raise HTTPException(status_code=503, detail="War Room data unavailable")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not SCHEMA_TABLES.issubset(table_names):
            connection.close()
            raise HTTPException(status_code=503, detail="War Room data unavailable")
        return connection
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="War Room data unavailable") from exc


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _redact_string(value: str) -> str:
    if len(value) > MAX_PUBLIC_STRING_LENGTH:
        value = value[:MAX_PUBLIC_STRING_LENGTH] + "…"
    if SENSITIVE_VALUE_PATTERN.search(value) or OPAQUE_SECRET_PATTERN.search(value):
        return "***REDACTED***"
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_KEY_PATTERN.search(key_text):
                result[key_text] = None if item is None else "***REDACTED***"
            else:
                result[key_text] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _project_or_404(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM war_projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def _request_principal(request: Request | None, actor: str | None, token: str | None) -> str | None:
    """Resolve the server-side principal for list filtering; never trust actor alone."""
    if request is not None:
        proxy_principal = request.headers.get("X-Authenticated-Principal") or request.headers.get("X-Forwarded-User")
        proxy_secret = os.environ.get("PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET", "")
        presented_proxy_secret = request.headers.get("X-War-Room-Proxy-Secret", "")
        if (
            proxy_secret
            and proxy_principal in ALLOWED_AGENT_IDS
            and hmac.compare_digest(proxy_secret, presented_proxy_secret)
        ):
            return proxy_principal
        cookie = request.cookies.get("war_room_session")
        if cookie:
            try:
                principal, signature = cookie.split(".", 1)
                secret = os.environ.get("PLACHEM_WAR_ROOM_SESSION_SECRET", "")
                if secret and hmac.compare_digest(signature, hmac.new(secret.encode(), principal.encode(), hashlib.sha256).hexdigest()):
                    return principal if principal in ALLOWED_AGENT_IDS else None
            except ValueError:
                pass
    supplied_token = token or (request.headers.get("X-War-Room-Token") if request is not None else None)
    advertised = actor or (request.headers.get("X-War-Room-Actor") if request is not None else None)
    try:
        token_map = json.loads(os.environ.get("PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS", "{}"))
    except json.JSONDecodeError:
        return None
    principal = next((str(key) for key, value in token_map.items() if isinstance(value, str) and value == supplied_token), None)
    return principal if principal and (not advertised or advertised == principal) else None


def _parse_updated_at(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _session_summary(agent_id: str, bindings: list[sqlite3.Row]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "agent_id": agent_id,
        "state": "unmapped" if not bindings else "missing",
        "session_count": 0,
        "mapping_count": len(bindings),
        "latest": None,
    }
    if not bindings:
        # Read-only auto adapter: discover only metadata from the named
        # agent's own sessions index. No session mapping or message data is
        # written, and no arbitrary project/session is exposed.
        sessions_path = _openclaw_home() / "agents" / agent_id / "sessions" / "sessions.json"
        raw = _safe_json(sessions_path) if sessions_path.is_file() else None
        if not isinstance(raw, dict):
            return base
        rows: list[dict[str, Any]] = []
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            updated_at = _parse_updated_at(item.get("updatedAt"))
            if updated_at is None:
                continue
            rows.append({
                "key": str(key),
                "session_id": item.get("sessionId"),
                "status": item.get("status") or "unknown",
                "updated_at": updated_at,
                "model": _redact_string(str(item.get("model") or "unknown")),
            })
        rows.sort(key=lambda item: (item["updated_at"], item["key"]), reverse=True)
        if rows:
            base.update({"state": "available", "session_count": len(rows), "latest": _redact(rows[0]), "adapter": "openclaw-sessions-readonly"})
        return base
    if agent_id not in ALLOWED_AGENT_IDS:
        base.update({"state": "unavailable", "error_code": "agent_not_allowed"})
        return base

    sessions_path = _openclaw_home() / "agents" / agent_id / "sessions" / "sessions.json"
    if not sessions_path.is_file():
        base.update({"state": "missing", "error_code": "sessions_missing"})
        return base
    try:
        raw = json.loads(sessions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        base.update({"state": "corrupt", "error_code": "sessions_corrupt"})
        return base
    if not isinstance(raw, dict):
        base.update({"state": "corrupt", "error_code": "sessions_corrupt"})
        return base

    rows: list[dict[str, Any]] = []
    invalid_count = 0
    unmatched_count = 0
    for binding in bindings:
        session_key = binding["session_key"]
        session_id = binding["session_id"]
        matched = False
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            if session_key is not None and str(key) != str(session_key):
                continue
            if session_id is not None and str(item.get("sessionId")) != str(session_id):
                continue
            matched = True
            updated_at = _parse_updated_at(item.get("updatedAt"))
            if updated_at is None:
                invalid_count += 1
                continue
            rows.append(
                {
                    "key": str(key),
                    "session_id": item.get("sessionId"),
                    "status": item.get("status") or "unknown",
                    "updated_at": updated_at,
                    "model": _redact_string(str(item.get("model") or "unknown")),
                }
            )
        if not matched:
            unmatched_count += 1

    rows.sort(key=lambda item: (item["updated_at"], item["key"]), reverse=True)
    base["session_count"] = len(rows)
    base["latest"] = _redact(rows[0]) if rows else None
    if invalid_count:
        base.update({"state": "invalid", "error_code": "session_metadata_invalid"})
    elif unmatched_count:
        base.update({"state": "available", "unmatched_mapping_count": unmatched_count})
    else:
        base["state"] = "available"
    return base


def _encode_cursor(created_at: int, message_id: str) -> str:
    payload = json.dumps([created_at, message_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[int, str]:
    # A numeric value remains readable for callers of the original draft API;
    # the opaque cursor emitted by this implementation is always composite.
    if value.isdigit():
        return int(value), "\uffff"
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        created_at, message_id = payload
        if isinstance(created_at, bool) or not isinstance(created_at, int) or not isinstance(message_id, str):
            raise ValueError
        return created_at, message_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, base64.binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid timeline cursor") from exc


@router.get("/projects")
def list_projects(request: Request = None, status: str | None = None, q: str | None = None, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect_readonly() as connection:
        principal = _request_principal(request, x_war_room_actor, x_war_room_token)
        if principal:
            rows = connection.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM war_participants x WHERE x.project_id = p.id AND x.active=1) AS participant_count,
                       (SELECT MAX(created_at) FROM war_messages m WHERE m.project_id = p.id) AS last_activity
                FROM war_projects p JOIN war_participants me ON me.project_id=p.id
                WHERE me.principal_id=? AND me.can_read=1 AND me.active=1
                ORDER BY p.updated_at DESC
                """, (principal,)
            ).fetchall()
        else:
            rows = connection.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM war_participants x
                    WHERE x.project_id = p.id AND x.principal_id IN ('main', 'ERPcoder', 'ERPmanager', 'ERPqa')) AS participant_count,
                   (SELECT MAX(created_at) FROM war_messages m WHERE m.project_id = p.id) AS last_activity
            FROM war_projects p
            WHERE p.id IN ('plachem-agent-war-room')
            ORDER BY updated_at DESC
            """,
            ).fetchall()
    # FastAPI may expose Query marker defaults when this route function is
    # called directly by the read-model unit tests. Normalize those markers
    # instead of treating them as user-provided strings.
    status_value = status if isinstance(status, str) else None
    query_value = q if isinstance(q, str) else None
    items = [dict(row) for row in rows if (status_value is None or row["status"] == status_value) and (query_value is None or query_value.lower() in row["name"].lower())]
    return {"mode": "readonly", "items": _redact(items)}


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    with _connect_readonly() as connection:
        project = _project_or_404(connection, project_id)
    return {"mode": "readonly", "project": _redact(dict(project)), "write_actions_enabled": False}


@router.get("/projects/{project_id}/participants")
def get_participants(project_id: str) -> dict[str, Any]:
    with _connect_readonly() as connection:
        _project_or_404(connection, project_id)
        rows = connection.execute(
            """
            SELECT * FROM war_participants
            WHERE project_id = ? AND principal_id IN ('main', 'ERPcoder', 'ERPmanager', 'ERPqa')
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
    return {"mode": "readonly", "items": _redact([dict(row) for row in rows])}


@router.get("/projects/{project_id}/access")
def get_project_access(
    project_id: str,
    request: Request,
    x_war_room_actor: str | None = Header(default=None),
    x_war_room_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return the server-derived permissions used to gate UI controls."""
    with _connect_readonly() as connection:
        _project_or_404(connection, project_id)
        principal = _request_principal(request, x_war_room_actor, x_war_room_token)
        if not principal:
            raise HTTPException(status_code=401, detail="War Room authentication required")
        row = connection.execute(
            "SELECT principal_id, role, active, can_read, can_comment, can_approve, can_execute FROM war_participants WHERE project_id=? AND principal_id=?",
            (project_id, principal),
        ).fetchone()
    if not row or not row["active"] or not row["can_read"]:
        raise HTTPException(status_code=403, detail="War Room permission denied")
    permissions = [name for name, column in (
        ("read", "can_read"), ("comment", "can_comment"),
        ("approve", "can_approve"), ("execute", "can_execute"),
    ) if row[column]]
    if row["role"] == "project_manager" and row["can_execute"]:
        permissions.append("manage")
    return {
        "mode": "readonly", "principal_id": row["principal_id"], "role": row["role"],
        "permissions": permissions,
        "is_representative": row["principal_id"] in {
            value.strip() for value in os.environ.get("PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS", "main").split(",") if value.strip()
        },
        "capabilities": {column: bool(row[column]) for column in ("can_read", "can_comment", "can_approve", "can_execute")},
    }


@router.get("/projects/{project_id}/timeline")
def get_timeline(
    project_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    before: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    message_type: Annotated[str | None, Query(max_length=32)] = None,
    author_id: Annotated[str | None, Query(max_length=128)] = None,
    delivery_status: Annotated[str | None, Query(max_length=32)] = None,
    from_ts: Annotated[int | None, Query(ge=0)] = None,
    to_ts: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, Any]:
    if message_type is not None and message_type not in MESSAGE_TYPES:
        raise HTTPException(status_code=422, detail="Invalid message type")
    if delivery_status is not None and delivery_status not in DELIVERY_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid delivery status")
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        raise HTTPException(status_code=422, detail="Invalid timeline range")
    with _connect_readonly() as connection:
        _project_or_404(connection, project_id)
        params: list[Any] = [project_id]
        conditions = ["m.project_id = ?"]
        if message_type is not None:
            conditions.append("m.message_type = ?")
            params.append(message_type)
        if author_id is not None:
            conditions.append("m.author_id = ?")
            params.append(author_id)
        if delivery_status is not None:
            conditions.append("EXISTS (SELECT 1 FROM war_deliveries ds WHERE ds.message_id=m.id AND ds.status=?)")
            params.append(delivery_status)
        if from_ts is not None:
            conditions.append("m.created_at >= ?")
            params.append(from_ts)
        if to_ts is not None:
            conditions.append("m.created_at <= ?")
            params.append(to_ts)
        if before is not None:
            before_created_at, before_id = _decode_cursor(before)
            conditions.append("(m.created_at < ? OR (m.created_at = ? AND m.id < ?))")
            params.extend([before_created_at, before_created_at, before_id])
        params.append(limit + 1)
        rows = connection.execute(
            f"""
            SELECT m.*, GROUP_CONCAT(DISTINCT d.status) AS delivery_statuses
            FROM war_messages m LEFT JOIN war_deliveries d ON d.message_id=m.id
            WHERE {' AND '.join(conditions)}
            GROUP BY m.id
            ORDER BY m.created_at DESC, m.id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    has_more = len(rows) > limit
    raw_items = [dict(row) for row in rows[:limit]]
    items = _redact(raw_items)
    next_cursor = _encode_cursor(raw_items[-1]["created_at"], raw_items[-1]["id"]) if has_more and raw_items else None
    return {
        "mode": "readonly",
        "items": items,
        "has_more": has_more,
        "next_before": next_cursor,
        "next_cursor": next_cursor,
    }


@router.get("/projects/{project_id}/operations")
def get_operations(project_id: str) -> dict[str, Any]:
    with _connect_readonly() as connection:
        _project_or_404(connection, project_id)
        participants = connection.execute(
            """
            SELECT principal_id FROM war_participants
            WHERE project_id = ? AND principal_id IN ('main', 'ERPcoder', 'ERPmanager', 'ERPqa')
            ORDER BY principal_id
            """,
            (project_id,),
        ).fetchall()
        bindings = connection.execute(
            """
            SELECT project_id, agent_id, session_key, session_id, enabled
            FROM war_project_sessions
            WHERE project_id = ? AND enabled = 1
              AND agent_id IN ('main', 'ERPcoder', 'ERPmanager', 'ERPqa')
            ORDER BY agent_id, session_key, session_id
            """,
            (project_id,),
        ).fetchall()
    by_agent: dict[str, list[sqlite3.Row]] = {agent_id: [] for agent_id in ALLOWED_AGENT_IDS}
    for binding in bindings:
        by_agent.setdefault(str(binding["agent_id"]), []).append(binding)
    checked_at = int(time.time())
    agents = [_redact(_session_summary(row["principal_id"], by_agent.get(row["principal_id"], []))) for row in participants]
    cache_key = (str(_db_path()), project_id)
    available = any(agent.get("state") == "available" for agent in agents)
    if available:
        _OPERATIONS_LAST_GOOD[cache_key] = {"captured_at": checked_at, "agents": agents}
    last_good = _OPERATIONS_LAST_GOOD.get(cache_key)
    return {
        "mode": "readonly",
        "agents": agents,
        "last_checked": checked_at,
        "degraded": bool(last_good and not available),
        "failure_time": checked_at if last_good and not available else None,
        "last_good_snapshot": last_good if last_good and not available else None,
    }


@router.get("/projects/{project_id}/manyfast-baseline")
def get_manyfast_baseline(project_id: str) -> dict[str, Any]:
    with _connect_readonly() as connection:
        project = _project_or_404(connection, project_id)
    return {
        "mode": "readonly",
        "project_id": project_id,
        "manyfast_project_id": project["manyfast_project_id"],
        "version": project["manyfast_version"],
        "counts": {
            "requirements": 9,
            "features": 10,
            "specs": 10,
            "user_flows": 1,
            "wireframe_pages": 6,
        },
        "known_gaps": ["실행 승인 화면", "실행 결과·QA 화면", "중지·재개 화면"],
    }
