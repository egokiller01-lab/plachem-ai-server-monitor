from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


class FakeAgents:
    def require(self, agent_id):
        if agent_id not in {"ERPcoder", "ERPqa"}:
            raise ValueError(f"UNKNOWN_AGENT:{agent_id}")
        return {"agent_id": agent_id}

    def candidate_agent_ids(self, required_capabilities=None):
        return ["ERPcoder", "ERPqa"]


class FakeCore:
    def __init__(self):
        self.agents = FakeAgents()
        self.records = {}
        self.dispatch_calls = []
        self.status_calls = []
        self.abort_calls = []
        self.adapter = type("Adapter", (), {"rpc": self})()

    def request_on_owner_connection(self, method, params, timeout):
        self.abort_calls.append((method, params, timeout))
        return {"status": "aborted"}

    def dispatch(self, **kwargs):
        self.dispatch_calls.append(kwargs)
        record = {
            "core_run_id": kwargs["core_run_id"],
            "status": "QUEUED",
            "openclaw_binding": {
                "session_key": f"agent:{kwargs['agent_id'].casefold()}:fixture",
                "openclaw_run_id": f"openclaw-{kwargs['core_run_id']}",
            },
        }
        self.records[kwargs["core_run_id"]] = record
        return record

    def status(self, core_run_id):
        self.status_calls.append(core_run_id)
        if core_run_id not in self.records:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        return self.records[core_run_id]

    def mark_user_cancelled_after_abort(self, core_run_id):
        self.records[core_run_id]["status"] = "CANCELLED"
        self.records[core_run_id]["reason"] = "USER_CANCEL"
        return self.records[core_run_id]


class FakeStopController:
    def __init__(self):
        self.calls = []

    def stop_core_run(self, core_run_id):
        self.calls.append(core_run_id)
        return type("Receipt", (), {"status": "stopped", "error_code": None})()


class WarRoomOrchestrationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old = {key: os.environ.get(key) for key in (
            "PLACHEM_WAR_ROOM_DB", "OPENCLAW_HOME", "PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS",
        )}
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(root / "war-room.sqlite3")
        os.environ["OPENCLAW_HOME"] = str(root / "openclaw")
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = '{"main":"main-token","ERPcoder":"coder-token"}'
        import war_room
        import war_room_actions
        import fast_gateway_service
        war_room.provision_database()
        war_room_actions._EXECUTION_ORCHESTRATORS.clear()
        fast_gateway_service._HARNESSES.clear()
        from app import app
        from war_room_execution_units import ExecutionUnitStore
        from war_room_orchestration import WarRoomOrchestrator
        self.client = TestClient(app)
        self.core = FakeCore()
        self.store = ExecutionUnitStore(Path(os.environ["PLACHEM_WAR_ROOM_DB"]))
        self.store.ensure_schema()
        self.stopper = FakeStopController()
        self.orchestrator = WarRoomOrchestrator(
            core_engine=self.core,
            execution_store=self.store,
            stop_controller=self.stopper,
        )
        self.headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "main-token"}

    def tearDown(self):
        import war_room_actions
        import fast_gateway_service
        war_room_actions._EXECUTION_ORCHESTRATORS.clear()
        fast_gateway_service._HARNESSES.clear()
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def task(self, project_id="plachem-agent-war-room"):
        response = self.client.post(
            f"/api/war-room/projects/{project_id}/tasks",
            json={
                "scope": "HTTP orchestration fixture",
                "assignee_agent_id": "ERPcoder",
                "call_limit": 1,
                "turn_limit": 1,
                "deadline_at": int(time.time()) + 3600,
                "document_version": "baseline-2026-08-23",
            },
            headers={**self.headers, "Idempotency-Key": f"task-{time.time_ns()}"},
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()["task_id"]

    def api(self):
        return mock.patch("war_room_actions._execution_orchestrator", return_value=self.orchestrator)

    def compile(self, task_id, key="compile-1", project_id="plachem-agent-war-room", agents=None, workflow=None):
        return self.client.post(
            f"/api/war-room/projects/{project_id}/tasks/{task_id}/executions/compile",
            json={"agent_ids": agents or ["ERPcoder"], **({"workflow": workflow} if workflow else {})},
            headers={**self.headers, "Idempotency-Key": key},
        )

    def test_candidates_require_read_authorization(self):
        with self.api():
            denied = self.client.get("/api/war-room/projects/plachem-agent-war-room/execution-candidates")
            self.assertEqual(401, denied.status_code)
            allowed = self.client.get(
                "/api/war-room/projects/plachem-agent-war-room/execution-candidates?required_capabilities=safe-smoke",
                headers=self.headers,
            )
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(["ERPcoder", "ERPqa"], allowed.json()["agent_ids"])

    def test_compile_requires_execute_and_is_idempotent(self):
        task_id = self.task()
        with self.api():
            denied = self.client.post(
                f"/api/war-room/projects/plachem-agent-war-room/tasks/{task_id}/executions/compile",
                json={"agent_ids": ["ERPcoder"]},
                headers={"X-War-Room-Actor": "ERPcoder", "X-War-Room-Token": "coder-token", "Idempotency-Key": "denied"},
            )
            first = self.compile(task_id)
            replay = self.compile(task_id)
        self.assertEqual(403, denied.status_code)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(1, len(self.store.get_units_by_task("plachem-agent-war-room", task_id)))

    def test_list_requires_read_authorization_and_projection_does_not_write_run_status(self):
        task_id = self.task()
        with self.api():
            self.assertEqual(200, self.compile(task_id).status_code)
            denied = self.client.get(f"/api/war-room/projects/plachem-agent-war-room/tasks/{task_id}/executions")
            with sqlite3.connect(os.environ["PLACHEM_WAR_ROOM_DB"]) as con:
                before = con.execute("SELECT COUNT(*) FROM war_execution_runs").fetchone()[0]
            listed = self.client.get(
                f"/api/war-room/projects/plachem-agent-war-room/tasks/{task_id}/executions",
                headers=self.headers,
            )
            with sqlite3.connect(os.environ["PLACHEM_WAR_ROOM_DB"]) as con:
                after = con.execute("SELECT COUNT(*) FROM war_execution_runs").fetchone()[0]
        self.assertEqual(401, denied.status_code)
        self.assertEqual(200, listed.status_code)
        self.assertEqual(before, after)

    def test_waiting_dispatch_is_rejected_without_core_dispatch(self):
        task_id = self.task()
        with self.api():
            result = self.compile(
                task_id,
                agents=["ERPcoder", "ERPqa"],
                workflow={
                    "ERPcoder": {"depends_on": []},
                    "ERPqa": {"depends_on": ["ERPcoder"]},
                },
            ).json()
            units = {unit["agent_id"]: unit for unit in result["execution_units"]}
            response = self.client.post(
                f"/api/war-room/executions/{units['ERPqa']['execution_id']}/dispatch",
                json={"message": "wait"},
                headers={**self.headers, "Idempotency-Key": "dispatch-wait"},
            )
        self.assertEqual(409, response.status_code)
        self.assertEqual([], self.core.dispatch_calls)

    def test_ready_dispatch_calls_core_once_and_stop_targets_execution(self):
        task_id = self.task()
        with self.api():
            result = self.compile(task_id, agents=["ERPcoder", "ERPqa"]).json()
            units = {unit["agent_id"]: unit for unit in result["execution_units"]}
            ready = units["ERPcoder"]["execution_id"]
            other = units["ERPqa"]["execution_id"]
            dispatched = self.client.post(
                f"/api/war-room/executions/{ready}/dispatch",
                json={"message": "ready"},
                headers={**self.headers, "Idempotency-Key": "dispatch-ready"},
            )
            stopped = self.client.post(
                f"/api/war-room/executions/{ready}/stop",
                json={},
                headers={**self.headers, "Idempotency-Key": "stop-ready"},
            )
        self.assertEqual(200, dispatched.status_code, dispatched.text)
        self.assertEqual(1, len(self.core.dispatch_calls))
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual([f"core-exec-{ready}"], self.stopper.calls)
        self.assertNotIn(f"core-exec-{other}", self.stopper.calls)

    def test_separate_http_dispatch_and_stop_reuse_owner_engine(self):
        import fast_gateway_service
        import war_room_actions
        task_id = self.task()
        with mock.patch.object(fast_gateway_service, "create_core_engine", return_value=self.core):
            war_room_actions._EXECUTION_ORCHESTRATORS.clear()
            compiled = self.compile(task_id, agents=["ERPcoder", "ERPqa"]).json()
            units = {unit["agent_id"]: unit for unit in compiled["execution_units"]}
            target = units["ERPcoder"]["execution_id"]
            other = units["ERPqa"]["execution_id"]
            dispatched = self.client.post(
                f"/api/war-room/executions/{target}/dispatch",
                json={"message": "owner lifecycle"},
                headers={**self.headers, "Idempotency-Key": "owner-dispatch"},
            )
            stopped = self.client.post(
                f"/api/war-room/executions/{target}/stop",
                json={},
                headers={**self.headers, "Idempotency-Key": "owner-stop"},
            )
        self.assertEqual(200, dispatched.status_code, dispatched.text)
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual(1, len(self.core.dispatch_calls))
        self.assertEqual(
            [("sessions.abort", {"key": "agent:erpcoder:fixture"}, 15.0)],
            self.core.abort_calls,
        )
        self.assertEqual("CANCELLED", self.core.records[f"core-exec-{target}"]["status"])
        self.assertEqual("QUEUED", self.core.records.get(f"core-exec-{other}", {}).get("status", "QUEUED"))

    def test_cross_project_task_is_rejected(self):
        import war_room
        other_project = "other-project"
        task_id = "other-task"
        now = int(time.time())
        with sqlite3.connect(os.environ["PLACHEM_WAR_ROOM_DB"]) as con:
            con.execute("INSERT INTO war_projects VALUES (?,?,?,?,?,?,?)", (other_project, "Other", "planning", "manyfast", "baseline-2026-08-23", now, now))
            con.execute(
                "INSERT INTO war_tasks (id,project_id,scope,status,manyfast_version,execution_mode,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, other_project, "other", "draft", "baseline-2026-08-23", "FAST_GATEWAY", now, now),
            )
        with self.api():
            response = self.compile(task_id, project_id=war_room.PROJECT_ID, key="cross-project")
            listed = self.client.get(
                f"/api/war-room/projects/{war_room.PROJECT_ID}/tasks/{task_id}/executions",
                headers=self.headers,
            )
        self.assertEqual(404, response.status_code)
        self.assertEqual(404, listed.status_code)
        self.assertEqual([], self.store.get_units_by_task(war_room.PROJECT_ID, task_id))

    def test_execution_store_requires_parent_schema(self):
        from war_room_execution_units import ExecutionUnitStore
        path = Path(self.temp.name) / "unprovisioned.sqlite3"
        with self.assertRaisesRegex(RuntimeError, "PARENT_SCHEMA_REQUIRED"):
            ExecutionUnitStore(path).ensure_schema()

    def test_orchestrator_factory_provisions_execution_store_schema(self):
        import fast_gateway_service
        import war_room_actions
        path = Path(self.temp.name) / "factory.sqlite3"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE war_projects (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE war_tasks (id TEXT PRIMARY KEY)")
        previous = os.environ["PLACHEM_WAR_ROOM_DB"]
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(path)
        try:
            with mock.patch.object(fast_gateway_service, "create_core_engine", return_value=self.core), \
                    mock.patch("war_room_orchestration.WarRoomOrchestrator", return_value=object()):
                war_room_actions._execution_orchestrator()
            with sqlite3.connect(path) as con:
                self.assertIsNotNone(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='war_execution_units'"
                ).fetchone())
        finally:
            os.environ["PLACHEM_WAR_ROOM_DB"] = previous


if __name__ == "__main__":
    unittest.main()
