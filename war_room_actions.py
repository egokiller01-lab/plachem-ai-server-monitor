from __future__ import annotations

import base64
import hashlib
import json
import os
import hmac
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

import war_room
from war_room_agents import canonical_agent_id, load_agent_catalog
from war_room_adapter import OpenClawSessionAdapter, TestSessionAdapter
from war_room_documents import (
    DocumentRegistrationError,
    context_documents,
    get_document as get_project_document,
    list_project_documents,
    list_versions as list_document_versions,
    provision_document_schema,
    register_document,
)


router = APIRouter(prefix="/api/war-room", tags=["war-room-actions"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS war_tasks (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES war_projects(id), source_message_id TEXT UNIQUE,
 assignee_agent_id TEXT, reviewer_agent_id TEXT, scope TEXT NOT NULL CHECK(length(scope) BETWEEN 1 AND 4096), status TEXT NOT NULL
 CHECK(status IN ('draft','awaiting_approval','approved','running','qa','completed','stopped','stop_unconfirmed','rework_required')),
 manyfast_version TEXT NOT NULL, document_version TEXT, call_limit INTEGER, turn_limit INTEGER,
 execution_mode TEXT NOT NULL DEFAULT 'LEGACY' CHECK(execution_mode IN ('LEGACY','FAST_GATEWAY')),
 deadline_at INTEGER, revision INTEGER NOT NULL DEFAULT 1, qa_cycle INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_approvals (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES war_tasks(id), approver_id TEXT NOT NULL,
 decision TEXT NOT NULL, scope_hash TEXT NOT NULL, document_version TEXT NOT NULL,
 assignee_agent_id TEXT, target_set_hash TEXT NOT NULL DEFAULT '', expires_at INTEGER, revoked_at INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_evidence (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES war_tasks(id), evidence_type TEXT NOT NULL,
 uri TEXT NOT NULL, summary TEXT NOT NULL, sha256 TEXT, task_revision INTEGER NOT NULL DEFAULT 1,
 scope_hash TEXT NOT NULL DEFAULT '', document_version TEXT NOT NULL DEFAULT '', qa_cycle INTEGER NOT NULL DEFAULT 0,
 run_id TEXT, source_command TEXT, expected_contains TEXT, immutable INTEGER NOT NULL DEFAULT 0,
 contract_evidence_id TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_audit_events (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES war_projects(id), actor_id TEXT NOT NULL,
 event_type TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
 payload_redacted TEXT NOT NULL, correlation_id TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_idempotency_keys (
 actor_id TEXT NOT NULL, scope TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(actor_id, scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS war_deliveries (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES war_messages(id), agent_id TEXT NOT NULL,
 task_revision INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL CHECK(status IN ('queued','sent','received','responded','failed','timed_out','stopped')),
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0), max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 20),
 sent_at INTEGER, received_at INTEGER,
 responded_at INTEGER, error_code TEXT, run_id TEXT, response_message_id TEXT,
 session_key TEXT, session_id TEXT, correlation_id TEXT,
 next_attempt_at INTEGER, deadline_at INTEGER,
 retry_count INTEGER NOT NULL DEFAULT 0, error_class TEXT, last_error_at INTEGER,
 created_at INTEGER NOT NULL, UNIQUE(message_id, agent_id, task_revision)
);
CREATE TABLE IF NOT EXISTS war_task_calls (
 task_id TEXT NOT NULL REFERENCES war_tasks(id), task_revision INTEGER NOT NULL DEFAULT 1,
 call_count INTEGER NOT NULL DEFAULT 0,
 turn_count INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
 ,PRIMARY KEY(task_id,task_revision)
);
CREATE TABLE IF NOT EXISTS war_task_agents (
 task_id TEXT NOT NULL REFERENCES war_tasks(id), agent_id TEXT NOT NULL,
 PRIMARY KEY(task_id,agent_id)
);
CREATE TABLE IF NOT EXISTS war_grounding_packets (
 task_id TEXT PRIMARY KEY REFERENCES war_tasks(id), packet_json TEXT NOT NULL,
 packet_hash TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_session_integrity (
 evidence_id TEXT PRIMARY KEY REFERENCES war_evidence(id), task_id TEXT NOT NULL REFERENCES war_tasks(id),
 scope TEXT NOT NULL, pre_count INTEGER NOT NULL, post_count INTEGER NOT NULL,
 changed_count INTEGER NOT NULL, deleted_count INTEGER NOT NULL, uncertain_count INTEGER NOT NULL,
 mtime_encoding TEXT NOT NULL, verified_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_project_control (
 project_id TEXT PRIMARY KEY REFERENCES war_projects(id), archived_at INTEGER,
 stop_requested_at INTEGER, stop_deadline INTEGER, stop_state TEXT NOT NULL DEFAULT 'running'
 CHECK(stop_state IN ('running','stop_requested','stopped','stop_unconfirmed','stop_failed')),
 updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_qa_verdicts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES war_tasks(id), qa_principal TEXT NOT NULL,
 verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL','REWORK')), evidence_profile TEXT NOT NULL,
 signature TEXT NOT NULL, signed_payload TEXT NOT NULL, task_revision INTEGER NOT NULL DEFAULT 1,
 scope_hash TEXT NOT NULL DEFAULT '', document_version TEXT NOT NULL DEFAULT '', qa_cycle INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_representative_approvals (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES war_tasks(id), representative_id TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')), task_revision INTEGER NOT NULL,
 scope_hash TEXT NOT NULL, document_version TEXT NOT NULL, qa_cycle INTEGER NOT NULL,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_manyfast_refs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES war_projects(id), task_id TEXT,
 manyfast_project_id TEXT NOT NULL, document_version TEXT NOT NULL, linked_by TEXT NOT NULL,
 created_at INTEGER NOT NULL, drift_status TEXT NOT NULL DEFAULT 'current', previous_document_version TEXT
);
CREATE TABLE IF NOT EXISTS war_manyfast_snapshots (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES war_projects(id), document_version TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, is_last_good INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_execution_runs (
 core_run_id TEXT PRIMARY KEY, war_project_id TEXT NOT NULL REFERENCES war_projects(id),
 war_task_id TEXT NOT NULL REFERENCES war_tasks(id), agent_id TEXT NOT NULL,
 openclaw_run_id TEXT UNIQUE, session_key TEXT, run_status TEXT NOT NULL,
 runtime_seconds REAL, result_summary TEXT, result_json TEXT, evidence_json TEXT, artifacts_json TEXT,
 raw_response TEXT, rejected_result_json TEXT, validation_error TEXT,
 policy_status TEXT, cancel_reason TEXT, escalation_required INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS war_execution_units (
 execution_id TEXT PRIMARY KEY, war_project_id TEXT NOT NULL REFERENCES war_projects(id),
 war_task_id TEXT NOT NULL REFERENCES war_tasks(id), correlation_id TEXT NOT NULL,
 agent_id TEXT NOT NULL, depends_on_execution_ids TEXT NOT NULL DEFAULT '[]',
 core_run_id TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_war_execution_units_task ON war_execution_units(war_project_id,war_task_id);
CREATE INDEX IF NOT EXISTS idx_war_tasks_project_status ON war_tasks(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_war_audit_project_created ON war_audit_events(project_id, created_at DESC, id DESC);
CREATE TRIGGER IF NOT EXISTS war_audit_no_update BEFORE UPDATE ON war_audit_events BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_audit_no_delete BEFORE DELETE ON war_audit_events BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_evidence_no_update BEFORE UPDATE ON war_evidence BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_evidence_no_delete BEFORE DELETE ON war_evidence BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_instruction_no_update BEFORE UPDATE ON war_messages
WHEN OLD.message_type = 'instruction' BEGIN SELECT RAISE(ABORT, 'instruction messages are immutable'); END;
CREATE TRIGGER IF NOT EXISTS war_instruction_no_delete BEFORE DELETE ON war_messages
WHEN OLD.message_type = 'instruction' BEGIN SELECT RAISE(ABORT, 'instruction messages are immutable'); END;
"""

ROLE_PERMISSIONS = {
    "project_manager": {"read", "comment", "approve", "execute", "manage"},
    "developer": {"read", "comment"},
    "qa": {"read", "comment"},
    "observer": {"read"},
}
WRITE_ACTIONS = {"comment", "approve", "execute", "manage"}
_EXECUTION_ORCHESTRATORS: dict[str, Any] = {}
_EXECUTION_ORCHESTRATOR_LOCK = threading.RLock()


def _delivery_state(row: sqlite3.Row | dict[str, Any]) -> str:
    value = row["error_class"] if isinstance(row, sqlite3.Row) and "error_class" in row.keys() else row.get("error_class")
    status = row["status"] if isinstance(row, sqlite3.Row) else row.get("status")
    return "system_error" if value == "system_error" else str(status or "unknown")


def _delivery_next_action(row: sqlite3.Row | dict[str, Any]) -> str:
    state = _delivery_state(row)
    if state == "system_error":
        attempts = int((row["retry_count"] if isinstance(row, sqlite3.Row) and "retry_count" in row.keys() else row.get("retry_count", row.get("attempt_count", 0))) or 0)
        maximum = int((row["max_attempts"] if isinstance(row, sqlite3.Row) and "max_attempts" in row.keys() else row.get("max_attempts", 3)) or 3)
        status = row["status"] if isinstance(row, sqlite3.Row) else row.get("status")
        return "재시도 대기" if attempts < maximum and status == "queued" else "원인 확인 후 수동 재전송 또는 담당자 교체"
    if state == "responded":
        return "결과와 QA 판정 비교"
    if state == "stopped":
        return "중지 확인"
    return "처리 완료 대기"


PROCESS_BOARD_STATES = ("WAITING", "READY", "RUNNING", "PASS", "FAIL", "REWORK", "BLOCKED", "SUPERSEDED", "DONE")


def _process_board_state(status: str | None, delivery_system_error: bool,
                         latest_qa_verdict: str | None, superseded: bool = False) -> str:
    """Map existing lifecycle data to one deterministic read-only board state.

    Precedence is intentionally fixed: delivery-derived system_error is a
    transport blocker; completed and stopped terminal boundaries follow; an
    explicit rework lifecycle wins; then the latest QA verdict is projected;
    finally the ordinary lifecycle statuses are mapped. No state is written
    and no new lifecycle value is introduced.
    """
    if superseded:
        return "SUPERSEDED"
    if delivery_system_error:
        return "BLOCKED"
    if status == "completed":
        return "DONE"
    if status in {"stopped", "stop_unconfirmed"}:
        return "BLOCKED"
    if status == "rework_required":
        return "REWORK"
    verdict = str(latest_qa_verdict or "").upper()
    if verdict in {"FAIL", "REWORK", "PASS"}:
        return verdict
    if status in {"draft", "awaiting_approval", "qa"}:
        return "WAITING"
    if status == "approved":
        return "READY"
    if status == "running":
        return "RUNNING"
    return "WAITING"


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _process_board_projection(con: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        """SELECT t.*,m.original_body AS instruction_body,m.source_session_id
           FROM war_tasks t LEFT JOIN war_messages m ON m.id=t.source_message_id
           WHERE t.project_id=? ORDER BY t.updated_at DESC,t.id DESC""",
        (project_id,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for task in rows:
        task_id = task["id"]
        task_revision = int(task["revision"] or 1)
        verdict = con.execute(
            """SELECT verdict,qa_principal,created_at FROM war_qa_verdicts
               WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        deliveries = con.execute(
            """SELECT d.*,rm.body AS response_body FROM war_deliveries d
               LEFT JOIN war_messages rm ON rm.id=d.response_message_id
               WHERE d.message_id=? ORDER BY d.created_at DESC,d.id DESC""",
            (task["source_message_id"],),
        ).fetchall() if task["source_message_id"] else []
        delivery_system_error = any(_delivery_state(delivery) == "system_error" for delivery in deliveries)
        superseded_row = con.execute(
            """SELECT payload_redacted,created_at FROM war_audit_events
               WHERE target_type='task' AND target_id=? AND event_type='task_superseded'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        superseded_payload = _json_object(superseded_row["payload_redacted"]) if superseded_row else {}
        state = _process_board_state(
            task["status"], delivery_system_error,
            verdict["verdict"] if verdict else None,
            superseded=bool(superseded_row),
        )

        packet_row = con.execute(
            "SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)
        ).fetchone()
        packet = _json_object(packet_row[0] if packet_row else None)
        conditions = packet.get("completion_conditions")
        pass_condition = conditions if isinstance(conditions, list) else None

        run = con.execute(
            """SELECT session_key,result_summary,result_json,run_status,updated_at
               FROM war_execution_runs WHERE war_task_id=?
               ORDER BY updated_at DESC,core_run_id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        latest_delivery = deliveries[0] if deliveries else None
        output = None
        if latest_delivery and latest_delivery["response_body"] is not None:
            output = latest_delivery["response_body"]
        elif run and (run["result_summary"] is not None or run["result_json"] is not None):
            output = run["result_summary"] if run["result_summary"] is not None else _json_object(run["result_json"])

        session = None
        for delivery in deliveries:
            if delivery["session_key"] is not None or delivery["session_id"] is not None:
                session = {"session_key": delivery["session_key"], "session_id": delivery["session_id"]}
                break
        if session is None and run and run["session_key"] is not None:
            session = {"session_key": run["session_key"], "session_id": None}
        if session is None and task["source_session_id"] is not None:
            session = {"session_key": None, "session_id": task["source_session_id"]}

        predecessor_ids: set[str] = set()
        for unit in con.execute(
            "SELECT depends_on_execution_ids FROM war_execution_units WHERE war_task_id=?",
            (task_id,),
        ).fetchall():
            try:
                dependency_ids = json.loads(unit[0] or "[]")
            except (TypeError, ValueError):
                dependency_ids = []
            if not isinstance(dependency_ids, list):
                continue
            for execution_id in dependency_ids:
                predecessor = con.execute(
                    "SELECT war_task_id FROM war_execution_units WHERE execution_id=? LIMIT 1",
                    (execution_id,),
                ).fetchone()
                if predecessor and predecessor[0] != task_id:
                    predecessor_ids.add(predecessor[0])

        rework_count = con.execute(
            """SELECT COUNT(*) FROM war_audit_events
               WHERE target_type='task' AND target_id=? AND event_type IN
               ('qa_verdict_rework_required','terminal_response_validation_rework',
                'representative_completion_rejected')""",
            (task_id,),
        ).fetchone()[0]
        items.append({
            "task_id": task_id,
            "step_id": None,
            "step_name": None,
            "assigned_agent": task["assignee_agent_id"],
            "assigned_agents": [row[0] for row in con.execute(
                "SELECT agent_id FROM war_task_agents WHERE task_id=? ORDER BY agent_id", (task_id,)
            ).fetchall()],
            "mapped_state": state,
            "lifecycle_status": task["status"],
            "predecessor_step": sorted(predecessor_ids) or None,
            "input": task["instruction_body"],
            "output": output,
            "pass_condition": pass_condition,
            "session": session,
            "rework_count": int(rework_count),
            "reviewer_agent": task["reviewer_agent_id"],
            "revision": task_revision,
            "qa_cycle": int(task["qa_cycle"] or 0),
            "latest_qa_verdict": dict(verdict) if verdict else None,
            "delivery_system_error": delivery_system_error,
            "superseded": bool(superseded_row),
            "superseded_by": superseded_payload.get("replacement_task_id"),
            "superseded_reason": superseded_payload.get("reason"),
            "superseded_at": superseded_row["created_at"] if superseded_row else None,
            "updated_at": task["updated_at"],
        })
    return items


TRANSITIONS = {
    "draft": {"awaiting_approval"},
    "awaiting_approval": {"approved", "draft"},
    "approved": {"running", "stopped"},
    "running": {"qa", "stopped", "stop_unconfirmed"},
    "stopped": {"awaiting_approval"},
    "stop_unconfirmed": {"awaiting_approval"},
    "qa": {"completed", "rework_required"},
    "rework_required": {"awaiting_approval"},
}


def _now() -> int:
    return int(time.time())


@contextmanager
def _transaction_connection(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


@contextmanager
def _connect_rw():
    path = war_room._db_path()
    if not path.is_file():
        raise HTTPException(503, "War Room data unavailable")
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 2000")
        with con:
            yield con
    finally:
        con.close()


def _connect_ro() -> sqlite3.Connection:
    """Open the War Room database without any write capability."""
    path = war_room._db_path()
    if not path.is_file():
        raise HTTPException(503, "War Room data unavailable")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 2000")
    return con


def provision_action_schema(path: str | None = None) -> str:
    target = path or str(war_room._db_path())
    with _transaction_connection(target) as con:
        con.executescript(SCHEMA)
        provision_document_schema(con)
        participant_columns = {row[1] for row in con.execute("PRAGMA table_info(war_participants)")}
        if "active" not in participant_columns:
            con.execute("ALTER TABLE war_participants ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        delivery_columns = {row[1] for row in con.execute("PRAGMA table_info(war_deliveries)")}
        delivery_sql_row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='war_deliveries'"
        ).fetchone()
        delivery_sql = str(delivery_sql_row[0] or "") if delivery_sql_row else ""
        if "task_revision" not in delivery_columns or "UNIQUE(message_id, agent_id)" in delivery_sql:
            # Rework requires a fresh delivery without rewriting the previous
            # attempt.  Rebuild the table once to version deliveries by task
            # revision while preserving every historical row and identifier.
            con.execute("ALTER TABLE war_deliveries RENAME TO war_deliveries_legacy_generation")
            con.execute("""CREATE TABLE war_deliveries (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES war_messages(id), agent_id TEXT NOT NULL,
                task_revision INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL CHECK(status IN ('queued','sent','received','responded','failed','timed_out','stopped')),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 20),
                sent_at INTEGER, received_at INTEGER, responded_at INTEGER, error_code TEXT,
                run_id TEXT, response_message_id TEXT, session_key TEXT, session_id TEXT,
                correlation_id TEXT, next_attempt_at INTEGER, deadline_at INTEGER,
                retry_count INTEGER NOT NULL DEFAULT 0, error_class TEXT, last_error_at INTEGER,
                claim_token TEXT, claim_expires_at INTEGER, stop_cycle_at INTEGER,
                created_at INTEGER NOT NULL, UNIQUE(message_id, agent_id, task_revision)
            )""")
            legacy_columns = {row[1] for row in con.execute("PRAGMA table_info(war_deliveries_legacy_generation)")}
            target_columns = [row[1] for row in con.execute("PRAGMA table_info(war_deliveries)")]
            copy_columns = [column for column in target_columns if column in legacy_columns]
            column_list = ",".join(copy_columns)
            con.execute(
                f"INSERT INTO war_deliveries ({column_list}) SELECT {column_list} FROM war_deliveries_legacy_generation"
            )
            con.execute("DROP TABLE war_deliveries_legacy_generation")
            delivery_columns = {row[1] for row in con.execute("PRAGMA table_info(war_deliveries)")}
        if "max_attempts" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3")
        if "next_attempt_at" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN next_attempt_at INTEGER")
        if "deadline_at" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN deadline_at INTEGER")
        if "run_id" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN run_id TEXT")
        if "response_message_id" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN response_message_id TEXT")
        if "claim_token" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN claim_token TEXT")
        if "claim_expires_at" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN claim_expires_at INTEGER")
        if "stop_cycle_at" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN stop_cycle_at INTEGER")
        if "retry_count" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "error_class" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN error_class TEXT")
        if "last_error_at" not in delivery_columns:
            con.execute("ALTER TABLE war_deliveries ADD COLUMN last_error_at INTEGER")
        for column, definition in (("session_key", "TEXT"), ("session_id", "TEXT"), ("correlation_id", "TEXT")):
            if column not in delivery_columns:
                con.execute(f"ALTER TABLE war_deliveries ADD COLUMN {column} {definition}")
        con.execute("UPDATE war_deliveries SET retry_count=attempt_count WHERE retry_count=0 AND attempt_count>0")
        call_columns = {row[1] for row in con.execute("PRAGMA table_info(war_task_calls)")}
        if "task_revision" not in call_columns:
            # Legacy counters are aggregate-only. Preserve them as revision 1
            # history, then account for every subsequent execution generation
            # independently.
            con.execute("ALTER TABLE war_task_calls RENAME TO war_task_calls_legacy_generation")
            con.execute("""CREATE TABLE war_task_calls (
                task_id TEXT NOT NULL REFERENCES war_tasks(id),
                task_revision INTEGER NOT NULL DEFAULT 1,
                call_count INTEGER NOT NULL DEFAULT 0,
                turn_count INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(task_id,task_revision)
            )""")
            con.execute("""INSERT INTO war_task_calls(task_id,task_revision,call_count,turn_count,updated_at)
                           SELECT task_id,1,call_count,turn_count,updated_at
                           FROM war_task_calls_legacy_generation""")
            con.execute("DROP TABLE war_task_calls_legacy_generation")
        reference_columns = {row[1] for row in con.execute("PRAGMA table_info(war_manyfast_refs)")}
        if "drift_status" not in reference_columns:
            con.execute("ALTER TABLE war_manyfast_refs ADD COLUMN drift_status TEXT NOT NULL DEFAULT 'current'")
        if "previous_document_version" not in reference_columns:
            con.execute("ALTER TABLE war_manyfast_refs ADD COLUMN previous_document_version TEXT")
        message_columns = {row[1] for row in con.execute("PRAGMA table_info(war_messages)")}
        if "original_body" not in message_columns:
            con.execute("ALTER TABLE war_messages ADD COLUMN original_body TEXT")
        task_columns = {row[1] for row in con.execute("PRAGMA table_info(war_tasks)")}
        if "reviewer_agent_id" not in task_columns:
            con.execute("ALTER TABLE war_tasks ADD COLUMN reviewer_agent_id TEXT")
        for column, definition in (("document_version", "TEXT"), ("call_limit", "INTEGER"), ("turn_limit", "INTEGER"), ("execution_mode", "TEXT NOT NULL DEFAULT 'LEGACY'"), ("deadline_at", "INTEGER"), ("revision", "INTEGER NOT NULL DEFAULT 1"), ("qa_cycle", "INTEGER NOT NULL DEFAULT 0")):
            if column not in task_columns:
                con.execute(f"ALTER TABLE war_tasks ADD COLUMN {column} {definition}")
        for table in ("war_evidence", "war_qa_verdicts"):
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            for column, definition in (("task_revision", "INTEGER NOT NULL DEFAULT 1"), ("scope_hash", "TEXT NOT NULL DEFAULT ''"), ("document_version", "TEXT NOT NULL DEFAULT ''"), ("qa_cycle", "INTEGER NOT NULL DEFAULT 0")):
                if column not in columns:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        evidence_columns = {row[1] for row in con.execute("PRAGMA table_info(war_evidence)")}
        for column, definition in (("run_id", "TEXT"), ("source_command", "TEXT"), ("expected_contains", "TEXT"), ("immutable", "INTEGER NOT NULL DEFAULT 0"), ("contract_evidence_id", "TEXT")):
            if column not in evidence_columns:
                con.execute(f"ALTER TABLE war_evidence ADD COLUMN {column} {definition}")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_war_evidence_identity ON war_evidence(task_id, run_id, id) WHERE run_id IS NOT NULL")
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_war_evidence_contract_generation
                       ON war_evidence(task_id,task_revision,qa_cycle,contract_evidence_id)
                       WHERE contract_evidence_id IS NOT NULL""")
        execution_columns = {row[1] for row in con.execute("PRAGMA table_info(war_execution_runs)")}
        for column in ("raw_response", "rejected_result_json", "validation_error"):
            if column not in execution_columns:
                con.execute(f"ALTER TABLE war_execution_runs ADD COLUMN {column} TEXT")
        execution_columns = {row[1] for row in con.execute("PRAGMA table_info(war_execution_runs)")}
        if "result_summary" not in execution_columns:
            con.execute("ALTER TABLE war_execution_runs ADD COLUMN result_summary TEXT")
        approval_columns = {row[1] for row in con.execute("PRAGMA table_info(war_approvals)")}
        if "target_set_hash" not in approval_columns:
            con.execute("ALTER TABLE war_approvals ADD COLUMN target_set_hash TEXT NOT NULL DEFAULT ''")
        duplicates = con.execute("SELECT source_message_id FROM war_tasks WHERE source_message_id IS NOT NULL GROUP BY source_message_id HAVING COUNT(*)>1").fetchone()
        if duplicates:
            raise RuntimeError("duplicate task source_message_id prevents safe provisioning")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_war_tasks_source_message ON war_tasks(source_message_id) WHERE source_message_id IS NOT NULL")
        session_columns = {row[1] for row in con.execute("PRAGMA table_info(war_project_sessions)")}
        for column, definition in (("purpose", "TEXT NOT NULL DEFAULT 'work'"), ("disposable", "INTEGER NOT NULL DEFAULT 0")):
            if column not in session_columns:
                con.execute(f"ALTER TABLE war_project_sessions ADD COLUMN {column} {definition}")
        con.execute("INSERT OR IGNORE INTO war_task_agents(task_id,agent_id) SELECT id,assignee_agent_id FROM war_tasks WHERE assignee_agent_id IS NOT NULL")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_war_participants_project_principal ON war_participants(project_id,principal_id)")
        con.execute("INSERT OR IGNORE INTO war_project_control(project_id,updated_at) SELECT id, strftime('%s','now') FROM war_projects")
        from war_room_stage_recovery import ensure_schema
        ensure_schema(con)
    return target


def _adapter() -> TestSessionAdapter | OpenClawSessionAdapter:
    if os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER") == "1":
        return TestSessionAdapter()
    from war_room_runtime import get_runtime
    return get_runtime().adapter


def _adapter_for_mode(mode: str, db_path: str) -> Any:
    if mode != "FAST_GATEWAY":
        return _adapter()
    from war_room_runtime import get_runtime
    return get_runtime().adapter_for(mode, db_path)


def _execution_mode(value: Any) -> str:
    mode = str(value or "FAST_GATEWAY").strip().upper()
    if mode not in {"LEGACY", "FAST_GATEWAY"}:
        raise HTTPException(422, "execution_mode must be LEGACY or FAST_GATEWAY")
    return mode


def _control(con: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM war_project_control WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        con.execute("INSERT INTO war_project_control(project_id,updated_at) VALUES (?,?)", (project_id, _now()))
        row = con.execute("SELECT * FROM war_project_control WHERE project_id=?", (project_id,)).fetchone()
    return row


def _require_not_stopped(con: sqlite3.Connection, project_id: str) -> None:
    state = _control(con, project_id)["stop_state"]
    if state != "running":
        raise HTTPException(409, f"project stop barrier active: {state}")


def _require_mutable_project(con: sqlite3.Connection, project_id: str) -> None:
    row = con.execute("SELECT status FROM war_projects WHERE id=?", (project_id,)).fetchone()
    if row and row["status"] == "archived":
        raise HTTPException(409, "archived project is immutable")


def _qa_signature(payload: str) -> str:
    secret = os.environ.get("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET")
    if not secret:
        raise HTTPException(503, "QA signing is unavailable")
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _representative_principals() -> set[str]:
    return war_room.representative_principals()


def _require_representative(actor: str) -> None:
    if actor not in _representative_principals():
        raise HTTPException(403, "representative principal required; server-side identity mapping is not configured")


def _eligible_agent_ids(con: sqlite3.Connection, project_id: str, required_capabilities: list[str] | None = None) -> list[str]:
    required = {item.casefold() for item in (required_capabilities or []) if item}
    participants = {
        row[0] for row in con.execute(
            "SELECT principal_id FROM war_participants WHERE project_id=? AND active=1 AND can_comment=1",
            (project_id,),
        )
    }
    return sorted(
        agent_id for agent_id, entry in load_agent_catalog().items()
        if entry.execution_eligible
        and agent_id in participants
        and required.issubset({item.casefold() for item in entry.capabilities})
    )


def _require_execution_agents(con: sqlite3.Connection, project_id: str, agents: list[str]) -> None:
    eligible = set(_eligible_agent_ids(con, project_id))
    if not agents or any(agent not in eligible for agent in agents):
        raise HTTPException(409, "every execution agent must be registered, enabled, Gateway-allowed, capable, and an active project participant")


def _require_independent_reviewer(reviewer: str | None, agents: list[str], *, required: bool = True) -> None:
    """Keep the approved reviewer outside the complete execution target set."""
    if (required and reviewer is None) or reviewer in set(agents):
        raise HTTPException(422, "reviewer must be independent from every execution agent")


def _stop_cycle_state(con: sqlite3.Connection, project_id: str, cycle_at: int) -> str:
    """Aggregate every delivery touched by one project stop request."""
    rows = con.execute(
        """SELECT d.status FROM war_deliveries d
           JOIN war_messages m ON m.id=d.message_id
           WHERE m.project_id=? AND d.stop_cycle_at=?""",
        (project_id, cycle_at),
    ).fetchall()
    statuses = [str(row[0]) for row in rows]
    if not statuses or any(status in {"queued", "sent", "received"} for status in statuses):
        return "stop_unconfirmed"
    if any(status in {"failed", "timed_out"} for status in statuses):
        return "stop_failed"
    return "stopped" if all(status == "stopped" for status in statuses) else "stop_unconfirmed"


def _actor(
    con: sqlite3.Connection,
    actor_id: str | None,
    permission: str,
    project_id: str,
    actor_token: str | None,
    request: Request | None = None,
) -> str:
    # The principal comes from a trusted reverse-proxy header, a signed
    # HttpOnly server session, or the server-side token map used by API clients.
    authenticated = war_room._request_principal(request, actor_id, actor_token)
    if not authenticated or not war_room._known_principal(authenticated):
        raise HTTPException(401, "Authenticated War Room actor required")
    if actor_id is not None and actor_id != authenticated:
        raise HTTPException(401, "Actor header does not match authenticated principal")
    actor_id = authenticated
    row = con.execute(
        "SELECT role, can_read, can_comment, can_approve, can_execute FROM war_participants WHERE project_id=? AND principal_id=? AND active=1",
        (project_id, actor_id),
    ).fetchone()
    capability_column = {"read":"can_read", "comment":"can_comment", "approve":"can_approve", "execute":"can_execute", "manage":"can_execute"}.get(permission)
    if not row or not row["can_read"] or (capability_column and not row[capability_column]) or permission not in ROLE_PERMISSIONS.get(row["role"], set()):
        raise HTTPException(403, "War Room permission denied")
    return actor_id


def _redacted_payload(payload: Any) -> str:
    return json.dumps(war_room._redact(payload), ensure_ascii=False, sort_keys=True)


def _audit(con: sqlite3.Connection, project_id: str, actor: str, event: str, target_type: str, target_id: str, payload: Any, correlation: str) -> None:
    con.execute(
        "INSERT INTO war_audit_events VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), project_id, actor, event, target_type, target_id, _redacted_payload(payload), correlation, _now()),
    )


def _idem(con: sqlite3.Connection, actor: str, key: str | None, scope: str, payload: Any) -> dict[str, Any] | None:
    if not key or len(key) > 128:
        raise HTTPException(400, "Idempotency-Key header required")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    row = con.execute("SELECT request_hash,response_json FROM war_idempotency_keys WHERE actor_id=? AND scope=? AND idempotency_key=?", (actor, scope, key)).fetchone()
    if not row: return None
    if row[0] != digest: raise HTTPException(409, "Idempotency-Key payload mismatch")
    return json.loads(row[1])


def _save_idem(con: sqlite3.Connection, actor: str, key: str, scope: str, payload: Any, response: dict[str, Any]) -> None:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    con.execute("INSERT INTO war_idempotency_keys VALUES (?,?,?,?,?,?)", (actor, scope, key, digest, json.dumps(response, ensure_ascii=False), _now()))


def _validated_task_payload(body: dict[str, Any], project: sqlite3.Row, now: int) -> tuple[str, str, int, int, int, str]:
    scope = body.get("scope")
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 4096:
        raise HTTPException(422, "scope must be 1..4096 characters")
    assignee = _canonical_agent_id(body.get("assignee_agent_id"), "assignee_agent_id")
    if assignee is None:
        raise HTTPException(422, "assignee not allowed")
    call_limit, turn_limit = body.get("call_limit"), body.get("turn_limit")
    for value, label, maximum in ((call_limit, "call_limit", 100), (turn_limit, "turn_limit", 1000)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
            raise HTTPException(422, f"{label} must be a bounded positive integer")
    deadline_at = body.get("deadline_at")
    if isinstance(deadline_at, bool) or not isinstance(deadline_at, int) or deadline_at <= now or deadline_at > now + 604800:
        raise HTTPException(422, "deadline_at must be within the next 7 days")
    document_version = body.get("document_version")
    if not isinstance(document_version, str) or not document_version.strip() or len(document_version) > 128:
        raise HTTPException(422, "document_version required")
    document_version = document_version.strip()
    if document_version != project["manyfast_version"]:
        raise HTTPException(409, "document_version does not match current project baseline")
    return scope, assignee, call_limit, turn_limit, deadline_at, document_version


async def _body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise HTTPException(400, "JSON body required") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "JSON object required")
    return value


_FRESH_CONTEXT_TTL_SECONDS = 120
_FRESH_CONTEXT_ACTIONS = {
    "project_stop": "project",
    "project_resume": "project",
    "task_stop": "task",
    "task_resume_qa": "task",
    "task_approve_execute": "task",
    "task_supersede": "task",
    "representative_completion": "task",
}


def _fresh_context_bypassed() -> bool:
    return (
        os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER") == "1"
        and os.environ.get("PLACHEM_WAR_ROOM_TEST_ENFORCE_FRESH_CONTEXT") != "1"
    )


def _fresh_context_secret() -> bytes:
    secret = os.environ.get("PLACHEM_WAR_ROOM_SESSION_SECRET")
    if not secret:
        raise HTTPException(503, "fresh context signing is unavailable")
    return secret.encode()


def _project_state_fingerprint(con: sqlite3.Connection, project_id: str) -> str:
    project = con.execute(
        "SELECT status,manyfast_version,updated_at FROM war_projects WHERE id=?", (project_id,),
    ).fetchone()
    if not project:
        raise HTTPException(404, "Project not found")
    control = con.execute(
        "SELECT stop_state,stop_requested_at,stop_deadline,updated_at FROM war_project_control WHERE project_id=?",
        (project_id,),
    ).fetchone()
    task_stats = con.execute(
        """SELECT COUNT(*),COALESCE(MAX(updated_at),0),COALESCE(SUM(revision),0),COALESCE(SUM(qa_cycle),0)
           FROM war_tasks WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    audit_stats = con.execute(
        "SELECT COUNT(*),COALESCE(MAX(created_at),0) FROM war_audit_events WHERE project_id=?",
        (project_id,),
    ).fetchone()
    approval_stats = con.execute(
        """SELECT COUNT(*),COALESCE(MAX(a.created_at),0),COALESCE(MAX(a.revoked_at),0)
           FROM war_approvals a JOIN war_tasks t ON t.id=a.task_id WHERE t.project_id=?""",
        (project_id,),
    ).fetchone()
    delivery_counts = con.execute(
        """SELECT d.status,COUNT(*) FROM war_deliveries d
           JOIN war_messages m ON m.id=d.message_id
           WHERE m.project_id=? GROUP BY d.status ORDER BY d.status""",
        (project_id,),
    ).fetchall()
    delivery_stamp = con.execute(
        """SELECT COALESCE(MAX(COALESCE(d.last_error_at,d.responded_at,d.received_at,d.sent_at,d.created_at)),0)
           FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id WHERE m.project_id=?""",
        (project_id,),
    ).fetchone()[0]
    payload = {
        "project": list(project),
        "control": list(control) if control else None,
        "tasks": list(task_stats),
        "audit": list(audit_stats),
        "approvals": list(approval_stats),
        "deliveries": [[row[0], row[1]] for row in delivery_counts],
        "delivery_stamp": int(delivery_stamp or 0),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _mutation_state_fingerprint(con: sqlite3.Connection, project_id: str,
                                action: str, target_id: str) -> str:
    """Task actions depend on the target, not unrelated workers' audit traffic."""
    if _FRESH_CONTEXT_ACTIONS.get(action) != "task":
        return _project_state_fingerprint(con, project_id)
    task = con.execute("SELECT * FROM war_tasks WHERE id=? AND project_id=?",
                       (target_id, project_id)).fetchone()
    if task is None:
        raise HTTPException(404, "Task not found")
    queries = {
        "task": ("SELECT * FROM war_tasks WHERE id=?", (target_id,)),
        "project": ("SELECT * FROM war_projects WHERE id=?", (project_id,)),
        "control": ("SELECT * FROM war_project_control WHERE project_id=?", (project_id,)),
        "agents": ("SELECT * FROM war_task_agents WHERE task_id=? ORDER BY agent_id", (target_id,)),
        "grounding": ("SELECT * FROM war_grounding_packets WHERE task_id=?", (target_id,)),
        "approvals": ("SELECT * FROM war_approvals WHERE task_id=? ORDER BY id", (target_id,)),
        "deliveries": ("SELECT * FROM war_deliveries WHERE message_id=? AND task_revision=? ORDER BY id", (task["source_message_id"], task["revision"])),
        "evidence": ("SELECT * FROM war_evidence WHERE task_id=? AND task_revision=? AND qa_cycle=? ORDER BY id", (target_id, task["revision"], task["qa_cycle"])),
        "verdicts": ("SELECT * FROM war_qa_verdicts WHERE task_id=? AND task_revision=? AND qa_cycle=? ORDER BY id", (target_id, task["revision"], task["qa_cycle"])),
    }
    payload = {name: [list(row) for row in con.execute(sql, params)]
               for name, (sql, params) in queries.items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _encode_fresh_context(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(_fresh_context_secret(), raw, hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _decode_fresh_context(token: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        expected = hmac.new(_fresh_context_secret(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload")
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, "STALE_CONTEXT") from exc


def _require_fresh_context(
    con: sqlite3.Connection,
    body: dict[str, Any],
    *,
    actor: str,
    project_id: str,
    action: str,
    target_id: str,
) -> None:
    if _fresh_context_bypassed():
        return
    token = body.get("context_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(409, "FRESH_CONTEXT_REQUIRED")
    payload = _decode_fresh_context(token)
    now = _now()
    if (
        payload.get("v") != 1
        or payload.get("actor") != actor
        or payload.get("project_id") != project_id
        or payload.get("action") != action
        or payload.get("target_id") != target_id
        or not isinstance(payload.get("iat"), int)
        or not isinstance(payload.get("exp"), int)
        or payload["iat"] > now + 5
        or payload["exp"] < now
        or now - payload["iat"] > _FRESH_CONTEXT_TTL_SECONDS + 5
    ):
        raise HTTPException(409, "STALE_CONTEXT")
    current = _mutation_state_fingerprint(con, project_id, action, target_id)
    if not hmac.compare_digest(str(payload.get("state_version") or ""), current):
        raise HTTPException(409, "STALE_CONTEXT")


def _validate_mutation_contract(body: dict[str, Any], task: sqlite3.Row) -> None:
    """Validate the explicit project/task/revision envelope used by the UI."""
    if body.get("contract_version") != 1:
        return
    if body.get("project_id") != task["project_id"]:
        raise HTTPException(409, "mutation project binding is stale")
    if body.get("task_id") != task["id"]:
        raise HTTPException(409, "mutation task binding is stale")
    revision = body.get("task_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise HTTPException(422, "task_revision must be a positive integer")
    if revision != int(task["revision"]):
        raise HTTPException(409, "mutation task revision is stale")


def _execution_orchestrator() -> Any:
    from fast_gateway_service import get_persistent_harness
    from war_room_execution_units import ExecutionUnitStore
    from war_room_orchestration import WarRoomOrchestrator
    db_path = war_room._db_path()
    cache_key = str(db_path.expanduser().resolve())
    with _EXECUTION_ORCHESTRATOR_LOCK:
        existing = _EXECUTION_ORCHESTRATORS.get(cache_key)
        if existing is not None:
            return existing
        harness = get_persistent_harness()
        engine = harness.engine
        store = ExecutionUnitStore(db_path)
        store.ensure_schema()
        orchestrator = WarRoomOrchestrator(core_engine=engine, execution_store=store)
        subscriber = getattr(orchestrator, "on_core_run_terminal", None)
        if callable(subscriber):
            harness.set_terminal_completion_subscriber(subscriber)
        _EXECUTION_ORCHESTRATORS[cache_key] = orchestrator
        return orchestrator


def _execution_error(exc: ValueError) -> HTTPException:
    return HTTPException(409, str(exc))


@router.get("/projects/{project_id}/execution-candidates")
def execution_candidates(project_id: str, request: Request, required_capabilities: str | None = None,
                         x_war_room_actor: str | None = Header(default=None),
                         x_war_room_token: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "read", project_id, x_war_room_token, request)
    capabilities = [value.strip() for value in (required_capabilities or "").split(",") if value.strip()]
    with _connect_rw() as con:
        agent_ids = _eligible_agent_ids(con, project_id, capabilities)
    return {"project_id": project_id, "agent_ids": agent_ids}


@router.post("/projects/{project_id}/tasks/{task_id}/executions/compile")
async def compile_executions(project_id: str, task_id: str, request: Request,
                             x_war_room_actor: str | None = Header(default=None),
                             x_war_room_token: str | None = Header(default=None),
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "execute", project_id, x_war_room_token, request)
        task = con.execute("SELECT id FROM war_tasks WHERE id=? AND project_id=?", (task_id, project_id)).fetchone()
        if task is None:
            raise HTTPException(404, "task not found for project")
        scope = f"POST:/projects/{project_id}/tasks/{task_id}/executions/compile"
        previous = _idem(con, actor, idempotency_key, scope, body)
        if previous:
            return previous
    agents = _canonical_agents(body.get("agent_ids"))
    with _connect_rw() as con:
        _require_execution_agents(con, project_id, agents)
    orchestrator = _execution_orchestrator()
    try:
        result = orchestrator.compile_and_persist(war_project_id=project_id, war_task_id=task_id,
                                                  agents=agents, workflow=body.get("workflow"),
                                                  correlation_id=body.get("correlation_id"))
    except ValueError as exc:
        raise _execution_error(exc) from exc
    with _connect_rw() as con:
        _save_idem(con, actor, idempotency_key, scope, body, result)
        con.commit()
    return result


@router.get("/projects/{project_id}/tasks/{task_id}/executions")
def list_executions(project_id: str, task_id: str, request: Request,
                    x_war_room_actor: str | None = Header(default=None),
                    x_war_room_token: str | None = Header(default=None)) -> dict[str, Any]:
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        _actor(con, x_war_room_actor, "read", project_id, x_war_room_token, request)
        task = con.execute("SELECT id FROM war_tasks WHERE id=? AND project_id=?", (task_id, project_id)).fetchone()
        if task is None:
            raise HTTPException(404, "task not found for project")
    return {"project_id": project_id, "task_id": task_id,
            "executions": _execution_orchestrator().list_executions(war_project_id=project_id, war_task_id=task_id)}


@router.post("/executions/{execution_id}/dispatch")
async def dispatch_execution(execution_id: str, request: Request,
                             x_war_room_actor: str | None = Header(default=None),
                             x_war_room_token: str | None = Header(default=None),
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    orchestrator = _execution_orchestrator()
    unit = orchestrator.store.get_unit(execution_id)
    if unit is None:
        raise HTTPException(404, "execution not found")
    with _connect_rw() as con:
        actor = _actor(con, x_war_room_actor, "execute", unit["war_project_id"], x_war_room_token, request)
        scope = f"POST:/executions/{execution_id}/dispatch"
        previous = _idem(con, actor, idempotency_key, scope, body)
        if previous:
            return previous
    try:
        result = orchestrator.dispatch_execution(execution_id=execution_id, message=body.get("message", ""),
                                                  timeout_seconds=body.get("timeout_seconds", 300),
                                                  goal_contract=body.get("goal_contract"))
    except ValueError as exc:
        raise _execution_error(exc) from exc
    with _connect_rw() as con:
        _save_idem(con, actor, idempotency_key, scope, body, result); con.commit()
    return result


@router.post("/executions/{execution_id}/stop")
async def stop_execution(execution_id: str, request: Request,
                         x_war_room_actor: str | None = Header(default=None),
                         x_war_room_token: str | None = Header(default=None),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    orchestrator = _execution_orchestrator()
    unit = orchestrator.store.get_unit(execution_id)
    if unit is None:
        raise HTTPException(404, "execution not found")
    with _connect_rw() as con:
        actor = _actor(con, x_war_room_actor, "execute", unit["war_project_id"], x_war_room_token, request)
        scope = f"POST:/executions/{execution_id}/stop"
        previous = _idem(con, actor, idempotency_key, scope, body)
        if previous:
            return previous
    try:
        result = orchestrator.stop_execution(execution_id)
    except ValueError as exc:
        raise _execution_error(exc) from exc
    with _connect_rw() as con:
        _save_idem(con, actor, idempotency_key, scope, body, result); con.commit()
    return result


def _canonical_agent_id(value: Any, field: str = "agent_id") -> str | None:
    del field
    return canonical_agent_id(value)


def _canonical_agents(values: Any, field: str = "agent_ids") -> list[str]:
    if not isinstance(values, list) or not values:
        raise HTTPException(422, f"{field} must be a unique non-empty allowlisted list")
    canonical: list[str] = []
    for value in values:
        agent = _canonical_agent_id(value, field)
        if agent is None or agent in canonical:
            raise HTTPException(422, f"{field} must be a unique non-empty allowlisted list")
        canonical.append(agent)
    return canonical


def _validated_agents(body: dict[str, Any]) -> list[str]:
    return _canonical_agents(body.get("agent_ids"))


def _normalize_required_evidence(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise HTTPException(422, "required_evidence must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.strip():
            item = {"id": value.strip(), "evidence_type": "", "source_command": "", "expected_contains": "", "legacy": True}
        elif isinstance(value, dict) and set(value) in (
            {"id", "evidence_type", "source_command", "expected_contains"},
            {"id", "evidence_type", "source_command", "expected_contains", "legacy"},
        ):
            item = {key: value[key] for key in ("id", "evidence_type", "source_command", "expected_contains")}
            if value.get("legacy") is True:
                item["legacy"] = True
        else:
            raise HTTPException(422, "required_evidence entries require exactly id, evidence_type, source_command, expected_contains")
        if not isinstance(item["id"], str) or not item["id"].strip() or item["id"] in seen:
            raise HTTPException(422, "required_evidence IDs must be unique and non-empty")
        if not all(isinstance(item[key], str) for key in ("evidence_type", "source_command", "expected_contains")):
            raise HTTPException(422, "required_evidence fields must be strings")
        seen.add(item["id"])
        normalized.append(item)
    return normalized


def _grounding_packet(body: dict[str, Any], project_id: str, document_version: str) -> dict[str, Any]:
    supplied = body.get("grounding") if isinstance(body.get("grounding"), dict) else {}
    from war_room_task_contract import profile_grounding
    try:
        supplied = profile_grounding(body, supplied)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    worktree = str(supplied.get("worktree") or os.environ.get("PLACHEM_WAR_ROOM_WORKTREE") or Path.cwd())
    verification_scope = str(supplied.get("verification_scope") or "TASK_RUN").strip().upper()
    if verification_scope not in {"TASK_RUN", "PROJECT_WINDOW"}:
        raise HTTPException(422, "verification_scope must be TASK_RUN or PROJECT_WINDOW")
    packet = {
        "worktree": worktree,
        "branch": str(supplied.get("branch") or os.environ.get("PLACHEM_WAR_ROOM_BRANCH") or "uncommitted-worktree"),
        "revision": str(supplied.get("revision") or os.environ.get("PLACHEM_WAR_ROOM_REVISION") or document_version),
        "api_base": str(supplied.get("api_base") or f"/api/war-room/projects/{project_id}"),
        "db_label": str(supplied.get("db_label") or os.environ.get("PLACHEM_WAR_ROOM_DB_LABEL") or "configured-isolated-db"),
        "forbidden": supplied.get("forbidden") or ["production DB", "existing work sessions", "merge/push/deploy"],
        "completion_conditions": supplied.get("completion_conditions") or ["focused tests pass", "full regression passes", "evidence paths supplied"],
        "required_evidence": _normalize_required_evidence(supplied.get("required_evidence") or ["test", "artifact"]),
        "verification_scope": verification_scope,
        "session_integrity_required": any("existing work sessions" in value.lower() for value in supplied.get("forbidden", []) if isinstance(value, str)),
    }
    for key in ("contract_version", "task_profile", "worker_timeout_seconds", "qa_timeout_seconds", "session_integrity_required"):
        if key in supplied:
            packet[key] = supplied[key]
    # Approved result-document paths are server-supplied task contract data:
    # the prepare request is the only source. Worker responses never
    # contribute to this list; the immutable grounding packet is the trust
    # boundary forwarded into the Fast Gateway grounding envelope.
    approved_paths_raw = supplied.get("approved_paths")
    approved_paths: list[str] = []
    if isinstance(approved_paths_raw, list):
        for value in approved_paths_raw:
            if isinstance(value, str) and value.strip().startswith("/") and len(value) <= 4096:
                approved_paths.append(value.strip())
    packet["approved_paths"] = approved_paths

    result_artifact_paths_raw = supplied.get("result_artifact_paths")
    result_artifact_paths: list[str] = []
    if result_artifact_paths_raw is not None:
        if not isinstance(result_artifact_paths_raw, list):
            raise HTTPException(422, "result_artifact_paths must be a list")
        for value in result_artifact_paths_raw:
            if not isinstance(value, str) or not value.strip().startswith("/") or len(value) > 4096:
                raise HTTPException(422, "result_artifact_paths must contain absolute paths")
            candidate = value.strip()
            if approved_paths and not any(
                candidate == root or candidate.startswith(root.rstrip("/") + "/")
                for root in approved_paths
            ):
                raise HTTPException(422, "result_artifact_path must be within approved_paths")
            result_artifact_paths.append(candidate)
    packet["result_artifact_paths"] = result_artifact_paths

    absolute_worktree = (
        Path(worktree).is_absolute()
        or PurePosixPath(worktree).is_absolute()
        or PureWindowsPath(worktree).is_absolute()
    )
    if (not absolute_worktree or any(not isinstance(packet[key], str) or not packet[key].strip() for key in ("branch","revision","api_base","db_label"))
            or any(not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values) for values in (packet["forbidden"], packet["completion_conditions"]))):
        raise HTTPException(422, "grounding packet is incomplete")
    return packet


def _grounded_instruction(instruction: str, packet: dict[str, Any], execution_mode: str = "LEGACY") -> str:
    if execution_mode == "FAST_GATEWAY":
        return "[FAST_GATEWAY_RESULT]\nFINAL RESPONSE CONTRACT (highest priority): return only one JSON object with exactly these top-level keys: " + (
            '{"status":"completed|blocked|failed","summary":"...","evidence":[{"type":"...","detail":"..."}],'
            '"artifacts":[{"path":"..."}],"scope":{"compliant":true,"violations":[]}}. '
            "The final response must be raw JSON only: its first character must be { and its last character must be }. Do not use Markdown, code fences, backticks, or any prose before or after the JSON object. Use evidence and artifacts only for actually verified work. Artifacts are outputs newly produced by this run; for a read-only task, put inspected existing paths in evidence and return an empty artifacts array. Do not add fields outside this contract.\n"
            "[IMMUTABLE_GROUNDING_PACKET]\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True) +
            "\n[ORIGINAL_INSTRUCTION_CONTEXT]\n" + instruction.strip() +
            "\nFollow the original instruction as the task. Use the envelope only for approved constraints and final response formatting."
        )
    return "[STRUCTURED_RESULT]\nFINAL RESPONSE CONTRACT (highest priority): return only one JSON object with exactly: " + (
        '{"confirmed_worktree":"...","confirmed_revision":"...","verdict":"PASS|FAIL|REWORK",'
        '"evidence":["/absolute/path"],"summary":"...","representative_completion_claimed":false,'
        '"documents":[{"title":"...","category":"requirements|architecture|decision|reference|report|handoff|other",'
        '"path":"/absolute/path","action":"create|update","summary":"...","document_id":"optional",'
        '"expected_version":1,"relation":"input|output|reference|decision|handoff"}]}. '
        "confirmed_worktree MUST exactly copy IMMUTABLE_GROUNDING_PACKET.worktree. "
        "confirmed_revision MUST exactly copy IMMUTABLE_GROUNDING_PACKET.revision; it is a War Room grounding revision, "
        "not a Git revision lookup. Never replace it with HEAD/commit hash unless the packet itself contains that hash. "
        "Do not claim representative completion; only main can approve it.\n"
        "[IMMUTABLE_GROUNDING_PACKET]\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True) +
        "\n[ORIGINAL_INSTRUCTION_CONTEXT]\n" + instruction.strip() +
        "\nReturn the JSON object above; treat the original instruction only as context."
    )


@router.post("/projects/{project_id}/prepare", status_code=201)
async def prepare_task(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Atomically create a task, immutable instruction, assignments and approval request."""
    body = await _body(request)
    instruction = body.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4096:
        raise HTTPException(422, "instruction must be 1..4096 characters")
    agents = _validated_agents(body)
    execution_mode = _execution_mode(body.get("execution_mode"))
    normalized = dict(body)
    normalized.update({
        "scope": body.get("scope", instruction),
        "assignee_agent_id": _canonical_agent_id(body.get("assignee_agent_id", agents[0]), "assignee_agent_id"),
        "agent_ids": agents,
        "call_limit": body.get("call_limit", len(agents)),
        "turn_limit": body.get("turn_limit", max(2, len(agents))),
        "execution_mode": execution_mode,
    })
    if normalized["assignee_agent_id"] not in agents:
        raise HTTPException(422, "assignee_agent_id must be included in agent_ids")
    with _connect_rw() as con:
        project = war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        con.execute("BEGIN IMMEDIATE")
        _require_not_stopped(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/prepare"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        now = _now()
        scope, assignee, call_limit, turn_limit, deadline_at, document_version = _validated_task_payload(normalized, project, now)
        reviewer = _canonical_agent_id(body.get("reviewer_agent_id"), "reviewer_agent_id") if body.get("reviewer_agent_id") is not None else None
        if reviewer is None:
            reviewer_row = con.execute(
                "SELECT principal_id FROM war_participants WHERE project_id=? AND active=1 AND role='qa' AND principal_id<>? ORDER BY principal_id LIMIT 1",
                (project_id, assignee),
            ).fetchone()
            reviewer = reviewer_row[0] if reviewer_row else None
        _require_independent_reviewer(reviewer, agents)
        if reviewer is not None and not con.execute(
            "SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=? AND active=1 AND role='qa'",
            (project_id, reviewer),
        ).fetchone():
            raise HTTPException(409, "reviewer must be an active project QA participant")
        _require_execution_agents(con, project_id, agents)
        task_id, message_id, correlation = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        packet = _grounding_packet(body, project_id, document_version)
        grounding_input = body.get("grounding") if isinstance(body.get("grounding"), dict) else {}
        required_document_ids_raw = grounding_input.get("required_document_ids") or []
        if not isinstance(required_document_ids_raw, list) or any(
            not isinstance(value, str) or not value.strip() for value in required_document_ids_raw
        ):
            raise HTTPException(422, "required_document_ids must be a list of document IDs")
        required_document_ids = list(dict.fromkeys(value.strip() for value in required_document_ids_raw))
        project_documents = context_documents(
            con, project_id, required_ids=required_document_ids or None, limit=12
        )
        if required_document_ids:
            found = {item["document_id"] for item in project_documents}
            missing = [value for value in required_document_ids if value not in found]
            if missing:
                raise HTTPException(422, f"required project documents not found: {', '.join(missing)}")
        packet["required_document_ids"] = required_document_ids
        packet["project_documents"] = project_documents
        # Persist and display the validated original itself. The worker adds
        # the immutable result contract immediately before submission, so the
        # contract cannot consume the instruction's 4096-character budget.
        clean = war_room._sanitize_stored_string(instruction)
        con.execute(
            "INSERT INTO war_messages (id,project_id,message_type,author_type,author_id,body,source_session_id,source_message_id,created_at,correlation_id,redaction_state,original_body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, project_id, "instruction", "agent", actor, clean, None, None, now, correlation, "redacted" if clean != instruction else "clean", clean),
        )
        con.execute("""INSERT INTO war_tasks
            (id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,manyfast_version,
             document_version,call_limit,turn_limit,execution_mode,deadline_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'awaiting_approval',?,?,?,?,?,?,?,?)""",
            (task_id, project_id, message_id, assignee, reviewer, scope, project["manyfast_version"], document_version, call_limit, turn_limit, execution_mode, deadline_at, now, now),
        )
        con.executemany("INSERT INTO war_task_agents(task_id,agent_id) VALUES (?,?)", [(task_id, agent) for agent in agents])
        packet_json = json.dumps(packet, ensure_ascii=False, sort_keys=True)
        con.execute("INSERT INTO war_grounding_packets VALUES (?,?,?,?)", (task_id, packet_json, hashlib.sha256(packet_json.encode()).hexdigest(), now))
        _audit(con, project_id, actor, "task_prepared", "task", task_id, {"message_id":message_id,"agent_ids":agents,"grounding_hash":hashlib.sha256(packet_json.encode()).hexdigest()}, correlation)
        result = {"mode":"controlled","task_id":task_id,"message_id":message_id,"status":"awaiting_approval","execution_mode":execution_mode,"assignee_agent_id":assignee,"reviewer_agent_id":reviewer,"agent_ids":agents,"correlation_id":correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/tasks/{task_id}/approve-execute")
async def approve_and_execute_task(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Atomically approve, move to running and enqueue every assigned delivery."""
    body = await _body(request)
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        _validate_mutation_contract(body, task)
        actor = _actor(con, x_war_room_actor, "approve", task["project_id"], x_war_room_token, request)
        _actor(con, actor, "execute", task["project_id"], x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, task["project_id"])
        con.execute("BEGIN IMMEDIATE")
        _require_not_stopped(con, task["project_id"])
        idem_scope = f"POST:/tasks/{task_id}/approve-execute"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        _require_fresh_context(
            con, body, actor=actor, project_id=task["project_id"],
            action="task_approve_execute", target_id=task_id,
        )
        agents = sorted(row[0] for row in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=?", (task_id,)).fetchall())
        _require_independent_reviewer(task["reviewer_agent_id"], agents)
        if task["status"] == "running" and task["source_message_id"]:
            existing = [dict(row) for row in con.execute("SELECT id AS delivery_id,agent_id,status FROM war_deliveries WHERE message_id=? AND task_revision=? ORDER BY agent_id", (task["source_message_id"], int(task["revision"]))).fetchall()]
            result = {"mode":"controlled","task_id":task_id,"status":"running","execution_state":"already_running","deliveries":existing}
            _save_idem(con, actor, idempotency_key, idem_scope, body, result)
            con.commit()
            return result
        if task["status"] != "awaiting_approval" or not task["source_message_id"]:
            raise HTTPException(409, "prepared task awaiting approval required")
        now = _now()
        expires_at = body.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= now or expires_at > now + 604800:
            raise HTTPException(422, "approval expiry must be within 7 days")
        if task["deadline_at"] is None or int(task["deadline_at"]) <= now:
            raise HTTPException(409, "task execution policy is stale")
        _require_execution_agents(con, task["project_id"], agents)
        if len(agents) > int(task["call_limit"]):
            raise HTTPException(409, "task call limit exceeded")
        for agent in agents:
            stale = con.execute("""SELECT d.id,m.project_id FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
                WHERE d.agent_id=? AND d.status IN ('queued','sent','received') AND d.deadline_at IS NOT NULL AND d.deadline_at<=?""", (agent, now)).fetchall()
            for expired in stale:
                con.execute("UPDATE war_deliveries SET status='timed_out',error_code='stale_active_call_reclaimed' WHERE id=?", (expired["id"],))
                _audit(con, expired["project_id"], actor, "stale_active_call_reclaimed", "delivery", expired["id"], {"agent_id":agent,"reclaimed_for_task":task_id}, str(uuid.uuid4()))
        scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
        target_hash = hashlib.sha256(json.dumps(agents).encode()).hexdigest()
        approval_id, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        con.execute("INSERT INTO war_approvals(id,task_id,approver_id,decision,scope_hash,document_version,assignee_agent_id,target_set_hash,expires_at,revoked_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (approval_id,task_id,actor,"approved",scope_hash,task["document_version"],task["assignee_agent_id"],target_hash,expires_at,None,now))
        con.execute("UPDATE war_tasks SET status='running',updated_at=? WHERE id=?", (now,task_id))
        deliveries = []
        for agent in agents:
            delivery_id = str(uuid.uuid4())
            delivery_correlation = str(uuid.uuid4())
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,task_revision,status,attempt_count,deadline_at,created_at,correlation_id) VALUES (?,?,?,?, 'queued',0,?,?,?)", (delivery_id,task["source_message_id"],agent,int(task["revision"]),task["deadline_at"],now,delivery_correlation))
            deliveries.append({"delivery_id":delivery_id,"agent_id":agent,"status":"queued","correlation_id":delivery_correlation})
        _audit(con, task["project_id"], actor, "task_approved_executed", "task", task_id, {"approval_id":approval_id,"agent_ids":agents}, correlation)
        result = {"mode":"controlled","task_id":task_id,"status":"running","execution_state":"queued","approval_id":approval_id,"deliveries":deliveries,"correlation_id":correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/projects/{project_id}/messages", status_code=201)
async def create_message(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    text = body.get("body")
    message_type = body.get("message_type", "opinion")
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise HTTPException(422, "body must be 1..4096 characters")
    if message_type not in war_room.MESSAGE_TYPES:
        raise HTTPException(422, "message_type not allowed")
    if message_type == "instruction":
        raise HTTPException(422, "instruction must be created with its linked task via /projects/{project_id}/instructions")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "comment", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        _require_not_stopped(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/messages"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        source_message_id = body.get("source_message_id")
        linked_task = None
        if source_message_id is not None:
            linked_task = con.execute(
                "SELECT t.* FROM war_tasks t JOIN war_messages m ON m.id=t.source_message_id WHERE m.id=? AND m.project_id=?",
                (source_message_id, project_id),
            ).fetchone()
            if linked_task is None:
                raise HTTPException(422, "source_message_id must identify a project task instruction")
        if message_type == "result" and (linked_task is None or actor != linked_task["assignee_agent_id"]):
            raise HTTPException(403, "only the approved assignee may submit a task result")
        requested_agent = body.get("requested_agent_id")
        if message_type == "opinion" and requested_agent is not None:
            requested_agent = _canonical_agent_id(requested_agent, "requested_agent_id")
            if requested_agent is None or not con.execute(
                "SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=? AND active=1 AND can_comment=1",
                (project_id, requested_agent),
            ).fetchone():
                raise HTTPException(409, "opinion target must be an active commenting participant")
        message_id = str(uuid.uuid4())
        correlation = str(uuid.uuid4())
        clean = war_room._sanitize_stored_string(text)
        con.execute("INSERT INTO war_messages (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,correlation_id,redaction_state,original_body) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (message_id, project_id, message_type, "agent", actor, clean, body.get("source_message_id"), _now(), correlation, "redacted" if clean != text else "clean", clean))
        _audit(con, project_id, actor, "message_created", "message", message_id, {"message_type": message_type, "source_message_id": source_message_id, "requested_agent_id": requested_agent}, correlation)
        result = {"mode": "controlled", "id": message_id, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/projects/{project_id}/instructions", status_code=201)
async def create_instruction_with_task(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Create an immutable instruction only against an existing project task."""
    body = await _body(request)
    text = body.get("body")
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise HTTPException(422, "body must be 1..4096 characters")
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise HTTPException(422, "task_id required")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        con.execute("BEGIN IMMEDIATE")
        _require_not_stopped(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/instructions"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        task = con.execute("SELECT * FROM war_tasks WHERE id=? AND project_id=?", (task_id, project_id)).fetchone()
        if not task:
            if con.execute("SELECT 1 FROM war_tasks WHERE id=?", (task_id,)).fetchone():
                raise HTTPException(403, "instruction task belongs to another project")
            raise HTTPException(422, "instruction task not found")
        if task["source_message_id"]:
            raise HTTPException(409, "task already has an immutable instruction")
        now = _now()
        message_id, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        clean = war_room._redact_string(text)
        con.execute("INSERT INTO war_messages (id,project_id,message_type,author_type,author_id,body,source_session_id,source_message_id,created_at,correlation_id,redaction_state,original_body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (message_id, project_id, "instruction", "agent", actor, clean, None, None, now, correlation, "redacted" if clean != text else "clean", clean))
        con.execute("UPDATE war_tasks SET source_message_id=?, updated_at=? WHERE id=?", (message_id, now, task_id))
        _audit(con, project_id, actor, "message_created", "message", message_id, {"message_type": "instruction", "task_id": task_id}, correlation)
        _audit(con, project_id, actor, "instruction_linked", "task", task_id, {"assignee_agent_id": task["assignee_agent_id"], "source_message_id": message_id}, correlation)
        result = {
            "mode": "controlled",
            "message_id": message_id,
            "task_id": task_id,
            "status": task["status"],
            "correlation_id": correlation,
        }
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/messages/{message_id}/deliveries", status_code=201)
async def deliver_message(message_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    requested_agents = body.get("agent_ids")
    if requested_agents is None:
        requested_agents = [body.get("agent_id")]
    requested_agents = _canonical_agents(requested_agents)
    body = {**body, "agent_ids": requested_agents}
    with _connect_rw() as con:
        message = con.execute("SELECT * FROM war_messages WHERE id=?", (message_id,)).fetchone()
        if not message: raise HTTPException(404, "Message not found")
        actor = _actor(con, x_war_room_actor, "execute", message["project_id"], x_war_room_token, request)
        _require_mutable_project(con, message["project_id"])
        _require_not_stopped(con, message["project_id"])
        requested_task_id = body.get("task_id")
        if not isinstance(requested_task_id, str) or not requested_task_id:
            raise HTTPException(422, "task_id required")
        task = con.execute("SELECT * FROM war_tasks WHERE id=? AND source_message_id=?", (requested_task_id, message_id)).fetchone()
        if not task:
            raise HTTPException(409, "delivery requires the linked instruction task_id")
        now = _now()
        allowed_agents = {row[0] for row in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=?", (task["id"],)).fetchall()}
        if not set(requested_agents).issubset(allowed_agents):
            raise HTTPException(409, "delivery target is outside the task assignment policy")
        _require_execution_agents(con, task["project_id"], requested_agents)
        if task["status"] != "running":
            raise HTTPException(409, "task must be running before delivery")
        if task["deadline_at"] is None or int(task["deadline_at"]) <= now:
            raise HTTPException(409, "task deadline exceeded")
        if not task["document_version"]:
            raise HTTPException(409, "task document version is missing")
        scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
        approval = con.execute(
            """SELECT * FROM war_approvals WHERE task_id=? AND decision='approved'
               AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1""", (task["id"],),
        ).fetchone()
        if (not approval or approval["expires_at"] is None or int(approval["expires_at"]) <= now
                or approval["scope_hash"] != scope_hash
                or approval["document_version"] != task["document_version"]
                or approval["assignee_agent_id"] != task["assignee_agent_id"]
                or approval["target_set_hash"] != hashlib.sha256(json.dumps(sorted(allowed_agents)).encode()).hexdigest()):
            raise HTTPException(409, "fresh matching approval required")
        calls = con.execute(
            "SELECT call_count,turn_count FROM war_task_calls WHERE task_id=? AND task_revision=?",
            (task["id"], int(task["revision"])),
        ).fetchone()
        call_count = int(calls["call_count"]) if calls else 0
        turn_count = int(calls["turn_count"]) if calls else 0
        if call_count + len(requested_agents) > int(task["call_limit"]):
            raise HTTPException(409, "task call limit exceeded")
        if turn_count >= int(task["turn_limit"]):
            raise HTTPException(409, "task turn limit exceeded")
        for agent_id in requested_agents:
            active = con.execute("""SELECT 1 FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
                   WHERE m.project_id=? AND d.agent_id=? AND d.status IN ('queued','sent','received') LIMIT 1""", (task["project_id"], agent_id)).fetchone()
            if active: raise HTTPException(409, "active agent call already exists")
        idem_scope = f"POST:/messages/{message_id}/deliveries"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        correlation = str(uuid.uuid4()); deliveries = []
        delivery_deadline = int(task["deadline_at"])
        for agent_id in requested_agents:
            delivery_id = str(uuid.uuid4()); delivery_correlation = str(uuid.uuid4())
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,task_revision,status,attempt_count,deadline_at,created_at,correlation_id) VALUES (?,?,?,?,'queued',0,?,?,?)", (delivery_id, message_id, agent_id, int(task["revision"]), delivery_deadline, _now(), delivery_correlation))
            deliveries.append({"delivery_id":delivery_id,"agent_id":agent_id,"status":"queued","correlation_id":delivery_correlation})
            _audit(con, message["project_id"], actor, "delivery_queued", "delivery", delivery_id, {"message_id": message_id, "agent_id": agent_id, "delivery_correlation_id": delivery_correlation}, delivery_correlation)
        result = {"mode":"controlled","deliveries":deliveries,"delivery_id":deliveries[0]["delivery_id"],"status":"queued","correlation_id":correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/projects/{project_id}/tasks", status_code=201)
async def create_task(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        project = war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        _require_not_stopped(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/tasks"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        now = _now()
        scope, assignee, call_limit, turn_limit, deadline_at, document_version = _validated_task_payload(body, project, now)
        execution_mode = _execution_mode(body.get("execution_mode"))
        reviewer = _canonical_agent_id(body.get("reviewer_agent_id"), "reviewer_agent_id") if body.get("reviewer_agent_id") is not None else None
        if reviewer is None:
            reviewer_row = con.execute(
                "SELECT principal_id FROM war_participants WHERE project_id=? AND active=1 AND role='qa' AND principal_id<>? ORDER BY principal_id LIMIT 1",
                (project_id, assignee),
            ).fetchone()
            reviewer = reviewer_row[0] if reviewer_row else None
        task_agents = _canonical_agents(body.get("agent_ids", [assignee]))
        if assignee not in task_agents:
            raise HTTPException(422, "agent_ids must be unique, allowlisted, and include assignee_agent_id")
        _require_independent_reviewer(reviewer, task_agents, required=False)
        if reviewer is not None and not con.execute(
            "SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=? AND active=1 AND role='qa'",
            (project_id, reviewer),
        ).fetchone():
            raise HTTPException(409, "reviewer must be an active project QA participant")
        task_id, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        source_message_id = body.get("source_message_id")
        if source_message_id is not None and not con.execute(
            "SELECT 1 FROM war_messages WHERE id=? AND project_id=? AND message_type='instruction'",
            (source_message_id, project_id),
        ).fetchone():
            raise HTTPException(422, "source_message_id must reference a project instruction")
        if source_message_id is not None and con.execute("SELECT 1 FROM war_tasks WHERE source_message_id=?", (source_message_id,)).fetchone():
            raise HTTPException(409, "instruction is already linked to a task")
        con.execute("""INSERT INTO war_tasks
            (id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,manyfast_version,
             document_version,call_limit,turn_limit,execution_mode,deadline_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, project_id, source_message_id, assignee, reviewer, scope, "draft", project["manyfast_version"],
             document_version, call_limit, turn_limit, execution_mode, deadline_at, now, now))
        con.executemany("INSERT INTO war_task_agents(task_id,agent_id) VALUES (?,?)", [(task_id, agent) for agent in task_agents])
        _audit(con, project_id, actor, "task_created", "task", task_id, {"assignee_agent_id": assignee}, correlation)
        result = {"mode": "controlled", "task_id": task_id, "status": "draft", "execution_mode": execution_mode, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.put("/projects/{project_id}/participants/{agent_id}/test-session")
async def bind_test_session(project_id: str, agent_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    agent_id = _canonical_agent_id(agent_id, "agent_id")
    session_key, session_id = body.get("session_key"), body.get("session_id")
    if agent_id is None or not isinstance(session_key, str) or not session_key.startswith("test:") or (session_id is not None and not isinstance(session_id, str)):
        raise HTTPException(422, "explicit test session_key and optional session_id required")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con,x_war_room_actor,"manage",project_id,x_war_room_token,request)
        _require_mutable_project(con,project_id)
        scope=f"PUT:/projects/{project_id}/participants/{agent_id}/test-session"; previous=_idem(con,actor,idempotency_key,scope,body)
        if previous: return previous
        if not con.execute("SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=? AND active=1",(project_id,agent_id)).fetchone(): raise HTTPException(409,"active participant required")
        con.execute("UPDATE war_project_sessions SET enabled=0 WHERE project_id=? AND agent_id=?",(project_id,agent_id))
        con.execute("INSERT INTO war_project_sessions(project_id,agent_id,session_key,session_id,enabled,purpose,disposable) VALUES (?,?,?,?,1,'test',1)",(project_id,agent_id,session_key,session_id))
        result={"mode":"controlled","project_id":project_id,"agent_id":agent_id,"session_key":session_key,"disposable":True}
        _save_idem(con,actor,idempotency_key,scope,body,result); con.commit(); return result


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: str) -> dict[str, Any]:
    with _connect_rw() as con:
        row=con.execute("SELECT d.*,m.project_id,t.id AS task_id,COALESCE(t.execution_mode,'LEGACY') AS execution_mode FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id LEFT JOIN war_tasks t ON t.source_message_id=m.id WHERE d.id=?",(delivery_id,)).fetchone()
        if not row: raise HTTPException(404,"Delivery not found")
    value = dict(row)
    value.pop("session_key", None); value.pop("session_id", None)
    return {"mode":"readonly","delivery":war_room._redact(value)}


@router.get("/projects/{project_id}/deliveries")
def list_deliveries(project_id: str) -> dict[str, Any]:
    with _connect_rw() as con:
        war_room._project_or_404(con,project_id)
        rows=con.execute("""SELECT d.*,m.project_id,t.id AS task_id,COALESCE(t.execution_mode,'LEGACY') AS execution_mode,m.body AS instruction_body,m.original_body AS original_instruction_body,rm.body AS response_body,
            er.core_run_id,er.openclaw_run_id,er.run_status,er.runtime_seconds,er.result_summary,er.result_json,er.evidence_json,er.artifacts_json,er.raw_response,er.rejected_result_json,er.validation_error,er.policy_status,er.cancel_reason,er.escalation_required
            FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
            LEFT JOIN war_tasks t ON t.source_message_id=m.id
            LEFT JOIN war_messages rm ON rm.id=d.response_message_id
            LEFT JOIN war_execution_runs er
              ON d.run_id IS NOT NULL
             AND er.war_task_id=t.id
             AND er.agent_id=d.agent_id
             AND er.core_run_id=CASE
                   WHEN d.run_id LIKE 'war-%' THEN d.run_id
                   ELSE 'war-' || d.run_id
                 END
            WHERE m.project_id=? ORDER BY d.created_at DESC,d.id DESC""",(project_id,)).fetchall()
        control=_control(con,project_id)
    items=[]
    for row in rows:
        value=dict(row); value.pop("session_key",None); value.pop("session_id",None)
        items.append(value)
    return {"mode":"readonly","stop_requested_at":control["stop_requested_at"],"items":war_room._redact(items)}


def _require_demo_mode() -> None:
    if os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER") != "1":
        raise HTTPException(404,"Not found")


@router.get("/demo-mode")
def demo_mode() -> dict[str, Any]:
    _require_demo_mode(); return {"enabled":True}


@router.post("/demo/process")
async def process_demo_queue(request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    _require_demo_mode(); body=await _body(request)
    from war_room_worker import process_due_deliveries
    results=process_due_deliveries(db_path=war_room._db_path(),adapter=TestSessionAdapter())
    return {"mode":"test-only","items":results}


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(delivery_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    requested_agent = body.get("agent_id")
    with _connect_rw() as con:
        row = con.execute("SELECT d.*,m.project_id,m.id AS message_id,t.id AS task_id FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id LEFT JOIN war_tasks t ON t.source_message_id=m.id WHERE d.id=?", (delivery_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Delivery not found")
        actor = _actor(con, x_war_room_actor, "execute", row["project_id"], x_war_room_token, request)
        _require_mutable_project(con, row["project_id"]); _require_not_stopped(con, row["project_id"])
        if row["status"] not in {"failed", "timed_out"}:
            raise HTTPException(409, "only final failed delivery can be manually retried")
        agent_id = _canonical_agent_id(requested_agent, "agent_id") if requested_agent is not None else row["agent_id"]
        if agent_id is None:
            raise HTTPException(422, "agent_id is not allowed")
        if not con.execute("SELECT 1 FROM war_task_agents WHERE task_id=? AND agent_id=?", (row["task_id"], agent_id)).fetchone():
            raise HTTPException(409, "replacement agent is outside task assignment")
        idem_scope = f"POST:/deliveries/{delivery_id}/retry"; previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        now = _now(); correlation = str(uuid.uuid4())
        con.execute("UPDATE war_deliveries SET agent_id=?,status='queued',retry_count=0,next_attempt_at=?,error_code=NULL,error_class=NULL,last_error_at=NULL WHERE id=?", (agent_id, now, delivery_id))
        _audit(con, row["project_id"], actor, "delivery_manual_retry", "delivery", delivery_id, {"agent_id": agent_id, "replaced_agent": agent_id != row["agent_id"]}, correlation)
        result = {"mode":"controlled", "delivery_id":delivery_id, "status":"queued", "agent_id":agent_id, "correlation_id":correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.get("/projects/{project_id}/process-board")
def process_board(project_id: str) -> dict[str, Any]:
    """Return a read-only Process Board projection of existing task lifecycle data."""
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        items = _process_board_projection(con, project_id)
    columns = {state: [] for state in PROCESS_BOARD_STATES}
    for item in items:
        columns[item["mapped_state"]].append(item)
    return war_room._redact({
        "mode": "readonly",
        "project_id": project_id,
        "mapping_precedence": [
            "task_superseded audit -> SUPERSEDED",
            "delivery error_class=system_error -> BLOCKED",
            "war_tasks.status=completed -> DONE",
            "war_tasks.status in stopped,stop_unconfirmed -> BLOCKED",
            "war_tasks.status=rework_required -> REWORK",
            "latest QA verdict FAIL/REWORK/PASS -> matching state",
            "draft/awaiting_approval/qa -> WAITING; approved -> READY; running -> RUNNING",
        ],
        "states": list(PROCESS_BOARD_STATES),
        "items": items,
        "columns": columns,
    })


def _completion_binding(task: sqlite3.Row) -> tuple[Any, ...]:
    return (
        task["id"],
        int(task["revision"]),
        hashlib.sha256(task["scope"].encode()).hexdigest(),
        task["document_version"],
        int(task["qa_cycle"]),
    )


def _grounding_packet_for_completion(
    con: sqlite3.Connection, task_id: str
) -> tuple[dict[str, Any], str | None]:
    row = con.execute(
        "SELECT packet_json,packet_hash FROM war_grounding_packets WHERE task_id=?", (task_id,),
    ).fetchone()
    if not row:
        return {}, None
    raw = str(row["packet_json"] or "")
    expected_hash = hashlib.sha256(raw.encode()).hexdigest()
    if not row["packet_hash"] or not hmac.compare_digest(expected_hash, str(row["packet_hash"])):
        return {}, "GROUNDING_PACKET_INVALID"
    try:
        packet = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, "GROUNDING_PACKET_INVALID"
    if not isinstance(packet, dict):
        return {}, "GROUNDING_PACKET_INVALID"
    return packet, None


def _valid_signed_qa_pass(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    binding: tuple[Any, ...],
    packet: dict[str, Any],
) -> tuple[sqlite3.Row | None, str | None]:
    try:
        required = _normalize_required_evidence(packet.get("required_evidence", ["test", "artifact"]))
    except (HTTPException, TypeError, ValueError):
        return None, "GROUNDING_PACKET_INVALID"
    profile = "required:" + ",".join(item["id"] for item in required)
    rows = con.execute(
        """SELECT qa_principal,verdict,evidence_profile,signature,signed_payload,created_at,
                  task_revision,scope_hash,document_version,qa_cycle
           FROM war_qa_verdicts
           WHERE task_id=? AND verdict='PASS' AND task_revision=? AND scope_hash=?
             AND document_version=? AND qa_cycle=?
           ORDER BY created_at DESC,id DESC""",
        binding,
    ).fetchall()
    expected_principal = str(task["reviewer_agent_id"] or "")
    expected_payload = json.dumps(
        {
            "task_id": task["id"],
            "verdict": "PASS",
            "evidence_profile": profile,
            "qa_principal": expected_principal,
            "task_revision": int(task["revision"]),
            "scope_hash": binding[2],
            "document_version": task["document_version"],
            "qa_cycle": int(task["qa_cycle"]),
        },
        sort_keys=True,
    )
    try:
        expected_signature = _qa_signature(expected_payload)
    except HTTPException:
        return None, "QA_SIGNATURE_UNAVAILABLE"
    for row in rows:
        if str(row["qa_principal"]) != expected_principal:
            continue
        if str(row["evidence_profile"]) != profile:
            continue
        if str(row["signed_payload"]) != expected_payload:
            continue
        if not hmac.compare_digest(str(row["signature"]), expected_signature):
            continue
        return row, None
    return None, "SIGNED_QA_PASS"


def _representative_completion_checks(con: sqlite3.Connection, task: sqlite3.Row) -> dict[str, Any]:
    binding = _completion_binding(task)
    evidence_count = int(con.execute(
        "SELECT COUNT(*) FROM war_evidence WHERE task_id=? AND task_revision=? "
        "AND scope_hash=? AND document_version=? AND qa_cycle=?", binding,
    ).fetchone()[0])
    packet, packet_error = _grounding_packet_for_completion(con, task["id"])
    qa_pass = None
    qa_error = None
    if packet_error is None:
        qa_pass, qa_error = _valid_signed_qa_pass(con, task, binding, packet)

    integrity_required = packet.get("session_integrity_required") is True if packet_error is None else False
    integrity_ok = not integrity_required
    if integrity_required and qa_pass:
        integrity_row = con.execute(
            """SELECT si.scope,si.pre_count,si.post_count,si.changed_count,si.deleted_count,
                      si.uncertain_count,si.mtime_encoding,si.verified_at,e.created_at
               FROM war_session_integrity si
               JOIN war_evidence e ON e.id=si.evidence_id
               WHERE si.task_id=? AND e.task_revision=? AND e.scope_hash=?
                 AND e.document_version=? AND e.qa_cycle=? AND e.evidence_type='session_integrity'
               ORDER BY si.verified_at DESC LIMIT 1""",
            binding,
        ).fetchone()
        integrity_ok = bool(
            integrity_row
            and isinstance(integrity_row["scope"], str) and integrity_row["scope"].strip()
            and all(
                isinstance(integrity_row[key], int) and not isinstance(integrity_row[key], bool)
                and int(integrity_row[key]) >= 0
                for key in ("pre_count", "post_count", "changed_count", "deleted_count", "uncertain_count")
            )
            and int(integrity_row["pre_count"]) == int(integrity_row["post_count"])
            and all(int(integrity_row[key]) == 0 for key in ("changed_count", "deleted_count", "uncertain_count"))
            and str(integrity_row["mtime_encoding"]) == "decimal_string"
            and isinstance(integrity_row["verified_at"], int)
            and int(integrity_row["verified_at"]) <= int(integrity_row["created_at"])
            and int(integrity_row["created_at"]) <= int(qa_pass["created_at"])
        )

    missing: list[str] = []
    if evidence_count <= 0:
        missing.append("CURRENT_EVIDENCE")
    if packet_error:
        missing.append(packet_error)
    if qa_error:
        missing.append(qa_error)
    if integrity_required and not integrity_ok:
        missing.append("SESSION_INTEGRITY")
    from war_room_task_contract import profiled
    if packet_error is None and profiled(packet):
        evidence_error = _qa_evidence_validation(con, task, packet)
        if evidence_error:
            missing.append(evidence_error)
    from war_room_stage_recovery import issues
    if issues(con, task["id"], int(task["revision"])):
        missing.append("PROCESSING_UNRESOLVED")
    return {"binding": binding, "missing": missing}


def _has_current_representative_approval(
    con: sqlite3.Connection, task: sqlite3.Row, binding: tuple[Any, ...]
) -> bool:
    return bool(con.execute(
        """SELECT 1 FROM war_representative_approvals
           WHERE task_id=? AND decision='approved' AND task_revision=? AND scope_hash=?
           AND document_version=? AND qa_cycle=? ORDER BY created_at DESC LIMIT 1""",
        binding,
    ).fetchone())


@router.get("/projects/{project_id}/readiness")
def project_readiness(
    project_id: str,
    request: Request,
    x_war_room_actor: str | None = Header(default=None),
    x_war_room_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return a fail-closed projection using representative-approval prerequisites."""
    state_counts = {state: 0 for state in PROCESS_BOARD_STATES}
    blocking_task_ids: list[str] = []
    blocking_reasons: dict[str, list[str]] = {}
    nonblocking_superseded_ids: list[str] = []
    considered_task_count = 0

    with _connect_ro() as con:
        war_room._project_or_404(con, project_id)
        _actor(con, x_war_room_actor, "read", project_id, x_war_room_token, request)
        items = _process_board_projection(con, project_id)
        for item in items:
            task_id = item["task_id"]
            state = item["mapped_state"]
            state_counts[state] += 1
            if state == "SUPERSEDED":
                nonblocking_superseded_ids.append(task_id)
                continue
            considered_task_count += 1
            reasons: list[str] = []
            task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                reasons.append("TASK_RECORD_MISSING")
            elif state == "PASS":
                if task["status"] != "qa":
                    reasons.append("TASK_NOT_IN_QA")
                reasons.extend(_representative_completion_checks(con, task)["missing"])
            elif state == "DONE":
                checks = _representative_completion_checks(con, task)
                reasons.extend(checks["missing"])
                if not _has_current_representative_approval(con, task, checks["binding"]):
                    reasons.append("REPRESENTATIVE_APPROVAL")
            else:
                reasons.append(f"STATE_{state}")
            if reasons:
                blocking_task_ids.append(task_id)
                blocking_reasons[task_id] = sorted(set(reasons))

    blocking_task_ids.sort()
    nonblocking_superseded_ids.sort()
    return {
        "mode": "readonly",
        "project_id": project_id,
        "state_counts": state_counts,
        "blocking_task_ids": blocking_task_ids,
        "blocking_reasons": blocking_reasons,
        "nonblocking_superseded_ids": nonblocking_superseded_ids,
        "considered_task_count": considered_task_count,
        "evaluated_task_count": considered_task_count,
        "ready_for_representative_completion": bool(
            considered_task_count > 0 and not blocking_task_ids
        ),
    }


@router.get("/projects/{project_id}/mutation-context")
def mutation_context(
    project_id: str,
    request: Request,
    action: str,
    target_id: str | None = None,
    x_war_room_actor: str | None = Header(default=None),
    x_war_room_token: str | None = Header(default=None),
) -> dict[str, Any]:
    target_type = _FRESH_CONTEXT_ACTIONS.get(action)
    if target_type is None:
        raise HTTPException(422, "unsupported mutation context action")
    with _connect_ro() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "read", project_id, x_war_room_token, request)
        _require_representative(actor)
        if target_type == "project":
            effective_target = project_id
            if target_id not in {None, "", project_id}:
                raise HTTPException(422, "project mutation target mismatch")
        else:
            if not isinstance(target_id, str) or not target_id:
                raise HTTPException(422, "task target_id required")
            row = con.execute(
                "SELECT 1 FROM war_tasks WHERE id=? AND project_id=?", (target_id, project_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Task not found")
            effective_target = target_id
        now = _now()
        payload = {
            "v": 1,
            "actor": actor,
            "project_id": project_id,
            "action": action,
            "target_id": effective_target,
            "state_version": _mutation_state_fingerprint(con, project_id, action, effective_target),
            "iat": now,
            "exp": now + _FRESH_CONTEXT_TTL_SECONDS,
        }
        token = _encode_fresh_context(payload)
    return {
        "mode": "readonly",
        "project_id": project_id,
        "action": action,
        "target_id": effective_target,
        "expires_at": payload["exp"],
        "state_version": payload["state_version"],
        "context_token": token,
    }


@router.post("/tasks/{task_id}/supersede")
async def supersede_task(
    task_id: str,
    request: Request,
    x_war_room_actor: str | None = Header(default=None),
    x_war_room_token: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Mark a terminal/non-running task as superseded by a verified replacement.

    This is an audit-only relation. The source task lifecycle row remains
    unchanged and append-only history is preserved; Process Board projects
    the source as SUPERSEDED.
    """
    body = await _body(request)
    replacement_task_id = body.get("replacement_task_id")
    reason = body.get("reason")
    if not isinstance(replacement_task_id, str) or not replacement_task_id.strip():
        raise HTTPException(422, "replacement_task_id required")
    replacement_task_id = replacement_task_id.strip()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
        raise HTTPException(422, "reason must be 1..1024 characters")
    reason = reason.strip()

    with _connect_rw() as con:
        source = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not source:
            raise HTTPException(404, "Task not found")
        actor = _actor(
            con, x_war_room_actor, "manage", source["project_id"],
            x_war_room_token, request,
        )
        _require_representative(actor)
        _require_mutable_project(con, source["project_id"])
        idem_scope = f"POST:/tasks/{task_id}/supersede"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        _require_fresh_context(
            con, body, actor=actor, project_id=source["project_id"],
            action="task_supersede", target_id=task_id,
        )

        if source["status"] not in {"rework_required", "draft", "stopped", "stop_unconfirmed"}:
            raise HTTPException(409, "only inactive failed/draft/stopped tasks may be superseded")
        if replacement_task_id == task_id:
            raise HTTPException(422, "replacement task must differ from source task")
        replacement = con.execute(
            "SELECT * FROM war_tasks WHERE id=? AND project_id=?",
            (replacement_task_id, source["project_id"]),
        ).fetchone()
        if not replacement:
            raise HTTPException(409, "replacement task must belong to the same project")
        latest_verdict = con.execute(
            """SELECT verdict FROM war_qa_verdicts
               WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (replacement_task_id,),
        ).fetchone()
        if (
            replacement["status"] not in {"qa", "completed"}
            or not latest_verdict
            or str(latest_verdict["verdict"]).upper() != "PASS"
        ):
            raise HTTPException(409, "replacement task requires latest QA PASS")

        if source["source_message_id"]:
            active_delivery = con.execute(
                """SELECT 1 FROM war_deliveries
                   WHERE message_id=? AND status IN ('queued','sent','received') LIMIT 1""",
                (source["source_message_id"],),
            ).fetchone()
            if active_delivery:
                raise HTTPException(409, "source task still has an active delivery")
        active_run = con.execute(
            """SELECT 1 FROM war_execution_runs WHERE war_task_id=?
               AND lower(run_status) NOT IN
               ('pass','fail','completed','failed','blocked','cancelled','canceled','timed_out','stopped')
               LIMIT 1""",
            (task_id,),
        ).fetchone()
        if active_run:
            raise HTTPException(409, "source task still has an active execution run")

        existing = con.execute(
            """SELECT 1 FROM war_audit_events
               WHERE target_type='task' AND target_id=? AND event_type='task_superseded'
               LIMIT 1""",
            (task_id,),
        ).fetchone()
        if existing:
            raise HTTPException(409, "task is already superseded")

        correlation = str(uuid.uuid4())
        payload = {
            "replacement_task_id": replacement_task_id,
            "reason": war_room._redact_string(reason),
            "source_status": source["status"],
            "replacement_status": replacement["status"],
            "replacement_qa_verdict": "PASS",
        }
        _audit(
            con, source["project_id"], actor, "task_superseded", "task",
            task_id, payload, correlation,
        )
        result = {
            "mode": "controlled",
            "task_id": task_id,
            "projected_state": "SUPERSEDED",
            "replacement_task_id": replacement_task_id,
            "correlation_id": correlation,
        }
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result



def _document_roots_for_registration(con: sqlite3.Connection, project_id: str, task_id: str | None) -> list[str]:
    if task_id:
        task = con.execute("SELECT project_id FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task or str(task["project_id"]) != project_id:
            raise HTTPException(422, "document task does not belong to project")
        packet_row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
        if packet_row:
            try:
                packet = json.loads(packet_row[0])
            except (TypeError, ValueError):
                packet = {}
            roots = packet.get("approved_paths") or [packet.get("worktree")]
            roots = [value for value in roots if isinstance(value, str) and value.startswith("/")]
            if roots:
                return roots
    configured = os.environ.get("PLACHEM_WAR_ROOM_WORKTREE")
    return [configured] if configured and configured.startswith("/") else [str(Path.cwd().resolve())]


@router.get("/projects/{project_id}/documents")
def list_documents(project_id: str, task_id: str | None = None, category: str | None = None) -> dict[str, Any]:
    with _connect_ro() as con:
        war_room._project_or_404(con, project_id)
        try:
            items = list_project_documents(con, project_id, task_id=task_id, category=category)
        except DocumentRegistrationError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"mode": "readonly", "items": war_room._redact(items)}


@router.get("/documents/{document_id}")
def get_document_record(document_id: str) -> dict[str, Any]:
    with _connect_ro() as con:
        item = get_project_document(con, document_id)
        if not item:
            raise HTTPException(404, "Document not found")
    return {"mode": "readonly", "document": war_room._redact(item)}


@router.get("/documents/{document_id}/versions")
def get_document_versions(document_id: str) -> dict[str, Any]:
    with _connect_ro() as con:
        if not get_project_document(con, document_id):
            raise HTTPException(404, "Document not found")
        items = list_document_versions(con, document_id)
    return {"mode": "readonly", "items": war_room._redact(items)}


@router.post("/projects/{project_id}/documents/register", status_code=201)
async def register_project_document(
    project_id: str,
    request: Request,
    x_war_room_actor: str | None = Header(default=None),
    x_war_room_token: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        con.execute("BEGIN IMMEDIATE")
        idem_scope = f"POST:/projects/{project_id}/documents/register"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        task_id = body.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise HTTPException(422, "task_id must be a non-empty string")
        expected_version = body.get("expected_version")
        if expected_version is not None and (not isinstance(expected_version, int) or expected_version < 0):
            raise HTTPException(422, "expected_version must be a non-negative integer")
        try:
            result = register_document(
                con,
                project_id=project_id,
                title=body.get("title"),
                uri=body.get("uri"),
                approved_roots=_document_roots_for_registration(con, project_id, task_id),
                created_by=actor,
                category=body.get("category"),
                summary=body.get("summary") or "",
                source_task_id=task_id,
                source_agent_id=actor,
                source_session_id=None,
                source_run_id=None,
                document_id=body.get("document_id"),
                expected_version=expected_version,
                relation=body.get("relation"),
            )
        except DocumentRegistrationError as exc:
            status_code = 409 if "version conflict" in str(exc) else 422
            raise HTTPException(status_code, str(exc)) from exc
        correlation = str(uuid.uuid4())
        _audit(
            con, project_id, actor,
            "document_version_registered" if result["changed"] else "document_registration_noop",
            "document", result["document_id"],
            {"version": result["version"], "uri": result["uri"], "sha256": result["sha256"], "task_id": task_id},
            correlation,
        )
        response = {"mode": "controlled", **result, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, response)
        con.commit()
        return response


@router.get("/projects/{project_id}/tasks")
def list_tasks(project_id: str, status: str | None = None, assignee_agent_id: str | None = None, q: str | None = None) -> dict[str, Any]:
    if assignee_agent_id is not None:
        assignee_agent_id = _canonical_agent_id(assignee_agent_id, "assignee_agent_id")
        if assignee_agent_id is None:
            raise HTTPException(422, "assignee_agent_id is not allowed")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        rows = con.execute("""SELECT t.*,m.original_body AS instruction_body
            FROM war_tasks t LEFT JOIN war_messages m ON m.id=t.source_message_id
            WHERE t.project_id=? ORDER BY t.updated_at DESC""", (project_id,)).fetchall()
        agents={row["id"]:[item[0] for item in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=? ORDER BY agent_id",(row["id"],)).fetchall()] for row in rows}
        from war_room_task_contract import public_contract
        from war_room_stage_recovery import issues
        reviews = {}
        for row in rows:
            evidence_count = con.execute("SELECT COUNT(*) FROM war_evidence WHERE task_id=?", (row["id"],)).fetchone()[0]
            verdict = con.execute("SELECT verdict,qa_principal,created_at FROM war_qa_verdicts WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (row["id"],)).fetchone()
            reviews[row["id"]] = {"evidence_count": evidence_count, "latest_qa_verdict": dict(verdict) if verdict else None, "execution_contract": public_contract(con, row["id"]), "processing_issues": issues(con, row["id"], row["revision"])}
        delivery_map = {row["id"]: con.execute("SELECT status,error_class,retry_count,attempt_count,max_attempts FROM war_deliveries WHERE message_id=?", (row["source_message_id"],)).fetchall() if row["source_message_id"] else [] for row in rows}
    items = []
    for row in rows:
        base = {**dict(row), "agent_ids": agents[row["id"]], **reviews[row["id"]]}
        deliveries = delivery_map[row["id"]]
        system_errors = sum(1 for delivery in deliveries if _delivery_state(delivery) == "system_error")
        base.update({"system_error_count": system_errors, "state": "system_error" if system_errors else row["status"]})
        if status is not None and row["status"] != status:
            continue
        if assignee_agent_id is not None and row["assignee_agent_id"] != assignee_agent_id:
            continue
        if q is not None and q.lower() not in row["scope"].lower():
            continue
        items.append(base)
    return {"mode": "readonly", "items": war_room._redact(items)}


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """Resolve one exact task independently of the currently selected project."""
    with _connect_rw() as con:
        row = con.execute(
            """SELECT t.*,m.original_body AS instruction_body
               FROM war_tasks t LEFT JOIN war_messages m ON m.id=t.source_message_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        agent_ids = [item[0] for item in con.execute(
            "SELECT agent_id FROM war_task_agents WHERE task_id=? ORDER BY agent_id", (task_id,)
        ).fetchall()]
        evidence_count = con.execute(
            "SELECT COUNT(*) FROM war_evidence WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        verdict = con.execute(
            """SELECT verdict,qa_principal,created_at FROM war_qa_verdicts
               WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        deliveries = con.execute(
            "SELECT status,error_class,retry_count,attempt_count,max_attempts FROM war_deliveries WHERE message_id=?",
            (row["source_message_id"],),
        ).fetchall() if row["source_message_id"] else []
        from war_room_task_contract import public_contract
        execution_contract = public_contract(con, task_id)
        from war_room_stage_recovery import issues
        processing_issues = issues(con, task_id, row["revision"])
    item = {
        **dict(row),
        "execution_contract": execution_contract,
        "processing_issues": processing_issues,
        "agent_ids": agent_ids,
        "evidence_count": evidence_count,
        "latest_qa_verdict": dict(verdict) if verdict else None,
    }
    system_errors = sum(1 for delivery in deliveries if _delivery_state(delivery) == "system_error")
    item.update({"system_error_count": system_errors, "state": "system_error" if system_errors else row["status"]})
    return {"mode": "readonly", "task": war_room._redact(item)}


@router.post("/projects/{project_id}/participants", status_code=201)
async def add_participant(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    participant = _canonical_agent_id(body.get("principal_id"), "principal_id")
    role = body.get("role", "developer")
    if participant is None or role not in ROLE_PERMISSIONS:
        raise HTTPException(422, "participant or role not allowed")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/participants"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        if con.execute(
            "SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=?",
            (project_id, participant),
        ).fetchone():
            raise HTTPException(409, "agent is already a project participant")
        registration = load_agent_catalog().get(participant)
        if not registration or not registration.registered or not registration.enabled:
            raise HTTPException(409, "participant must be registered and enabled")
        # QA is an independent reviewer role, not an execution target.  It
        # must remain usable even when the Gateway correctly excludes that
        # principal from Worker admission.
        if role != "qa" and not registration.execution_eligible:
            raise HTTPException(409, "execution participant must be Gateway-allowed")
        row_id = f"participant-{project_id}-{participant}"
        defaults = ROLE_PERMISSIONS[role]
        flags: dict[str, int] = {}
        for capability, column in (("read","can_read"),("comment","can_comment"),("approve","can_approve"),("execute","can_execute")):
            value = body.get(column, capability in defaults)
            if not isinstance(value, bool):
                raise HTTPException(422, f"{column} must be boolean")
            if value and capability not in defaults:
                raise HTTPException(422, f"{column} exceeds role capability")
            flags[column] = int(value)
        con.execute("""INSERT INTO war_participants
            (id,project_id,principal_type,principal_id,role,can_read,can_comment,can_approve,can_execute,active)
            VALUES (?,?,'agent',?,?,?,?,?,?,1)
            """,
            (row_id, project_id, participant, role, flags["can_read"], flags["can_comment"], flags["can_approve"], flags["can_execute"]))
        correlation = str(uuid.uuid4()); _audit(con, project_id, actor, "participant_upserted", "participant", row_id, {"role": role}, correlation)
        result = {"mode": "controlled", "participant_id": row_id, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/tasks/{task_id}/approvals")
async def approve_task(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    decision = body.get("decision")
    if decision not in {"approved", "rejected"}:
        raise HTTPException(422, "decision must be approved or rejected")
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task: raise HTTPException(404, "Task not found")
        _validate_mutation_contract(body, task)
        actor = _actor(con, x_war_room_actor, "approve", task["project_id"], x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, task["project_id"])
        idem_scope = f"POST:/tasks/{task_id}/approvals"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        if task["status"] != "awaiting_approval": raise HTTPException(409, "Task is not awaiting approval")
        if not task["assignee_agent_id"] or task["call_limit"] is None or task["turn_limit"] is None or task["deadline_at"] is None or not task["document_version"]:
            raise HTTPException(409, "task execution policy is incomplete")
        scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
        agent_ids = sorted(row[0] for row in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=?", (task_id,)).fetchall())
        _require_independent_reviewer(task["reviewer_agent_id"], agent_ids, required=False)
        target_set_hash = hashlib.sha256(json.dumps(agent_ids).encode()).hexdigest()
        approval_id, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        now = _now()
        expires_at = body.get("expires_at")
        if decision == "approved" and expires_at is None:
            raise HTTPException(422, "approval expires_at required")
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= now or expires_at > now + 604800):
            raise HTTPException(422, "approval expiry must be within 7 days")
        con.execute("INSERT INTO war_approvals(id,task_id,approver_id,decision,scope_hash,document_version,assignee_agent_id,target_set_hash,expires_at,revoked_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (approval_id, task_id, actor, decision, scope_hash, task["document_version"], task["assignee_agent_id"], target_set_hash, expires_at, None, now))
        new_status = "approved" if decision == "approved" else "draft"
        con.execute("UPDATE war_tasks SET status=?, updated_at=? WHERE id=?", (new_status, _now(), task_id))
        _audit(con, task["project_id"], actor, "task_approval_" + decision, "task", task_id, {"approval_id": approval_id, "scope_hash": scope_hash}, correlation)
        result = {"mode": "controlled", "task_id": task_id, "status": new_status, "approval_id": approval_id, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/tasks/{task_id}/reviewer")
async def assign_existing_task_reviewer(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Assign a reviewer to one legacy task without rewriting its history."""
    body = await _body(request)
    reviewer = _canonical_agent_id(body.get("reviewer_agent_id"), "reviewer_agent_id")
    reason = body.get("reason")
    expected_revision = body.get("task_revision")
    if reviewer is None:
        raise HTTPException(422, "registered reviewer_agent_id required")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
        raise HTTPException(422, "reason must be 1..1024 characters")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise HTTPException(422, "task_revision must be a positive integer")
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        actor = _actor(con, x_war_room_actor, "manage", task["project_id"], x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, task["project_id"])
        con.execute("BEGIN IMMEDIATE")
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        idem_scope = f"POST:/tasks/{task_id}/reviewer"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        if task["status"] == "completed":
            raise HTTPException(409, "completed task reviewer is immutable")
        if task["reviewer_agent_id"] is not None:
            raise HTTPException(409, "task reviewer is already assigned")
        if int(task["revision"]) != expected_revision:
            raise HTTPException(409, "task revision changed")
        participant = con.execute(
            """SELECT principal_id,role,active,can_read,can_comment FROM war_participants
               WHERE project_id=? AND principal_id=?""",
            (task["project_id"], reviewer),
        ).fetchone()
        catalog_entry = load_agent_catalog().get(reviewer)
        if (not catalog_entry or not catalog_entry.registered or not catalog_entry.enabled
                or not participant or not participant["active"] or participant["role"] != "qa"
                or not participant["can_read"] or not participant["can_comment"]):
            raise HTTPException(409, "reviewer must be a registered, enabled, active project QA participant")
        executors = sorted(row[0] for row in con.execute(
            "SELECT agent_id FROM war_task_agents WHERE task_id=?", (task_id,)
        ).fetchall())
        _require_independent_reviewer(reviewer, executors)
        active_delivery = con.execute(
            """SELECT 1 FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
               WHERE m.id=? AND d.status IN ('queued','sent','received') LIMIT 1""",
            (task["source_message_id"],),
        ).fetchone() if task["source_message_id"] else None
        active_run = con.execute(
            """SELECT 1 FROM war_execution_runs WHERE war_task_id=?
               AND lower(run_status) NOT IN ('pass','fail','completed','failed','blocked','cancelled','canceled','timed_out','stopped') LIMIT 1""",
            (task_id,),
        ).fetchone()
        if active_delivery or active_run:
            raise HTTPException(409, "queued or active task execution must finish before reviewer assignment")
        now = _now()
        revoked_ids = [row[0] for row in con.execute(
            """SELECT id FROM war_approvals WHERE task_id=? AND decision='approved'
               AND revoked_at IS NULL AND expires_at>? ORDER BY created_at,id""",
            (task_id, now),
        ).fetchall()]
        if revoked_ids:
            con.executemany("UPDATE war_approvals SET revoked_at=? WHERE id=?", [(now, value) for value in revoked_ids])
        updated = con.execute(
            """UPDATE war_tasks SET reviewer_agent_id=?,status='awaiting_approval',
               revision=revision+1,updated_at=?
               WHERE id=? AND reviewer_agent_id IS NULL AND status<>'completed' AND revision=?""",
            (reviewer, now, task_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "task changed during reviewer assignment")
        correlation = str(uuid.uuid4())
        _audit(con, task["project_id"], actor, "legacy_task_reviewer_assigned", "task", task_id, {
            "reviewer_agent_id": reviewer,
            "reason": war_room._redact_string(reason.strip()),
            "previous_status": task["status"],
            "previous_revision": expected_revision,
            "new_revision": expected_revision + 1,
            "revoked_approval_ids": revoked_ids,
        }, correlation)
        result = {
            "mode": "controlled", "task_id": task_id, "reviewer_agent_id": reviewer,
            "status": "awaiting_approval", "revision": expected_revision + 1,
            "revoked_approval_ids": revoked_ids, "new_approval_required": True,
            "correlation_id": correlation,
        }
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/tasks/{task_id}/transition")
async def transition_task(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request); target = body.get("status")
    if target not in {"awaiting_approval", "running", "qa", "completed", "stopped", "stop_unconfirmed", "rework_required"}:
        raise HTTPException(422, "unsupported transition")
    if target == "completed":
        raise HTTPException(403, "completion requires representative approval endpoint")
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task: raise HTTPException(404, "Task not found")
        _validate_mutation_contract(body, task)
        permission = "execute" if target in {"running", "stopped", "stop_unconfirmed", "qa"} else "manage"
        actor = _actor(con, x_war_room_actor, permission, task["project_id"], x_war_room_token, request)
        _require_mutable_project(con, task["project_id"])
        idem_scope = f"POST:/tasks/{task_id}/transition"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        if target in {"running","qa","completed"}:
            _require_not_stopped(con, task["project_id"])
        if target not in TRANSITIONS.get(task["status"], set()): raise HTTPException(409, f"invalid transition {task['status']} -> {target}")
        if target in {"stopped", "stop_unconfirmed"}:
            _require_representative(actor)
        if target == "running":
            _require_representative(actor)
            assigned = sorted(row[0] for row in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=?", (task_id,)).fetchall())
            _require_independent_reviewer(task["reviewer_agent_id"], assigned, required=False)
            _require_execution_agents(con, task["project_id"], assigned)
            approval = con.execute("SELECT * FROM war_approvals WHERE task_id=? AND decision='approved' AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            now = _now()
            expected_scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
            expected_target_hash = hashlib.sha256(json.dumps(assigned).encode()).hexdigest()
            approval_is_current = bool(
                approval and approval["expires_at"] is not None and int(approval["expires_at"]) > now
                and approval["scope_hash"] == expected_scope_hash
                and approval["document_version"] == task["document_version"]
                and approval["assignee_agent_id"] == task["assignee_agent_id"]
                and approval["target_set_hash"] == expected_target_hash
            )
            effective_deadline = int(task["deadline_at"] or 0)
            if effective_deadline <= now and int(task["revision"]) > 1 and approval_is_current:
                # Compatibility repair for a rework approval created before
                # deadline renewal was introduced. The already authenticated
                # approval expiry is the strict upper bound; history is kept.
                effective_deadline = int(approval["expires_at"])
                con.execute(
                    "UPDATE war_tasks SET deadline_at=?,updated_at=? WHERE id=?",
                    (effective_deadline, now, task_id),
                )
                _audit(con, task["project_id"], actor, "task_deadline_aligned_to_fresh_approval", "task", task_id, {
                    "approval_id": approval["id"],
                    "previous_deadline_at": task["deadline_at"],
                    "deadline_at": effective_deadline,
                    "task_revision": task["revision"],
                }, str(uuid.uuid4()))
            if (not task["assignee_agent_id"] or task["call_limit"] is None or task["turn_limit"] is None
                    or effective_deadline <= now
                    or not task["document_version"]
                    or not approval_is_current):
                raise HTTPException(409, "valid current approval required")
        if target == "qa" and task["source_message_id"]:
            deliveries = con.execute(
                "SELECT status,response_message_id,error_code FROM war_deliveries WHERE message_id=? AND task_revision=?",
                (task["source_message_id"], int(task["revision"])),
            ).fetchall()
            if (not deliveries or any(row["status"] != "responded" or not row["response_message_id"]
                                     or row["error_code"] for row in deliveries)):
                raise HTTPException(409, "only fully responded successful deliveries may enter QA")
            if task["execution_mode"] == "FAST_GATEWAY":
                runs = con.execute(
                    "SELECT lower(run_status) FROM war_execution_runs WHERE war_task_id=?",
                    (task_id,),
                ).fetchall()
                if not runs or any(row[0] not in {"pass", "completed"} for row in runs):
                    raise HTTPException(409, "validated PASS execution required before QA")
        if target == "completed":
            binding = (task_id, task["revision"], hashlib.sha256(task["scope"].encode()).hexdigest(), task["document_version"], task["qa_cycle"])
            evidence_count = con.execute("SELECT COUNT(*) FROM war_evidence WHERE task_id=? AND task_revision=? AND scope_hash=? AND document_version=? AND qa_cycle=?", binding).fetchone()[0]
            verdict = con.execute("SELECT 1 FROM war_qa_verdicts WHERE task_id=? AND verdict='PASS' AND task_revision=? AND scope_hash=? AND document_version=? AND qa_cycle=? ORDER BY created_at DESC LIMIT 1", binding).fetchone()
            if not evidence_count or not verdict:
                raise HTTPException(409, "signed QA PASS and evidence required")
        correlation = str(uuid.uuid4())
        renewed_deadline = None
        revoked_approval_ids: list[str] = []
        if target == "awaiting_approval" and task["status"] in {"rework_required", "stopped", "stop_unconfirmed", "qa"}:
            now = _now()
            renewed_deadline = body.get("deadline_at", now + 1800)
            if (isinstance(renewed_deadline, bool) or not isinstance(renewed_deadline, int)
                    or renewed_deadline <= now or renewed_deadline > now + 604800):
                raise HTTPException(422, "deadline_at must be within the next 7 days")
            revoked_approval_ids = [row[0] for row in con.execute(
                """SELECT id FROM war_approvals WHERE task_id=? AND decision='approved'
                   AND revoked_at IS NULL ORDER BY created_at,id""",
                (task_id,),
            ).fetchall()]
            if revoked_approval_ids:
                con.executemany(
                    "UPDATE war_approvals SET revoked_at=? WHERE id=?",
                    [(now, approval_id) for approval_id in revoked_approval_ids],
                )
        if target == "qa":
            con.execute("UPDATE war_tasks SET status=?,qa_cycle=qa_cycle+1,updated_at=? WHERE id=?", (target, _now(), task_id))
        elif target in {"awaiting_approval", "rework_required"} and task["status"] in {"stopped", "stop_unconfirmed", "qa"}:
            con.execute(
                "UPDATE war_tasks SET status=?,revision=revision+1,deadline_at=COALESCE(?,deadline_at),updated_at=? WHERE id=?",
                (target, renewed_deadline, _now(), task_id),
            )
        elif target == "awaiting_approval" and task["status"] == "rework_required":
            # QA FAIL already advanced the revision. Renew only the execution
            # window here so the preserved revision receives a fresh approval.
            con.execute(
                "UPDATE war_tasks SET status=?,deadline_at=?,updated_at=? WHERE id=?",
                (target, renewed_deadline, _now(), task_id),
            )
        else:
            con.execute("UPDATE war_tasks SET status=?, updated_at=? WHERE id=?", (target, _now(), task_id))
        audit_detail: dict[str, Any] = {"from": task["status"], "to": target}
        if renewed_deadline is not None:
            audit_detail.update({
                "previous_deadline_at": task["deadline_at"],
                "deadline_at": renewed_deadline,
                "revoked_approval_ids": revoked_approval_ids,
                "new_approval_required": True,
            })
        _audit(con, task["project_id"], actor, "task_transition", "task", task_id, audit_detail, correlation)
        result = {"mode": "controlled", "task_id": task_id, "status": target, "correlation_id": correlation}
        if renewed_deadline is not None:
            result.update({
                "deadline_at": renewed_deadline,
                "revoked_approval_ids": revoked_approval_ids,
                "new_approval_required": True,
            })
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/tasks/{task_id}/representative-completion")
async def representative_completion(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    if body.get("decision") not in {"approved", "rejected"}:
        raise HTTPException(422, "decision must be approved or rejected")
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        actor = war_room._request_principal(request, x_war_room_actor, x_war_room_token)
        if not actor:
            raise HTTPException(401, "Authenticated War Room actor required")
        _require_representative(actor)
        actor = _actor(con, x_war_room_actor, "manage", task["project_id"], x_war_room_token, request)
        _require_mutable_project(con, task["project_id"])
        _require_not_stopped(con, task["project_id"])
        con.execute("BEGIN IMMEDIATE")
        idem_scope = f"POST:/tasks/{task_id}/representative-completion"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        _require_fresh_context(
            con, body, actor=actor, project_id=task["project_id"],
            action="representative_completion", target_id=task_id,
        )
        if task["status"] != "qa":
            raise HTTPException(409, "task must be in QA")
        decision = body["decision"]
        checks = _representative_completion_checks(con, task)
        binding = checks["binding"]
        if decision == "approved" and checks["missing"]:
            if "QA_SIGNATURE_UNAVAILABLE" in checks["missing"]:
                raise HTTPException(503, "QA signature verification is unavailable")
            if "GROUNDING_PACKET_INVALID" in checks["missing"]:
                raise HTTPException(409, "valid grounding packet required")
            if "CURRENT_EVIDENCE" in checks["missing"] or "SIGNED_QA_PASS" in checks["missing"]:
                raise HTTPException(409, "signed QA PASS and evidence required")
            if "SESSION_INTEGRITY" in checks["missing"]:
                raise HTTPException(409, "verified session_integrity evidence required before QA PASS and representative approval")
            raise HTTPException(409, "representative completion prerequisites not satisfied")
        approval_id, correlation, now = str(uuid.uuid4()), str(uuid.uuid4()), _now()
        con.execute("INSERT INTO war_representative_approvals VALUES (?,?,?,?,?,?,?,?,?)", (approval_id,task_id,actor,decision,task["revision"],binding[2],task["document_version"],task["qa_cycle"],now))
        status = "completed" if decision == "approved" else "rework_required"
        if decision == "rejected":
            con.execute("UPDATE war_tasks SET status=?,revision=revision+1,updated_at=? WHERE id=?", (status,now,task_id))
        else:
            con.execute("UPDATE war_tasks SET status=?,updated_at=? WHERE id=?", (status,now,task_id))
        _audit(con, task["project_id"], actor, "representative_completion_" + decision, "task", task_id, {"approval_id":approval_id}, correlation)
        result = {"mode":"controlled","task_id":task_id,"status":status,"representative_approval_id":approval_id,"correlation_id":correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/tasks/{task_id}/approvals/{approval_id}/revoke")
async def revoke_approval(task_id: str, approval_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        approval = con.execute("SELECT * FROM war_approvals WHERE id=? AND task_id=?", (approval_id, task_id)).fetchone()
        if not task or not approval: raise HTTPException(404, "Approval not found")
        actor = _actor(con, x_war_room_actor, "approve", task["project_id"], x_war_room_token, request)
        idem_scope = f"POST:/tasks/{task_id}/approvals/{approval_id}/revoke"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        if approval["revoked_at"] is not None: raise HTTPException(409, "Approval already revoked")
        now = _now(); con.execute("UPDATE war_approvals SET revoked_at=? WHERE id=?", (now, approval_id))
        correlation = str(uuid.uuid4()); _audit(con, task["project_id"], actor, "approval_revoked", "approval", approval_id, {}, correlation)
        result = {"mode": "controlled", "approval_id": approval_id, "status": "revoked", "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.get("/tasks/{task_id}/audit")
def task_audit(task_id: str) -> dict[str, Any]:
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task: raise HTTPException(404, "Task not found")
        rows = con.execute("SELECT * FROM war_audit_events WHERE target_id=? ORDER BY created_at, id", (task_id,)).fetchall()
    return {"mode": "readonly", "items": war_room._redact([dict(row) for row in rows])}


@router.get("/projects/{project_id}/audit")
def project_audit(project_id: str, limit: int = 100) -> dict[str, Any]:
    if isinstance(limit, bool) or not 1 <= int(limit) <= 200:
        raise HTTPException(422, "limit must be 1..200")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        rows = con.execute(
            "SELECT * FROM war_audit_events WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (project_id, int(limit)),
        ).fetchall()
    return {"mode": "readonly", "items": war_room._redact([dict(row) for row in rows])}


@router.get("/tasks/{task_id}/evidence")
def list_evidence(task_id: str) -> dict[str, Any]:
    with _connect_rw() as con:
        if not con.execute("SELECT 1 FROM war_tasks WHERE id=?", (task_id,)).fetchone():
            raise HTTPException(404, "Task not found")
        rows = con.execute("SELECT * FROM war_evidence WHERE task_id=? ORDER BY created_at, id", (task_id,)).fetchall()
    return {"mode": "readonly", "items": war_room._redact([dict(row) for row in rows])}


@router.post("/tasks/{task_id}/evidence", status_code=201)
async def add_evidence(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    uri, summary = body.get("uri"), body.get("summary")
    if not isinstance(uri, str) or not uri.startswith("/") or len(uri) > 1024 or not isinstance(summary, str) or len(summary) > 4096:
        raise HTTPException(422, "evidence requires local uri and summary")
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task: raise HTTPException(404, "Task not found")
        actor = _actor(con, x_war_room_actor, "comment", task["project_id"], x_war_room_token, request)
        _require_mutable_project(con, task["project_id"])
        _require_not_stopped(con, task["project_id"])
        if task["status"] != "qa":
            raise HTTPException(409, "evidence may only be added during QA")
        idem_scope = f"POST:/tasks/{task_id}/evidence"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        evidence_type = body.get("evidence_type", "file")
        integrity = body.get("session_integrity")
        if evidence_type == "session_integrity":
            required = {"scope","pre_count","post_count","changed_count","deleted_count","uncertain_count","mtime_encoding","verified_at"}
            if not isinstance(integrity, dict) or not required.issubset(integrity):
                raise HTTPException(422, "complete session_integrity summary required")
            counts = [integrity[k] for k in ("pre_count","post_count","changed_count","deleted_count","uncertain_count")]
            if (not isinstance(integrity["scope"], str) or not integrity["scope"].strip()
                    or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts)
                    or integrity["pre_count"] != integrity["post_count"]
                    or any(integrity[k] != 0 for k in ("changed_count","deleted_count","uncertain_count"))
                    or integrity["mtime_encoding"] != "decimal_string"
                    or isinstance(integrity["verified_at"], bool) or not isinstance(integrity["verified_at"], int)
                    or integrity["verified_at"] > _now()):
                raise HTTPException(409, "session integrity verification failed or uncertain")
        physical_id, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        contract_id = body.get("evidence_id")
        contract_evidence_id = None
        if contract_id is not None:
            if not isinstance(contract_id, str) or not contract_id.strip():
                raise HTTPException(422, "evidence_id must be a non-empty string")
            contract_evidence_id = contract_id.strip()
            if (not isinstance(body.get("evidence_type"), str) or not body["evidence_type"].strip()
                    or not isinstance(body.get("source_command"), str) or not body["source_command"].strip()
                    or not isinstance(body.get("expected_contains"), str)):
                raise HTTPException(422, "structured evidence requires evidence_type, source_command, expected_contains")
            if body.get("immutable") is not True or not isinstance(body.get("run_id"), str) or not body["run_id"].strip():
                raise HTTPException(422, "immutable evidence requires run_id and immutable=true")
            try:
                actual_sha = hashlib.sha256(Path(uri).read_bytes()).hexdigest()
            except OSError as exc:
                raise HTTPException(422, "evidence file is not readable") from exc
            if not isinstance(body.get("sha256"), str) or not hmac.compare_digest(actual_sha, body["sha256"]):
                raise HTTPException(409, "EVIDENCE_TAMPERED")
        scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
        created = _now()
        con.execute("""INSERT INTO war_evidence
            (id,task_id,evidence_type,uri,summary,sha256,task_revision,scope_hash,document_version,qa_cycle,
             run_id,source_command,expected_contains,immutable,contract_evidence_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (physical_id, task_id, evidence_type, uri, war_room._redact_string(summary), body.get("sha256"),
             task["revision"], scope_hash, task["document_version"], task["qa_cycle"], body.get("run_id"),
             body.get("source_command"), body.get("expected_contains"), 1 if body.get("immutable") is True else 0,
             contract_evidence_id, created))
        if evidence_type == "session_integrity":
            con.execute("INSERT INTO war_session_integrity VALUES (?,?,?,?,?,?,?,?,?,?,?)", (physical_id,task_id,integrity["scope"],integrity["pre_count"],integrity["post_count"],integrity["changed_count"],integrity["deleted_count"],integrity["uncertain_count"],integrity["mtime_encoding"],integrity["verified_at"],created))
        visible_id = contract_evidence_id or physical_id
        _audit(con, task["project_id"], actor, "evidence_added", "task", task_id, {"evidence_id": visible_id, "record_id": physical_id}, correlation)
        result = {"mode": "controlled", "evidence_id": visible_id, "record_id": physical_id, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/projects", status_code=201)
async def create_project(request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request); name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 200: raise HTTPException(422, "valid name required")
    with _connect_rw() as con:
        actor = war_room._request_principal(request, x_war_room_actor, x_war_room_token)
        if not actor or not war_room._known_principal(actor): raise HTTPException(401, "authentication required")
        if not con.execute("SELECT 1 FROM war_participants WHERE principal_id=? AND role='project_manager'", (actor,)).fetchone(): raise HTTPException(403, "project manager required")
        idem_scope = "POST:/projects"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        if con.execute("SELECT 1 FROM war_projects WHERE lower(name)=lower(?) AND status != 'archived'", (name.strip(),)).fetchone(): raise HTTPException(409, "duplicate project name")
        project_id = str(uuid.uuid4()); now = _now(); mf = body.get("manyfast_project_id", war_room.MANYFAST_PROJECT_ID); version = body.get("manyfast_version", "unknown")
        con.execute("INSERT INTO war_projects VALUES (?,?,?,?,?,?,?)", (project_id, name.strip(), "planning", mf, version, now, now))
        con.execute("INSERT INTO war_project_control(project_id,updated_at) VALUES (?,?)", (project_id, now))
        con.execute("INSERT INTO war_participants VALUES (?,?,?,?,?,?,?,?,?,?)", (f"participant-{project_id}-{actor}", project_id, "agent", actor, "project_manager", 1, 1, 1, 1, 1))
        correlation = str(uuid.uuid4()); _audit(con, project_id, actor, "project_created", "project", project_id, {"name": name}, correlation)
        result = {"mode": "controlled", "project_id": project_id, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/projects/{project_id}/archive")
async def archive_project(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id); actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        idem_scope = f"POST:/projects/{project_id}/archive"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        _require_mutable_project(con, project_id)
        now = _now(); con.execute("UPDATE war_projects SET status='archived',updated_at=? WHERE id=?", (now, project_id)); con.execute("UPDATE war_project_control SET archived_at=?,updated_at=? WHERE project_id=?", (now,now,project_id))
        correlation = str(uuid.uuid4()); _audit(con, project_id, actor, "project_archived", "project", project_id, {}, correlation)
        result = {"mode":"controlled","project_id":project_id,"status":"archived","correlation_id":correlation}; _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """R-GOAQPQ/F-XCFFIW: project lifecycle update with unique-name guard."""
    body = await _body(request)
    with _connect_rw() as con:
        project = war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        previous = _idem(con, actor, idempotency_key, f"PATCH:/projects/{project_id}", body)
        if previous:
            return previous
        name = body.get("name", project["name"])
        status = body.get("status", project["status"])
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise HTTPException(422, "valid name required")
        if status not in {"planning", "active", "paused", "archived"}:
            raise HTTPException(422, "invalid project status")
        duplicate = con.execute("SELECT 1 FROM war_projects WHERE lower(name)=lower(?) AND id<>? AND status!='archived'", (name.strip(), project_id)).fetchone()
        if duplicate:
            raise HTTPException(409, "duplicate project name")
        now = _now()
        con.execute("UPDATE war_projects SET name=?,status=?,updated_at=? WHERE id=?", (name.strip(), status, now, project_id))
        if status == "archived":
            con.execute("UPDATE war_project_control SET archived_at=?,updated_at=? WHERE project_id=?", (now, now, project_id))
        correlation = str(uuid.uuid4())
        _audit(con, project_id, actor, "project_updated", "project", project_id, {"name": name.strip(), "status": status}, correlation)
        result = {"mode": "controlled", "project_id": project_id, "name": name.strip(), "status": status, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, f"PATCH:/projects/{project_id}", body, result)
        con.commit()
        return result


@router.patch("/projects/{project_id}/participants/{principal_id}")
async def update_participant(project_id: str, principal_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """R-GOAQPQ/F-XCFFIW: role change/deactivation; observer is read-only."""
    body = await _body(request)
    principal_id = _canonical_agent_id(principal_id, "principal_id")
    if principal_id is None:
        raise HTTPException(422, "participant is not allowed")
    role = body.get("role")
    active = body.get("active")
    if role is not None and role not in ROLE_PERMISSIONS:
        raise HTTPException(422, "participant role not allowed")
    if active is not None and not isinstance(active, bool):
        raise HTTPException(422, "active must be boolean")
    for column in ("can_read", "can_comment", "can_approve", "can_execute"):
        if column in body and not isinstance(body[column], bool):
            raise HTTPException(422, f"{column} must be boolean")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        previous = _idem(con, actor, idempotency_key, f"PATCH:/projects/{project_id}/participants/{principal_id}", body)
        if previous:
            return previous
        row = con.execute("SELECT * FROM war_participants WHERE project_id=? AND principal_id=?", (project_id, principal_id)).fetchone()
        if not row:
            raise HTTPException(404, "participant not found")
        next_role = role or row["role"]
        next_active = int(active if active is not None else bool(row["active"]))
        permissions = ROLE_PERMISSIONS[next_role]
        flags: dict[str, int] = {}
        for capability, column in (("read","can_read"),("comment","can_comment"),("approve","can_approve"),("execute","can_execute")):
            value = body.get(column, bool(row[column]) if role is None else capability in permissions)
            if value and capability not in permissions:
                raise HTTPException(422, f"{column} exceeds role capability")
            flags[column] = int(value)
        con.execute("UPDATE war_participants SET role=?,active=?,can_read=?,can_comment=?,can_approve=?,can_execute=? WHERE id=?", (next_role, next_active, flags["can_read"], flags["can_comment"], flags["can_approve"], flags["can_execute"], row["id"]))
        correlation = str(uuid.uuid4())
        flag_result = {column: bool(value) for column, value in flags.items()}
        _audit(con, project_id, actor, "participant_updated", "participant", row["id"], {"principal_id": principal_id, "role": next_role, "active": bool(next_active), **flag_result}, correlation)
        result = {"mode": "controlled", "participant_id": row["id"], "principal_id": principal_id, "role": next_role, "active": bool(next_active), **flag_result, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, f"PATCH:/projects/{project_id}/participants/{principal_id}", body, result)
        con.commit()
        return result


@router.post("/projects/{project_id}/manyfast-reference")
@router.put("/projects/{project_id}/manyfast-reference")
async def save_manyfast_reference(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Persist an optional document reference without mutating existing work."""
    body = await _body(request)
    version = body.get("document_version", body.get("manyfast_version"))
    manyfast_project_id = body.get("manyfast_project_id", war_room.MANYFAST_PROJECT_ID)
    if not isinstance(version, str) or not version.strip() or not isinstance(manyfast_project_id, str):
        raise HTTPException(422, "manyfast_project_id and document_version required")
    with _connect_rw() as con:
        project = war_room._project_or_404(con, project_id)
        actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        idem_scope = f"{request.method}:/projects/{project_id}/manyfast-reference"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        old_version = str(project["manyfast_version"])
        drift = old_version != version.strip() or str(project["manyfast_project_id"]) != manyfast_project_id
        now = _now()
        ref_id = str(uuid.uuid4())
        task_id = body.get("task_id")
        con.execute("INSERT INTO war_manyfast_refs (id,project_id,task_id,manyfast_project_id,document_version,linked_by,created_at,drift_status,previous_document_version) VALUES (?,?,?,?,?,?,?,?,?)", (ref_id, project_id, task_id, manyfast_project_id, version.strip(), actor, now, "drift" if drift else "current", old_version if drift else None))
        con.execute("UPDATE war_projects SET manyfast_project_id=?,manyfast_version=?,updated_at=? WHERE id=?", (manyfast_project_id, version.strip(), now, project_id))
        correlation = str(uuid.uuid4())
        _audit(con, project_id, actor, "manyfast_reference_" + ("changed" if drift else "saved"), "project", project_id, {"old_version": old_version, "new_version": version.strip(), "existing_tasks_preserved": True}, correlation)
        result = {"mode": "controlled", "project_id": project_id, "manyfast_project_id": manyfast_project_id, "document_version": version.strip(), "drift": drift, "invalidated_tasks": 0, "existing_tasks_preserved": True, "correlation_id": correlation}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.get("/projects/{project_id}/manyfast-reference")
def list_manyfast_references(project_id: str) -> dict[str, Any]:
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        rows = con.execute("SELECT * FROM war_manyfast_refs WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
    return {"mode": "readonly", "items": war_room._redact([dict(row) for row in rows])}


@router.post("/projects/{project_id}/manyfast-snapshot", status_code=201)
async def save_manyfast_snapshot(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    version = body.get("document_version")
    if not isinstance(version, str) or not version.strip() or not isinstance(body.get("snapshot"), dict):
        raise HTTPException(422, "document_version and snapshot required")
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id); actor = _actor(con, x_war_room_actor, "manage", project_id, x_war_room_token, request)
        _require_mutable_project(con, project_id)
        scope = f"POST:/projects/{project_id}/manyfast-snapshot"; previous = _idem(con, actor, idempotency_key, scope, body)
        if previous: return previous
        snapshot_id = str(uuid.uuid4()); now = _now()
        con.execute("UPDATE war_manyfast_snapshots SET is_last_good=0 WHERE project_id=?", (project_id,))
        con.execute("INSERT INTO war_manyfast_snapshots VALUES (?,?,?,?,?,?)", (snapshot_id, project_id, version.strip(), json.dumps(war_room._redact(body["snapshot"]), sort_keys=True), 1, now))
        result = {"mode":"controlled", "snapshot_id":snapshot_id, "document_version":version.strip(), "last_good":True}
        _save_idem(con, actor, idempotency_key, scope, body, result); con.commit(); return result


@router.get("/projects/{project_id}/manyfast-snapshot")
def get_manyfast_snapshot(project_id: str) -> dict[str, Any]:
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id)
        row = con.execute("SELECT * FROM war_manyfast_snapshots WHERE project_id=? AND is_last_good=1 ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
    return {"mode":"readonly", "snapshot": war_room._redact(dict(row)) if row else None}


@router.post("/projects/{project_id}/stop")
async def stop_project(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id); actor = _actor(con, x_war_room_actor, "execute", project_id, x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/stop"; previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        _require_fresh_context(
            con, body, actor=actor, project_id=project_id,
            action="project_stop", target_id=project_id,
        )
        now = _now(); deadline = now + 300
        con.execute("UPDATE war_project_control SET stop_requested_at=?,stop_deadline=?,stop_state='stop_requested',updated_at=? WHERE project_id=?", (now,deadline,now,project_id))
        con.execute("UPDATE war_deliveries SET status='stopped',error_code='project_stop_barrier',stop_cycle_at=? WHERE status='queued' AND message_id IN (SELECT id FROM war_messages WHERE project_id=?)", (now,project_id))
        active = con.execute(
            """SELECT d.*,t.id AS task_id,COALESCE(t.execution_mode,'LEGACY') AS execution_mode FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
               LEFT JOIN war_tasks t ON t.source_message_id=m.id
               WHERE m.project_id=? AND d.status IN ('sent','received')""",
            (project_id,),
        ).fetchall()
        stop_results: list[dict[str, str]] = []
        for delivery in active:
            from war_room_worker import stop_bound_delivery
            active_adapter, receipt = stop_bound_delivery(con, delivery, adapter=_adapter(), adapter_selector=_adapter_for_mode)
            if delivery["execution_mode"] == "FAST_GATEWAY":
                snapshotter = getattr(active_adapter, "execution_snapshot", None)
                if snapshotter:
                    from war_room_worker import _persist_execution
                    _persist_execution(con, snapshot=snapshotter(delivery["id"]), project_id=project_id, task_id=delivery["task_id"], agent_id=delivery["agent_id"], now=now)
            status = receipt.status if receipt.status in {"stopped", "failed", "timed_out"} else "failed"
            con.execute("UPDATE war_deliveries SET status=?,error_code=?,stop_cycle_at=? WHERE id=?", (status, receipt.error_code, now, delivery["id"]))
            stop_results.append({"delivery_id": delivery["id"], "status": status})
        if any(item["status"] in {"failed", "timed_out"} for item in stop_results):
            final_state = "stop_failed"
        elif all(item["status"] == "stopped" for item in stop_results):
            final_state = "stopped"
        else:
            final_state = "stop_requested"
        con.execute("UPDATE war_project_control SET stop_state=?,updated_at=? WHERE project_id=?", (final_state, now, project_id))
        con.execute("UPDATE war_approvals SET revoked_at=? WHERE revoked_at IS NULL AND task_id IN (SELECT id FROM war_tasks WHERE project_id=?)", (now, project_id))
        task_status = "stopped" if final_state == "stopped" else "stop_unconfirmed"
        con.execute("UPDATE war_tasks SET status=?,revision=revision+1,updated_at=? WHERE project_id=? AND status IN ('approved','running','qa','rework_required')", (task_status, now, project_id))
        correlation = str(uuid.uuid4()); _audit(con, project_id, actor, "project_stop_requested", "project", project_id, {"deadline":deadline,"stop_results":stop_results,"final_state":final_state}, correlation); result = {"mode":"controlled","project_id":project_id,"status":final_state,"deadline":deadline,"deliveries":stop_results,"correlation_id":correlation}; _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Stop only the selected task and its live Worker deliveries."""

    body = await _body(request)
    with _connect_rw() as con:
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        actor = _actor(con, x_war_room_actor, "execute", task["project_id"], x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, task["project_id"])
        idem_scope = f"POST:/tasks/{task_id}/stop"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        _require_fresh_context(
            con, body, actor=actor, project_id=task["project_id"],
            action="task_stop", target_id=task_id,
        )
        if task["status"] == "completed":
            raise HTTPException(409, "completed task cannot be stopped")
        now = _now()
        correlation = str(uuid.uuid4())
        queued = con.execute(
            "SELECT id FROM war_deliveries WHERE message_id=? AND status='queued'",
            (task["source_message_id"],),
        ).fetchall() if task["source_message_id"] else []
        queued_ids = [row["id"] for row in queued]
        if queued_ids:
            marks = ",".join("?" for _ in queued_ids)
            con.execute(
                f"UPDATE war_deliveries SET status='stopped',error_code='task_stop_before_dispatch',stop_cycle_at=? WHERE id IN ({marks})",
                (now, *queued_ids),
            )
        active = con.execute(
            """SELECT d.*,COALESCE(t.execution_mode,'LEGACY') AS execution_mode
               FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
               LEFT JOIN war_tasks t ON t.source_message_id=m.id
               WHERE t.id=? AND d.status IN ('sent','received')""",
            (task_id,),
        ).fetchall()
        stop_results: list[dict[str, Any]] = [
            {"delivery_id": delivery_id, "status": "stopped", "error_code": "task_stop_before_dispatch"}
            for delivery_id in queued_ids
        ]
        for delivery in active:
            from war_room_worker import stop_bound_delivery
            active_adapter, receipt = stop_bound_delivery(con, delivery, adapter=_adapter(), adapter_selector=_adapter_for_mode)
            status = receipt.status if receipt.status in {"stopped", "failed", "timed_out"} else "failed"
            con.execute(
                "UPDATE war_deliveries SET status=?,error_code=?,stop_cycle_at=? WHERE id=? AND status IN ('sent','received')",
                (status, receipt.error_code, now, delivery["id"]),
            )
            snapshotter = getattr(active_adapter, "execution_snapshot", None)
            if snapshotter:
                from war_room_worker import _persist_execution
                _persist_execution(
                    con, snapshot=snapshotter(delivery["id"]), project_id=task["project_id"],
                    task_id=task_id, agent_id=delivery["agent_id"], now=now,
                )
            stop_results.append({
                "delivery_id": delivery["id"], "status": status,
                "error_code": receipt.error_code,
            })
        if any(item["status"] in {"failed", "timed_out"} for item in stop_results):
            final_status = "stop_unconfirmed"
        else:
            final_status = "stopped"
        con.execute(
            "UPDATE war_approvals SET revoked_at=? WHERE task_id=? AND decision='approved' AND revoked_at IS NULL",
            (now, task_id),
        )
        if task["status"] != final_status:
            con.execute(
                "UPDATE war_tasks SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                (final_status, now, task_id),
            )
        _audit(
            con, task["project_id"], actor, "task_stop_requested", "task", task_id,
            {"deliveries": stop_results, "confirmed": final_status == "stopped"}, correlation,
        )
        result = {
            "mode": "controlled", "task_id": task_id, "status": final_status,
            "stop_requested": bool(active or queued_ids), "confirmed": final_status == "stopped",
            "delivery_ids": [item["delivery_id"] for item in stop_results],
            "deliveries": stop_results, "correlation_id": correlation,
        }
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/projects/{project_id}/stop-ack")
async def stop_ack(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id); actor = _actor(con, x_war_room_actor, "execute", project_id, x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/stop-ack"; previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        control = _control(con, project_id)
        if control["stop_requested_at"] is None or control["stop_state"] not in {"stop_requested", "stopped", "stop_unconfirmed", "stop_failed"}:
            raise HTTPException(409, "stop was not requested")
        delivery_id = body.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise HTTPException(422, "delivery_id required")
        delivery = con.execute("SELECT d.* FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id WHERE d.id=? AND m.project_id=?", (delivery_id, project_id)).fetchone()
        if not delivery or delivery["status"] != "stopped" or delivery["stop_cycle_at"] != control["stop_requested_at"]:
            raise HTTPException(409, "ACK must match a stopped project delivery")
        cycle_state = _stop_cycle_state(con, project_id, int(control["stop_requested_at"]))
        con.execute("UPDATE war_project_control SET stop_state=?,updated_at=? WHERE project_id=?", (cycle_state,_now(),project_id)); correlation=str(uuid.uuid4()); _audit(con, project_id, actor, "project_stop_ack", "delivery", delivery_id, {"cycle_state":cycle_state}, correlation); result={"mode":"controlled","project_id":project_id,"delivery_id":delivery_id,"status":cycle_state,"correlation_id":correlation}; _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request)
    with _connect_rw() as con:
        war_room._project_or_404(con, project_id); actor = _actor(con, x_war_room_actor, "approve", project_id, x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, project_id)
        idem_scope = f"POST:/projects/{project_id}/resume"; previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous: return previous
        _require_fresh_context(
            con, body, actor=actor, project_id=project_id,
            action="project_resume", target_id=project_id,
        )
        control = _control(con, project_id)
        if control["stop_state"] != "stopped": raise HTTPException(409,"project stop cycle is not fully confirmed")
        cycle_at = int(control["stop_requested_at"] or 0)
        if con.execute("""SELECT 1 FROM war_tasks t WHERE t.project_id=?
            AND EXISTS (
              SELECT 1 FROM war_messages m
              JOIN war_deliveries d ON d.message_id=m.id
              WHERE m.id=t.source_message_id AND m.project_id=t.project_id AND d.stop_cycle_at=?
            )
            AND NOT EXISTS (SELECT 1 FROM war_approvals a WHERE a.task_id=t.id AND a.decision='approved'
              AND a.revoked_at IS NULL AND a.created_at>? AND a.expires_at>?) LIMIT 1""",
            (project_id, cycle_at, cycle_at, _now())).fetchone():
            raise HTTPException(409,"fresh approval required for every stopped task")
        con.execute("UPDATE war_project_control SET stop_state='running',stop_requested_at=NULL,stop_deadline=NULL,updated_at=? WHERE project_id=?", (_now(),project_id)); correlation=str(uuid.uuid4()); _audit(con, project_id, actor, "project_resumed", "project", project_id, {}, correlation); result={"mode":"controlled","project_id":project_id,"status":"running","correlation_id":correlation}; _save_idem(con, actor, idempotency_key, idem_scope, body, result); con.commit(); return result


def _qa_evidence_validation(con: sqlite3.Connection, task: sqlite3.Row, packet: dict[str, Any], submitted_ids: list[str] | None = None) -> str | None:
    required = _normalize_required_evidence(packet.get("required_evidence", ["test", "artifact"]))
    required_ids = [item["id"] for item in required]
    scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
    rows = con.execute(
        "SELECT * FROM war_evidence WHERE task_id=? AND task_revision=? AND scope_hash=? AND document_version=? AND qa_cycle=? ORDER BY created_at,id",
        (task["id"], task["revision"], scope_hash, task["document_version"], task["qa_cycle"]),
    ).fetchall()
    legacy = all(item.get("legacy") for item in required)
    def contract_id(row: sqlite3.Row) -> str:
        if legacy:
            return str(row["evidence_type"])
        value = row["contract_evidence_id"] if "contract_evidence_id" in row.keys() else None
        return str(value or row["id"])

    actual_ids = [contract_id(row) for row in rows]
    submitted = actual_ids if submitted_ids is None else submitted_ids
    if len(submitted) != len(set(submitted)) or set(submitted) != set(required_ids):
        return "QA_CONTRACT_ERROR"
    selected = [row for row in rows if contract_id(row) in set(required_ids)]
    if len(selected) != len(required_ids):
        return "QA_CONTRACT_ERROR"
    if legacy:
        return None
    from war_room_task_contract import profiled, verify_receipt
    if profiled(packet):
        if required_ids != ["execution_receipt"] or len(selected) != 1:
            return "QA_CONTRACT_ERROR"
        return verify_receipt(con, task, selected[0])
    for row in selected:
        expected = next(item for item in required if item["id"] == contract_id(row))
        if (str(row["evidence_type"] or "") != expected["evidence_type"]
                or str(row["source_command"] or "") != expected["source_command"]
                or str(row["expected_contains"] or "") != expected["expected_contains"]):
            return "QA_CONTRACT_ERROR"
    for row in selected:
        expected = next(item for item in required if item["id"] == contract_id(row))
        if int(row["immutable"] or 0) != 1 or not row["sha256"] or row["run_id"] is None or row["source_command"] is None:
            return "EVIDENCE_UNVERIFIED"
        try:
            data = Path(row["uri"]).read_bytes()
        except OSError:
            return "EVIDENCE_UNVERIFIED"
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(row["sha256"])):
            return "EVIDENCE_UNVERIFIED"
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return "EVIDENCE_UNVERIFIED"
        if expected["expected_contains"] and expected["expected_contains"] not in content:
            return "EVIDENCE_UNVERIFIED"
        evidence_scope = packet.get("approved_paths") or [packet.get("worktree")]
        if not isinstance(evidence_scope, list) or not war_room.path_within_approved_roots(str(row["uri"]), evidence_scope):
            return "EVIDENCE_SCOPE_VIOLATION"
    return None


@router.post("/tasks/{task_id}/qa-verdict")
async def qa_verdict(task_id: str, request: Request, x_war_room_actor: str | None = Header(default=None), x_war_room_token: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    body = await _body(request); verdict = body.get("verdict")
    if verdict not in {"PASS","FAIL","REWORK"}: raise HTTPException(422,"invalid verdict")
    qa_principal = _canonical_agent_id(body.get("qa_principal"), "qa_principal")
    if qa_principal is None:
        raise HTTPException(422, "qa_principal required")
    with _connect_rw() as con:
        task=con.execute("SELECT * FROM war_tasks WHERE id=?",(task_id,)).fetchone()
        if not task: raise HTTPException(404,"Task not found")
        actor=_actor(con,x_war_room_actor,"comment",task["project_id"],x_war_room_token,request)
        idem_scope = f"POST:/tasks/{task_id}/qa-verdict"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        _require_mutable_project(con, task["project_id"])
        _require_not_stopped(con, task["project_id"])
        if task["status"] != "qa":
            raise HTTPException(409,"QA verdict may only be submitted during QA")
        qa_row = con.execute("SELECT role,active FROM war_participants WHERE project_id=? AND principal_id=?", (task["project_id"], actor)).fetchone()
        performed = con.execute("SELECT 1 FROM war_task_agents WHERE task_id=? AND agent_id=?", (task_id, actor)).fetchone()
        if (actor != qa_principal or actor == task["assignee_agent_id"] or performed
                or task["reviewer_agent_id"] != actor
                or not qa_row or qa_row["role"] != "qa" or not qa_row["active"]):
            raise HTTPException(403,"independent QA principal required")
        packet_row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
        packet = json.loads(packet_row[0]) if packet_row else {}
        required_list = _normalize_required_evidence(packet.get("required_evidence", ["test", "artifact"]))
        required_ids = [item["id"] for item in required_list]
        profile = "required:" + ",".join(required_ids)
        scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
        binding = {"task_revision":task["revision"],"scope_hash":scope_hash,"document_version":task["document_version"],"qa_cycle":task["qa_cycle"]}
        payload=json.dumps({"task_id":task_id,"verdict":verdict,"evidence_profile":profile,"qa_principal":qa_principal,**binding},sort_keys=True); signature=body.get("signature")
        if body.get("source") == "agent_result":
            signature = _qa_signature(payload)
        elif not signature or not hmac.compare_digest(signature,_qa_signature(payload)):
            raise HTTPException(403,"invalid QA signature")
        submitted_ids = body.get("evidence_ids") or body.get("verified_evidence_ids")
        if submitted_ids is None and isinstance(body.get("evidence_profile"), str) and body["evidence_profile"].startswith("required:"):
            profile_ids = [value for value in body["evidence_profile"][9:].split(",") if value]
            submitted_ids = profile_ids or None
        if submitted_ids is not None and (not isinstance(submitted_ids, list) or not all(isinstance(value, str) and value for value in submitted_ids)):
            raise HTTPException(409, "QA_CONTRACT_ERROR")
        if verdict == "PASS":
            evidence_error = _qa_evidence_validation(con, task, packet, submitted_ids)
            if evidence_error:
                raise HTTPException(409, evidence_error)
        evidence_types={r[0] for r in con.execute("SELECT evidence_type FROM war_evidence WHERE task_id=? AND task_revision=? AND scope_hash=? AND document_version=? AND qa_cycle=?",(task_id, task["revision"], scope_hash, task["document_version"], task["qa_cycle"])).fetchall()}
        if verdict == "PASS" and packet.get("session_integrity_required") is True and "session_integrity" not in evidence_types:
            raise HTTPException(409, "session_integrity evidence required before QA PASS")
        vid, correlation, now = str(uuid.uuid4()), str(uuid.uuid4()), _now()
        con.execute("INSERT INTO war_qa_verdicts (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(vid,task_id,actor,verdict,profile,signature,payload,task["revision"],scope_hash,task["document_version"],task["qa_cycle"],now))
        status = task["status"]
        _audit(con, task["project_id"], actor, "qa_verdict_recorded", "task", task_id, {
            "verdict_id": vid,
            "verdict": verdict,
            "task_revision": task["revision"],
            "qa_cycle": task["qa_cycle"],
        }, correlation)
        if verdict in {"FAIL", "REWORK"}:
            status = "rework_required"
            con.execute("UPDATE war_tasks SET status=?,revision=revision+1,updated_at=? WHERE id=?", (status, now, task_id))
            _audit(con, task["project_id"], actor, "qa_verdict_rework_required", "task", task_id, {"verdict_id":vid,"verdict":verdict,"from":"qa","to":status}, correlation)
        result = {"mode":"controlled","verdict_id":vid,"verdict":verdict,"status":status}
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result


@router.post("/tasks/{task_id}/resume-qa")
async def resume_task_qa(task_id: str, request: Request,
                         x_war_room_actor: str | None = Header(default=None),
                         x_war_room_token: str | None = Header(default=None),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Resume QA only; neither worker execution nor final approval is performed."""
    body = await _body(request)
    with _connect_rw() as con:
        con.execute("BEGIN IMMEDIATE")
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        _validate_mutation_contract(body, task)
        actor = _actor(con, x_war_room_actor, "execute", task['project_id'], x_war_room_token, request)
        _require_representative(actor)
        _require_mutable_project(con, task['project_id'])
        _require_not_stopped(con, task['project_id'])
        idem_scope = f"POST:/tasks/{task_id}/resume-qa"
        previous = _idem(con, actor, idempotency_key, idem_scope, body)
        if previous:
            return previous
        _require_fresh_context(con, body, actor=actor, project_id=task['project_id'],
                              action="task_resume_qa", target_id=task_id)
        from war_room_stage_recovery import resume_qa
        try:
            result = resume_qa(con, task, adapter=_adapter(), now=_now())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        result.update(task_id=task_id, task_revision=task['revision'])
        _audit(con, task['project_id'], actor, 'qa_only_resume_requested', 'task', task_id,
               result, str(uuid.uuid4()))
        _save_idem(con, actor, idempotency_key, idem_scope, body, result)
        con.commit()
        return result
