import sqlite3
import tempfile
import unittest
from pathlib import Path

from war_room_fast_gateway import FastGatewayWarRoomAdapter
from war_room_execution_units import ExecutionUnitStore
from war_room_orchestration import WarRoomOrchestrator


class FakeControl:
    def __init__(self):
        self.keys = []

    def abort(self, *, session_key):
        self.keys.append(session_key)
        return "aborted"


class FakeCore:
    def __init__(self):
        self.records = {}

    def status(self, core_run_id):
        if core_run_id not in self.records:
            raise ValueError("UNKNOWN_CORE_RUN")
        return dict(self.records[core_run_id])

    def mark_user_cancelled_after_abort(self, core_run_id):
        record = self.records[core_run_id]
        if record["status"] == "RUNNING":
            record.update(status="CANCELLED", cancel_reason="USER_CANCEL")
        return dict(record)

    def mark_user_cancelled(self, core_run_id):
        return self.mark_user_cancelled_after_abort(core_run_id)

    def reconcile_external_completion(self, core_run_id):
        return self.status(core_run_id)


class ExecutionStopTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "stop.db"
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE war_projects (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE war_tasks (id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO war_projects VALUES ('proj-1')")
            con.execute("INSERT INTO war_tasks VALUES ('task-1')")
        self.store = ExecutionUnitStore(self.db_path)
        self.store.ensure_schema()
        self.engine = FakeCore()
        self.control = FakeControl()
        self.orch = WarRoomOrchestrator(
            core_engine=self.engine, execution_store=self.store,
            stop_controller=FastGatewayWarRoomAdapter(
                self.engine, self.db_path, control_rpc=self.control,
            ),
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def add_unit(self, execution_id, agent_id, core_run_id, status="RUNNING", session_key=None):
        self.store.create_unit(
            execution_id=execution_id, war_project_id="proj-1", war_task_id="task-1",
            correlation_id="corr-1", agent_id=agent_id, core_run_id=core_run_id,
        )
        self.engine.records[core_run_id] = {
            "core_run_id": core_run_id, "status": status,
            "agent_id": agent_id,
            "openclaw_binding": {"openclaw_run_id": "oc-" + execution_id, "session_key": session_key},
            "runtime_class": "LOCAL", "result": {"summary": "done", "evidence": ["/tmp/evidence"]},
            "policy_status": "NORMAL", "cancel_reason": "", "escalation_required": False,
        }

    def test_stop_uses_actual_bound_key_and_projects_live_fields(self):
        self.add_unit("exec-1", "ERPmanager", "core-1", session_key="agent:erpmanager:child")
        projection = self.orch.get_execution("exec-1")
        self.assertTrue({"execution_id", "agent_id", "readiness", "run_status", "core_run_id", "runtime_class", "result_summary", "evidence", "policy_status", "cancel_reason", "escalation_required"}.issubset(projection))
        result = self.orch.stop_execution("exec-1")
        self.assertEqual("stopped", result["status"])
        self.assertEqual(["agent:erpmanager:child"], self.control.keys)
        self.assertEqual("CANCELLED", self.engine.records["core-1"]["status"])
        self.assertEqual("USER_CANCEL", self.engine.records["core-1"]["cancel_reason"])

    def test_terminal_duplicate_missing_binding_and_unrelated_child(self):
        self.add_unit("exec-1", "ERPmanager", "core-1", session_key="agent:erpmanager:one")
        self.add_unit("exec-2", "ERPqa", "core-2", session_key="agent:erpqa:two")
        self.assertEqual("stopped", self.orch.stop_execution("exec-1")["status"])
        self.assertEqual("stopped", self.orch.stop_execution("exec-1")["status"])
        self.assertEqual(["agent:erpmanager:one"], self.control.keys)
        self.assertEqual("RUNNING", self.engine.records["core-2"]["status"])

        self.add_unit("exec-pass", "ERPcoder", "core-pass", status="PASS", session_key="agent:erpcoder:pass")
        self.assertEqual("stopped", self.orch.stop_execution("exec-pass")["status"])
        self.assertEqual(["agent:erpmanager:one"], self.control.keys)

        self.add_unit("exec-missing", "ERPcoder", "core-missing", session_key=None)
        missing = self.orch.stop_execution("exec-missing")
        self.assertEqual("failed", missing["status"])
        self.assertEqual("FAST_GATEWAY_BINDING_MISSING", missing["error_code"])


if __name__ == "__main__":
    unittest.main()
