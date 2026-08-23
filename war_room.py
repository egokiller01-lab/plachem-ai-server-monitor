from __future__ import annotations

import base64
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query


router = APIRouter(prefix="/api/war-room", tags=["war-room"])

PROJECT_ID = "plachem-agent-war-room"
MANYFAST_PROJECT_ID = "1b2eeb07-03bb-4e98-8b52-2f0ae1f716d9"
ALLOWED_PROJECT_IDS = frozenset({PROJECT_ID})
ALLOWED_AGENT_IDS = frozenset({"main", "ERPcoder", "ERPqa"})
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
            can_execute INTEGER NOT NULL DEFAULT 0
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
            ("participant-main", "main", "project_manager", 1, 1, 0),
            ("participant-erpcoder", "ERPcoder", "developer", 1, 0, 0),
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
    if project_id not in ALLOWED_PROJECT_IDS:
        raise HTTPException(status_code=404, detail="Project not found")
    row = connection.execute("SELECT * FROM war_projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


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
def list_projects() -> dict[str, Any]:
    with _connect_readonly() as connection:
        placeholders = ",".join("?" for _ in ALLOWED_PROJECT_IDS)
        rows = connection.execute(
            f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM war_participants x
                    WHERE x.project_id = p.id AND x.principal_id IN ('main', 'ERPcoder', 'ERPqa')) AS participant_count,
                   (SELECT MAX(created_at) FROM war_messages m WHERE m.project_id = p.id) AS last_activity
            FROM war_projects p
            WHERE p.id IN ({placeholders})
            ORDER BY updated_at DESC
            """,
            tuple(ALLOWED_PROJECT_IDS),
        ).fetchall()
    return {"mode": "readonly", "items": _redact([dict(row) for row in rows])}


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
            WHERE project_id = ? AND principal_id IN ('main', 'ERPcoder', 'ERPqa')
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
    return {"mode": "readonly", "items": _redact([dict(row) for row in rows])}


@router.get("/projects/{project_id}/timeline")
def get_timeline(
    project_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    before: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
) -> dict[str, Any]:
    with _connect_readonly() as connection:
        _project_or_404(connection, project_id)
        params: list[Any] = [project_id]
        condition = ""
        if before is not None:
            before_created_at, before_id = _decode_cursor(before)
            condition = "AND (created_at < ? OR (created_at = ? AND id < ?))"
            params.extend([before_created_at, before_created_at, before_id])
        params.append(limit + 1)
        rows = connection.execute(
            f"""
            SELECT * FROM war_messages
            WHERE project_id = ? {condition}
            ORDER BY created_at DESC, id DESC LIMIT ?
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
            WHERE project_id = ? AND principal_id IN ('main', 'ERPcoder', 'ERPqa')
            ORDER BY principal_id
            """,
            (project_id,),
        ).fetchall()
        bindings = connection.execute(
            """
            SELECT project_id, agent_id, session_key, session_id, enabled
            FROM war_project_sessions
            WHERE project_id = ? AND enabled = 1
              AND agent_id IN ('main', 'ERPcoder', 'ERPqa')
            ORDER BY agent_id, session_key, session_id
            """,
            (project_id,),
        ).fetchall()
    by_agent: dict[str, list[sqlite3.Row]] = {agent_id: [] for agent_id in ALLOWED_AGENT_IDS}
    for binding in bindings:
        by_agent.setdefault(str(binding["agent_id"]), []).append(binding)
    return {
        "mode": "readonly",
        "agents": [_redact(_session_summary(row["principal_id"], by_agent.get(row["principal_id"], []))) for row in participants],
        "last_checked": int(time.time()),
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
