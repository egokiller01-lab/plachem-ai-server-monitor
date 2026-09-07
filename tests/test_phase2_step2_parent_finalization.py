import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_war_room_orchestration import FakeCoreEngine
from war_room_execution_units import ExecutionUnitStore
from war_room_orchestration import WarRoomOrchestrator, _core_run_id_for


class Phase2Step2ParentFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tmpdir.name) / "execution.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE war_projects (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE war_tasks (id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO war_projects VALUES ('p')")
            con.execute("INSERT INTO war_tasks VALUES ('t')")
            con.execute("INSERT INTO war_tasks VALUES ('t-observer')")
        self.store = ExecutionUnitStore(self.db)
        self.store.ensure_schema()
        self.engine = FakeCoreEngine()
        self.orchestrator = WarRoomOrchestrator(
            core_engine=self.engine, execution_store=self.store
        )
        compiled = self.orchestrator.compile_and_persist(
            war_project_id="p",
            war_task_id="t",
            agents=["A", "B", "C"],
            workflow={
                "A": {"depends_on": [], "message": "run A", "timeout_seconds": 10},
                "B": {"depends_on": ["A"], "message": "run B", "timeout_seconds": 10},
                "C": {"depends_on": ["A"], "message": "run C", "timeout_seconds": 10},
            },
            correlation_id="step2",
        )
        self.units = {row["agent_id"]: row for row in compiled["execution_units"]}
        self._dispatch("A")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _dispatch(self, agent):
        unit = self.units[agent]
        self.orchestrator.dispatch_execution(
            execution_id=unit["execution_id"], message=f"run {agent}", timeout_seconds=10
        )

    def _terminal(self, agent, status):
        core_id = _core_run_id_for(self.units[agent]["execution_id"])
        self.engine.set_status(core_id, status)
        self.orchestrator.on_core_run_terminal(
            {"core_run_id": core_id, "status": status}
        )

    def test_all_pass_finalizes_completed_once(self):
        self._terminal("A", "PASS")
        self.assertEqual("RUNNING", self.orchestrator.parent_projection("step2")["status"])
        self._terminal("B", "PASS")
        self._terminal("C", "PASS")
        projection = self.orchestrator.parent_projection("step2")
        self.assertEqual("COMPLETED", projection["status"])
        self.assertTrue(projection["finalized"])
        self.assertEqual(1, projection["finalize_count"])

    def test_child_fail_finalizes_failed_after_no_child_running(self):
        self._terminal("A", "PASS")
        self._terminal("B", "FAIL")
        self.assertEqual("RUNNING", self.orchestrator.parent_projection("step2")["status"])
        self._terminal("C", "PASS")
        self.assertEqual("FAILED", self.orchestrator.parent_projection("step2")["status"])

    def test_dependency_blocked_finalizes_blocked(self):
        self._terminal("A", "BLOCKED")
        projection = self.orchestrator.parent_projection("step2")
        self.assertEqual("BLOCKED", projection["status"])
        self.assertEqual(1, projection["finalize_count"])

    def test_cancelled_child_uses_existing_unsuccessful_semantics(self):
        self._terminal("A", "CANCELLED")
        self.assertEqual("FAILED", self.orchestrator.parent_projection("step2")["status"])

    def test_running_child_prevents_finalization(self):
        self._terminal("A", "PASS")
        self._terminal("B", "PASS")
        projection = self.orchestrator.parent_projection("step2")
        self.assertEqual("RUNNING", projection["status"])
        self.assertFalse(projection["finalized"])
        self.assertEqual(0, projection["finalize_count"])

    def test_callback_payload_does_not_override_registry_truth(self):
        core_id = _core_run_id_for(self.units["A"]["execution_id"])
        self.orchestrator.on_core_run_terminal(
            {"core_run_id": core_id, "status": "PASS"}
        )
        projection = self.orchestrator.parent_projection("step2")
        self.assertEqual("RUNNING", projection["status"])
        self.assertFalse(projection["finalized"])
        self.assertEqual(1, len(self.engine.dispatch_calls))

    def test_concurrent_terminals_finalize_once(self):
        self._terminal("A", "PASS")
        for agent in ("B", "C"):
            self.engine.set_status(_core_run_id_for(self.units[agent]["execution_id"]), "PASS")
        threads = [
            threading.Thread(
                target=self.orchestrator.on_core_run_terminal,
                args=({"core_run_id": _core_run_id_for(self.units[a]["execution_id"]), "status": "PASS"},),
            )
            for a in ("B", "C")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, self.orchestrator.parent_projection("step2")["finalize_count"])

    def test_cross_instance_concurrent_terminals_finalize_once(self):
        self._terminal("A", "PASS")
        for agent in ("B", "C"):
            self.engine.set_status(_core_run_id_for(self.units[agent]["execution_id"]), "PASS")
        other = WarRoomOrchestrator(
            core_engine=self.engine, execution_store=ExecutionUnitStore(self.db)
        )
        callbacks = (self.orchestrator, other)
        threads = [
            threading.Thread(
                target=orchestrator.on_core_run_terminal,
                args=({"core_run_id": _core_run_id_for(self.units[agent]["execution_id"]), "status": "PASS"},),
            )
            for orchestrator, agent in zip(callbacks, ("B", "C"))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, self.orchestrator.parent_projection("step2")["finalize_count"])

    def test_duplicate_callback_does_not_finalize_twice(self):
        self._terminal("A", "PASS")
        self._terminal("B", "PASS")
        self._terminal("C", "PASS")
        self._terminal("C", "PASS")
        self.assertEqual(1, self.orchestrator.parent_projection("step2")["finalize_count"])

    def test_step1_auto_dispatch_regression(self):
        self._terminal("A", "PASS")
        self.assertEqual(1, sum(call["agent_id"] == "B" for call in self.engine.dispatch_calls))
        self.assertEqual(1, sum(call["agent_id"] == "C" for call in self.engine.dispatch_calls))
        self.assertEqual("RUNNING", self.orchestrator.parent_projection("step2")["status"])

    def test_observer_is_not_required_for_completion(self):
        compiled = self.orchestrator.compile_and_persist(
            war_project_id="p",
            war_task_id="t-observer",
            agents=["A", "C"],
            workflow={
                "A": {"role": "implementation", "depends_on": [], "message": "required", "timeout_seconds": 10},
                "C": {"role": "observer", "depends_on": [], "message": "observe", "timeout_seconds": 10},
            },
            correlation_id="observer-workflow",
        )
        units = {row["agent_id"]: row for row in compiled["execution_units"]}
        self.assertEqual("observer", self.store.get_unit(units["C"]["execution_id"])["workflow_role"])
        self.orchestrator.dispatch_execution(
            execution_id=units["A"]["execution_id"], message="required", timeout_seconds=10
        )
        core_id = _core_run_id_for(units["A"]["execution_id"])
        self.engine.set_status(core_id, "PASS")
        self.orchestrator.on_core_run_terminal({"core_run_id": core_id, "status": "PASS"})
        projection = self.orchestrator.parent_projection("observer-workflow")
        self.assertEqual("RUNNING", projection["status"])
        observer_core = _core_run_id_for(units["C"]["execution_id"])
        self.engine.set_status(observer_core, "FAIL")
        self.orchestrator.on_core_run_terminal({"core_run_id": observer_core, "status": "FAIL"})
        projection = self.orchestrator.parent_projection("observer-workflow")
        self.assertEqual("COMPLETED", projection["status"])
        self.assertEqual(1, projection["finalize_count"])


if __name__ == "__main__":
    unittest.main()
