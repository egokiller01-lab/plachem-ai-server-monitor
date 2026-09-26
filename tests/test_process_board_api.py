from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_ID = "plachem-agent-war-room"


class ProcessBoardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "war-room.sqlite3"
        self.old_db = os.environ.get("PLACHEM_WAR_ROOM_DB")
        self.old_tokens = os.environ.get("PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS")
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(self.db)
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = '{"main":"fixture-main-token"}'

        import war_room
        import war_room_actions

        war_room.provision_database(self.db)
        war_room_actions.provision_action_schema(self.db)
        self._seed()
        from app import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.old_db is None:
            os.environ.pop("PLACHEM_WAR_ROOM_DB", None)
        else:
            os.environ["PLACHEM_WAR_ROOM_DB"] = self.old_db
        if self.old_tokens is None:
            os.environ.pop("PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS", None)
        else:
            os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = self.old_tokens
        self.temp_dir.cleanup()

    def _seed(self) -> None:
        statuses = (
            ("draft", "task-draft"),
            ("awaiting_approval", "task-awaiting"),
            ("approved", "task-approved"),
            ("running", "task-running"),
            ("qa", "task-qa"),
            ("completed", "task-completed"),
            ("stopped", "task-stopped"),
            ("stop_unconfirmed", "task-stop-unconfirmed"),
            ("rework_required", "task-rework"),
        )
        now = 1_800_000_000
        with sqlite3.connect(self.db) as con:
            for index, (status, task_id) in enumerate(statuses):
                message_id = f"message-{task_id}"
                con.execute(
                    """INSERT INTO war_messages
                       (id,project_id,message_type,author_type,author_id,body,
                        source_session_id,created_at,redaction_state,original_body)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        message_id, PROJECT_ID, "instruction", "agent", "main",
                        f"Input for {task_id}", f"source-session-{index}", now + index,
                        "clean", f"Input for {task_id}",
                    ),
                )
                con.execute(
                    """INSERT INTO war_tasks
                       (id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,
                        scope,status,manyfast_version,document_version,call_limit,turn_limit,
                        execution_mode,deadline_at,revision,qa_cycle,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, PROJECT_ID, message_id, "ERPcoder", "ERPqa",
                        f"Scope for {task_id}", status, "fixture", "fixture", 1, 1,
                        "LEGACY", now + 3600, 1, 0, now + index, now + index,
                    ),
                )
                con.execute(
                    "INSERT INTO war_task_agents(task_id,agent_id) VALUES (?,?)",
                    (task_id, "ERPcoder"),
                )
            con.execute(
                """INSERT INTO war_deliveries
                   (id,message_id,agent_id,task_revision,status,error_class,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                ("delivery-error", "message-task-running", "ERPcoder", 1,
                 "failed", "system_error", now + 100),
            )
            con.execute(
                """INSERT INTO war_audit_events
                   (id,project_id,actor_id,event_type,target_type,target_id,payload_redacted,correlation_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("audit-rework", PROJECT_ID, "ERPqa", "qa_verdict_rework_required", "task",
                 "task-rework", "{}", "fixture", now + 199),
            )
            for verdict, task_id in (("PASS", "task-qa"), ("FAIL", "task-stopped"),
                                     ("REWORK", "task-rework")):
                con.execute(
                    """INSERT INTO war_qa_verdicts
                       (id,task_id,qa_principal,verdict,evidence_profile,signature,
                        signed_payload,created_at,task_revision,scope_hash,document_version,qa_cycle)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"verdict-{task_id}", task_id, "ERPqa", verdict, "fixture",
                        "fixture", "{}", now + 200, 1, "fixture", "fixture", 0,
                    ),
                )
            con.commit()

    def test_process_board_projection_and_precedence(self) -> None:
        import war_room_actions

        self.assertEqual("BLOCKED", war_room_actions._process_board_state("running", True, None))
        self.assertEqual("DONE", war_room_actions._process_board_state("completed", False, "FAIL"))
        self.assertEqual("REWORK", war_room_actions._process_board_state("rework_required", False, "FAIL"))
        self.assertEqual("PASS", war_room_actions._process_board_state("qa", False, "PASS"))
        self.assertEqual("FAIL", war_room_actions._process_board_state("qa", False, "FAIL"))
        self.assertEqual("REWORK", war_room_actions._process_board_state("qa", False, "REWORK"))
        self.assertEqual("SUPERSEDED", war_room_actions._process_board_state("rework_required", True, "FAIL", superseded=True))
        self.assertIn("SUPERSEDED", war_room_actions.PROCESS_BOARD_STATES)

        before = self.db.read_bytes()
        headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}
        response = self.client.get(f"/api/war-room/projects/{PROJECT_ID}/process-board", headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual("readonly", payload["mode"])
        self.assertEqual(list(war_room_actions.PROCESS_BOARD_STATES), payload["states"])
        self.assertEqual(9, len(payload["items"]))

        by_id = {item["task_id"]: item for item in payload["items"]}
        expected = {
            "task-draft": "WAITING",
            "task-awaiting": "WAITING",
            "task-approved": "READY",
            "task-running": "BLOCKED",
            "task-qa": "PASS",
            "task-completed": "DONE",
            "task-stopped": "BLOCKED",
            "task-stop-unconfirmed": "BLOCKED",
            "task-rework": "REWORK",
        }
        self.assertEqual(expected, {key: by_id[key]["mapped_state"] for key in expected})
        card = by_id["task-completed"]
        for field in (
            "task_id", "step_id", "step_name", "assigned_agent", "mapped_state",
            "predecessor_step", "input", "output", "pass_condition", "session",
            "rework_count",
        ):
            self.assertIn(field, card)
        self.assertEqual("ERPcoder", card["assigned_agent"])
        self.assertEqual("Input for task-completed", card["input"])
        self.assertEqual("source-session-5", card["session"]["session_id"])
        self.assertEqual([], payload["columns"]["RUNNING"])
        self.assertEqual(1, by_id["task-rework"]["rework_count"])

        legacy = self.client.get(f"/api/war-room/projects/{PROJECT_ID}/tasks", headers=headers)
        self.assertEqual(200, legacy.status_code, legacy.text)
        self.assertEqual({"mode", "items"}, set(legacy.json()))
        self.assertNotIn("mapped_state", legacy.json()["items"][0])
        self.assertEqual(before, self.db.read_bytes())


if __name__ == "__main__":
    unittest.main()
