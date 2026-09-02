import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dependency_readiness import DependencyReadinessEvaluator
from dispatch_boundary import DispatchBoundaryError, ExplicitDispatchBoundary
from dispatch_selection import DispatchCandidateSelector
from run_query import RunQuery


class ExplicitDispatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = Path(self.temp_dir.name) / "runs.jsonl"
        evaluator = DependencyReadinessEvaluator(
            RunQuery(self.registry), known_task_ids={"task-a", "task-b", "task-c"}
        )
        self.selector = DispatchCandidateSelector(evaluator)
        self.dispatcher = mock.Mock(return_value={"status": "PASS"})
        self.boundary = ExplicitDispatchBoundary(self.selector, self.dispatcher)

    def task(self, task_id, dependencies=None, worker="worker", index=None):
        task = {
            "task_id": task_id,
            "requested_worker": worker,
            "workspace_id": "workspace-1",
            "correlation_id": "corr-1",
            "external_reference": {"source": "war_room"},
            "workflow_role": "implementation",
            "depends_on_task_ids": dependencies or [],
            "dependency_mode": "all_success",
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

    def test_ready_task_calls_existing_dispatcher_once(self):
        task = self.task("task-a")

        result = self.boundary.dispatch_selected("task-a", [task])

        self.assertEqual(result, {"status": "PASS"})
        self.dispatcher.assert_called_once_with(task)

    def test_waiting_task_is_rejected_without_dispatch(self):
        task = self.task("task-b", ["task-a"])

        with self.assertRaisesRegex(DispatchBoundaryError, "TASK_NOT_READY"):
            self.boundary.dispatch_selected("task-b", [task])

        self.dispatcher.assert_not_called()

    def test_blocked_task_is_rejected_without_dispatch(self):
        self.record("task-a", "FAIL")
        task = self.task("task-a")

        with self.assertRaisesRegex(DispatchBoundaryError, "TASK_ALREADY_TERMINAL"):
            self.boundary.dispatch_selected("task-a", [task])

        self.dispatcher.assert_not_called()

    def test_active_run_is_rejected_at_dispatch_time(self):
        task = self.task("task-a")
        original_select = self.selector.select
        calls = 0

        def select_with_race(tasks):
            nonlocal calls
            calls += 1
            result = original_select(tasks)
            if calls == 1:
                self.record("task-a", "RUNNING")
            return result

        with mock.patch.object(self.selector, "select", side_effect=select_with_race):
            with self.assertRaisesRegex(DispatchBoundaryError, "TASK_ALREADY_ACTIVE"):
                self.boundary.dispatch_selected("task-a", [task])

        self.dispatcher.assert_not_called()

    def test_terminal_task_is_rejected(self):
        self.record("task-a", "PASS")

        with self.assertRaisesRegex(DispatchBoundaryError, "TASK_ALREADY_TERMINAL"):
            self.boundary.dispatch_selected("task-a", [self.task("task-a")])

        self.dispatcher.assert_not_called()

    def test_unknown_task_is_rejected(self):
        with self.assertRaisesRegex(DispatchBoundaryError, "TASK_NOT_FOUND"):
            self.boundary.dispatch_selected("missing", [self.task("task-a")])

        self.dispatcher.assert_not_called()

    def test_one_request_dispatches_only_selected_task(self):
        tasks = [self.task("task-a"), self.task("task-b"), self.task("task-c")]

        self.boundary.dispatch_selected("task-b", tasks)

        self.dispatcher.assert_called_once_with(tasks[1])

    def test_identity_is_passed_unchanged_to_dispatcher(self):
        task = self.task("task-a", worker="ERPcoder")
        task.update({
            "run_id": "run-a",
            "external_reference": {
                "source": "war_room",
                "project_id": "project-1",
                "external_task_id": "war-task-1",
            },
        })

        self.boundary.dispatch_selected("task-a", [task])

        request = self.dispatcher.call_args.args[0]
        for field in (
            "task_id", "run_id", "correlation_id", "requested_worker",
            "workspace_id", "external_reference",
        ):
            self.assertEqual(request.get(field), task.get(field))

    def test_human_approval_does_not_create_authorization(self):
        task = self.task("task-a")
        task["war_room_metadata"] = {"approval": {"status": "approved"}}
        authorization_factory = mock.Mock()

        self.boundary.dispatch_selected(
            "task-a", [task], authorization_factory=authorization_factory
        )

        authorization_factory.assert_not_called()
        self.dispatcher.assert_called_once()

    def test_dispatcher_failure_is_returned_without_retry(self):
        self.dispatcher.return_value = {"status": "FAIL"}

        result = self.boundary.dispatch_selected("task-a", [self.task("task-a")])

        self.assertEqual(result["status"], "FAIL")
        self.dispatcher.assert_called_once()

    def test_review_waits_until_implementation_succeeds(self):
        implementation = self.task("task-a", worker="ERPcoder", index=0)
        review = self.task("task-b", ["task-a"], worker="ERPqa", index=1)

        with self.assertRaisesRegex(DispatchBoundaryError, "TASK_NOT_READY"):
            self.boundary.dispatch_selected("task-b", [implementation, review])
        self.dispatcher.assert_not_called()

        self.record("task-a", "PASS")
        self.boundary.dispatch_selected("task-b", [implementation, review])
        self.dispatcher.assert_called_once_with(review)

    def test_dispatch_is_read_only_before_dispatcher(self):
        tasks = [self.task("task-a")]
        before_tasks = copy.deepcopy(tasks)
        before_registry = self.registry.read_bytes() if self.registry.exists() else b""

        self.boundary.dispatch_selected("task-a", tasks)

        self.assertEqual(tasks, before_tasks)
        self.assertEqual(self.registry.read_bytes() if self.registry.exists() else b"", before_registry)


if __name__ == "__main__":
    unittest.main()
