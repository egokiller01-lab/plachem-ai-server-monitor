from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import war_room
from war_room_actions import provision_action_schema
from war_room_adapter import DeliveryReceipt, OpenClawSessionAdapter
from war_room_worker import process_due_deliveries


def test_direct_adapter_timeout_is_3600(monkeypatch):
    class Bridge:
        connection_id = "c1"
        def __init__(self):
            self.params = None
        def request(self, method, params, timeout_ms=15000):
            if method == "agent":
                self.params = params
                return {"runId":"r1","status":"accepted","sessionKey":params["sessionKey"]}, self.connection_id
            return {"aborted": True}, self.connection_id

    monkeypatch.setenv("PLACHEM_WAR_ROOM_REAL_ADAPTER", "1")
    bridge = Bridge()
    adapter = OpenClawSessionAdapter(bridge=bridge)
    adapter.bind_delivery(
        "d1",
        session_key="agent:erpcoder:war-room-test:fixture",
        session_id="s1",
        disposable=True,
        purpose="test",
        agent_id="ERPcoder",
    )
    receipt = adapter.deliver(
        delivery_id="d1", agent_id="ERPcoder",
        instruction_id="i1", body="safe",
    )
    assert receipt.status == "received"
    assert bridge.params["timeout"] == 3600


def test_worker_completion_auto_queues_and_records_independent_qa(monkeypatch, tmp_path):
    db = tmp_path / "war.sqlite3"
    monkeypatch.setenv("PLACHEM_WAR_ROOM_DB", str(db))
    monkeypatch.setenv("PLACHEM_WAR_ROOM_AUTO_QA", "1")
    monkeypatch.setenv("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", "fixture-secret")

    war_room.provision_database(db)
    provision_action_schema(str(db))
    project = war_room.PROJECT_ID
    now = int(time.time())
    report = tmp_path / "report.txt"
    task_id = "task-autoqa"
    message_id = "msg-autoqa"
    packet = {
        "worktree": str(tmp_path),
        "branch": "test",
        "revision": "v1",
        "api_base": "/x",
        "db_label": "test",
        "forbidden": ["production DB"],
        "completion_conditions": ["report"],
        "required_evidence": [{
            "id": "evidence-autoqa",
            "evidence_type": "artifact",
            "source_command": f"cat {report}",
            "expected_contains": "AUTO_QA_OK",
        }],
        "session_integrity_required": False,
        "approved_paths": [str(tmp_path)],
    }

    with sqlite3.connect(db) as con:
        con.execute(
            """INSERT OR IGNORE INTO war_participants
               (id,project_id,principal_type,principal_id,role,can_read,can_comment,can_approve,can_execute,active)
               VALUES (?,?,?,?,?,?,?,?,?,1)""",
            ("p-coder", project, "agent", "ERPcoder", "developer", 1, 1, 0, 0),
        )
        con.execute(
            """INSERT OR IGNORE INTO war_participants
               (id,project_id,principal_type,principal_id,role,can_read,can_comment,can_approve,can_execute,active)
               VALUES (?,?,?,?,?,?,?,?,?,1)""",
            ("p-qa", project, "agent", "ERPqa", "qa", 1, 1, 0, 0),
        )
        con.execute(
            """INSERT INTO war_messages
               (id,project_id,message_type,author_type,author_id,body,created_at,correlation_id,redaction_state,original_body)
               VALUES (?,?,'instruction','agent','main',?,?,?,'clean',?)""",
            (message_id, project, "Create report", now, "corr", "Create report"),
        )
        con.execute(
            """INSERT INTO war_tasks
               (id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,
                manyfast_version,document_version,call_limit,turn_limit,execution_mode,deadline_at,
                revision,qa_cycle,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'running','v1','v1',1,1,'LEGACY',?,1,0,?,?)""",
            (task_id, project, message_id, "ERPcoder", "ERPqa", "read-only test", now + 1800, now, now),
        )
        con.execute("INSERT INTO war_task_agents(task_id,agent_id) VALUES (?,?)", (task_id, "ERPcoder"))
        packet_json = json.dumps(packet, sort_keys=True)
        con.execute(
            "INSERT INTO war_grounding_packets(task_id,packet_json,packet_hash,created_at) VALUES (?,?,?,?)",
            (task_id, packet_json, hashlib.sha256(packet_json.encode()).hexdigest(), now),
        )
        con.execute(
            """INSERT INTO war_deliveries
               (id,message_id,agent_id,task_revision,status,attempt_count,deadline_at,created_at,correlation_id)
               VALUES ('d-worker',?,'ERPcoder',1,'queued',0,?,?,'corr-worker')""",
            (message_id, now + 1800, now),
        )
        con.commit()

    class Adapter:
        def __init__(self):
            self.calls = []
            self.sessions = []
        def create_disposable_session(self, *, agent_id, project_id):
            self.sessions.append(agent_id)
            return {
                "session_key": f"agent:{agent_id.lower()}:war-room-test:auto",
                "session_id": f"s-{agent_id}",
                "purpose": "test",
                "disposable": True,
            }
        def deliver(self, *, delivery_id, agent_id, instruction_id, body):
            self.calls.append((agent_id, body))
            report.write_text("AUTO_QA_OK\n", encoding="utf-8")
            payload = {
                "confirmed_worktree": str(tmp_path),
                "confirmed_revision": "v1",
                "verdict": "PASS",
                "evidence": [str(report)],
                "summary": "worker complete" if agent_id == "ERPcoder" else "independent QA verified",
                "representative_completion_claimed": False,
            }
            return DeliveryReceipt(
                delivery_id, "responded",
                session_id=f"s-{agent_id}",
                run_id=f"r-{agent_id}",
                response_body=json.dumps(payload),
            )

    adapter = Adapter()
    first = process_due_deliveries(db_path=db, adapter=adapter, now=now + 1)
    assert first[0]["status"] == "responded"

    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT status,qa_cycle,revision FROM war_tasks WHERE id=?", (task_id,)
        ).fetchone() == ("qa", 1, 1)
        assert con.execute(
            "SELECT status FROM war_deliveries WHERE message_id=? AND agent_id='ERPqa' AND task_revision=1",
            (message_id,),
        ).fetchone() == ("queued",)
        evidence_row = con.execute(
            "SELECT id,contract_evidence_id,evidence_type,immutable,run_id FROM war_evidence WHERE task_id=?", (task_id,)
        ).fetchone()
        assert evidence_row[0] != "evidence-autoqa"
        assert evidence_row[1:] == ("evidence-autoqa", "artifact", 1, "r-ERPcoder")

    second = process_due_deliveries(db_path=db, adapter=adapter, now=now + 2)
    assert second[0]["status"] == "responded"

    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT verdict,qa_principal FROM war_qa_verdicts WHERE task_id=?", (task_id,)
        ).fetchone() == ("PASS", "ERPqa")
        assert con.execute(
            "SELECT status FROM war_tasks WHERE id=?", (task_id,)
        ).fetchone() == ("qa",)
        assert con.execute(
            "SELECT call_count,turn_count FROM war_task_calls WHERE task_id=? AND task_revision=1",
            (task_id,),
        ).fetchone() == (1, 1)

    assert adapter.sessions == ["ERPqa"]
    assert [agent for agent, _ in adapter.calls] == ["ERPcoder", "ERPqa"]
    assert "[AUTO_QA_REVIEW]" in adapter.calls[1][1]
