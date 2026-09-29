"""Stage-local recovery. Original executions and approvals are never rewritten."""
from __future__ import annotations
import json
import hashlib
import sqlite3
import uuid
from pathlib import Path
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


def recover_false_qa_rework(
    con: sqlite3.Connection,
    task: Any,
    *,
    core_run_id: str,
    qa_verdict_id: str,
    recovery_code: str,
    adapter: Any,
    now: int,
) -> dict[str, Any]:
    """Open one fresh QA cycle for a false QA rework, without worker dispatch."""
    if task['status'] != 'rework_required':
        raise ValueError('QA_REWORK_RECOVERY_NOT_ELIGIBLE')
    if recovery_code != 'QA_FALSE_REWORK_RECEIPT_PATH':
        raise ValueError('QA_REWORK_RECOVERY_CODE_NOT_ALLOWED')
    verdict = con.execute(
        "SELECT * FROM war_qa_verdicts WHERE id=? AND task_id=?", (qa_verdict_id, task['id'])
    ).fetchone()
    if not verdict or verdict['verdict'] not in ('FAIL', 'REWORK'):
        raise ValueError('QA_REWORK_VERDICT_NOT_FOUND')
    if int(task['revision']) <= int(verdict['task_revision']):
        raise ValueError('QA_REWORK_REVISION_NOT_ADVANCED')
    run = con.execute(
        """SELECT * FROM war_execution_runs
           WHERE core_run_id=? AND war_task_id=? AND war_project_id=? AND agent_id=?""",
        (core_run_id, task['id'], task['project_id'], task['assignee_agent_id']),
    ).fetchone()
    if not run or str(run['run_status']).lower() not in {'pass', 'completed'}:
        raise ValueError('QA_REWORK_RUN_MISMATCH')
    if run['cancel_reason'] or not run['openclaw_run_id'] or not run['session_key']:
        raise ValueError('QA_REWORK_RUN_CANCELLED_OR_UNBOUND')
    delivery = con.execute(
        """SELECT * FROM war_deliveries
           WHERE message_id=? AND agent_id=? AND task_revision=? AND run_id=?
             AND status='responded' ORDER BY created_at DESC LIMIT 1""",
        (task['source_message_id'], task['assignee_agent_id'], int(verdict['task_revision']), core_run_id),
    ).fetchone()
    run_ids = {str(run['core_run_id']), str(run['openclaw_run_id'] or '')}
    if not delivery or delivery['run_id'] not in run_ids or delivery['session_key'] != run['session_key']:
        raise ValueError('QA_REWORK_DELIVERY_BINDING_MISMATCH')
    qa_delivery = con.execute(
        """SELECT d.*,m.body AS response_body FROM war_deliveries d
           JOIN war_messages m ON m.id=d.response_message_id
           WHERE d.message_id=? AND d.agent_id=? AND d.task_revision=?
             AND d.status='responded' AND d.response_message_id IS NOT NULL
           ORDER BY d.created_at DESC,d.id DESC LIMIT 1""",
        (task['source_message_id'], task['reviewer_agent_id'], int(verdict['task_revision'])),
    ).fetchone()
    if not qa_delivery:
        raise ValueError('QA_REWORK_QA_RESPONSE_MISSING')
    try:
        qa_response = json.loads(str(qa_delivery['response_body']).strip().strip('`'))
    except (TypeError, ValueError):
        raise ValueError('QA_REWORK_QA_RESPONSE_INVALID')
    summary = str(qa_response.get('summary') or '').lower()
    if (str(qa_response.get('verdict') or '').upper() != str(verdict['verdict']).upper()
            or 'receipt' not in summary
            or not any(marker in summary for marker in ('does not exist', 'not found', 'missing'))):
        raise ValueError('QA_REWORK_RECEIPT_PATH_ERROR_UNPROVEN')
    if int(qa_delivery['task_revision']) != int(verdict['task_revision']):
        raise ValueError('QA_REWORK_QA_RESPONSE_REVISION_MISMATCH')
    approval = con.execute(
        """SELECT * FROM war_approvals
           WHERE task_id=? AND decision='approved' AND revoked_at IS NULL
             AND scope_hash=? AND document_version=? AND assignee_agent_id=?
           ORDER BY created_at DESC LIMIT 1""",
        (task['id'], hashlib.sha256(task['scope'].encode()).hexdigest(),
         task['document_version'], task['assignee_agent_id']),
    ).fetchone()
    agents = sorted(row[0] for row in con.execute(
        "SELECT agent_id FROM war_task_agents WHERE task_id=? ORDER BY agent_id", (task['id'],)
    ).fetchall())
    expected_targets = hashlib.sha256(json.dumps(agents).encode()).hexdigest()
    if (not approval or approval['expires_at'] is None or int(approval['expires_at']) <= now
            or approval['scope_hash'] != hashlib.sha256(task['scope'].encode()).hexdigest()
            or approval['document_version'] != task['document_version']
            or approval['assignee_agent_id'] != task['assignee_agent_id']
            or approval['target_set_hash'] not in {'', expected_targets}):
        raise ValueError('QA_REWORK_APPROVAL_CONTRACT_MISMATCH')
    open_issue = next((item for item in issues(con, task['id'], int(task['revision'])) if item['stage'] == 'QA'), None)
    if open_issue:
        if recoverable_stage(str(open_issue['code']), qa=True) != 'QA':
            raise ValueError('QA_REWORK_STAGE_NOT_RECOVERABLE')
        if 'RECEIPT' not in str(open_issue['code']).upper() or 'PATH' not in str(open_issue['code']).upper():
            raise ValueError('QA_REWORK_REASON_NOT_RECEIPT_PATH')
    source_rows = con.execute(
        "SELECT * FROM war_evidence WHERE task_id=? AND task_revision=? AND qa_cycle=? ORDER BY created_at,id",
        (task['id'], int(verdict['task_revision']), int(verdict['qa_cycle'])),
    ).fetchall()
    if not source_rows:
        raise ValueError('QA_REWORK_EVIDENCE_MISSING')
    if not open_issue and (verdict['verdict'] != 'REWORK'
                           or not any(row['evidence_type'] == 'execution_receipt' for row in source_rows)):
        raise ValueError('QA_REWORK_REASON_NOT_RECEIPT_PATH')
    for row in source_rows:
        if (int(row['immutable'] or 0) != 1 or not row['sha256']
                or row['run_id'] not in run_ids):
            raise ValueError('QA_REWORK_EVIDENCE_IDENTITY_INVALID')
        try:
            if hashlib.sha256(Path(row['uri']).read_bytes()).hexdigest() != str(row['sha256']):
                raise ValueError('QA_REWORK_EVIDENCE_TAMPERED')
        except OSError as exc:
            raise ValueError('QA_REWORK_EVIDENCE_UNAVAILABLE') from exc
    next_cycle = int(task['qa_cycle']) + 1
    con.execute("UPDATE war_tasks SET status='qa',qa_cycle=?,updated_at=? WHERE id=? AND status='rework_required'",
                (next_cycle, now, task['id']))
    current = con.execute("SELECT * FROM war_tasks WHERE id=?", (task['id'],)).fetchone()
    for row in source_rows:
        contract_id = row['contract_evidence_id']
        if contract_id:
            duplicate = con.execute(
                """SELECT 1 FROM war_evidence WHERE task_id=? AND task_revision=? AND qa_cycle=?
                   AND contract_evidence_id=?""",
                (task['id'], int(current['revision']), next_cycle, contract_id),
            ).fetchone()
        else:
            duplicate = con.execute(
                """SELECT 1 FROM war_evidence WHERE task_id=? AND task_revision=? AND qa_cycle=?
                   AND contract_evidence_id IS NULL AND evidence_type=? AND uri=? AND sha256 IS ?""",
                (task['id'], int(current['revision']), next_cycle, row['evidence_type'], row['uri'], row['sha256']),
            ).fetchone()
        if duplicate:
            continue
        con.execute(
            """INSERT INTO war_evidence
               (id,task_id,evidence_type,uri,summary,sha256,task_revision,scope_hash,
                document_version,qa_cycle,run_id,source_command,expected_contains,immutable,
                contract_evidence_id,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), task['id'], row['evidence_type'], row['uri'],
             'Reused immutable evidence from original Worker run', row['sha256'], current['revision'],
             row['scope_hash'], current['document_version'], next_cycle, run['core_run_id'],
             row['source_command'], row['expected_contains'], row['immutable'], contract_id, now),
        )
    from war_room_worker import _queue_auto_qa_delivery
    queued = _queue_auto_qa_delivery(
        con, task_id=task['id'], message_id=task['source_message_id'],
        execution_mode=task['execution_mode'], adapter=adapter, now=now,
    )
    if not queued:
        raise ValueError('QA_DELIVERY_UNAVAILABLE')
    if open_issue:
        resolve(con, task['id'], int(task['revision']), open_issue['delivery_id'], now)
    attempt = archive(con, current, 'QA_FALSE_REWORK_RECOVERY', delivery['id'], {
        'original_qa_verdict_id': qa_verdict_id, 'original_qa_verdict': verdict['verdict'],
        'core_run_id': core_run_id, 'worker_delivery_id': delivery['id'],
        'worker_redispatched': False, 'qa_delivery_queued': True,
    }, now)
    return {
        'status': 'qa', 'qa_cycle': next_cycle, 'task_revision': int(current['revision']),
        'original_qa_verdict_id': qa_verdict_id, 'original_qa_verdict': verdict['verdict'],
        'original_worker_delivery_id': delivery['id'], 'worker_redispatched': False,
        'qa_delivery_queued': True, 'stage_attempt_id': attempt,
    }
