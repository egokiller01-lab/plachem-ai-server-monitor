"""Explicitly invoked War Room delivery retry and stop timer worker.

There is intentionally no import-time or service-start hook: a gateway/service
restart must not replay a terminal delivery. Retries are explicit and reuse the
original delivery identifier.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import war_room
from war_room_adapter import DeliveryReceipt, SessionAdapter


def _now() -> int:
    return int(time.time())


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=2000")
    columns={row[1] for row in con.execute("PRAGMA table_info(war_deliveries)")}
    if "claim_token" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN claim_token TEXT")
    if "claim_expires_at" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN claim_expires_at INTEGER")
    if "stop_cycle_at" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN stop_cycle_at INTEGER")
    return con


def _audit(con: sqlite3.Connection, project_id: str, event: str, target_id: str, payload: dict[str, Any]) -> None:
    con.execute(
        "INSERT INTO war_audit_events VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), project_id, "worker", event, "delivery", target_id,
         json.dumps(war_room._redact(payload), sort_keys=True), str(uuid.uuid4()), _now()),
    )


def _structured_result(con: sqlite3.Connection, message_id: str, agent_id: str, body: str) -> tuple[dict[str, Any] | None, str | None]:
    task = con.execute("SELECT id FROM war_tasks WHERE source_message_id=?", (message_id,)).fetchone()
    if not task or not con.execute("SELECT 1 FROM war_grounding_packets WHERE task_id=?", (task["id"],)).fetchone():
        return {}, None
    try:
        text = body.strip()
        if "```" in text:
            text = text.split("```", 2)[1].removeprefix("json").strip()
        result = json.loads(text)
    except (ValueError, TypeError, IndexError):
        return None, "structured_response_invalid_json"
    required = {"confirmed_worktree","confirmed_revision","verdict","evidence","summary","representative_completion_claimed"}
    if not isinstance(result, dict) or not required.issubset(result):
        return None, "structured_response_fields_missing"
    packet = json.loads(con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task["id"],)).fetchone()[0])
    expected_revision = packet["revision"]
    confirmed_revision = result["confirmed_revision"]
    normalized_expected = expected_revision.strip().lower() if isinstance(expected_revision, str) else expected_revision
    normalized_confirmed = confirmed_revision.strip().lower() if isinstance(confirmed_revision, str) else confirmed_revision
    revisions_match = normalized_confirmed == normalized_expected
    if (
        not revisions_match
        and isinstance(normalized_confirmed, str)
        and isinstance(normalized_expected, str)
        and len(normalized_confirmed) >= 7
        and len(normalized_expected) >= 7
        and re.fullmatch(r"[0-9a-f]+", normalized_confirmed)
        and re.fullmatch(r"[0-9a-f]+", normalized_expected)
    ):
        revisions_match = (
            normalized_confirmed.startswith(normalized_expected)
            or normalized_expected.startswith(normalized_confirmed)
        )
    if result["confirmed_worktree"] != packet["worktree"] or not revisions_match:
        return None, "context_mismatch"
    if result["verdict"] not in {"PASS","FAIL","REWORK"} or not isinstance(result["summary"], str):
        return None, "structured_response_invalid_verdict"
    if not isinstance(result["evidence"], list) or not result["evidence"] or any(not isinstance(v, str) or not v.startswith("/") for v in result["evidence"]):
        return None, "structured_response_evidence_missing"
    if result["representative_completion_claimed"] is not False:
        return None, "representative_authority_exceeded"
    return result, None


def _apply_collaboration_outcome(con: sqlite3.Connection, task_id: str, project_id: str, message_id: str, now: int) -> None:
    rows = con.execute("""SELECT d.agent_id,m.body FROM war_deliveries d LEFT JOIN war_messages m ON m.id=d.response_message_id
        WHERE d.message_id=? AND d.status='responded'""", (message_id,)).fetchall()
    parsed = {}
    for row in rows:
        result, error = _structured_result(con, message_id, row["agent_id"], row["body"] or "")
        if error or not result:
            continue
        parsed[row["agent_id"]] = result
    verdicts = {agent: result["verdict"] for agent, result in parsed.items()}
    qa_failure = verdicts.get("ERPqa") in {"FAIL","REWORK"}
    conflicting = len(set(verdicts.values())) > 1
    if qa_failure or conflicting:
        con.execute("UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=?", (now, task_id))
        _audit(con, project_id, "collaboration_conflict_rework", task_id, {"verdicts":verdicts,"qa_priority":qa_failure})
    else:
        con.execute("UPDATE war_tasks SET status='qa',qa_cycle=qa_cycle+1,updated_at=? WHERE id=? AND status='running'", (now, task_id))


def _terminal_validation_failure(con: sqlite3.Connection, *, message_id: str, project_id: str, delivery_id: str, error_code: str, now: int) -> None:
    task = con.execute("SELECT id,status FROM war_tasks WHERE source_message_id=?", (message_id,)).fetchone()
    if not task:
        return
    con.execute("UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=? AND status!='rework_required'", (now, task["id"]))
    con.execute("UPDATE war_deliveries SET status='failed',error_code='cancelled_after_terminal_validation' WHERE message_id=? AND id!=? AND status='queued'", (message_id, delivery_id))
    _audit(con, project_id, "terminal_response_validation_rework", delivery_id, {"task_id":task["id"],"error_code":error_code,"queued_policy":"cancelled","in_flight_policy":"finish_without_state_override"})


def process_due_deliveries(*, db_path: str | Path, adapter: SessionAdapter, now: int | None = None) -> list[dict[str, Any]]:
    """Process only explicitly queued, due rows; never replay terminal rows."""
    current = _now() if now is None else int(now)
    results: list[dict[str, Any]] = []
    with _connect(db_path) as con:
        rows = con.execute(
            """SELECT d.*,m.project_id,m.body FROM war_deliveries d
               JOIN war_messages m ON m.id=d.message_id
               WHERE (d.status='queued' OR (d.status='sent' AND d.run_id IS NULL AND d.claim_expires_at<=?))
                 AND COALESCE(d.next_attempt_at,d.created_at)<=?
               ORDER BY d.created_at,d.id""", (current,current),
        ).fetchall()
        for row in rows:
            active_other = con.execute("SELECT 1 FROM war_deliveries WHERE agent_id=? AND id!=? AND status IN ('sent','received') LIMIT 1", (row["agent_id"], row["id"])).fetchone()
            if active_other:
                con.execute("UPDATE war_deliveries SET error_code='agent_busy_queued' WHERE id=? AND status='queued'", (row["id"],))
                con.commit()
                results.append({"delivery_id":row["id"],"status":"queued","reason":"agent_busy"})
                continue
            claim_token=str(uuid.uuid4())
            claimed = con.execute("""UPDATE war_deliveries SET status='sent',claim_token=?,claim_expires_at=?
                WHERE id=? AND (status='queued' OR (status='sent' AND run_id IS NULL AND claim_expires_at<=?))""", (claim_token,current+30,row["id"],current))
            if claimed.rowcount != 1:
                continue
            con.commit()
            if row["deadline_at"] is not None and int(row["deadline_at"]) <= current:
                con.execute("UPDATE war_deliveries SET status='timed_out',error_code='delivery_deadline_exceeded' WHERE id=?", (row["id"],))
                _audit(con, row["project_id"], "delivery_timed_out", row["id"], {"reason":"deadline"})
                results.append({"delivery_id":row["id"],"status":"timed_out"})
                continue
            binding = con.execute("SELECT session_key,session_id,purpose,disposable FROM war_project_sessions WHERE project_id=? AND agent_id=? AND enabled=1 LIMIT 1", (row["project_id"],row["agent_id"])).fetchone()
            binder = getattr(adapter, "bind_delivery", None)
            if binder and not binding:
                receipt = DeliveryReceipt(row["id"], "failed", error_code="explicit_session_binding_missing")
            else:
                if binder:
                    try:
                        binder(row["id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]), agent_id=row["agent_id"])
                    except ValueError:
                        receipt = DeliveryReceipt(row["id"], "failed", error_code="session_binding_not_disposable_test")
                    else:
                        receipt = adapter.deliver(delivery_id=row["id"], agent_id=row["agent_id"], instruction_id=row["message_id"], body=row["body"])
                else:
                    receipt = adapter.deliver(delivery_id=row["id"], agent_id=row["agent_id"], instruction_id=row["message_id"], body=row["body"])
            status = receipt.status if receipt.status in {"received","responded","failed","timed_out","stopped"} else "failed"
            response_message_id = row["response_message_id"]
            response_body = getattr(receipt, "response_body", None)
            if status == "responded" and (not isinstance(response_body, str) or not response_body.strip()) and not response_message_id:
                status = "failed"
                receipt = DeliveryReceipt(row["id"], "failed", run_id=receipt.run_id, error_code="response_body_missing")
            if status == "responded" and isinstance(response_body, str) and response_body.strip():
                _, validation_error = _structured_result(con, row["message_id"], row["agent_id"], response_body)
                if validation_error:
                    status = "failed"
                    receipt = DeliveryReceipt(row["id"], "failed", run_id=receipt.run_id, error_code=validation_error)
                    _terminal_validation_failure(con, message_id=row["message_id"], project_id=row["project_id"], delivery_id=row["id"], error_code=validation_error, now=current)
            if status == "responded" and isinstance(response_body, str) and response_body.strip() and not response_message_id:
                response_message_id = str(uuid.uuid4())
                con.execute(
                    """INSERT INTO war_messages
                       (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,correlation_id,redaction_state)
                       VALUES (?,?,'result','agent',?,?,?,?,?,'clean')""",
                    (response_message_id,row["project_id"],row["agent_id"],war_room._redact_string(response_body),row["message_id"],current,str(uuid.uuid4())),
                )
            con.execute(
                """UPDATE war_deliveries SET status=?,attempt_count=attempt_count+1,
                   sent_at=COALESCE(sent_at,?),
                   received_at=CASE WHEN ? IN ('received','responded') THEN ? ELSE received_at END,
                   responded_at=CASE WHEN ?='responded' THEN ? ELSE responded_at END,
                   error_code=?,run_id=COALESCE(?,run_id),response_message_id=?,claim_token=NULL,claim_expires_at=NULL WHERE id=? AND claim_token=?""",
                (status,current,status,current,status,current,receipt.error_code,receipt.run_id,response_message_id,row["id"],claim_token),
            )
            task = con.execute("SELECT id FROM war_tasks WHERE source_message_id=?", (row["message_id"],)).fetchone()
            if task:
                con.execute("INSERT INTO war_task_calls(task_id,call_count,turn_count,updated_at) VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET call_count=call_count+1,turn_count=turn_count+?,updated_at=?", (task["id"],1,1 if status=="responded" else 0,current,1 if status=="responded" else 0,current))
                if status == "responded":
                    pending = con.execute("SELECT COUNT(*) FROM war_deliveries WHERE message_id=? AND status!='responded'", (row["message_id"],)).fetchone()[0]
                    if pending == 0:
                        _apply_collaboration_outcome(con, task["id"], row["project_id"], row["message_id"], current)
            _audit(con, row["project_id"], "delivery_"+status, row["id"], {"error_code":receipt.error_code})
            results.append({"delivery_id":row["id"],"status":status})
        con.commit()
    return results


def request_delivery_retry(*, db_path: str | Path, delivery_id: str, now: int | None = None) -> bool:
    """Explicitly requeue a failed/timed-out delivery without making a new row."""
    current = _now() if now is None else int(now)
    with _connect(db_path) as con:
        row = con.execute("SELECT * FROM war_deliveries WHERE id=?", (delivery_id,)).fetchone()
        if not row or row["status"] not in {"failed","timed_out"}:
            return False
        max_attempts = int(row["max_attempts"] or 3)
        if int(row["attempt_count"]) >= max_attempts:
            return False
        con.execute("UPDATE war_deliveries SET status='queued',next_attempt_at=?,error_code=NULL WHERE id=?", (current,delivery_id))
        project = con.execute("SELECT project_id FROM war_messages WHERE id=?", (row["message_id"],)).fetchone()
        if project:
            _audit(con, project["project_id"], "delivery_retry_requested", delivery_id, {"attempt_count":row["attempt_count"]})
        con.commit()
        return True


def recover_received_deliveries(*, db_path: str | Path, gateway: Any, now: int | None = None) -> list[dict[str, Any]]:
    """Poll non-terminal received runs after worker restart without resending."""
    current = _now() if now is None else int(now)
    results: list[dict[str, Any]] = []
    with _connect(db_path) as con:
        rows = con.execute("SELECT d.*,m.project_id,m.body FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id WHERE d.status='received' AND d.run_id IS NOT NULL").fetchall()
        for row in rows:
            if row["deadline_at"] is not None and int(row["deadline_at"]) <= current:
                con.execute("UPDATE war_deliveries SET status='timed_out',error_code='delivery_deadline_exceeded' WHERE id=?", (row["id"],))
                _audit(con, row["project_id"], "delivery_recovered_timed_out", row["id"], {"run_id": row["run_id"]})
                results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": "timed_out"})
                continue
            binding = con.execute(
                "SELECT session_key,session_id,purpose,disposable FROM war_project_sessions WHERE project_id=? AND agent_id=? AND enabled=1 LIMIT 1",
                (row["project_id"], row["agent_id"]),
            ).fetchone()
            run_binder = getattr(gateway, "bind_run", None)
            if run_binder:
                if not binding:
                    con.execute("UPDATE war_deliveries SET status='failed',error_code='explicit_session_binding_missing' WHERE id=?", (row["id"],))
                    results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": "failed"})
                    continue
                try:
                    run_binder(row["run_id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]), delivery_id=row["id"], started_at=row["sent_at"], agent_id=row["agent_id"])
                except TypeError:
                    run_binder(row["run_id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]))
                except ValueError:
                    con.execute("UPDATE war_deliveries SET status='failed',error_code='session_binding_not_disposable_test' WHERE id=?", (row["id"],))
                    results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": "failed"})
                    continue
            try:
                run = gateway.poll(run_id=row["run_id"], agent_id=row["agent_id"])
            except TypeError:
                run = gateway.poll(row["run_id"])
            status = getattr(run, "status", None)
            if status not in {"responded", "failed", "timed_out", "stopped"}:
                continue
            response_message_id = row["response_message_id"]
            response_body = getattr(run, "response_body", None)
            error_code = getattr(run, "error_code", None)
            if status == "responded" and (not isinstance(response_body, str) or not response_body.strip()) and not response_message_id:
                status = "failed"
                error_code = error_code or "response_body_missing"
            if status == "responded" and isinstance(response_body, str) and response_body.strip():
                _, validation_error = _structured_result(con, row["message_id"], row["agent_id"], response_body)
                if validation_error:
                    status = "failed"
                    error_code = validation_error
                    _terminal_validation_failure(con, message_id=row["message_id"], project_id=row["project_id"], delivery_id=row["id"], error_code=validation_error, now=current)
            if status == "responded" and isinstance(response_body, str) and response_body.strip() and not response_message_id:
                response_message_id = str(uuid.uuid4())
                con.execute(
                    """INSERT INTO war_messages
                       (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,correlation_id,redaction_state)
                       VALUES (?,?,'result','agent',?,?,?,?,?,'clean')""",
                    (response_message_id, row["project_id"], row["agent_id"], war_room._redact_string(response_body), row["message_id"], current, str(uuid.uuid4())),
                )
            con.execute("UPDATE war_deliveries SET status=?,responded_at=CASE WHEN ?='responded' THEN ? ELSE responded_at END,error_code=?,response_message_id=? WHERE id=?", (status, status, current, error_code, response_message_id, row["id"]))
            if status == "responded":
                task = con.execute("SELECT id FROM war_tasks WHERE source_message_id=?", (row["message_id"],)).fetchone()
                if task:
                    con.execute("UPDATE war_task_calls SET turn_count=turn_count+1,updated_at=? WHERE task_id=?", (current, task["id"]))
                    pending = con.execute("SELECT COUNT(*) FROM war_deliveries WHERE message_id=? AND status!='responded'", (row["message_id"],)).fetchone()[0]
                    if pending == 0:
                        _apply_collaboration_outcome(con, task["id"], row["project_id"], row["message_id"], current)
            _audit(con, row["project_id"], "delivery_recovered_" + status, row["id"], {"run_id": row["run_id"]})
            results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": status})
        con.commit()
    return results


def request_project_stop(*, db_path: str | Path, project_id: str, actor_id: str,
                         adapter: SessionAdapter, now: int | None = None,
                         deadline: int | None = None, delivery_ids: list[str] | None = None) -> dict[str, Any]:
    """Send explicit stop requests and preserve adapter failure as failed."""
    current = _now() if now is None else int(now)
    stop_deadline = current + 300 if deadline is None else int(deadline)
    results: list[dict[str, Any]] = []
    with _connect(db_path) as con:
        if delivery_ids is None:
            rows = con.execute("""SELECT d.* FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
                WHERE m.project_id=? AND d.status IN ('sent','received','responded')""", (project_id,)).fetchall()
        else:
            marks = ",".join("?" for _ in delivery_ids) or "NULL"
            rows = con.execute(f"SELECT * FROM war_deliveries WHERE id IN ({marks})", tuple(delivery_ids)).fetchall()
        con.execute("UPDATE war_project_control SET stop_requested_at=?,stop_deadline=?,stop_state='stop_requested',updated_at=? WHERE project_id=?", (current,stop_deadline,current,project_id))
        for row in rows:
            binder = getattr(adapter, "bind_delivery", None)
            run_binder = getattr(adapter, "bind_run", None)
            if binder:
                binding = con.execute("SELECT session_key,session_id,purpose,disposable FROM war_project_sessions WHERE project_id=? AND agent_id=? AND enabled=1 LIMIT 1", (project_id,row["agent_id"])).fetchone()
                if not binding:
                    receipt = DeliveryReceipt(row["id"], "failed", error_code="explicit_session_binding_missing")
                else:
                    try:
                        binder(row["id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]), agent_id=row["agent_id"])
                        if run_binder and row["run_id"]:
                            run_binder(row["run_id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]), delivery_id=row["id"], started_at=row["sent_at"], agent_id=row["agent_id"])
                        receipt = adapter.stop(delivery_id=row["id"], agent_id=row["agent_id"])
                    except ValueError:
                        receipt = DeliveryReceipt(row["id"], "failed", error_code="session_binding_not_disposable_test")
            else:
                receipt = adapter.stop(delivery_id=row["id"], agent_id=row["agent_id"])
            status = receipt.status if receipt.status in {"stopped","failed","timed_out"} else "failed"
            con.execute("UPDATE war_deliveries SET status=?,error_code=? WHERE id=?", (status,receipt.error_code,row["id"]))
            _audit(con, project_id, "delivery_stop_"+status, row["id"], {"actor_id":actor_id,"error_code":receipt.error_code})
            results.append({"delivery_id":row["id"],"status":status})
        con.commit()
    return {"project_id":project_id,"status":"stop_requested","deadline":stop_deadline,"deliveries":results}


def process_stop_timers(*, db_path: str | Path, now: int | None = None) -> list[dict[str, Any]]:
    """Convert expired pending acknowledgements to stop_unconfirmed."""
    current = _now() if now is None else int(now)
    results: list[dict[str, Any]] = []
    with _connect(db_path) as con:
        controls = con.execute("SELECT * FROM war_project_control WHERE stop_state='stop_requested' AND stop_deadline IS NOT NULL AND stop_deadline<=?", (current,)).fetchall()
        for control in controls:
            pending = con.execute("""SELECT COUNT(*) FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
                WHERE m.project_id=? AND d.status IN ('sent','received','responded','queued')""", (control["project_id"],)).fetchone()[0]
            state = "stop_unconfirmed" if pending else "stopped"
            con.execute("UPDATE war_project_control SET stop_state=?,updated_at=? WHERE project_id=?", (state,current,control["project_id"]))
            _audit(con, control["project_id"], "project_"+state, control["project_id"], {"pending_deliveries":pending})
            results.append({"project_id":control["project_id"],"status":state})
        con.commit()
    return results
