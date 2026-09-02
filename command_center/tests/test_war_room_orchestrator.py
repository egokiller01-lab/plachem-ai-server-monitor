import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_registry import AgentRegistry
from dependency_readiness import DependencyReadinessEvaluator
from dispatch_boundary import ExplicitDispatchBoundary
from dispatch_selection import DispatchCandidateSelector
from run_query import RunQuery
from war_room_adapter import WarRoomTaskAdapter, WarRoomTaskCompiler
from workspace_registry import WorkspaceEntry, WorkspaceRegistry
from war_room_orchestrator import WarRoomOrchestrator


class WarRoomOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.run_path = root / "runs.jsonl"
        self.project_root = root / "project"
        self.project_root.mkdir()
        self.agents = AgentRegistry({
            "Achilles": {"enabled": True, "priority": 10, "capabilities": ["coding", "review", "erp"], "runtime_profile": "achilles"},
            "Athena": {"enabled": True, "priority": 20, "capabilities": ["review", "code_review"], "runtime_profile": "athena"},
        })
        self.workspaces = WorkspaceRegistry({
            "workspace-1": WorkspaceEntry("workspace-1", self.project_root, "phase2-worker-identity", "ACTIVE")
        })
        self.orchestrator = WarRoomOrchestrator(
            self.agents,
            self.workspaces,
            run_registry_path=self.run_path,
            dispatcher=mock.Mock(return_value={"status": "PASS"}),
        )

    def payload(self, **overrides):
        value = {
            "war_project_id": "project-1",
            "war_task_id": "task-1",
            "scope": "inspect the project",
            "requested_agents": ["Achilles"],
            "workspace_id": "workspace-1",
            "required_capabilities": ["coding"],
            "external_reference": {"ticket": "WR-1"},
        }
        value.update(overrides)
        return value

    def test_single_submit_preserves_identity_without_execution(self):
        result = self.orchestrator.submit(self.payload(correlation_id="corr-1"))
        self.assertEqual(result["correlation_id"], "corr-1")
        self.assertEqual(result["war_project_id"], "project-1")
        self.assertEqual(result["war_task_id"], "task-1")
        self.assertEqual(len(result["command_tasks"]), 1)
        self.assertEqual(result["selection_mode"], "strict")
        self.assertEqual(self.orchestrator._dispatcher.call_count, 0)

    def test_multi_agent_dependency_graph_and_observer_completion(self):
        result = self.orchestrator.submit(self.payload(
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "implementation", "depends_on": []},
                {"agent_id": "Athena", "role": "review", "depends_on": ["Achilles"]},
            ],
        ))
        self.assertEqual(len(result["command_tasks"]), 2)
        self.assertEqual(result["command_tasks"][1]["depends_on_task_ids"], [result["command_tasks"][0]["task_id"]])
        self.assertEqual({task["correlation_id"] for task in result["command_tasks"]}, {result["correlation_id"]})

    def test_selection_modes_default_to_strict_and_fallback_is_explicit(self):
        strict = self.orchestrator.submit(self.payload())
        self.assertEqual(strict["selection_mode"], "strict")
        self.assertEqual(strict["command_tasks"][0]["worker_selection_mode"], "strict")
        fallback = self.orchestrator.submit(self.payload(
            war_task_id="task-fallback",
            selection_mode="fallback",
            preferred_worker="Achilles",
        ))
        self.assertEqual(fallback["selection_mode"], "fallback")
        self.assertEqual(fallback["command_tasks"][0]["worker_selection_mode"], "fallback")

    def test_duplicate_and_revision_conflict_use_existing_compiler_semantics(self):
        first = self.orchestrator.submit(self.payload())
        self.assertEqual(self.orchestrator.submit(self.payload()), first)
        with self.assertRaisesRegex(ValueError, "COMPILATION_REVISION_REQUIRED"):
            self.orchestrator.submit(self.payload(requested_agents=["Athena"]))

    def test_candidates_and_next_ready_are_read_only(self):
        result = self.orchestrator.submit(self.payload(
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "implementation", "depends_on": []},
                {"agent_id": "Athena", "role": "review", "depends_on": ["Achilles"]},
            ],
        ))
        before = self.run_path.read_bytes() if self.run_path.exists() else b""
        candidates = self.orchestrator.candidates("project-1", "task-1")
        next_ready = self.orchestrator.next_ready("project-1", "task-1")
        self.assertEqual(len(candidates["candidates"]), 1)
        self.assertEqual(next_ready[0]["task_id"], result["command_tasks"][0]["task_id"])
        self.assertEqual(self.run_path.read_bytes() if self.run_path.exists() else b"", before)

    def test_status_exposes_run_and_result_fields_and_next_ready_advances(self):
        result = self.orchestrator.submit(self.payload(
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "implementation", "depends_on": []},
                {"agent_id": "Athena", "role": "review", "depends_on": ["Achilles"]},
            ],
        ))
        implementation, review = result["command_tasks"]
        initial = self.orchestrator.status("project-1", "task-1")
        self.assertEqual(initial["tasks"][0]["readiness"], "READY")
        self.assertIsNone(initial["tasks"][0]["run_id"])
        self.assertFalse(initial["tasks"][0]["result_available"])
        self.run_path.write_text(json.dumps({
            "run_id": "run-implementation",
            "task_id": implementation["task_id"],
            "worker": "Achilles",
            "status": "PASS",
            "gateway_result": {"status": "PASS"},
        }) + "\n", encoding="utf-8")
        self.assertEqual(self.orchestrator.next_ready("project-1", "task-1")[0]["task_id"], review["task_id"])
        current = self.orchestrator.status("project-1", "task-1")
        self.assertEqual(current["tasks"][0]["run_id"], "run-implementation")
        self.assertTrue(current["tasks"][0]["result_available"])
        self.assertEqual(current["tasks"][1]["readiness"], "READY")

    def test_observer_does_not_block_completion_and_failed_dependency_is_blocked(self):
        result = self.orchestrator.submit(self.payload(
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "implementation", "depends_on": []},
                {"agent_id": "Athena", "role": "observer", "depends_on": ["Achilles"]},
            ],
        ))
        implementation = result["command_tasks"][0]
        self.run_path.write_text(json.dumps({"run_id": "run-pass", "task_id": implementation["task_id"], "worker": "Achilles", "status": "PASS"}) + "\n", encoding="utf-8")
        self.assertEqual(self.orchestrator.summary("project-1", "task-1")["overall"], "COMPLETED")

        blocked = self.orchestrator.submit(self.payload(
            war_task_id="blocked",
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "observer", "depends_on": []},
                {"agent_id": "Athena", "role": "review", "depends_on": ["Achilles"]},
            ],
        ))
        observer = blocked["command_tasks"][0]
        with self.run_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"run_id": "run-fail", "task_id": observer["task_id"], "worker": "Achilles", "status": "FAIL"}) + "\n")
        self.assertEqual(self.orchestrator.summary("project-1", "blocked")["overall"], "BLOCKED")

    def test_summary_pending_when_required_child_waits_on_unstarted_observer(self):
        self.orchestrator.submit(self.payload(
            war_task_id="pending",
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "observer", "depends_on": []},
                {"agent_id": "Athena", "role": "review", "depends_on": ["Achilles"]},
            ],
        ))
        self.assertEqual(self.orchestrator.summary("project-1", "pending")["overall"], "PENDING")

    def test_explicit_dispatch_uses_boundary_only(self):
        result = self.orchestrator.submit(self.payload())
        dispatched = self.orchestrator.dispatch(result["command_tasks"][0]["task_id"])
        self.assertEqual(dispatched["status"], "PASS")
        self.orchestrator._dispatcher.assert_called_once()

    def test_unknown_workspace_and_raw_path_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_WORKSPACE"):
            self.orchestrator.submit(self.payload(workspace_id="missing"))
        with self.assertRaisesRegex(ValueError, "raw workspace path"):
            self.orchestrator.submit(self.payload(project_root=str(self.project_root)))

    def test_summary_maps_pending_ready_in_progress_failed_completed_and_observer(self):
        result = self.orchestrator.submit(self.payload(
            requested_agents=["Achilles", "Athena"],
            workflow=[
                {"agent_id": "Achilles", "role": "implementation", "depends_on": []},
                {"agent_id": "Athena", "role": "review", "depends_on": ["Achilles"]},
            ],
        ))
        self.assertEqual(self.orchestrator.summary("project-1", "task-1")["overall"], "READY")
        task_a, task_b = result["command_tasks"]
        self.run_path.write_text('\n'.join([
            '{"run_id":"r-a","task_id":"%s","worker":"Achilles","status":"RUNNING"}' % task_a["task_id"],
        ]) + '\n', encoding="utf-8")
        self.assertEqual(self.orchestrator.summary("project-1", "task-1")["overall"], "IN_PROGRESS")

    def test_summary_completed_failed_and_blocked(self):
        completed = self.orchestrator.submit(self.payload(war_task_id="completed"))
        task_id = completed["command_tasks"][0]["task_id"]
        self.run_path.write_text(json.dumps({"run_id": "r1", "task_id": task_id, "worker": "Achilles", "status": "PASS"}) + "\n", encoding="utf-8")
        self.assertEqual(self.orchestrator.summary("project-1", "completed")["overall"], "COMPLETED")

        failed = self.orchestrator.submit(self.payload(war_task_id="failed"))
        task_id = failed["command_tasks"][0]["task_id"]
        with self.run_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"run_id": "r2", "task_id": task_id, "worker": "Achilles", "status": "FAIL"}) + "\n")
        self.assertEqual(self.orchestrator.summary("project-1", "failed")["overall"], "FAILED")


if __name__ == "__main__":
    unittest.main()
