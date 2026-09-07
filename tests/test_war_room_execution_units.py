"""Focused unit tests for War Room Phase 2 execution units."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from war_room_execution_compiler import compile_task_intent
from war_room_execution_readiness import ExecutionReadinessEvaluator
from war_room_execution_units import ExecutionUnitStore


class TestCompileTaskIntent(unittest.TestCase):
    """Tests for the task intent compiler."""

    def test_compile_single_agent(self):
        """Compile a single agent task."""
        result = compile_task_intent(
            war_project_id="proj-1",
            war_task_id="task-1",
            agents=["erpcoder"],
        )
        self.assertEqual(result["war_project_id"], "proj-1")
        self.assertEqual(result["war_task_id"], "task-1")
        self.assertIn("correlation_id", result)
        self.assertEqual(len(result["execution_units"]), 1)
        unit = result["execution_units"][0]
        self.assertEqual(unit["agent_id"], "erpcoder")
        self.assertEqual(unit["depends_on_execution_ids"], [])

    def test_compile_multi_agent(self):
        """Compile a multi-agent task."""
        result = compile_task_intent(
            war_project_id="proj-1",
            war_task_id="task-1",
            agents=["erpmanager", "erpcoder", "secretary"],
        )
        self.assertEqual(len(result["execution_units"]), 3)
        self.assertEqual(len(result["workflow_graph"]), 3)

        # All units should have the same correlation_id
        correlation_ids = {u["correlation_id"] for u in result["execution_units"]}
        self.assertEqual(len(correlation_ids), 1)

    def test_compile_deduplicates_agents(self):
        """Compile should deduplicate agents."""
        result = compile_task_intent(
            war_project_id="proj-1",
            war_task_id="task-1",
            agents=["erpcoder", "erpmanager", "erpcoder"],
        )
        self.assertEqual(len(result["execution_units"]), 2)

    def test_compile_with_workflow_dependencies(self):
        """Compile with workflow dependencies."""
        result = compile_task_intent(
            war_project_id="proj-1",
            war_task_id="task-1",
            agents=["erpcoder", "erpmanager"],
            workflow={
                "erpcoder": {"role": "implementation", "depends_on": []},
                "erpmanager": {"role": "review", "depends_on": ["erpcoder"]},
            },
        )
        self.assertEqual(len(result["execution_units"]), 2)

        # Find units by agent
        by_agent = {u["agent_id"]: u for u in result["execution_units"]}
        self.assertEqual(by_agent["erpcoder"]["depends_on_execution_ids"], [])
        self.assertEqual(by_agent["erpmanager"]["depends_on_execution_ids"], [by_agent["erpcoder"]["execution_id"]])

    def test_compile_rejects_empty_agents(self):
        """Compile should reject empty agents list."""
        with self.assertRaises(ValueError):
            compile_task_intent(
                war_project_id="proj-1",
                war_task_id="task-1",
                agents=[],
            )

    def test_compile_rejects_invalid_project_id(self):
        """Compile should reject invalid project_id."""
        with self.assertRaises(ValueError):
            compile_task_intent(
                war_project_id="",
                war_task_id="task-1",
                agents=["erpcoder"],
            )

    def test_compile_rejects_invalid_task_id(self):
        """Compile should reject invalid task_id."""
        with self.assertRaises(ValueError):
            compile_task_intent(
                war_project_id="proj-1",
                war_task_id="",
                agents=["erpcoder"],
            )

    def test_compile_rejects_self_dependency(self):
        """Compile should reject self-dependency."""
        with self.assertRaises(ValueError):
            compile_task_intent(
                war_project_id="proj-1",
                war_task_id="task-1",
                agents=["erpcoder"],
                workflow={
                    "erpcoder": {"role": "implementation", "depends_on": ["erpcoder"]},
                },
            )

    def test_compile_rejects_unknown_dependency(self):
        """Compile should reject unknown dependency."""
        with self.assertRaises(ValueError):
            compile_task_intent(
                war_project_id="proj-1",
                war_task_id="task-1",
                agents=["erpcoder"],
                workflow={
                    "erpcoder": {"role": "implementation", "depends_on": ["unknown"]},
                },
            )

    def test_compile_rejects_cycle(self):
        """Compile should reject dependency cycles."""
        with self.assertRaises(ValueError):
            compile_task_intent(
                war_project_id="proj-1",
                war_task_id="task-1",
                agents=["erpcoder", "erpmanager"],
                workflow={
                    "erpcoder": {"role": "implementation", "depends_on": ["erpmanager"]},
                    "erpmanager": {"role": "review", "depends_on": ["erpcoder"]},
                },
            )

    def test_compile_preserves_correlation_id(self):
        """Compile should preserve provided correlation_id."""
        result = compile_task_intent(
            war_project_id="proj-1",
            war_task_id="task-1",
            agents=["erpcoder"],
            correlation_id="corr-custom-123",
        )
        self.assertEqual(result["correlation_id"], "corr-custom-123")
        for unit in result["execution_units"]:
            self.assertEqual(unit["correlation_id"], "corr-custom-123")


class TestExecutionUnitStore(unittest.TestCase):
    """Tests for the execution unit store."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._create_schema()
        self.store = ExecutionUnitStore(self.db_path)
        self.store.ensure_schema()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_schema(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE war_projects (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE war_tasks (id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO war_projects VALUES ('proj-1')")
            con.execute("INSERT INTO war_tasks VALUES ('task-1')")

    def test_create_and_get_unit(self):
        """Create and retrieve a unit."""
        unit = self.store.create_unit(
            execution_id="exec-1",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpcoder",
            depends_on_execution_ids=[],
        )
        self.assertEqual(unit["execution_id"], "exec-1")

        retrieved = self.store.get_unit("exec-1")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["war_project_id"], "proj-1")
        self.assertEqual(retrieved["war_task_id"], "task-1")
        self.assertEqual(retrieved["correlation_id"], "corr-1")
        self.assertEqual(retrieved["agent_id"], "erpcoder")
        self.assertEqual(retrieved["depends_on_execution_ids"], [])

    def test_create_duplicate_unit_raises(self):
        """Create duplicate unit should raise."""
        self.store.create_unit(
            execution_id="exec-1",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpcoder",
        )
        with self.assertRaises(ValueError):
            self.store.create_unit(
                execution_id="exec-1",
                war_project_id="proj-1",
                war_task_id="task-1",
                correlation_id="corr-1",
                agent_id="erpcoder",
            )

    def test_get_units_by_correlation(self):
        """Get units by correlation."""
        self.store.create_unit(
            execution_id="exec-1",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpcoder",
        )
        self.store.create_unit(
            execution_id="exec-2",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpmanager",
        )
        units = self.store.get_units_by_correlation("corr-1")
        self.assertEqual(len(units), 2)

    def test_get_units_by_task(self):
        """Get units by task."""
        self.store.create_unit(
            execution_id="exec-1",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpcoder",
        )
        units = self.store.get_units_by_task("proj-1", "task-1")
        self.assertEqual(len(units), 1)

    def test_update_core_run_id(self):
        """Update core_run_id."""
        self.store.create_unit(
            execution_id="exec-1",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpcoder",
        )
        self.store.update_core_run_id("exec-1", "core-run-1")
        unit = self.store.get_unit("exec-1")
        self.assertEqual(unit["core_run_id"], "core-run-1")

    def test_restart_recovery(self):
        """Restart recovery: data persists across store instances."""
        self.store.create_unit(
            execution_id="exec-1",
            war_project_id="proj-1",
            war_task_id="task-1",
            correlation_id="corr-1",
            agent_id="erpcoder",
        )

        # Simulate restart with new store instance
        store2 = ExecutionUnitStore(self.db_path)
        unit = store2.get_unit("exec-1")
        self.assertIsNotNone(unit)
        self.assertEqual(unit["war_project_id"], "proj-1")


class TestExecutionReadinessEvaluator(unittest.TestCase):
    """Tests for the execution readiness evaluator."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "readiness.db"
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE war_projects (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE war_tasks (id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO war_projects VALUES ('proj-1')")
            con.execute("INSERT INTO war_tasks VALUES ('task-1')")
        self.store = ExecutionUnitStore(self.db_path)
        self.store.ensure_schema()

        class StatusOnlyEngine:
            def __init__(self):
                self.statuses = {}

            def status(self, core_run_id):
                if core_run_id not in self.statuses:
                    raise ValueError("UNKNOWN_CORE_RUN")
                return {"status": self.statuses[core_run_id], "core_run_id": core_run_id}

            def get_execution(self, execution_id):
                raise AssertionError("readiness must use ExecutionUnitStore")

        self.engine = StatusOnlyEngine()
        self.evaluator = ExecutionReadinessEvaluator(self.store, self.engine)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _set_run_status(self, core_run_id: str, status: str):
        self.engine.statuses[core_run_id] = status

    def _set_execution(self, execution_id: str, core_run_id: str | None = None):
        self.store.create_unit(
            execution_id=execution_id, war_project_id="proj-1", war_task_id="task-1",
            correlation_id="corr-1", agent_id="erpcoder", core_run_id=core_run_id,
        )

    def test_ready_no_dependencies(self):
        """Ready when no dependencies."""
        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": [],
        }
        result = self.evaluator.evaluate(unit)
        self.assertEqual(result["readiness"], "READY")

    def test_waiting_for_active_dependency(self):
        """Waiting when dependency is active."""
        self._set_execution("exec-dep", "core-dep")
        self._set_run_status("core-dep", "RUNNING")

        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": ["exec-dep"],
        }
        result = self.evaluator.evaluate(unit)
        self.assertEqual(result["readiness"], "WAITING")

    def test_ready_for_passed_dependency(self):
        """Ready when dependency passed."""
        self._set_execution("exec-dep", "core-dep")
        self._set_run_status("core-dep", "PASS")

        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": ["exec-dep"],
        }
        result = self.evaluator.evaluate(unit)
        self.assertEqual(result["readiness"], "READY")

    def test_blocked_for_failed_dependency(self):
        """Blocked when dependency failed."""
        self._set_execution("exec-dep", "core-dep")
        self._set_run_status("core-dep", "FAIL")

        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": ["exec-dep"],
        }
        result = self.evaluator.evaluate(unit)
        self.assertEqual(result["readiness"], "BLOCKED")

    def test_blocked_for_cancelled_dependency(self):
        """Blocked when dependency cancelled."""
        self._set_execution("exec-dep", "core-dep")
        self._set_run_status("core-dep", "CANCELLED")

        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": ["exec-dep"],
        }
        result = self.evaluator.evaluate(unit)
        self.assertEqual(result["readiness"], "BLOCKED")

    def test_blocked_for_not_found_dependency(self):
        """Blocked when dependency not found."""
        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": ["exec-dep"],
        }
        result = self.evaluator.evaluate(unit)
        self.assertEqual(result["readiness"], "BLOCKED")

    def test_waiting_to_ready_transition(self):
        """Transition from WAITING to READY as dependency completes."""
        self._set_execution("exec-dep", "core-dep")
        self._set_run_status("core-dep", "RUNNING")

        unit = {
            "execution_id": "exec-1",
            "agent_id": "erpcoder",
            "depends_on_execution_ids": ["exec-dep"],
        }

        # Initially waiting
        result1 = self.evaluator.evaluate(unit)
        self.assertEqual(result1["readiness"], "WAITING")

        # After dependency passes
        self._set_run_status("core-dep", "PASS")
        result2 = self.evaluator.evaluate(unit)
        self.assertEqual(result2["readiness"], "READY")


if __name__ == "__main__":
    unittest.main()
