from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient


PROJECT_ID = "plachem-agent-war-room"
HEADERS = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}


class WarRoomFreshContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.env_keys = (
            "PLACHEM_WAR_ROOM_DB", "OPENCLAW_HOME", "PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS",
            "PLACHEM_WAR_ROOM_TEST_ADAPTER", "PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS",
            "PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE",
            "PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", "PLACHEM_WAR_ROOM_SESSION_SECRET",
            "PLACHEM_WAR_ROOM_TEST_ENFORCE_FRESH_CONTEXT",
        )
        self.old_env = {key: os.environ.get(key) for key in self.env_keys}
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(root / "war-room.sqlite3")
        os.environ["OPENCLAW_HOME"] = str(root / "openclaw")
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = (
            '{"main":"fixture-main-token","ERPcoder":"fixture-erpcoder-token",'
            '"ERPmanager":"fixture-erpmanager-token","ERPqa":"fixture-erpqa-token"}'
        )
        os.environ["PLACHEM_WAR_ROOM_TEST_ADAPTER"] = "1"
        os.environ["PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS"] = "main"
        os.environ["PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE"] = "1"
        os.environ["PLACHEM_WAR_ROOM_QA_SIGNING_SECRET"] = "fixture-qa-secret"
        os.environ["PLACHEM_WAR_ROOM_SESSION_SECRET"] = "fixture-fresh-context-secret"
        os.environ["PLACHEM_WAR_ROOM_TEST_ENFORCE_FRESH_CONTEXT"] = "1"
        import war_room
        import war_room_actions
        war_room.provision_database()
        war_room_actions.provision_action_schema()
        from app import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def _context(self, action: str, target_id: str | None = None) -> str:
        params = {"action": action}
        if target_id:
            params["target_id"] = target_id
        response = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/mutation-context?{urlencode(params)}",
            headers=HEADERS,
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["context_token"]

    def _prepare(self, key: str) -> str:
        response = self.client.post(
            f"/api/war-room/projects/{PROJECT_ID}/prepare",
            json={
                "instruction": "fresh context test",
                "agent_ids": ["ERPcoder"],
                "deadline_at": int(time.time()) + 1200,
                "document_version": "baseline-2026-08-23",
            },
            headers={**HEADERS, "Idempotency-Key": key},
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()["task_id"]

    def test_project_stop_requires_fresh_and_rejects_stale_context(self) -> None:
        missing = self.client.post(
            f"/api/war-room/projects/{PROJECT_ID}/stop", json={},
            headers={**HEADERS, "Idempotency-Key": "fresh-stop-missing"},
        )
        self.assertEqual(409, missing.status_code, missing.text)
        self.assertEqual("FRESH_CONTEXT_REQUIRED", missing.json()["detail"])

        stale_token = self._context("project_stop")
        self._prepare("fresh-state-change")
        stale = self.client.post(
            f"/api/war-room/projects/{PROJECT_ID}/stop",
            json={"context_token": stale_token},
            headers={**HEADERS, "Idempotency-Key": "fresh-stop-stale"},
        )
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual("STALE_CONTEXT", stale.json()["detail"])

        fresh_token = self._context("project_stop")
        stopped = self.client.post(
            f"/api/war-room/projects/{PROJECT_ID}/stop",
            json={"context_token": fresh_token},
            headers={**HEADERS, "Idempotency-Key": "fresh-stop-valid"},
        )
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual("stopped", stopped.json()["status"])

    def test_task_approve_rejects_missing_and_wrong_action_context(self) -> None:
        task_id = self._prepare("fresh-task-prepare")
        body = {"expires_at": int(time.time()) + 600}
        missing = self.client.post(
            f"/api/war-room/tasks/{task_id}/approve-execute", json=body,
            headers={**HEADERS, "Idempotency-Key": "fresh-approve-missing"},
        )
        self.assertEqual(409, missing.status_code, missing.text)
        self.assertEqual("FRESH_CONTEXT_REQUIRED", missing.json()["detail"])

        wrong = self._context("task_stop", task_id)
        mismatched = self.client.post(
            f"/api/war-room/tasks/{task_id}/approve-execute",
            json={**body, "context_token": wrong},
            headers={**HEADERS, "Idempotency-Key": "fresh-approve-wrong"},
        )
        self.assertEqual(409, mismatched.status_code, mismatched.text)

        token = self._context("task_approve_execute", task_id)
        approved = self.client.post(
            f"/api/war-room/tasks/{task_id}/approve-execute",
            json={**body, "context_token": token},
            headers={**HEADERS, "Idempotency-Key": "fresh-approve-valid"},
        )
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertEqual("running", approved.json()["status"])

    def test_ui_uses_fresh_context_for_high_risk_actions(self) -> None:
        javascript = (Path(__file__).resolve().parents[1] / "static" / "war-room-ui.js").read_text()
        self.assertIn("freshMutationContext", javascript)
        self.assertIn("guardedPost", javascript)
        self.assertIn("/mutation-context?", javascript)
        for action in (
            "project_stop", "project_resume", "task_stop",
            "task_approve_execute", "representative_completion",
        ):
            self.assertIn(action, javascript)


if __name__ == "__main__":
    unittest.main()
