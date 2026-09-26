from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import war_room
from war_room_actions import _grounded_instruction, _grounding_packet, _qa_evidence_validation, provision_action_schema
from war_room_worker import (_after_responded_delivery, _binding_for_delivery, _capture_required_evidence, _fast_gateway_result, _qa_review_instruction, _structured_result, _terminal_validation_failure)


def test_redaction_preserves_token_telemetry_terms_but_blocks_credentials():
    telemetry = "/logs/token-telemetry-baseline-7d-20260926.json token-usage is NORMAL"
    assert war_room._redact_string(telemetry) == telemetry
    redacted = war_room._redact_string("token=synthetic-secret-value")
    assert "synthetic-secret-value" not in redacted
    assert "REDACTED" in redacted


def test_grounded_result_contract_requires_packet_revision_echo():
    packet = {
        "worktree": "/safe/worktree",
        "branch": "test",
        "revision": "multi-reliability-v1",
        "api_base": "/api",
        "db_label": "test",
        "forbidden": ["production DB"],
        "completion_conditions": ["done"],
        "required_evidence": ["artifact"],
        "approved_paths": ["/safe/worktree"],
        "result_artifact_paths": [],
    }
    message = _grounded_instruction("inspect", packet, "LEGACY")
    assert "confirmed_revision MUST exactly copy IMMUTABLE_GROUNDING_PACKET.revision" in message
    assert "not a Git revision lookup" in message


def _seed_rework_db(tmp_path: Path):
    db = tmp_path / "war.sqlite3"
    os.environ["PLACHEM_WAR_ROOM_DB"] = str(db)
    war_room.provision_database(db)
    provision_action_schema(str(db))
    project = war_room.PROJECT_ID
    now = int(time.time())
    task_id = "task-rework-evidence"
    message_id = "msg-rework-evidence"
    artifact = tmp_path / "result.md"
    packet = {
        "worktree": str(tmp_path),
        "branch": "test",
        "revision": "v1",
        "api_base": "/api",
        "db_label": "test",
        "forbidden": ["production DB"],
        "completion_conditions": ["RESULT_PASS"],
        "required_evidence": [{
            "id": "result-artifact",
            "evidence_type": "artifact",
            "source_command": f"cat {artifact}",
            "expected_contains": "RESULT_PASS",
        }],
        "session_integrity_required": False,
        "approved_paths": [str(tmp_path)],
        "result_artifact_paths": [],
    }
    with sqlite3.connect(db) as con:
        con.execute(
            """INSERT INTO war_messages
               (id,project_id,message_type,author_type,author_id,body,created_at,correlation_id,redaction_state,original_body)
               VALUES (?,?,'instruction','agent','main','do work',?,'corr','clean','do work')""",
            (message_id, project, now),
        )
        con.execute(
            """INSERT INTO war_tasks
               (id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,
                manyfast_version,document_version,call_limit,turn_limit,execution_mode,deadline_at,
                revision,qa_cycle,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'qa','v1','v1',1,1,'LEGACY',?,1,1,?,?)""",
            (task_id, project, message_id, "ERPcoder", "ERPqa", "scope", now + 1800, now, now),
        )
        con.execute("INSERT INTO war_task_agents(task_id,agent_id) VALUES (?,?)", (task_id, "ERPcoder"))
        pj = json.dumps(packet, sort_keys=True)
        con.execute(
            "INSERT INTO war_grounding_packets(task_id,packet_json,packet_hash,created_at) VALUES (?,?,?,?)",
            (task_id, pj, hashlib.sha256(pj.encode()).hexdigest(), now),
        )
    return db, task_id, message_id, artifact, packet


