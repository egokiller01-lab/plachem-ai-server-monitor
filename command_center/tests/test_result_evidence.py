import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from result_evidence import normalize_result


class ResultEvidenceTests(unittest.TestCase):
    def test_normalizes_pass_result_and_preserves_evidence(self):
        gateway_result = {
            "task_id": "core2-pass-001",
            "agent": "achilles",
            "auth": {"requested": ["read_only_review"]},
            "status": "PASS",
            "attempts": [{"attempt": 1, "valid": True}],
            "worker_result_type": "read_only",
            "result": {
                "artifacts": [],
                "changes": [],
                "reason": "",
                "validation_result": {"status": "PASS", "findings": []},
            },
            "authorization_consumed": True,
            "execution_evidence": {"model": "qwen", "api_calls": 1, "tool_calls": 0},
            "timestamp": "2026-08-31T00:00:00+00:00",
        }
        original = copy.deepcopy(gateway_result)

        package = normalize_result(gateway_result)

        self.assertEqual(
            package,
            {
                "task_id": "core2-pass-001",
                "worker": "achilles",
                "requested_actions": ["read_only_review"],
                "gateway_result": "PASS",
                "worker_attempts": 1,
                "result_type": "read_only",
                "artifacts": [],
                "files_modified": [],
                "authorization_consumed": True,
                "execution_evidence": {"model": "qwen", "api_calls": 1, "tool_calls": 0},
                "validation_result": {"status": "PASS", "findings": []},
                "failure_reason": "",
                "completed_at": "2026-08-31T00:00:00+00:00",
            },
        )
        self.assertEqual(gateway_result, original)

    def test_normalizes_fail_result_and_preserves_failure_reason(self):
        gateway_result = {
            "task_id": "core2-fail-001",
            "agent": "achilles",
            "auth": {"requested": ["read_only_review"]},
            "status": "FAIL",
            "attempts": [{"attempt": 1, "valid": False}],
            "result": {"reason": "REPEATED_FAILURE", "changes": []},
            "timestamp": "2026-08-31T00:01:00+00:00",
        }

        package = normalize_result(gateway_result)

        self.assertEqual(package["gateway_result"], "FAIL")
        self.assertEqual(package["failure_reason"], "REPEATED_FAILURE")
        self.assertEqual(package["worker_attempts"], 1)
        self.assertEqual(package["files_modified"], [])
        self.assertIsNone(package["result_type"])
        self.assertIsNone(package["authorization_consumed"])

    def test_does_not_invent_missing_evidence_or_change_source(self):
        gateway_result = {"task_id": "minimal-001", "status": "BLOCKED"}
        original = copy.deepcopy(gateway_result)

        package = normalize_result(gateway_result)

        self.assertEqual(package["task_id"], "minimal-001")
        self.assertEqual(package["gateway_result"], "BLOCKED")
        for field in (
            "worker",
            "requested_actions",
            "worker_attempts",
            "result_type",
            "artifacts",
            "files_modified",
            "authorization_consumed",
            "execution_evidence",
            "validation_result",
            "failure_reason",
            "completed_at",
        ):
            self.assertIsNone(package[field])
        self.assertEqual(gateway_result, original)


if __name__ == "__main__":
    unittest.main()
