import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dependency_readiness import DependencyReadinessEvaluator
from dispatch_selection import DispatchCandidateSelector
from run_query import RunQuery


class DispatchCandidateSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = Path(self.temp_dir.name) / "runs.jsonl"
        self.known_tasks = {"task-a", "task-b", "task-c"}
        evaluator = DependencyReadinessEvaluator(
            RunQuery(self.registry), known_task_ids=self.known_tasks
        )
        self.selector = DispatchCandidateSelector(evaluator)

    def task(self, task_id, dependencies=None, worker="worker", index=None, mode="all_success"):
        task = {
            "task_id": task_id,
            "requested_worker": worker,
            "workspace_id": "workspace-1",
            "correlation_id": "corr-1",
            "external_reference": {"source": "war_room"},
            "workflow_role": "implementation",
            "depends_on_task_ids": dependencies or [],
            "dependency_mode": mode,
        }
        if index is not None:
            task["compile_index"] = index
        return task

    def record(self, task_id, status, run_id=None):
        with self.registry.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "run_id": run_id or f"run-{task_id}",
                "task_id": task_id,
                "status": status,
                "worker": "worker",
            }) + "\n")

    def test_single_ready_candidate_excludes_waiting(self):
        tasks = [self.task("task-a"), self.task("task-b", ["task-a"])]

        result = self.selector.select(tasks)

        self.assertEqual([item["task_id"] for item in result["candidates"]], ["task-a"])
        self.assertEqual(result["excluded"][0]["task_id"], "task-b")
        self.assertEqual(result["excluded"][0]["readiness"], "WAITING")

    def test_blocked_task_is_excluded(self):
        self.record("task-a", "FAIL")

        result = self.selector.select([self.task("task-a")])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["readiness"], "BLOCKED")

    def test_two_independent_ready_tasks_are_candidates(self):
        result = self.selector.select([self.task("task-a"), self.task("task-b")])

        self.assertEqual([item["task_id"] for item in result["candidates"]], ["task-a", "task-b"])

    def test_order_is_deterministic_by_compile_index(self):
        tasks = [self.task("task-b", index=1), self.task("task-a", index=0)]

        first = self.selector.select(tasks)
        second = self.selector.select(tasks)

        self.assertEqual(
            [item["task_id"] for item in first["candidates"]],
            ["task-a", "task-b"],
        )
        self.assertEqual(first, second)

    def test_active_task_is_not_candidate(self):
        self.record("task-a", "RUNNING")

        result = self.selector.select([self.task("task-a")])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["reason"], "task_already_active")

    def test_terminal_task_is_not_candidate(self):
        self.record("task-a", "PASS")

        result = self.selector.select([self.task("task-a")])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["reason"], "task_already_terminal")

    def test_unknown_dependency_is_fail_closed(self):
        result = self.selector.select([self.task("task-a", ["missing"])])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["readiness"], "BLOCKED")

    def test_duplicate_task_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "DUPLICATE_TASK_ID:task-a"):
            self.selector.select([self.task("task-a"), self.task("task-a")])

    def test_sequential_review_becomes_candidate_after_success(self):
        implementation = self.task("task-a", worker="ERPcoder", index=0)
        review = self.task("task-b", ["task-a"], worker="ERPqa", index=1)

        initial = self.selector.select([implementation, review])
        self.assertEqual([item["task_id"] for item in initial["candidates"]], ["task-a"])

        self.record("task-a", "PASS")
        after_success = self.selector.select([implementation, review])
        self.assertEqual([item["task_id"] for item in after_success["candidates"]], ["task-b"])

    def test_selection_is_read_only_and_explainable(self):
        tasks = [self.task("task-a")]
        before_task = json.dumps(tasks, sort_keys=True)
        before_registry = self.registry.read_bytes() if self.registry.exists() else b""

        result = self.selector.select(tasks)

        self.assertEqual(json.dumps(tasks, sort_keys=True), before_task)
        self.assertEqual(self.registry.read_bytes() if self.registry.exists() else b"", before_registry)
        self.assertEqual(result["candidates"][0]["reason"], "dependencies satisfied")
        self.assertIn("reason", result["excluded"] if result["excluded"] else result["candidates"][0])

    def test_unsupported_mode_is_excluded(self):
        result = self.selector.select([self.task("task-a", mode="any_success")])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["readiness"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