def _add_worker_response(db: Path, task_id: str, message_id: str, artifact: Path, revision: int, qa_cycle: int, run_id: str):
    artifact.write_text(f"RESULT_PASS\nrevision={revision}\n", encoding="utf-8")
    response = json.dumps({
        "confirmed_worktree": str(artifact.parent),
        "confirmed_revision": "v1",
        "verdict": "PASS",
        "evidence": [str(artifact)],
        "summary": f"revision {revision}",
        "representative_completion_claimed": False,
    })
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        response_id = f"resp-{revision}"
        con.execute(
            """INSERT INTO war_messages
               (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,correlation_id,redaction_state,original_body)
               SELECT ?,project_id,'result','agent','ERPcoder',?,id,?,'corr','clean',?
               FROM war_messages WHERE id=?""",
            (response_id, response, now, response, message_id),
        )
        con.execute(
            """INSERT INTO war_deliveries
               (id,message_id,agent_id,task_revision,status,attempt_count,deadline_at,created_at,correlation_id,
                response_message_id,run_id)
               VALUES (?,?,?,?, 'responded',1,?,?,?, ?, ?)""",
            (f"d-{revision}", message_id, "ERPcoder", revision, now + 1800, now, f"corr-{revision}",
             response_id, run_id),
        )
        con.execute(
            "UPDATE war_tasks SET revision=?,qa_cycle=?,status='qa',updated_at=? WHERE id=?",
            (revision, qa_cycle, now, task_id),
        )
        con.commit()
        _capture_required_evidence(
            con, task_id=task_id, message_id=message_id,
            task_revision=revision, now=now,
        )
        con.commit()


def test_rework_evidence_contract_id_is_reusable_across_revisions(tmp_path, monkeypatch):
    db, task_id, message_id, artifact, packet = _seed_rework_db(tmp_path)
    _add_worker_response(db, task_id, message_id, artifact, 1, 1, "run-1")
    _add_worker_response(db, task_id, message_id, artifact, 2, 2, "run-2")

    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT id,contract_evidence_id,task_revision,qa_cycle
               FROM war_evidence WHERE task_id=? ORDER BY task_revision""",
            (task_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] != rows[1]["id"]
        assert [r["contract_evidence_id"] for r in rows] == ["result-artifact", "result-artifact"]
        assert [(r["task_revision"], r["qa_cycle"]) for r in rows] == [(1, 1), (2, 2)]

        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        assert _qa_evidence_validation(con, task, packet, ["result-artifact"]) is None


def test_fast_gateway_result_artifact_path_is_exact_and_scoped(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    artifact = root / "result.md"
    packet = _grounding_packet({
        "grounding": {
            "worktree": str(root),
            "branch": "test",
            "revision": "v1",
            "api_base": "/api",
            "db_label": "test",
            "forbidden": ["production DB"],
            "completion_conditions": ["done"],
            "approved_paths": [str(root)],
            "result_artifact_paths": [str(artifact)],
        }
    }, "project", "v1")
    assert packet["approved_paths"] == [str(root)]
    assert packet["result_artifact_paths"] == [str(artifact)]


def test_fast_gateway_result_artifact_cannot_escape_approved_root(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    with pytest.raises(Exception):
        _grounding_packet({
            "grounding": {
                "worktree": str(root),
                "branch": "test",
                "revision": "v1",
                "api_base": "/api",
                "db_label": "test",
                "forbidden": ["production DB"],
                "completion_conditions": ["done"],
                "approved_paths": [str(root)],
                "result_artifact_paths": [str(tmp_path / "outside.md")],
            }
        }, "project", "v1")


def test_fast_gateway_completed_result_bridges_artifact_paths_for_auto_qa(tmp_path):
    artifact = tmp_path / "fast.md"
    artifact.write_text("FAST_GATEWAY_CONTROLLED_PASS\n", encoding="utf-8")
    body = json.dumps({
        "status": "completed",
        "summary": "verified",
        "evidence": [{"type": "artifact", "detail": "created"}],
        "artifacts": [{"path": str(artifact)}],
        "scope": {"compliant": True, "violations": []},
    })
    parsed, error = _fast_gateway_result(body)
    assert error is None
    assert parsed is not None
    assert parsed["verdict"] == "PASS"
    assert parsed["evidence"] == [str(artifact)]


def test_approved_scope_rejects_symlink_escape(tmp_path):
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    target = outside / "secret.md"
    target.write_text("RESULT_PASS\n", encoding="utf-8")
    link = approved / "result.md"
    link.symlink_to(target)

    assert not war_room.path_within_approved_roots(
        str(link), [str(approved)]
    )

    real = approved / "real.md"
    real.write_text("RESULT_PASS\n", encoding="utf-8")
    assert war_room.path_within_approved_roots(
        str(real), [str(approved)]
    )


def test_structured_result_rejects_extra_top_level_fields(tmp_path):
    db, task_id, message_id, artifact, packet = _seed_rework_db(tmp_path)
    artifact.write_text("RESULT_PASS\n", encoding="utf-8")
    body = json.dumps({
        "confirmed_worktree": str(tmp_path),
        "confirmed_revision": "v1",
        "verdict": "PASS",
        "evidence": [str(artifact)],
        "summary": "verified",
        "representative_completion_claimed": False,
        "unexpected": "must fail closed",
    })
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        result, error = _structured_result(
            con, message_id, "ERPcoder", body
        )
    assert result is None
    assert error == "structured_response_fields_mismatch"


def test_stale_revision_outcomes_cannot_override_current_task(tmp_path):
    db, task_id, message_id, artifact, packet = _seed_rework_db(tmp_path)
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        con.execute(
            "UPDATE war_tasks SET status='running',revision=2,qa_cycle=1 WHERE id=?",
            (task_id,),
        )
        con.commit()
        _terminal_validation_failure(
            con, message_id=message_id, project_id=war_room.PROJECT_ID,
            delivery_id="stale-failure", task_revision=1,
            error_code="old failure", now=now,
        )
        row = con.execute(
            "SELECT status,revision FROM war_tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert tuple(row) == ("running", 2)

        _after_responded_delivery(
            con,
            row={"id":"stale-response","task_id":task_id,"task_revision":1},
            adapter=None, structured_result={"verdict":"PASS"}, now=now,
        )
        row = con.execute(
            "SELECT status,revision FROM war_tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert tuple(row) == ("running", 2)


def test_recovery_requires_delivery_scoped_binding_and_never_falls_back():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE war_project_sessions(
          project_id TEXT,agent_id TEXT,session_key TEXT,session_id TEXT,
          purpose TEXT,disposable INTEGER,enabled INTEGER
        )
    """)
    con.execute(
        "INSERT INTO war_project_sessions VALUES ('p1','ERPmanager','agent:erpmanager:war-room-test:current','current-session','test',1,1)"
    )
    con.execute("""
        CREATE TABLE delivery_fixture(
          project_id TEXT,agent_id TEXT,session_key TEXT,session_id TEXT
        )
    """)
    con.execute("INSERT INTO delivery_fixture VALUES ('p1','ERPmanager',NULL,NULL)")
    row = con.execute("SELECT * FROM delivery_fixture").fetchone()

    fallback = _binding_for_delivery(con, row)
    assert fallback["session_id"] == "current-session"

    # Recovery of an already-running delivery must never jump to whatever
    # project-level session happens to be current now.
    assert _binding_for_delivery(con, row, require_delivery_binding=True) is None


