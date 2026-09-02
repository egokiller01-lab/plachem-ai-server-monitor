import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import result_evidence
import workflow_coordinator
from run_query import RunQuery
from run_registry import RunRegistry
from task_intake import create_task_package


class RunIdentityFoundationTests(unittest.TestCase):
    def test_same_task_can_create_multiple_unique_run_ids(self):
        with tempfile.TemporaryDirectory() as td:
            registry = RunRegistry(Path(td) / "runs.jsonl")

            first = registry.create("task-shared", "plachem-agent-control", "worker-a")
            second = registry.create("task-shared", "plachem-agent-control", "worker-b")

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertNotEqual(first["run_id"], first["task_id"])
            self.assertNotEqual(second["run_id"], second["task_id"])
            self.assertTrue(first["created_at"])
            self.assertTrue(first["updated_at"])
            self.assertEqual(registry.get_run(first["run_id"])["worker"], "worker-a")
            self.assertEqual(registry.get_run(second["run_id"])["worker"], "worker-b")

    def test_existing_task_intake_call_remains_valid_and_generates_correlation(self):
        package = create_task_package("기존 형식 작업", requested_worker="achilles")
        second = create_task_package("두 번째 기존 형식 작업")

        self.assertTrue(package["task_id"].startswith("task-"))
        self.assertTrue(package["correlation_id"].startswith("corr-"))
        self.assertNotEqual(package["correlation_id"], second["correlation_id"])
        self.assertNotIn("external_reference", package)

    def test_task_intake_preserves_optional_external_reference(self):
        package = create_task_package(
            "War Room 요청",
            requested_worker="achilles",
            correlation_id="corr-war-001",
            source="war_room",
            project_id="test-project",
            external_task_id="war-task-001",
        )

        self.assertEqual(package["correlation_id"], "corr-war-001")
        self.assertEqual(
            package["external_reference"],
            {
                "source": "war_room",
                "project_id": "test-project",
                "external_task_id": "war-task-001",
            },
        )

    def test_correlation_propagates_task_run_result_and_query(self):
        package = create_task_package(
            "상관관계 전파",
            requested_worker="achilles",
            requested_actions=["read_only_review"],
            correlation_id="corr-shared-001",
            source="war_room",
            project_id="test-project",
            external_task_id="war-task-001",
        )
        package["project_id"] = "plachem-agent-control"
        raw_gateway_result = {
            "task_id": package["task_id"],
            "agent": "achilles",
            "status": "PASS",
            "result": {"reason": "", "changes": []},
            "attempts": [{"attempt": 1, "valid": True}],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            registry = RunRegistry(path)
            dispatcher = mock.Mock(return_value=raw_gateway_result)

            sequence = workflow_coordinator.run_sequence(
                [package],
                dispatch_kwargs={},
                run_registry=registry,
                dispatcher=dispatcher,
            )
            dispatched = dispatcher.call_args.args[0]
            run_id = dispatched["run_id"]
            run = RunQuery(path).get_run(run_id)
            correlated = RunQuery(path).by_correlation_id("corr-shared-001")

        self.assertNotEqual(run_id, package["task_id"])
        self.assertEqual(dispatched["correlation_id"], "corr-shared-001")
        self.assertEqual(run["task_id"], package["task_id"])
        self.assertEqual(run["correlation_id"], "corr-shared-001")
        self.assertEqual(run["external_reference"], package["external_reference"])
        self.assertEqual([record["run_id"] for record in correlated], [run_id])
        self.assertEqual(sequence["results"][0]["run_id"], run_id)
        self.assertEqual(sequence["results"][0]["correlation_id"], "corr-shared-001")
        self.assertEqual(
            sequence["results"][0]["external_reference"],
            package["external_reference"],
        )

    def test_run_query_supports_run_task_and_correlation_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            registry = RunRegistry(path)
            first = registry.create(
                "task-shared",
                "plachem-agent-control",
                "worker-a",
                correlation_id="corr-shared",
            )
            second = registry.create(
                "task-shared",
                "plachem-agent-control",
                "worker-b",
                correlation_id="corr-shared",
            )
            query = RunQuery(path)

            by_run = query.get_run(first["run_id"])
            by_task = query.by_task_id("task-shared")
            by_correlation = query.by_correlation_id("corr-shared")

        self.assertEqual(by_run["run_id"], first["run_id"])
        self.assertEqual(
            [record["run_id"] for record in by_task],
            [second["run_id"], first["run_id"]],
        )
        self.assertEqual(
            [record["run_id"] for record in by_correlation],
            [second["run_id"], first["run_id"]],
        )

    def test_result_evidence_preserves_identity_when_present(self):
        gateway_result = {
            "task_id": "task-identity",
            "run_id": "run-identity",
            "correlation_id": "corr-identity",
            "external_reference": {
                "source": "war_room",
                "project_id": "test-project",
                "external_task_id": "war-task-identity",
            },
            "status": "PASS",
        }

        normalized = result_evidence.normalize_result(gateway_result)

        self.assertEqual(normalized["task_id"], "task-identity")
        self.assertEqual(normalized["run_id"], "run-identity")
        self.assertEqual(normalized["correlation_id"], "corr-identity")
        self.assertEqual(normalized["external_reference"], gateway_result["external_reference"])


if __name__ == "__main__":
    unittest.main()
