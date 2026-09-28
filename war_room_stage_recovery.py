"""Stage-local recovery. Original executions and approvals are never rewritten."""
from __future__ import annotations
import json
import sqlite3
import uuid
from typing import Any


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS war_processing_issues (
        task_id TEXT NOT NULL, task_revision INTEGER NOT NULL,
        delivery_id TEXT NOT NULL, stage TEXT NOT NULL, code TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'OPEN', created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, PRIMARY KEY(task_id,task_revision,delivery_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS war_stage_attempts (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_revision INTEGER NOT NULL,
        stage TEXT NOT NULL, delivery_id TEXT, record_json TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")


def mark(con: sqlite3.Connection, task: Any, delivery_id: str, stage: str, code: str, now: int) -> None:
    ensure_schema(con)
    con.execute("""INSERT INTO war_processing_issues
        (task_id,task_revision,delivery_id,stage,code,state,created_at,updated_at)
        VALUES (?,?,?,?,?,'OPEN',?,?) ON CONFLICT(task_id,task_revision,delivery_id)
        DO UPDATE SET stage=excluded.stage,code=excluded.code,state='OPEN',updated_at=excluded.updated_at""",
        (task['id'], int(task['revision']), delivery_id, stage, str(code)[:1000], now, now))


def resolve(con: sqlite3.Connection, task_id: str, revision: int, delivery_id: str, now: int) -> None:
    con.execute("UPDATE war_processing_issues SET state='RESOLVED',updated_at=? WHERE task_id=? AND task_revision=? AND delivery_id=?",
                (now, task_id, revision, delivery_id))


def issues(con: sqlite3.Connection, task_id: str, revision: int) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute("SELECT * FROM war_processing_issues WHERE task_id=? AND task_revision=? AND state='OPEN' ORDER BY created_at,delivery_id", (task_id, revision))]


def archive(con: sqlite3.Connection, task: Any, stage: str, delivery_id: str | None, record: dict, now: int) -> str:
    attempt = str(uuid.uuid4())
    con.execute("INSERT INTO war_stage_attempts VALUES (?,?,?,?,?,?,?)",
                (attempt, task['id'], task['revision'], stage, delivery_id,
                 json.dumps(record, ensure_ascii=False, sort_keys=True), now))
    return attempt


def recoverable_stage(code: str, *, qa: bool = False) -> str | None:
    upper = code.upper()
    if any(s in upper for s in ('SCOPE_VIOLATION','NON_COMPLIANT','AUTH_', 'IDENTITY_MISMATCH', 'TAMPERED', 'READ_ONLY_', 'AUTHORITY_EXCEEDED')):
        return None
    if qa:
        return 'QA'
    if any(s in upper for s in ('_VALIDATION_FAILED:', 'MISSING_RESULT', 'RESULT_FORMAT', 'RESULT_VALIDATION_REQUIRED', 'STRUCTURED_RESPONSE_', 'EVIDENCE_', 'CONTEXT_MISMATCH')):
        return 'RESULT'
    if any(s in upper for s in ('TRANSPORT', 'OBSERVATION', 'HISTORY_', 'WAIT_REJECTED')):
        return 'RECONCILE'
    return None


def resume_qa(con: sqlite3.Connection, task: Any, *, adapter: Any, now: int) -> dict[str, Any]:
    """Retry only infrastructure-failed QA, preserving worker revisions and results."""
    from war_room_worker import _capture_required_evidence, _queue_auto_qa_delivery
    if task['status'] != 'qa':
        raise ValueError('QA_STAGE_NOT_RESUMABLE')
    review = con.execute("SELECT verdict FROM war_qa_verdicts WHERE task_id=? AND task_revision=? AND qa_cycle=? ORDER BY created_at DESC LIMIT 1",
                         (task['id'], task['revision'], task['qa_cycle'])).fetchone()
    if review and review['verdict'] in ('PASS','FAIL','REWORK'):
        raise ValueError('QA_VERDICT_ALREADY_RECORDED')
    if not task['reviewer_agent_id']:
        raise ValueError('QA_REVIEWER_MISSING')
    previous = con.execute("SELECT * FROM war_deliveries WHERE message_id=? AND agent_id=? AND task_revision=?",
                           (task['source_message_id'], task['reviewer_agent_id'], task['revision'])).fetchone()
    if previous and previous['status'] in ('queued','sent','received'):
        return {'status': 'already_running', 'delivery_id': previous['id'], 'worker_redispatched': False}
    if previous:
        archive(con, task, 'QA_RETRY', previous['id'], dict(previous), now)
        # The old delivery snapshot and result message remain immutable in the archive.
        con.execute("DELETE FROM war_deliveries WHERE id=?", (previous['id'],))
    con.execute("UPDATE war_tasks SET qa_cycle=qa_cycle+1,deadline_at=MAX(COALESCE(deadline_at,0),?),updated_at=? WHERE id=? AND revision=? AND status='qa'",
                (now+3600, now, task['id'], task['revision']))
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task['id'],)).fetchone()
    _capture_required_evidence(con, task_id=task['id'], message_id=task['source_message_id'], task_revision=task['revision'], now=now)
    queued = _queue_auto_qa_delivery(con, task_id=task['id'], message_id=task['source_message_id'],
                                    execution_mode=task['execution_mode'], adapter=adapter, now=now)
    if queued:
        con.execute("UPDATE war_processing_issues SET state='RESOLVED',updated_at=? WHERE task_id=? AND task_revision=? AND stage='QA'",
                    (now, task['id'], task['revision']))
    return {'status': 'queued' if queued else 'qa_unavailable', 'worker_redispatched': False, 'qa_cycle': task['qa_cycle']}