def test_grounding_verification_scope_defaults_to_task_run_and_validates_values(tmp_path):
    root = tmp_path / "scope"
    root.mkdir()
    base = {
        "grounding": {
            "worktree": str(root),
            "branch": "test",
            "revision": "v1",
            "api_base": "/api",
            "db_label": "test",
            "forbidden": ["production DB write"],
            "completion_conditions": ["Production changes are zero"],
            "approved_paths": [str(root)],
        }
    }
    packet = _grounding_packet(base, "project", "v1")
    assert packet["verification_scope"] == "TASK_RUN"

    project_window = json.loads(json.dumps(base))
    project_window["grounding"]["verification_scope"] = "PROJECT_WINDOW"
    packet2 = _grounding_packet(project_window, "project", "v1")
    assert packet2["verification_scope"] == "PROJECT_WINDOW"

    invalid = json.loads(json.dumps(base))
    invalid["grounding"]["verification_scope"] = "EVERYTHING"
    with pytest.raises(Exception):
        _grounding_packet(invalid, "project", "v1")


def test_auto_qa_task_run_scope_does_not_treat_prior_project_changes_as_current_violation(tmp_path):
    db, task_id, message_id, artifact, packet = _seed_rework_db(tmp_path)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        instruction = _qa_review_instruction(con, task_id, message_id)
    finally:
        con.close()
    assert "[QA_VERIFICATION_SCOPE]\nTASK_RUN" in instruction
    assert "Historical project maintenance or code changes from earlier tasks are context, not violations" in instruction
    assert "Explicitly approved result-artifact writes are task outputs, not production mutations" in instruction
