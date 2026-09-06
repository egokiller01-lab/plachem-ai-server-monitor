import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_war_room_orchestration import FakeCoreEngine
from war_room_orchestration import WarRoomOrchestrator, _core_run_id_for
from war_room_execution_units import ExecutionUnitStore


class Phase2Step1AutoDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tmpdir.name) / "execution.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE war_projects (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE war_tasks (id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO war_projects VALUES ('p')")
            con.execute("INSERT INTO war_tasks VALUES ('t')")
        self.store = ExecutionUnitStore(self.db)
        self.store.ensure_schema()
        self.engine = FakeCoreEngine()
        self.orchestrator = WarRoomOrchestrator(core_engine=self.engine, execution_store=self.store)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _compile(self, *, message=True):
        workflow = {
            "A": {"role": "first", "depends_on": [], "message": "run A", "timeout_seconds": 10},
            "B": {"role": "second", "depends_on": ["A"]},
        }
        if message:
            workflow["B"].update({"message": "run B", "timeout_seconds": 20, "goal_contract": {"step": "B"}})
        result = self.orchestrator.compile_and_persist(
            war_project_id="p", war_task_id="t", agents=["A", "B"], workflow=workflow,
        )
        return {row["agent_id"]: row for row in result["execution_units"]}

    def _finish_a(self, status):
        units = self._compile()
        a = units["A"]
        self.orchestrator.dispatch_execution(execution_id=a["execution_id"], message="run A", timeout_seconds=10)
        core_id = _core_run_id_for(a["execution_id"])
        self.engine.set_status(core_id, status)
        return units, core_id

    def test_pass_terminal_dispatches_ready_child_once(self):
        units, core_id = self._finish_a("PASS")
        result = self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "PASS"})
        self.assertEqual([units["B"]["execution_id"]], [row["execution_id"] for row in result])
        self.assertEqual(2, len(self.engine.dispatch_calls))
        self.assertEqual("run B", self.engine.dispatch_calls[-1]["message"])

    def test_pass_terminal_readies_and_dispatches_b_and_c_once_each(self):
        workflow = {
            "A": {"role": "first", "depends_on": [], "message": "run A", "timeout_seconds": 10},
            "B": {"role": "second", "depends_on": ["A"], "message": "run B", "timeout_seconds": 20},
            "C": {"role": "third", "depends_on": ["A"], "message": "run C", "timeout_seconds": 30},
        }
        compiled = self.orchestrator.compile_and_persist(
            war_project_id="p", war_task_id="t", agents=["A", "B", "C"], workflow=workflow,
        )
        units = {row["agent_id"]: row for row in compiled["execution_units"]}
        self.assertEqual("WAITING", self.orchestrator.readiness(units["B"]["execution_id"])["readiness"])
        self.assertEqual("WAITING", self.orchestrator.readiness(units["C"]["execution_id"])["readiness"])

        self.orchestrator.dispatch_execution(
            execution_id=units["A"]["execution_id"], message="run A", timeout_seconds=10,
        )
        core_id = _core_run_id_for(units["A"]["execution_id"])
        self.engine.set_status(core_id, "PASS")
        self.assertEqual("READY", self.orchestrator.readiness(units["B"]["execution_id"])["readiness"])
        self.assertEqual("READY", self.orchestrator.readiness(units["C"]["execution_id"])["readiness"])

        result = self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "PASS"})

        self.assertEqual(
            {units["B"]["execution_id"], units["C"]["execution_id"]},
            {row["execution_id"] for row in result},
        )
        self.assertEqual(1, sum(call["agent_id"] == "B" for call in self.engine.dispatch_calls))
        self.assertEqual(1, sum(call["agent_id"] == "C" for call in self.engine.dispatch_calls))
        self.assertEqual(3, len(self.engine.dispatch_calls))

    def test_fail_terminal_leaves_child_blocked_and_undispatched(self):
        units, core_id = self._finish_a("FAIL")
        self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "FAIL"})
        self.assertEqual("BLOCKED", self.orchestrator.readiness(units["B"]["execution_id"])["readiness"])
        self.assertIsNone(self.store.get_unit(units["B"]["execution_id"])["core_run_id"])

    def test_cancelled_terminal_leaves_child_blocked_and_undispatched(self):
        units, core_id = self._finish_a("CANCELLED")
        self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "CANCELLED"})
        self.assertEqual("BLOCKED", self.orchestrator.readiness(units["B"]["execution_id"])["readiness"])
        self.assertIsNone(self.store.get_unit(units["B"]["execution_id"])["core_run_id"])

    def test_duplicate_terminal_events_do_not_dispatch_twice(self):
        units, core_id = self._finish_a("PASS")
        event = {"core_run_id": core_id, "status": "PASS"}
        self.orchestrator.on_core_run_terminal(event)
        self.orchestrator.on_core_run_terminal(event)
        self.assertEqual(2, len(self.engine.dispatch_calls))
        self.assertIsNotNone(self.store.get_unit(units["B"]["execution_id"])["dispatch_claimed_at"])

    def test_ready_child_with_existing_core_run_id_is_not_redispatched(self):
        units, core_id = self._finish_a("PASS")
        child_core_id = "core-existing-b"
        self.store.update_core_run_id(units["B"]["execution_id"], child_core_id)
        self.engine.records[child_core_id] = {
            "core_run_id": child_core_id,
            "agent_id": "B",
            "message": "run B",
            "idempotency_key": "existing-b",
            "status": "RUNNING",
        }

        self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "PASS"})

        self.assertEqual(1, len(self.engine.dispatch_calls))
        self.assertEqual(child_core_id, self.store.get_unit(units["B"]["execution_id"])["core_run_id"])

    def test_concurrent_terminal_callbacks_do_not_double_dispatch(self):
        units, core_id = self._finish_a("PASS")
        threads = [threading.Thread(target=self.orchestrator.on_core_run_terminal,
                                    args=({"core_run_id": core_id, "status": "PASS"},)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(2, len(self.engine.dispatch_calls))
        self.assertEqual(_core_run_id_for(units["B"]["execution_id"]),
                         self.store.get_unit(units["B"]["execution_id"])["core_run_id"])

    def test_unrelated_terminal_run_is_ignored(self):
        self._compile()
        self.orchestrator.on_core_run_terminal({"core_run_id": "not-a-unit", "status": "PASS"})
        self.assertEqual([], self.engine.dispatch_calls)

    def test_missing_persisted_child_input_is_not_auto_dispatched(self):
        units, core_id = self._finish_a("PASS")
        self.store.update_dispatch_input(units["B"]["execution_id"], message="", timeout_seconds=20)
        self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "PASS"})
        self.assertEqual(1, len(self.engine.dispatch_calls))
        self.assertIsNone(self.store.get_unit(units["B"]["execution_id"])["core_run_id"])


if __name__ == "__main__":
    unittest.main()
