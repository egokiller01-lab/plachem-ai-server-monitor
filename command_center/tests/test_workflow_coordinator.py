import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import result_evidence
import workflow_coordinator
from run_registry import RunRegistry


class WorkflowCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.run_registry = RunRegistry(Path(self._temp_dir.name) / "runs.jsonl")

    def package(self, task_id, worker="achilles"):
        return {
            "task_id": task_id,
            "project_id": "plachem-agent-control",
            "original_instruction": f"instruction-{task_id}",
            "instruction_sha256": f"sha-{task_id}",
            "requested_worker": worker,
            "requested_actions": ["read_only_review"],
            "created_at": "2026-09-01T00:00:00+00:00",
            "status": "CREATED",
        }

    def gateway_record(self, task_id, status="PASS", worker="achilles", reason=""):
        worker_status = {"PASS": "completed", "FAIL": "failed", "BLOCKED": "blocked"}[status]
        return {
            "timestamp": "2026-09-01T00:00:01+00:00",
            "task_id": task_id,
            "agent": worker,
            "auth": {"requested": ["read_only_review"]},
            "attempts": [{"attempt": 1, "valid": True, "failures": []}],
            "result": {
                "status": worker_status,
                "reason": reason,
                "changes": [],
                "review_result": "PASS",
                "findings": [],
            },
            "result_type": "read_only",
            "status": status,
        }

    def run_sequence(self, packages, dispatcher, dispatch_kwargs=None):
        return workflow_coordinator.run_sequence(
            packages,
            dispatcher=dispatcher,
            dispatch_kwargs=dispatch_kwargs or {},
            run_registry=self.run_registry,
        )

    def test_two_tasks_run_sequentially_and_preserve_result_evidence(self):
        packages = [self.package("task-a"), self.package("task-b")]
        raw_results = [self.gateway_record("task-a"), self.gateway_record("task-b")]
        dispatcher = mock.Mock(side_effect=raw_results)
        dispatch_kwargs = {"auth_path": "auth.json", "agents_path": "agents.json"}

        sequence = self.run_sequence(packages, dispatcher, dispatch_kwargs)

        self.assertEqual(sequence["status"], "PASS")
        self.assertEqual(
            sequence["results"],
            [result_evidence.normalize_result(result) for result in raw_results],
        )
        self.assertEqual([result["task_id"] for result in sequence["results"]], ["task-a", "task-b"])
        self.assertEqual(
            dispatcher.call_args_list,
            [
                mock.call(packages[0], **dispatch_kwargs),
                mock.call(packages[1], **dispatch_kwargs),
            ],
        )
        self.assertEqual(self.run_registry.get("task-a")["status"], "PASS")
        self.assertEqual(self.run_registry.get("task-b")["status"], "PASS")
        self.assertEqual(self.run_registry.get("task-a")["gateway_result"], raw_results[0])

    def test_a_pass_is_recorded_before_b_enters_running(self):
        packages = [self.package("task-a"), self.package("task-b")]
        observed = []

        def dispatcher(package):
            current = self.run_registry.get(package["task_id"])
            observed.append((package["task_id"], current["status"]))
            if package["task_id"] == "task-b":
                observed.append(("task-a-before-b", self.run_registry.get("task-a")["status"]))
            return self.gateway_record(package["task_id"])

        sequence = self.run_sequence(packages, dispatcher)

        self.assertEqual(sequence["status"], "PASS")
        self.assertEqual(
            observed,
            [("task-a", "RUNNING"), ("task-b", "RUNNING"), ("task-a-before-b", "PASS")],
        )

    def test_tasks_with_different_workers_are_forwarded_and_preserved_unchanged(self):
        packages = [
            self.package("task-a", worker="worker-a"),
            self.package("task-b", worker="worker-b"),
        ]
        raw_results = [
            self.gateway_record("task-a", worker="worker-a"),
            self.gateway_record("task-b", worker="worker-b"),
        ]
        dispatcher = mock.Mock(side_effect=raw_results)

        sequence = self.run_sequence(packages, dispatcher)

        self.assertEqual(sequence["status"], "PASS")
        self.assertEqual([result["worker"] for result in sequence["results"]], ["worker-a", "worker-b"])
        self.assertEqual([package["requested_worker"] for package in packages], ["worker-a", "worker-b"])
        self.assertEqual(
            dispatcher.call_args_list,
            [mock.call(packages[0]), mock.call(packages[1])],
        )
        self.assertEqual(self.run_registry.get("task-a")["worker"], "worker-a")
        self.assertEqual(self.run_registry.get("task-b")["worker"], "worker-b")

    def test_first_task_fail_stops_before_second_dispatch_and_run_creation(self):
        packages = [self.package("task-a"), self.package("task-b")]
        first_result = self.gateway_record("task-a", status="FAIL", reason="validation failed")
        dispatcher = mock.Mock(side_effect=[first_result, self.gateway_record("task-b")])

        sequence = self.run_sequence(packages, dispatcher)

        self.assertEqual(sequence["status"], "FAIL")
        self.assertEqual(sequence["results"], [result_evidence.normalize_result(first_result)])
        dispatcher.assert_called_once_with(packages[0])
        self.assertEqual(self.run_registry.get("task-a")["status"], "FAIL")
        self.assertEqual(self.run_registry.get("task-a")["failure_reason"], "validation failed")
        self.assertIsNotNone(self.run_registry.get("task-a")["completed_at"])
        self.assertIsNone(self.run_registry.get("task-b"))

    def test_first_task_blocked_stops_before_second_dispatch_and_run_creation(self):
        packages = [self.package("task-a"), self.package("task-b")]
        first_result = self.gateway_record("task-a", status="BLOCKED", reason="workspace blocked")
        dispatcher = mock.Mock(side_effect=[first_result, self.gateway_record("task-b")])

        sequence = self.run_sequence(packages, dispatcher)

        self.assertEqual(sequence["status"], "BLOCKED")
        self.assertEqual(sequence["results"], [result_evidence.normalize_result(first_result)])
        dispatcher.assert_called_once_with(packages[0])
        self.assertEqual(self.run_registry.get("task-a")["status"], "BLOCKED")
        self.assertEqual(self.run_registry.get("task-a")["failure_reason"], "workspace blocked")
        self.assertIsNotNone(self.run_registry.get("task-a")["completed_at"])
        self.assertIsNone(self.run_registry.get("task-b"))


if __name__ == "__main__":
    unittest.main()
