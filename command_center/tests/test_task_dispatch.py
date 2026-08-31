import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_dispatch


class TaskDispatchTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.agents_path = self.write_agents(Path(self._temp_dir.name))

    def package(self, instruction="읽기 전용 검토를 수행해", worker="athena", actions=None):
        return {
            "task_id": "dispatch-001",
            "original_instruction": instruction,
            "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            "requested_worker": worker,
            "requested_actions": actions if actions is not None else ["read_only_review"],
            "created_at": "2026-08-31T00:00:00+00:00",
            "status": "CREATED",
        }

    def write_agents(self, root: Path, agents=None) -> Path:
        path = root / "agents.json"
        path.write_text(
            json.dumps(
                agents
                if agents is not None
                else {
                    "athena": {
                        "provider": "hermes-profile",
                        "profile": "athena",
                        "model": "gpt-5.6-luna",
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_unknown_agent_blocks_before_broker_and_gateway(self):
        package = self.package(worker="missing")
        with tempfile.TemporaryDirectory() as td:
            agents_path = self.write_agents(Path(td))
            with (
                mock.patch.object(task_dispatch, "load_task_authorization") as broker,
                mock.patch.object(task_dispatch, "run_gateway") as gateway,
            ):
                result = task_dispatch.dispatch(
                    package,
                    "auth.json",
                    agents_path,
                    "policy.json",
                    Path("."),
                    Path("runs.jsonl"),
                )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "UNKNOWN_AGENT:missing")
        broker.assert_not_called()
        gateway.assert_not_called()

    def test_valid_package_checks_auth_and_hands_off_to_gateway(self):
        package = self.package()
        auth = {"broker_called": True, "allow": ["read_only_review"], "worker": "athena"}
        gateway_result = {"status": "PASS", "task_id": "dispatch-001"}
        with (
            mock.patch.object(task_dispatch, "load_task_authorization", return_value=auth) as broker,
            mock.patch.object(task_dispatch, "fast_gateway") as gateway,
        ):
            gateway.merge_policy.return_value = {}
            gateway.run.return_value = gateway_result
            result = task_dispatch.dispatch(package, "auth.json", self.agents_path, "policy.json", Path("."), Path("runs.jsonl"))
        self.assertEqual(result, gateway_result)
        broker.assert_called_once_with(Path("auth.json"), "dispatch-001", "athena", ["read_only_review"], consume=False)
        gateway.run.assert_called_once()
        request = gateway.run.call_args.args[0]
        self.assertEqual(request["task"], package["original_instruction"])
        self.assertEqual(request["requested_actions"], ["read_only_review"])
        self.assertEqual(
            gateway.run.call_args.args[1],
            json.loads(self.agents_path.read_text(encoding="utf-8")),
        )
        gateway.load_json.assert_not_called()

    def test_missing_or_rejected_auth_blocks_before_gateway(self):
        package = self.package()
        with (
            mock.patch.object(task_dispatch, "load_task_authorization", side_effect=ValueError("authorization not found")),
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = task_dispatch.dispatch(package, "auth.json", self.agents_path, "policy.json", Path("."), Path("runs.jsonl"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("authorization not found", result["reason"])
        gateway.assert_not_called()

    def test_tampered_instruction_blocks_before_auth(self):
        package = self.package()
        package["original_instruction"] = "변조된 원문"
        with (
            mock.patch.object(task_dispatch, "load_task_authorization") as broker,
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = task_dispatch.dispatch(package, "auth.json", self.agents_path, "policy.json", Path("."), Path("runs.jsonl"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "INSTRUCTION_SHA256_MISMATCH")
        broker.assert_not_called()
        gateway.assert_not_called()

    def test_worker_or_action_mismatch_is_returned_as_blocked(self):
        package = self.package(worker="athena", actions=["workspace_modify"])
        with (
            mock.patch.object(task_dispatch, "load_task_authorization", side_effect=ValueError("action not authorized")),
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = task_dispatch.dispatch(package, "auth.json", self.agents_path, "policy.json", Path("."), Path("runs.jsonl"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("action not authorized", result["reason"])
        gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
