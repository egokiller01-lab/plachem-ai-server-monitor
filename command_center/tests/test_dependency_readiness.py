import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dependency_readiness import DependencyReadinessEvaluator
from run_query import RunQuery


class DependencyReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = Path(self.temp_dir.name) / "runs.jsonl"

    def record(self, task_id, status, run_id=None, index=0):
        with self.registry.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "run_id": run_id or f"run-{task_id}-{index}",
                "task_id": task_id,
                "status": status,
                "worker": "worker",
            }) + "\n")

    def evaluate(self, task, *records):
        for task_id, status, run_id, index in records:
            self.record(task_id, status, run_id, index)
        known = {task.get("task_id"), "task-a", "task-c", "task-b"}
        return DependencyReadinessEvaluator(
            RunQuery(self.registry), known_task_ids=known
        ).evaluate(task)

    def task(self, task_id="task-b", dependencies=None, mode="all_success"):
        return {
            "task_id": task_id,
            "depends_on_task_ids": dependencies or [],
            "dependency_mode": mode,
        }

    def test_root_without_run_is_ready(self):
        result = self.evaluate(self.task(task_id="task-a"))

        self.assertEqual(result["readiness"], "READY")
        self.assertEqual(result["reason"], "no dependencies")

    def test_upstream_without_run_is_waiting(self):
        result = self.evaluate(self.task(dependencies=["task-a"]))

        self.assertEqual(result["readiness"], "WAITING")
        self.assertEqual(result["dependencies"][0]["outcome"], "NOT_STARTED")

    def test_upstream_active_is_waiting(self):
        result = self.evaluate(
            self.task(dependencies=["task-a"]),
            ("task-a", "RUNNING", "run-a", 0),
        )

        self.assertEqual(result["readiness"], "WAITING")
        self.assertEqual(result["dependencies"][0]["outcome"], "ACTIVE")

    def test_upstream_success_is_ready(self):
        result = self.evaluate(
            self.task(dependencies=["task-a"]),
            ("task-a", "PASS", "run-a", 0),
        )

        self.assertEqual(result["readiness"], "READY")
        self.assertEqual(result["dependencies"][0]["outcome"], "SUCCESS")

    def test_upstream_failure_is_blocked(self):
        result = self.evaluate(
            self.task(dependencies=["task-a"]),
            ("task-a", "FAIL", "run-a", 0),
        )

        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertEqual(result["dependencies"][0]["outcome"], "FAILURE")

    def test_multiple_dependencies_wait_then_ready_or_block(self):
        waiting = self.evaluate(
            self.task(dependencies=["task-a", "task-c"]),
            ("task-a", "PASS", "run-a", 0),
            ("task-c", "RUNNING", "run-c", 0),
        )
        self.assertEqual(waiting["readiness"], "WAITING")

        self.registry.unlink()
        ready = self.evaluate(
            self.task(dependencies=["task-a", "task-c"]),
            ("task-a", "PASS", "run-a", 0),
            ("task-c", "PASS", "run-c", 0),
        )
        self.assertEqual(ready["readiness"], "READY")

        self.registry.unlink()
        blocked = self.evaluate(
            self.task(dependencies=["task-a", "task-c"]),
            ("task-a", "PASS", "run-a", 0),
            ("task-c", "BLOCKED", "run-c", 0),
        )
        self.assertEqual(blocked["readiness"], "BLOCKED")

    def test_no_workflow_children_are_independently_ready(self):
        for task_id in ("task-a", "task-b"):
            result = self.evaluate(self.task(task_id=task_id))
            self.assertEqual(result["readiness"], "READY")
            self.registry.unlink(missing_ok=True)

    def test_latest_run_is_authoritative_attempt(self):
        success = self.evaluate(
            self.task(task_id="task-a"),
            ("task-a", "FAIL", "run-old", 0),
            ("task-a", "PASS", "run-new", 1),
        )
        self.assertEqual(success["task_outcome"], "SUCCESS")

        self.registry.unlink()
        failure = self.evaluate(
            self.task(task_id="task-a"),
            ("task-a", "PASS", "run-old", 0),
            ("task-a", "FAIL", "run-new", 1),
        )
        self.assertEqual(failure["task_outcome"], "FAILURE")

    def test_unknown_dependency_is_blocked_not_ready(self):
        result = self.evaluate(self.task(dependencies=["missing-task"]))

        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertEqual(result["dependencies"][0]["outcome"], "NOT_FOUND")

    def test_unsupported_dependency_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_DEPENDENCY_MODE"):
            self.evaluate(self.task(mode="any_success"))

    def test_terminal_task_is_not_marked_ready(self):
        result = self.evaluate(
            self.task(task_id="task-a"),
            ("task-a", "PASS", "run-a", 0),
        )

        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertEqual(result["reason"], "task_already_terminal")

    def test_explainability_contains_outcome_and_reason(self):
        result = self.evaluate(
            self.task(dependencies=["task-a"]),
            ("task-a", "RUNNING", "run-a", 0),
        )

        self.assertEqual(result["dependency_mode"], "all_success")
        self.assertEqual(result["dependencies"][0]["task_id"], "task-a")
        self.assertTrue(result["dependencies"][0]["reason"])
        self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
