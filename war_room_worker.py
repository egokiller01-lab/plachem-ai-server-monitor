"""Explicitly invoked War Room delivery retry and stop timer worker.

There is intentionally no import-time or service-start hook: a gateway/service
restart must not replay a terminal delivery. Retries are explicit and reuse the
original delivery identifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import war_room
from war_room_adapter import DeliveryReceipt, SessionAdapter
from war_room_documents import (
    DocumentRegistrationError,
    document_candidate,
    inferred_title,
    register_document,
)


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
    if "task_revision" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN task_revision INTEGER NOT NULL DEFAULT 1")
    if "retry_count" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    if "error_class" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN error_class TEXT")
    if "last_error_at" not in columns:
        con.execute("ALTER TABLE war_deliveries ADD COLUMN last_error_at INTEGER")
    for column, definition in (("session_key", "TEXT"), ("session_id", "TEXT"), ("correlation_id", "TEXT")):
        if column not in columns:
            con.execute(f"ALTER TABLE war_deliveries ADD COLUMN {column} {definition}")
    return con


def _audit(
    con: sqlite3.Connection,
    project_id: str,
    event: str,
    target_id: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
    target_type: str = "delivery",
) -> None:
    con.execute(
        "INSERT INTO war_audit_events VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), project_id, "worker", event, target_type, target_id,
         json.dumps(war_room._redact(payload), sort_keys=True), correlation_id or str(uuid.uuid4()), _now()),
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
    allowed = required | {"documents"}
    if not isinstance(result, dict) or not required.issubset(result) or not set(result).issubset(allowed):
        return None, "structured_response_fields_mismatch"
    if "documents" not in result:
        result["documents"] = []
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
    documents = result.get("documents")
    if not isinstance(documents, list):
        return None, "structured_response_documents_invalid"
    for item in documents:
        if not isinstance(item, dict):
            return None, "structured_response_documents_invalid"
        required_document_fields = {"title", "category", "path", "action", "summary"}
        allowed_document_fields = required_document_fields | {"document_id", "expected_version", "relation"}
        if not required_document_fields.issubset(item) or not set(item).issubset(allowed_document_fields):
            return None, "structured_response_documents_invalid"
        if item.get("action") not in {"create", "update"}:
            return None, "structured_response_documents_invalid"
        if not isinstance(item.get("path"), str) or not item["path"].startswith("/"):
            return None, "structured_response_documents_invalid"
        if not all(isinstance(item.get(key), str) for key in ("title", "category", "summary")):
            return None, "structured_response_documents_invalid"
        if item.get("expected_version") is not None and (
            not isinstance(item["expected_version"], int) or item["expected_version"] < 0
        ):
            return None, "structured_response_documents_invalid"
    return result, None


def _fast_gateway_result(body: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse only a Core-validated Fast Gateway terminal result for QA bridging."""
    try:
        value = json.loads(body.strip())
    except (ValueError, TypeError):
        return None, "fast_gateway_result_invalid_json"
    if not isinstance(value, dict):
        return None, "fast_gateway_result_invalid"
    status = str(value.get("status") or "").lower()
    if status != "completed":
        return None, "fast_gateway_result_not_completed"
    summary = value.get("summary")
    artifacts = value.get("artifacts")
    if not isinstance(summary, str) or not isinstance(artifacts, list):
        return None, "fast_gateway_result_fields_missing"
    artifact_paths: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict):
            return None, "fast_gateway_result_artifact_invalid"
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            return None, "fast_gateway_result_artifact_invalid"
        artifact_paths.append(path)
    if not artifact_paths:
        return None, "fast_gateway_result_artifact_missing"
    return {
        "verdict": "PASS",
        "summary": summary,
        "evidence": artifact_paths,
        "artifact_paths": artifact_paths,
    }, None


def _store_response_message(
    con: sqlite3.Connection, row: sqlite3.Row, response_body: str, now: int,
    *, structured_result: dict[str, Any] | None = None,
) -> str:
    """Persist response lineage while preserving an already validated result contract."""
    response_message_id = str(uuid.uuid4())
    if structured_result is not None:
        safe_result = dict(structured_result)
        safe_result["summary"] = war_room._redact_string(str(safe_result.get("summary") or ""))
        clean = json.dumps(safe_result, ensure_ascii=False, separators=(",", ":"))
    else:
        clean = war_room._redact_string(response_body)
    con.execute(
        """INSERT INTO war_messages
           (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,correlation_id,redaction_state,original_body)
           VALUES (?,?,'result','agent',?,?,?,?,?,'clean',?)""",
        (response_message_id, row["project_id"], row["agent_id"], clean, row["message_id"], now,
         row["correlation_id"] or str(uuid.uuid4()), clean),
    )
    return response_message_id


def _apply_collaboration_outcome(con: sqlite3.Connection, task_id: str, project_id: str, message_id: str, task_revision: int, now: int) -> None:
    rows = con.execute("""SELECT d.agent_id,m.body FROM war_deliveries d LEFT JOIN war_messages m ON m.id=d.response_message_id
        WHERE d.message_id=? AND d.task_revision=? AND d.status='responded'""", (message_id, task_revision)).fetchall()
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



