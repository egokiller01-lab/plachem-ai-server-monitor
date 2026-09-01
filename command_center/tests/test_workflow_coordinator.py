import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import result_evidence
import workflow_coordinator


class WorkflowCoordinatorTests(unittest.TestCase):
    def package(self, task_id, worker="achilles"):
        return {
            "task_id": task_id,
            "original_instruction": f"instruction-{task_id}",
            "instruction_sha256": f"sha-{task_id}",
            "requested_worker": worker,
            "requested_actions": ["read_only_review"],
            "created_at": "2026-09-01T00:00:00+00:00",
            "status": "CREATED",
        }

    def gateway_record(self, task_id, status="PASS", worker="achilles"):
        worker_status = {"PASS": "completed", "FAIL": "failed", "BLOCKED": "blocked"}[status]
        return {
            "timestamp": "2026-09-01T00:00:01+00:00",
            "task_id": task_id,
            "agent": worker,
            "auth": {"requested": ["read_only_review"]},
            "attempts": [{"attempt": 1, "valid": True, "failures": []}],
            "result": {
                "status": worker_status,
                "reason": "",
                "changes": [],
                "review_result": "PASS",
                "findings": [],
            },
            "result_type": "read_only",
            "status": status,
        }

    def test_two_tasks_run_sequentially_and_preserve_result_evidence(self):
        packages = [self.package("task-a"), self.package("task-b")]
        raw_results = [self.gateway_record("task-a"), self.gateway_record("task-b")]
        dispatcher = mock.Mock(side_effect=raw_results)
        dispatch_kwargs = {"auth_path": "auth.json", "agents_path": "agents.json"}

        sequence = workflow_coordinator.run_sequence(
            packages,
            dispatcher=dispatcher,
            dispatch_kwargs=dispatch_kwargs,
        )

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

        sequence = workflow_coordinator.run_sequence(
            packages,
            dispatcher=dispatcher,
            dispatch_kwargs={},
        )

        self.assertEqual(sequence["status"], "PASS")
        self.assertEqual([result["worker"] for result in sequence["results"]], ["worker-a", "worker-b"])
        self.assertEqual([package["requested_worker"] for package in packages], ["worker-a", "worker-b"])
        self.assertEqual(
            dispatcher.call_args_list,
            [mock.call(packages[0]), mock.call(packages[1])],
        )

    def test_first_task_fail_stops_before_second_dispatch(self):
        packages = [self.package("task-a"), self.package("task-b")]
        first_result = self.gateway_record("task-a", status="FAIL")
        dispatcher = mock.Mock(side_effect=[first_result, self.gateway_record("task-b")])

        sequence = workflow_coordinator.run_sequence(
            packages,
            dispatcher=dispatcher,
            dispatch_kwargs={},
        )

        self.assertEqual(sequence["status"], "FAIL")
        self.assertEqual(sequence["results"], [result_evidence.normalize_result(first_result)])
        dispatcher.assert_called_once_with(packages[0])

    def test_first_task_blocked_stops_before_second_dispatch(self):
        packages = [self.package("task-a"), self.package("task-b")]
        first_result = self.gateway_record("task-a", status="BLOCKED")
        dispatcher = mock.Mock(side_effect=[first_result, self.gateway_record("task-b")])

        sequence = workflow_coordinator.run_sequence(
            packages,
            dispatcher=dispatcher,
            dispatch_kwargs={},
        )

        self.assertEqual(sequence["status"], "BLOCKED")
        self.assertEqual(sequence["results"], [result_evidence.normalize_result(first_result)])
        dispatcher.assert_called_once_with(packages[0])


if __name__ == "__main__":
    unittest.main()
