import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_registry import RunRegistry


class Clock:
    def __init__(self, *values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


class RunRegistryTests(unittest.TestCase):
    def registry(self, root: Path, *times: datetime) -> RunRegistry:
        return RunRegistry(root / "runs.jsonl", clock=Clock(*times))

    def test_running_to_pass_records_started_completed_and_gateway_result(self):
        started = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
        completed = datetime(2026, 9, 1, 1, 1, tzinfo=timezone.utc)
        gateway_result = {"task_id": "task-a", "status": "PASS", "result": {"reason": ""}}
        with tempfile.TemporaryDirectory() as td:
            registry = self.registry(Path(td), started, completed)
            registry.create("task-a", "plachem-agent-control", "achilles")
            registry.transition("task-a", "DISPATCHING")
            registry.transition("task-a", "RUNNING")
            registry.transition("task-a", "PASS", gateway_result=gateway_result)

            record = registry.get("task-a")

        self.assertEqual(record["status"], "PASS")
        self.assertEqual(record["started_at"], started.isoformat())
        self.assertEqual(record["completed_at"], completed.isoformat())
        self.assertEqual(record["gateway_result"], gateway_result)
        self.assertEqual(record["failure_reason"], "")

    def test_running_to_fail_preserves_failure_reason(self):
        started = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
        completed = datetime(2026, 9, 1, 2, 1, tzinfo=timezone.utc)
        gateway_result = {
            "task_id": "task-fail",
            "status": "FAIL",
            "result": {"reason": "worker validation failed"},
        }
        with tempfile.TemporaryDirectory() as td:
            registry = self.registry(Path(td), started, completed)
            registry.create("task-fail", "plachem-agent-control", "achilles")
            registry.transition("task-fail", "DISPATCHING")
            registry.transition("task-fail", "RUNNING")
            registry.transition(
                "task-fail",
                "FAIL",
                gateway_result=gateway_result,
                failure_reason="worker validation failed",
            )

            record = registry.get("task-fail")

        self.assertEqual(record["status"], "FAIL")
        self.assertEqual(record["completed_at"], completed.isoformat())
        self.assertEqual(record["failure_reason"], "worker validation failed")
        self.assertEqual(record["gateway_result"], gateway_result)

    def test_running_to_blocked_preserves_failure_reason(self):
        started = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
        completed = datetime(2026, 9, 1, 3, 1, tzinfo=timezone.utc)
        gateway_result = {
            "task_id": "task-blocked",
            "status": "BLOCKED",
            "reason": "WORKSPACE_PATH_MISMATCH",
        }
        with tempfile.TemporaryDirectory() as td:
            registry = self.registry(Path(td), started, completed)
            registry.create("task-blocked", "plachem-agent-control", "achilles")
            registry.transition("task-blocked", "DISPATCHING")
            registry.transition("task-blocked", "RUNNING")
            registry.transition(
                "task-blocked",
                "BLOCKED",
                gateway_result=gateway_result,
                failure_reason="WORKSPACE_PATH_MISMATCH",
            )

            record = registry.get("task-blocked")

        self.assertEqual(record["status"], "BLOCKED")
        self.assertEqual(record["completed_at"], completed.isoformat())
        self.assertEqual(record["failure_reason"], "WORKSPACE_PATH_MISMATCH")

    def test_same_task_id_returns_latest_state(self):
        started = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            registry = self.registry(Path(td), started)
            registry.create("task-latest", "plachem-agent-control", "athena")
            registry.transition("task-latest", "DISPATCHING")
            registry.transition("task-latest", "RUNNING")

            record = registry.get("task-latest")

        self.assertEqual(record["task_id"], "task-latest")
        self.assertEqual(record["project_id"], "plachem-agent-control")
        self.assertEqual(record["worker"], "athena")
        self.assertEqual(record["status"], "RUNNING")
        self.assertIsNone(record["completed_at"])
        self.assertIsNone(record["gateway_result"])


if __name__ == "__main__":
    unittest.main()