def _auto_qa_enabled() -> bool:
    return str(os.environ.get("PLACHEM_WAR_ROOM_AUTO_QA", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _qa_delivery_for_task(con: sqlite3.Connection, task_id: str, agent_id: str) -> bool:
    task = con.execute(
        "SELECT reviewer_agent_id,status FROM war_tasks WHERE id=?", (task_id,)
    ).fetchone()
    return bool(
        task
        and task["status"] == "qa"
        and task["reviewer_agent_id"]
        and str(task["reviewer_agent_id"]) == str(agent_id)
    )


def _qa_review_instruction(con: sqlite3.Connection, task_id: str, message_id: str) -> str:
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
    original = con.execute("SELECT body FROM war_messages WHERE id=?", (message_id,)).fetchone()
    packet_row = con.execute(
        "SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)
    ).fetchone()
    try:
        packet = json.loads(packet_row["packet_json"]) if packet_row else {}
    except (TypeError, ValueError):
        packet = {}
    verification_scope = str(packet.get("verification_scope") or "TASK_RUN").upper()
    if verification_scope == "PROJECT_WINDOW":
        scope_instruction = (
            "Verification scope is PROJECT_WINDOW: evaluate completion and forbidden-change conditions "
            "against the relevant project history as well as the current task."
        )
    else:
        verification_scope = "TASK_RUN"
        scope_instruction = (
            "Verification scope is TASK_RUN: evaluate completion and forbidden-change conditions only "
            "against mutations attributable to this task revision/delivery. Historical project maintenance "
            "or code changes from earlier tasks are context, not violations of this task. Explicitly approved "
            "result-artifact writes are task outputs, not production mutations, unless the original task says otherwise."
        )
    workers = con.execute(
        """SELECT d.agent_id,d.run_id,rm.body
           FROM war_deliveries d
           LEFT JOIN war_messages rm ON rm.id=d.response_message_id
           JOIN war_task_agents a ON a.task_id=? AND a.agent_id=d.agent_id
           WHERE d.message_id=? AND d.task_revision=? AND d.status='responded'
           ORDER BY d.agent_id""",
        (task_id, message_id, int(task["revision"])),
    ).fetchall()
    summaries = []
    evidence = []
    for row in workers:
        result, error = _structured_result(con, message_id, row["agent_id"], row["body"] or "")
        if (error or not result) and str(task["execution_mode"] or "") == "FAST_GATEWAY":
            result, error = _fast_gateway_result(row["body"] or "")
        if error or not result:
            summaries.append({"agent": row["agent_id"], "error": error or "missing_result"})
            continue
        summaries.append({
            "agent": row["agent_id"],
            "verdict": result.get("verdict"),
            "summary": str(result.get("summary") or "")[:1200],
            "run_id": row["run_id"],
        })
        evidence.extend(v for v in result.get("evidence", []) if isinstance(v, str) and v.startswith("/"))
    unique_evidence = list(dict.fromkeys(evidence))[:20]
    return (
        "[AUTO_QA_REVIEW]\n"
        "You are the independent QA reviewer for this War Room task. "
        "Do not perform the worker's task again and do not modify production state. "
        "Independently verify the worker claims against the original instruction, current read-only state, "
        "and the listed evidence paths. Return PASS only when the completion conditions are actually proven; "
        "otherwise return FAIL or REWORK. The outer STRUCTURED_RESULT contract is mandatory. "
        "Set representative_completion_claimed=false.\n"
        f"[QA_VERIFICATION_SCOPE]\n{verification_scope}\n{scope_instruction}\n"
        f"[ORIGINAL_TASK]\n{str(original['body'] if original else '')[:3000]}\n"
        f"[WORKER_RESULTS]\n{json.dumps(summaries, ensure_ascii=False)}\n"
        f"[WORKER_EVIDENCE_PATHS]\n{json.dumps(unique_evidence, ensure_ascii=False)}\n"
    )


def _capture_required_evidence(
    con: sqlite3.Connection,
    *,
    task_id: str,
    message_id: str,
    task_revision: int,
    now: int,
) -> None:
    """Materialize worker file evidence into the existing immutable QA evidence contract."""
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
    packet_row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
    if not task or not packet_row or task["status"] != "qa":
        return
    from war_room_actions import _normalize_required_evidence
    packet = json.loads(packet_row[0])
    required = _normalize_required_evidence(packet.get("required_evidence", ["test", "artifact"]))
    workers = con.execute(
        """SELECT d.agent_id,d.run_id,rm.body
           FROM war_deliveries d
           LEFT JOIN war_messages rm ON rm.id=d.response_message_id
           JOIN war_task_agents a ON a.task_id=? AND a.agent_id=d.agent_id
           WHERE d.message_id=? AND d.task_revision=? AND d.status='responded'
           ORDER BY d.agent_id""",
        (task_id, message_id, task_revision),
    ).fetchall()
    candidates: list[tuple[str, str | None]] = []
    for row in workers:
        result, error = _structured_result(con, message_id, row["agent_id"], row["body"] or "")
        if (error or not result) and str(task["execution_mode"] or "") == "FAST_GATEWAY":
            result, error = _fast_gateway_result(row["body"] or "")
        if error or not result:
            continue
        for uri in result.get("evidence", []):
            if isinstance(uri, str) and uri.startswith("/") and Path(uri).is_file():
                candidates.append((uri, row["run_id"]))
    if not candidates:
        _audit(con, task["project_id"], "qa_auto_evidence_missing", task_id, {"reason": "no_readable_worker_evidence"})
        return
    scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
    approved = packet.get("approved_paths") or [packet.get("worktree")]
    legacy = all(item.get("legacy") for item in required)
    for item in required:
        evidence_type = item["id"] if legacy else item["evidence_type"]
        contract_evidence_id = None if legacy else item["id"]
        evidence_id = str(uuid.uuid4())
        if not legacy:
            existing = con.execute(
                """SELECT id FROM war_evidence
                   WHERE task_id=? AND task_revision=? AND qa_cycle=?
                     AND COALESCE(contract_evidence_id,id)=? LIMIT 1""",
                (task_id, int(task["revision"]), int(task["qa_cycle"]), contract_evidence_id),
            ).fetchone()
            if existing:
                continue
        chosen = None
        for uri, run_id in candidates:
            if not war_room.path_within_approved_roots(uri, approved):
                continue
            if not legacy and item["expected_contains"]:
                try:
                    if item["expected_contains"] not in Path(uri).read_text(encoding="utf-8"):
                        continue
                except (OSError, UnicodeDecodeError):
                    continue
            chosen = (uri, run_id)
            break
        if not chosen:
            _audit(con, task["project_id"], "qa_auto_evidence_requirement_missing", task_id, {"evidence_id": item["id"]})
            continue
        uri, run_id = chosen
        data = Path(uri).read_bytes()
        con.execute(
            """INSERT INTO war_evidence
               (id,task_id,evidence_type,uri,summary,sha256,task_revision,scope_hash,document_version,qa_cycle,
                run_id,source_command,expected_contains,immutable,contract_evidence_id,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id, task_id, evidence_type, uri,
                "Auto-captured from verified worker structured result",
                hashlib.sha256(data).hexdigest() if not legacy else None,
                task["revision"], scope_hash, task["document_version"], task["qa_cycle"],
                run_id if not legacy else None,
                item["source_command"] if not legacy else None,
                item["expected_contains"] if not legacy else None,
                0 if legacy else 1, contract_evidence_id, now,
            ),
        )
        _audit(con, task["project_id"], "qa_auto_evidence_added", task_id, {
            "evidence_id": contract_evidence_id or evidence_id,
            "record_id": evidence_id,
            "uri": uri,
        })


def _ensure_auto_qa_session(
    con: sqlite3.Connection,
    *,
    task: sqlite3.Row,
    adapter: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Create a fresh reviewer session for exactly one QA delivery.

    QA sessions are delivery-scoped, not project/agent-scoped. This prevents
    concurrent or sequential task reviews from sharing conversational context.
    """
    reviewer = str(task["reviewer_agent_id"] or "")
    if not reviewer:
        return None, "reviewer_missing"
    creator = getattr(adapter, "create_disposable_session", None)
    if not callable(creator):
        return None, "reviewer_session_provision_unavailable"
    try:
        created = creator(agent_id=reviewer, project_id=task["project_id"])
    except Exception as exc:
        return None, f"reviewer_session_provision_failed:{type(exc).__name__}"
    if (
        created.get("purpose") != "test"
        or created.get("disposable") is not True
        or not str(created.get("session_key") or "").startswith(f"agent:{reviewer.lower()}:war-room-test:")
        or not created.get("session_id")
    ):
        return None, "unsafe_reviewer_session_binding"
    return {
        "session_key": str(created["session_key"]),
        "session_id": str(created["session_id"]),
        "purpose": "test",
        "disposable": True,
    }, None

def _queue_auto_qa_delivery(
    con: sqlite3.Connection,
    *,
    task_id: str,
    message_id: str,
    execution_mode: str,
    adapter: Any,
    now: int,
) -> bool:
    if not _auto_qa_enabled():
        return False
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
    if not task or task["status"] != "qa" or not task["reviewer_agent_id"]:
        return False
    reviewer = str(task["reviewer_agent_id"])
    if con.execute(
        "SELECT 1 FROM war_task_agents WHERE task_id=? AND agent_id=?", (task_id, reviewer)
    ).fetchone():
        con.execute("UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=?", (now, task_id))
        _audit(con, task["project_id"], "qa_auto_blocked", task_id, {"reason": "reviewer_not_independent"})
        return False
    existing = con.execute(
        "SELECT id FROM war_deliveries WHERE message_id=? AND agent_id=? AND task_revision=?",
        (message_id, reviewer, int(task["revision"])),
    ).fetchone()
    if existing:
        return True
    # QA is always read-only and runs through the direct disposable-session lane,
    # even when the Worker used Fast Gateway / Controlled Lane.
    qa_binding, reason = _ensure_auto_qa_session(con, task=task, adapter=adapter)
    if not qa_binding:
        con.execute("UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=?", (now, task_id))
        _audit(con, task["project_id"], "qa_auto_blocked", task_id, {"reason": reason})
        return False
    delivery_id = str(uuid.uuid4())
    correlation = str(uuid.uuid4())
    con.execute(
        """INSERT INTO war_deliveries
           (id,message_id,agent_id,task_revision,status,attempt_count,deadline_at,created_at,correlation_id,
            session_key,session_id)
           VALUES (?,?,?,?, 'queued',0,?,?,?,?,?)""",
        (
            delivery_id, message_id, reviewer, int(task["revision"]), task["deadline_at"], now, correlation,
            qa_binding["session_key"], qa_binding["session_id"],
        ),
    )
    _audit(con, task["project_id"], "qa_delivery_queued", delivery_id, {
        "task_id": task_id,
        "reviewer": reviewer,
        "session_id": qa_binding["session_id"],
        "session_scope": "delivery",
    }, correlation)
    return True


def _record_auto_qa_verdict(
    con: sqlite3.Connection,
    *,
    task_id: str,
    reviewer: str,
    result: dict[str, Any],
    now: int,
) -> str:
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
    if not task or task["status"] != "qa" or str(task["reviewer_agent_id"] or "") != reviewer:
        return "qa_state_mismatch"
    if con.execute("SELECT 1 FROM war_task_agents WHERE task_id=? AND agent_id=?", (task_id, reviewer)).fetchone():
        return "qa_not_independent"
    qa_row = con.execute(
        "SELECT role,active FROM war_participants WHERE project_id=? AND principal_id=?",
        (task["project_id"], reviewer),
    ).fetchone()
    if not qa_row or qa_row["role"] != "qa" or not qa_row["active"]:
        return "qa_principal_invalid"
    from war_room_actions import _normalize_required_evidence, _qa_evidence_validation, _qa_signature
    packet_row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
    packet = json.loads(packet_row[0]) if packet_row else {}
    required = _normalize_required_evidence(packet.get("required_evidence", ["test", "artifact"]))
    required_ids = [item["id"] for item in required]
    verdict = str(result.get("verdict") or "").upper()
    if verdict not in {"PASS", "FAIL", "REWORK"}:
        return "qa_invalid_verdict"
    if verdict == "PASS":
        error = _qa_evidence_validation(con, task, packet, required_ids)
        if error:
            con.execute(
                "UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=?",
                (now, task_id),
            )
            _audit(con, task["project_id"], "qa_auto_pass_blocked", task_id, {"reason": error, "reviewer": reviewer})
            return error
    profile = "required:" + ",".join(required_ids)
    scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
    signed_payload = json.dumps(
        {
            "task_id": task_id,
            "verdict": verdict,
            "evidence_profile": profile,
            "qa_principal": reviewer,
            "task_revision": task["revision"],
            "scope_hash": scope_hash,
            "document_version": task["document_version"],
            "qa_cycle": task["qa_cycle"],
        },
        sort_keys=True,
    )
    signature = _qa_signature(signed_payload)
    verdict_id = str(uuid.uuid4())
    con.execute(
        """INSERT INTO war_qa_verdicts
           (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,
            task_revision,scope_hash,document_version,qa_cycle,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            verdict_id, task_id, reviewer, verdict, profile, signature, signed_payload,
            task["revision"], scope_hash, task["document_version"], task["qa_cycle"], now,
        ),
    )
    _audit(con, task["project_id"], "qa_verdict_recorded", task_id, {"verdict_id": verdict_id, "verdict": verdict, "source": "auto_reviewer"})
    if verdict in {"FAIL", "REWORK"}:
        con.execute(
            "UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=?",
            (now, task_id),
        )
        _audit(con, task["project_id"], "qa_verdict_rework_required", task_id, {"verdict_id": verdict_id, "verdict": verdict})
    return verdict



def _capture_project_documents(
    con: sqlite3.Connection,
    *,
    task: sqlite3.Row,
    delivery_row: sqlite3.Row,
    structured_result: dict[str, Any] | None,
    now: int,
) -> None:
    packet_row = con.execute(
        "SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task["id"],)
    ).fetchone()
    if not packet_row:
        return
    try:
        packet = json.loads(packet_row[0])
    except (TypeError, ValueError):
        return
    roots = packet.get("approved_paths") or [packet.get("worktree")]
    roots = [value for value in roots if isinstance(value, str) and value.startswith("/")]
    if not roots:
        return

    current_delivery = con.execute(
        "SELECT response_message_id,session_id,run_id FROM war_deliveries WHERE id=?",
        (delivery_row["id"],),
    ).fetchone()
    session_id = current_delivery["session_id"] if current_delivery else delivery_row["session_id"]
    run_id = current_delivery["run_id"] if current_delivery else delivery_row["run_id"]

    declared: list[dict[str, Any]] = []
    if structured_result and isinstance(structured_result.get("documents"), list):
        declared.extend(structured_result["documents"])

    # Fast Gateway keeps its existing strict result contract. Its newly produced
    # document-like artifacts are projected into the Registry without changing
    # the Core result schema.
    if not declared and str(task["execution_mode"] or "") == "FAST_GATEWAY":
        response_message_id = current_delivery["response_message_id"] if current_delivery else None
        if response_message_id:
            message = con.execute("SELECT body FROM war_messages WHERE id=?", (response_message_id,)).fetchone()
            if message:
                try:
                    fast_value = json.loads(str(message["body"] or "").strip())
                except (TypeError, ValueError):
                    fast_value = {}
                artifacts = fast_value.get("artifacts") if isinstance(fast_value, dict) else None
                summary = fast_value.get("summary") if isinstance(fast_value, dict) else ""
                if isinstance(artifacts, list):
                    for artifact in artifacts:
                        path = artifact.get("path") if isinstance(artifact, dict) else None
                        if isinstance(path, str) and document_candidate(path):
                            declared.append({
                                "title": inferred_title(path),
                                "category": "",
                                "path": path,
                                "action": "create",
                                "summary": str(summary or "")[:4096],
                                "relation": "output",
                            })

    seen_paths: set[str] = set()
    for item in declared:
        path = item.get("path")
        if not isinstance(path, str) or path in seen_paths or not document_candidate(path):
            continue
        seen_paths.add(path)
        try:
            result = register_document(
                con,
                project_id=str(task["project_id"]),
                title=str(item.get("title") or inferred_title(path)),
                uri=path,
                approved_roots=roots,
                created_by=str(delivery_row["agent_id"]),
                category=item.get("category") or None,
                summary=str(item.get("summary") or "")[:4096],
                source_task_id=str(task["id"]),
                source_agent_id=str(delivery_row["agent_id"]),
                source_session_id=session_id,
                source_run_id=run_id,
                document_id=item.get("document_id"),
                expected_version=item.get("expected_version"),
                relation=item.get("relation"),
                now=now,
            )
        except DocumentRegistrationError as exc:
            _audit(
                con, str(task["project_id"]), "document_registration_rejected",
                str(delivery_row["id"]),
                {"task_id": task["id"], "path": path, "reason": str(exc)},
                target_type="document",
            )
            continue
        _audit(
            con, str(task["project_id"]),
            "document_version_registered" if result["changed"] else "document_registration_noop",
            str(result["document_id"]),
            {
                "task_id": task["id"],
                "version": result["version"],
                "uri": result["uri"],
                "sha256": result["sha256"],
                "agent_id": delivery_row["agent_id"],
                "session_id": session_id,
                "run_id": run_id,
            },
            target_type="document",
        )


def _after_responded_delivery(
    con: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    adapter: Any,
    structured_result: dict[str, Any] | None,
    now: int,
) -> None:
    if not row["task_id"]:
        return
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (row["task_id"],)).fetchone()
    if not task:
        return
    revision = int(row["task_revision"] or task["revision"] or 1)
    if int(task["revision"] or 1) != revision:
        _audit(con, task["project_id"], "stale_revision_response_ignored", row["id"], {
            "task_id": task["id"],
            "delivery_revision": revision,
            "current_revision": int(task["revision"] or 1),
        })
        return
    if str(task["reviewer_agent_id"] or "") == str(row["agent_id"]) and task["status"] == "qa":
        if structured_result:
            _record_auto_qa_verdict(
                con, task_id=task["id"], reviewer=str(row["agent_id"]),
                result=structured_result, now=now,
            )
        return
    _capture_project_documents(
        con, task=task, delivery_row=row, structured_result=structured_result, now=now
    )
    worker_pending = con.execute(
        """SELECT COUNT(*)
           FROM war_deliveries d
           JOIN war_task_agents a ON a.task_id=? AND a.agent_id=d.agent_id
           WHERE d.message_id=? AND d.task_revision=? AND d.status!='responded'""",
        (task["id"], row["message_id"], revision),
    ).fetchone()[0]
    if worker_pending != 0:
        return
    _apply_collaboration_outcome(
        con, task["id"], row["project_id"], row["message_id"], revision, now
    )
    refreshed = con.execute("SELECT * FROM war_tasks WHERE id=?", (task["id"],)).fetchone()
    if refreshed and refreshed["status"] == "qa" and _auto_qa_enabled():
        _capture_required_evidence(
            con, task_id=task["id"], message_id=row["message_id"],
            task_revision=revision, now=now,
        )
        _queue_auto_qa_delivery(
            con, task_id=task["id"], message_id=row["message_id"],
            execution_mode=row["execution_mode"], adapter=adapter, now=now,
        )


def _terminal_validation_failure(con: sqlite3.Connection, *, message_id: str, project_id: str, delivery_id: str, task_revision: int, error_code: str, now: int) -> None:
    task = con.execute("SELECT id,status,revision FROM war_tasks WHERE source_message_id=?", (message_id,)).fetchone()
    if not task:
        return
    if int(task["revision"] or 1) != int(task_revision):
        _audit(con, project_id, "stale_revision_failure_ignored", delivery_id, {
            "task_id": task["id"],
            "delivery_revision": int(task_revision),
            "current_revision": int(task["revision"] or 1),
            "error_code": error_code,
        })
        return
    con.execute("UPDATE war_tasks SET status='rework_required',revision=revision+1,updated_at=? WHERE id=? AND status!='rework_required'", (now, task["id"]))
    con.execute("UPDATE war_deliveries SET status='failed',error_code='cancelled_after_terminal_validation' WHERE message_id=? AND task_revision=? AND id!=? AND status='queued'", (message_id, task_revision, delivery_id))
    _audit(con, project_id, "terminal_response_validation_rework", delivery_id, {"task_id":task["id"],"error_code":error_code,"queued_policy":"cancelled","in_flight_policy":"finish_without_state_override"})


def _persist_execution(con: sqlite3.Connection, *, snapshot: dict[str, Any] | None,
                       project_id: str, task_id: str | None, agent_id: str, now: int) -> None:
    if not snapshot or not task_id or not snapshot.get("core_run_id"):
        return
    columns = {row[1] for row in con.execute("PRAGMA table_info(war_execution_runs)")}
    if "rejected_result_json" not in columns or "validation_error" not in columns:
        con.execute("""INSERT INTO war_execution_runs
            (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,session_key,run_status,runtime_seconds,result_summary,result_json,evidence_json,artifacts_json,policy_status,cancel_reason,escalation_required,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(core_run_id) DO UPDATE SET openclaw_run_id=excluded.openclaw_run_id,session_key=excluded.session_key,
              run_status=excluded.run_status,runtime_seconds=excluded.runtime_seconds,result_summary=excluded.result_summary,result_json=excluded.result_json,
              evidence_json=excluded.evidence_json,artifacts_json=excluded.artifacts_json,policy_status=excluded.policy_status,
              cancel_reason=excluded.cancel_reason,escalation_required=excluded.escalation_required,updated_at=excluded.updated_at""",
            (snapshot.get("core_run_id"), project_id, task_id, agent_id, snapshot.get("openclaw_run_id"), snapshot.get("session_key"), snapshot.get("run_status", "UNKNOWN"), snapshot.get("runtime_seconds"), snapshot.get("result_summary"), snapshot.get("result_json"), snapshot.get("evidence_json"), snapshot.get("artifacts_json"), snapshot.get("policy_status"), snapshot.get("cancel_reason"), int(snapshot.get("escalation_required", 0)), now, now))
        return
    if "raw_response" not in columns:
        con.execute("""INSERT INTO war_execution_runs
            (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,session_key,run_status,runtime_seconds,result_summary,result_json,evidence_json,artifacts_json,rejected_result_json,validation_error,policy_status,cancel_reason,escalation_required,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(core_run_id) DO UPDATE SET openclaw_run_id=excluded.openclaw_run_id,session_key=excluded.session_key,
              run_status=excluded.run_status,runtime_seconds=excluded.runtime_seconds,result_summary=excluded.result_summary,result_json=excluded.result_json,
              evidence_json=excluded.evidence_json,artifacts_json=excluded.artifacts_json,rejected_result_json=excluded.rejected_result_json,
              validation_error=excluded.validation_error,policy_status=excluded.policy_status,cancel_reason=excluded.cancel_reason,
              escalation_required=excluded.escalation_required,updated_at=excluded.updated_at""",
            (snapshot.get("core_run_id"), project_id, task_id, agent_id, snapshot.get("openclaw_run_id"), snapshot.get("session_key"), snapshot.get("run_status", "UNKNOWN"), snapshot.get("runtime_seconds"), snapshot.get("result_summary"), snapshot.get("result_json"), snapshot.get("evidence_json"), snapshot.get("artifacts_json"), snapshot.get("rejected_result_json"), snapshot.get("validation_error"), snapshot.get("policy_status"), snapshot.get("cancel_reason"), int(snapshot.get("escalation_required", 0)), now, now))
        return
    con.execute("""INSERT INTO war_execution_runs
        (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,session_key,run_status,runtime_seconds,result_summary,result_json,evidence_json,artifacts_json,raw_response,rejected_result_json,validation_error,policy_status,cancel_reason,escalation_required,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(core_run_id) DO UPDATE SET openclaw_run_id=excluded.openclaw_run_id,session_key=excluded.session_key,
          run_status=excluded.run_status,runtime_seconds=excluded.runtime_seconds,result_summary=excluded.result_summary,result_json=excluded.result_json,
          evidence_json=excluded.evidence_json,artifacts_json=excluded.artifacts_json,raw_response=excluded.raw_response,rejected_result_json=excluded.rejected_result_json,
          validation_error=excluded.validation_error,policy_status=excluded.policy_status,cancel_reason=excluded.cancel_reason,
          escalation_required=excluded.escalation_required,updated_at=excluded.updated_at""",
        (snapshot.get("core_run_id"), project_id, task_id, agent_id, snapshot.get("openclaw_run_id"), snapshot.get("session_key"), snapshot.get("run_status", "UNKNOWN"), snapshot.get("runtime_seconds"), snapshot.get("result_summary"), snapshot.get("result_json"), snapshot.get("evidence_json"), snapshot.get("artifacts_json"), snapshot.get("raw_response"), snapshot.get("rejected_result_json"), snapshot.get("validation_error"), snapshot.get("policy_status"), snapshot.get("cancel_reason"), int(snapshot.get("escalation_required", 0)), now, now))


def _binding_for_delivery(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    require_delivery_binding: bool = False,
) -> dict[str, Any] | sqlite3.Row | None:
    """Return the delivery binding; optionally fail closed instead of project fallback."""
    keys = row.keys()
    session_key = row["session_key"] if "session_key" in keys else None
    session_id = row["session_id"] if "session_id" in keys else None
    if session_key and session_id and ":war-room-test:" in str(session_key):
        return {
            "session_key": session_key,
            "session_id": session_id,
            "purpose": "test",
            "disposable": 1,
        }
    if require_delivery_binding:
        return None
    return con.execute(
        """SELECT session_key,session_id,purpose,disposable
           FROM war_project_sessions
           WHERE project_id=? AND agent_id=? AND enabled=1
           ORDER BY rowid DESC LIMIT 1""",
        (row["project_id"], row["agent_id"]),
    ).fetchone()


def process_due_deliveries(*, db_path: str | Path, adapter: SessionAdapter, adapter_selector: Any | None = None, now: int | None = None) -> list[dict[str, Any]]:
    """Process only explicitly queued, due rows; never replay terminal rows."""
    current = _now() if now is None else int(now)
    results: list[dict[str, Any]] = []
    with _connect(db_path) as con:
        rows = con.execute(
            """SELECT d.*,m.project_id,m.body,t.id AS task_id,COALESCE(t.execution_mode,'LEGACY') AS execution_mode FROM war_deliveries d
               JOIN war_messages m ON m.id=d.message_id
               LEFT JOIN war_tasks t ON t.source_message_id=m.id
               WHERE (d.status='queued' OR (d.status='sent' AND d.run_id IS NULL AND d.claim_expires_at<=?))
                 AND COALESCE(d.next_attempt_at,d.created_at)<=?
               ORDER BY d.created_at,d.id""", (current,current),
        ).fetchall()
        for row in rows:
            from war_room_agents import canonical_agent_id, load_agent_catalog
            canonical = canonical_agent_id(row["agent_id"])
            entry = load_agent_catalog().get(canonical) if canonical else None
            participant = con.execute(
                "SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=? AND active=1 AND can_comment=1",
                (row["project_id"], row["agent_id"]),
            ).fetchone()
            if entry is None or not entry.execution_eligible or not participant:
                con.execute(
                    "UPDATE war_deliveries SET status='failed',error_code='agent_admission_revoked',error_class='system_error',last_error_at=? WHERE id=? AND status='queued'",
                    (current, row["id"]),
                )
                if row["task_id"]:
                    _terminal_validation_failure(
                        con, message_id=row["message_id"], project_id=row["project_id"],
                        delivery_id=row["id"], task_revision=int(row["task_revision"] or 1),
                        error_code="agent_admission_revoked", now=current,
                    )
                con.commit()
                results.append({"delivery_id": row["id"], "status": "failed", "reason": "agent_admission_revoked"})
                continue
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
                con.execute("UPDATE war_deliveries SET status='timed_out',error_code='delivery_deadline_exceeded',error_class='system_error',last_error_at=? WHERE id=?", (current,row["id"]))
                if row["task_id"]:
                    _terminal_validation_failure(
                        con, message_id=row["message_id"], project_id=row["project_id"],
                        delivery_id=row["id"], task_revision=int(row["task_revision"] or 1),
                        error_code="delivery_deadline_exceeded", now=current,
                    )
                _audit(con, row["project_id"], "delivery_timed_out", row["id"], {"reason":"deadline"})
                results.append({"delivery_id":row["id"],"status":"timed_out"})
                continue
            is_qa_delivery = bool(row["task_id"] and _qa_delivery_for_task(con, row["task_id"], row["agent_id"]))
            delivery_execution_mode = "LEGACY" if is_qa_delivery else row["execution_mode"]
            active_adapter = adapter if is_qa_delivery else (adapter_selector(row["execution_mode"], db_path) if adapter_selector else adapter)
            delivery_body = row["body"]
            if is_qa_delivery:
                delivery_body = _qa_review_instruction(con, row["task_id"], row["message_id"])
            if row["task_id"] and not delivery_body.startswith(("[STRUCTURED_RESULT]", "[FAST_GATEWAY_RESULT]")):
                packet_row = con.execute(
                    "SELECT packet_json FROM war_grounding_packets WHERE task_id=?",
                    (row["task_id"],),
                ).fetchone()
                if packet_row:
                    from war_room_actions import _grounded_instruction
                    delivery_body = _grounded_instruction(
                        delivery_body, json.loads(packet_row[0]), delivery_execution_mode
                    )
            binding = _binding_for_delivery(con, row)
            if binding and delivery_execution_mode != "FAST_GATEWAY":
                con.execute("UPDATE war_deliveries SET session_key=?,session_id=? WHERE id=?", (binding["session_key"], binding["session_id"], row["id"]))
            binder = getattr(active_adapter, "bind_delivery", None)
            if binder and not binding:
                receipt = DeliveryReceipt(row["id"], "failed", error_code="explicit_session_binding_missing")
            else:
                if binder:
                    try:
                        binder(row["id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]), agent_id=row["agent_id"])
                    except ValueError:
                        receipt = DeliveryReceipt(row["id"], "failed", error_code="session_binding_not_disposable_test")
                    else:
                        receipt = active_adapter.deliver(delivery_id=row["id"], agent_id=row["agent_id"], instruction_id=row["message_id"], body=delivery_body)
                else:
                    receipt = active_adapter.deliver(delivery_id=row["id"], agent_id=row["agent_id"], instruction_id=row["message_id"], body=delivery_body)
            status = receipt.status if receipt.status in {"received","responded","failed","timed_out","stopped"} else "failed"
            response_message_id = row["response_message_id"]
            response_body = getattr(receipt, "response_body", None)
            validation_error = None
            structured_result = None
            if status == "responded" and (not isinstance(response_body, str) or not response_body.strip()) and not response_message_id:
                status = "failed"
                receipt_error_code = "response_body_missing"
            else:
                receipt_error_code = getattr(receipt, "error_code", None)
            if status == "responded" and isinstance(response_body, str) and response_body.strip():
                structured_result, validation_error = (None, None) if (row["execution_mode"] == "FAST_GATEWAY" and not is_qa_delivery) else _structured_result(con, row["message_id"], row["agent_id"], response_body)
                if validation_error:
                    status = "failed"
                    receipt_error_code = validation_error
            total_attempt = int(row["attempt_count"] or 0) + 1
            cycle_attempt = int(row["retry_count"] or 0) + 1
            maximum = int(row["max_attempts"] or 3)
            terminal_contract_failure = bool(validation_error) or any(
                marker in str(receipt_error_code or "")
                for marker in ("_VALIDATION_FAILED:", "RESULT_VALIDATION_REQUIRED", "SCOPE_VIOLATION")
            )
            retryable = (
                status in {"failed", "timed_out"}
                and not terminal_contract_failure
                and cycle_attempt < maximum
                and (row["deadline_at"] is None or int(row["deadline_at"]) > current)
            )
            stored_status = "queued" if retryable else status
            next_attempt_at = current + min(60, 2 ** cycle_attempt) if retryable else None
            error_class = "system_error" if status in {"failed", "timed_out"} else None
            updated = con.execute(
                """UPDATE war_deliveries SET status=?,attempt_count=?,retry_count=?,error_class=?,last_error_at=CASE WHEN ?='system_error' THEN ? ELSE last_error_at END,
                   sent_at=COALESCE(sent_at,?),
                   received_at=CASE WHEN ? IN ('received','responded') THEN ? ELSE received_at END,
                   responded_at=CASE WHEN ?='responded' THEN ? ELSE responded_at END,
                   error_code=?,run_id=COALESCE(?,run_id),response_message_id=?,session_id=COALESCE(?,session_id),next_attempt_at=?,claim_token=NULL,claim_expires_at=NULL WHERE id=? AND status='sent' AND claim_token=?""",
                (stored_status,total_attempt,cycle_attempt,error_class,error_class,current,current,status,current,status,current,receipt_error_code,receipt.run_id,response_message_id,receipt.session_id,next_attempt_at,row["id"],claim_token),
            )
            if updated.rowcount != 1:
                continue
            if isinstance(response_body, str) and response_body.strip():
                if not response_message_id:
                    response_message_id = _store_response_message(
                        con, row, response_body, current, structured_result=structured_result
                    )
                    con.execute("UPDATE war_deliveries SET response_message_id=? WHERE id=? AND status=?", (response_message_id, row["id"], stored_status))
            if status in {"failed", "timed_out"} and not retryable:
                _terminal_validation_failure(
                    con, message_id=row["message_id"], project_id=row["project_id"],
                    delivery_id=row["id"], task_revision=int(row["task_revision"] or 1),
                    error_code=receipt_error_code or validation_error or status, now=current,
                )
            snapshotter = getattr(active_adapter, "execution_snapshot", None)
            if snapshotter:
                _persist_execution(con, snapshot=snapshotter(row["id"]), project_id=row["project_id"], task_id=row["task_id"], agent_id=row["agent_id"], now=current)
            task = con.execute("SELECT * FROM war_tasks WHERE source_message_id=?", (row["message_id"],)).fetchone()
            if task:
                is_reviewer = str(task["reviewer_agent_id"] or "") == str(row["agent_id"]) and task["status"] == "qa"
                # QA is a separate review lane and does not consume Worker call/turn budget.
                call_delta = 0 if is_reviewer or getattr(active_adapter, "reserves_call_budget", False) is True else 1
                turn_delta = 0 if is_reviewer else (1 if status == "responded" else 0)
                con.execute("INSERT INTO war_task_calls(task_id,task_revision,call_count,turn_count,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(task_id,task_revision) DO UPDATE SET call_count=call_count+?,turn_count=turn_count+?,updated_at=?", (task["id"],int(row["task_revision"] or task["revision"] or 1),call_delta,turn_delta,current,call_delta,turn_delta,current))
                if status == "responded":
                    _after_responded_delivery(
                        con, row=row, adapter=adapter,
                        structured_result=structured_result, now=current,
                    )
            _audit(con, row["project_id"], "delivery_retry_scheduled" if retryable else "delivery_"+status, row["id"], {"error_code":receipt_error_code,"attempt":total_attempt,"retry_count":cycle_attempt,"max_attempts":maximum,"next_attempt_at":next_attempt_at,"session_key":row["session_key"],"session_id":receipt.session_id or row["session_id"],"run_id":receipt.run_id,"source_message_id":row["message_id"],"response_message_id":response_message_id}, row["correlation_id"])
            results.append({"delivery_id":row["id"],"status":"retry_scheduled" if retryable else status,"state":"system_error" if error_class else stored_status,"attempt_count":total_attempt,"retry_count":cycle_attempt,"next_attempt_at":next_attempt_at})
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
        if int(row["retry_count"] or row["attempt_count"] or 0) >= max_attempts:
            return False
        con.execute("UPDATE war_deliveries SET status='queued',next_attempt_at=?,error_code=NULL,error_class=NULL,last_error_at=NULL WHERE id=?", (current,delivery_id))
        project = con.execute("SELECT project_id FROM war_messages WHERE id=?", (row["message_id"],)).fetchone()
        if project:
            _audit(con, project["project_id"], "delivery_retry_requested", delivery_id, {"attempt_count":row["attempt_count"]})
        con.commit()
        return True


def recover_received_deliveries(*, db_path: str | Path, gateway: Any, gateway_selector: Any | None = None, now: int | None = None) -> list[dict[str, Any]]:
    """Poll non-terminal received runs after worker restart without resending."""
    current = _now() if now is None else int(now)
    results: list[dict[str, Any]] = []
    with _connect(db_path) as con:
        rows = con.execute("SELECT d.*,m.project_id,m.body,t.id AS task_id,COALESCE(t.execution_mode,'LEGACY') AS execution_mode FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id LEFT JOIN war_tasks t ON t.source_message_id=m.id WHERE d.status='received' AND d.run_id IS NOT NULL").fetchall()
        for row in rows:
            if row["deadline_at"] is not None and int(row["deadline_at"]) <= current:
                con.execute("UPDATE war_deliveries SET status='timed_out',error_code='delivery_deadline_exceeded' WHERE id=?", (row["id"],))
                if row["task_id"]:
                    _terminal_validation_failure(
                        con, message_id=row["message_id"], project_id=row["project_id"],
                        delivery_id=row["id"], task_revision=int(row["task_revision"] or 1),
                        error_code="delivery_deadline_exceeded", now=current,
                    )
                _audit(con, row["project_id"], "delivery_recovered_timed_out", row["id"], {"run_id": row["run_id"]})
                results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": "timed_out"})
                continue
            is_qa_delivery = bool(row["task_id"] and _qa_delivery_for_task(con, row["task_id"], row["agent_id"]))
            active_gateway = gateway if is_qa_delivery else (gateway_selector(row["execution_mode"], db_path) if gateway_selector else gateway)
            binding = _binding_for_delivery(con, row, require_delivery_binding=True)
            run_binder = getattr(active_gateway, "bind_run", None)
            if run_binder:
                if not binding:
                    con.execute("UPDATE war_deliveries SET status='failed',error_code='explicit_session_binding_missing',error_class='system_error',last_error_at=? WHERE id=?", (current,row["id"]))
                    results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": "failed"})
                    continue
                try:
                    run_binder(row["run_id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]), delivery_id=row["id"], started_at=row["sent_at"], agent_id=row["agent_id"])
                except TypeError:
                    run_binder(row["run_id"], session_key=binding["session_key"], session_id=binding["session_id"], purpose=binding["purpose"], disposable=bool(binding["disposable"]))
                except ValueError:
                    con.execute("UPDATE war_deliveries SET status='failed',error_code='session_binding_not_disposable_test',error_class='system_error',last_error_at=? WHERE id=?", (current,row["id"]))
                    results.append({"delivery_id": row["id"], "run_id": row["run_id"], "status": "failed"})
                    continue
            try:
                run = active_gateway.poll(run_id=row["run_id"], agent_id=row["agent_id"])
            except TypeError:
                run = active_gateway.poll(row["run_id"])
            current_delivery = con.execute("SELECT status FROM war_deliveries WHERE id=?", (row["id"],)).fetchone()
            if not current_delivery or current_delivery["status"] != "received":
                continue
            status = getattr(run, "status", None)
            if status not in {"responded", "failed", "timed_out", "stopped"}:
                continue
            response_message_id = row["response_message_id"]
            response_body = getattr(run, "response_body", None)
            error_code = getattr(run, "error_code", None)
            validation_error = None
            structured_result = None
            if status == "responded" and (not isinstance(response_body, str) or not response_body.strip()) and not response_message_id:
                status = "failed"
                error_code = error_code or "response_body_missing"
            if status == "responded" and isinstance(response_body, str) and response_body.strip():
                structured_result, validation_error = (None, None) if (row["execution_mode"] == "FAST_GATEWAY" and not is_qa_delivery) else _structured_result(con, row["message_id"], row["agent_id"], response_body)
                if validation_error:
                    status = "failed"
                    error_code = validation_error
            updated = con.execute("UPDATE war_deliveries SET status=?,responded_at=CASE WHEN ?='responded' THEN ? ELSE responded_at END,error_code=?,response_message_id=?,session_id=COALESCE(?,session_id) WHERE id=? AND status='received'", (status, status, current, error_code, response_message_id, getattr(run, "session_id", None), row["id"]))
            if updated.rowcount != 1:
                continue
            if isinstance(response_body, str) and response_body.strip():
                if not response_message_id:
                    response_message_id = _store_response_message(
                        con, row, response_body, current, structured_result=structured_result
                    )
                    con.execute("UPDATE war_deliveries SET response_message_id=? WHERE id=? AND status=?", (response_message_id, row["id"], status))
            if status in {"failed", "timed_out"}:
                _terminal_validation_failure(
                    con, message_id=row["message_id"], project_id=row["project_id"],
                    delivery_id=row["id"], task_revision=int(row["task_revision"] or 1),
                    error_code=error_code or validation_error or status, now=current,
                )
            snapshotter = getattr(active_gateway, "execution_snapshot", None)
            if snapshotter:
                _persist_execution(con, snapshot=snapshotter(row["id"]), project_id=row["project_id"], task_id=row["task_id"], agent_id=row["agent_id"], now=current)
            if status == "responded":
                task = con.execute("SELECT * FROM war_tasks WHERE source_message_id=?", (row["message_id"],)).fetchone()
                if task:
                    is_reviewer = str(task["reviewer_agent_id"] or "") == str(row["agent_id"]) and task["status"] == "qa"
                    if not is_reviewer:
                        con.execute("""INSERT INTO war_task_calls(task_id,task_revision,call_count,turn_count,updated_at)
                                       VALUES (?,?,0,1,?)
                                       ON CONFLICT(task_id,task_revision) DO UPDATE SET
                                         turn_count=turn_count+1,updated_at=excluded.updated_at""",
                                    (task["id"], int(row["task_revision"] or task["revision"] or 1), current))
                    _after_responded_delivery(
                        con, row=row, adapter=gateway,
                        structured_result=structured_result, now=current,
                    )
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
                binding = _binding_for_delivery(con, row)
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
