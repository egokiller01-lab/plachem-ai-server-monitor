"""Task-specific contracts shared by preparation, transport and independent QA.

A receipt proves response delivery, never the correctness of the worker's claims.
Only the server creates receipts; workers are not asked to manufacture evidence.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

PROFILES = {"ACKNOWLEDGEMENT", "READ_ONLY", "CODE_CHANGE"}
WORKER_SECONDS = 3600
QA_SECONDS = 3600
RECEIPT_SOURCE = "war-room:current-run-response"


def profiled(packet: Mapping[str, Any]) -> bool:
    return packet.get("contract_version") == 2 and packet.get("task_profile") in PROFILES


def profile_grounding(body: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(supplied)
    profile = body.get("task_profile")
    if profile is None:
        return result
    if profile not in PROFILES:
        raise ValueError("INVALID_TASK_PROFILE")
    # Forbidden actions and extra evidence obligations are separate contracts.
    # Never create a global session-snapshot obligation from a phrase alone.
    # Explicit strict evidence remains on the existing advanced contract path.
    if result.get("session_integrity_required") is True or result.get("required_evidence"):
        raise ValueError("EXPLICIT_EVIDENCE_CONTRACT_REQUIRES_ADVANCED_MODE")
    result["session_integrity_required"] = False
    conditions = {
        "ACKNOWLEDGEMENT": ["Return the response required by the original instruction without invoking tools."],
        "READ_ONLY": ["Answer the original request using verifiable sources; do not change the inspected state."],
        "CODE_CHANGE": ["Implement only the approved change and verify its requested acceptance conditions."],
    }
    result.setdefault("completion_conditions", conditions[profile])
    result["required_evidence"] = [{
        "id": "execution_receipt", "evidence_type": "execution_receipt",
        "source_command": RECEIPT_SOURCE, "expected_contains": "",
    }]
    result.update(contract_version=2, task_profile=profile,
                  worker_timeout_seconds=WORKER_SECONDS, qa_timeout_seconds=QA_SECONDS)
    return result


def contract_from_message(message: str) -> dict[str, Any]:
    """Read the server envelope, not a worker response or quoted inner instruction."""
    if not message.startswith("[FAST_GATEWAY_RESULT]\n"):
        return {}
    raw = message.partition("\n[IMMUTABLE_GROUNDING_PACKET]\n")[2]
    raw, separator, _ = raw.partition("\n[ORIGINAL_INSTRUCTION_CONTEXT]\n")
    if not separator:
        return {}
    try:
        packet = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return packet if isinstance(packet, dict) and profiled(packet) else {}


def _receipt_payload(con: sqlite3.Connection, task: sqlite3.Row) -> dict[str, Any]:
    rows = con.execute(
        "SELECT d.agent_id,d.run_id,d.response_message_id,m.body FROM war_deliveries d "
        "JOIN war_messages m ON m.id=d.response_message_id "
        "JOIN war_task_agents a ON a.task_id=? AND a.agent_id=d.agent_id "
        "WHERE d.message_id=? AND d.task_revision=? AND d.status='responded' "
        "ORDER BY d.agent_id,d.id",
        (task["id"], task["source_message_id"], int(task["revision"])),
    ).fetchall()
    if not rows or any(not row["run_id"] for row in rows):
        raise ValueError("CURRENT_RUN_RECEIPT_UNAVAILABLE")
    return {"schema": "war-room-response-receipt/v1", "task_id": task["id"],
            "task_revision": int(task["revision"]), "qa_cycle": int(task["qa_cycle"]),
            "scope_hash": hashlib.sha256(task["scope"].encode()).hexdigest(),
            "document_version": task["document_version"],
            "proves": "Current-run response delivery; task correctness requires independent QA.",
            "responses": [dict(row) for row in rows]}


def _receipt_root(con: sqlite3.Connection) -> Path:
    path = con.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        raise ValueError("RECEIPT_STORE_REQUIRES_FILE_DATABASE")
    return Path(path).resolve().parent / "run-receipts"


def _serialized(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def capture_receipt(con: sqlite3.Connection, task: sqlite3.Row, now: int) -> None:
    binding = (task["id"], int(task["revision"]), int(task["qa_cycle"]))
    existing = con.execute(
        "SELECT id FROM war_evidence WHERE task_id=? AND task_revision=? AND qa_cycle=? "
        "AND contract_evidence_id='execution_receipt'", binding,
    ).fetchone()
    if existing:
        return
    payload = _receipt_payload(con, task)
    data = _serialized(payload)
    digest = hashlib.sha256(data).hexdigest()
    directory = _receipt_root(con)
    if directory.is_symlink():
        raise ValueError("UNSAFE_RECEIPT_STORE")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("RECEIPT_CONTENT_CONFLICT")
    con.execute(
        "INSERT INTO war_evidence (id,task_id,evidence_type,uri,summary,sha256,task_revision,"
        "scope_hash,document_version,qa_cycle,run_id,source_command,expected_contains,immutable,"
        "contract_evidence_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), task["id"], "execution_receipt", str(path),
         "Server-collected response record; not a QA verdict", digest, task["revision"],
         payload["scope_hash"], task["document_version"], task["qa_cycle"],
         payload["responses"][0]["run_id"], RECEIPT_SOURCE, "", 1, "execution_receipt", now),
    )


def verify_receipt(con: sqlite3.Connection, task: sqlite3.Row, row: sqlite3.Row) -> str | None:
    try:
        expected = _serialized(_receipt_payload(con, task))
        digest = hashlib.sha256(expected).hexdigest()
        path = _receipt_root(con) / (digest + ".json")
        if (row["source_command"] != RECEIPT_SOURCE or row["sha256"] != digest
                or row["evidence_type"] != "execution_receipt"
                or row["run_id"] != json.loads(expected)["responses"][0]["run_id"]
                or row["uri"] != str(path) or int(row["immutable"] or 0) != 1
                or path.is_symlink() or path.parent.is_symlink() or path.read_bytes() != expected):
            return "EVIDENCE_UNVERIFIED"
    except (OSError, ValueError, TypeError, KeyError):
        return "EVIDENCE_UNVERIFIED"
    return None


def receipt_paths(con: sqlite3.Connection, task: sqlite3.Row) -> list[str]:
    rows = con.execute(
        "SELECT * FROM war_evidence WHERE task_id=? AND task_revision=? AND qa_cycle=? "
        "AND contract_evidence_id='execution_receipt' AND source_command=?",
        (task["id"], int(task["revision"]), int(task["qa_cycle"]), RECEIPT_SOURCE),
    ).fetchall()
    return [row["uri"] for row in rows if verify_receipt(con, task, row) is None]


def public_contract(con: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
    try:
        packet = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        return {}
    if not isinstance(packet, dict) or not profiled(packet):
        return {}
    return {key: packet[key] for key in (
        "contract_version", "task_profile", "worker_timeout_seconds", "qa_timeout_seconds",
        "completion_conditions",
    ) if key in packet}
