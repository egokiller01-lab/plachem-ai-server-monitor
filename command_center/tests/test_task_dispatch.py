import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_dispatch
import workspace_registry


PROJECT_ID = "plachem-agent-control"
BRANCH = "phase2-worker-identity"


class TaskDispatchTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_root = Path(self._temp_dir.name)
        self.project_root = self.temp_root / "project"
        self.project_root.mkdir()
        self.agents_path = self.write_agents(self.temp_root)
        self.workspace_registry_path = self.write_workspaces(
            self.temp_root,
            self.project_root,
        )
        registry_patcher = mock.patch.object(
            task_dispatch,
            "_WORKSPACE_REGISTRY_PATH",
            self.workspace_registry_path,
        )
        registry_patcher.start()
        self.addCleanup(registry_patcher.stop)
        branch_patcher = mock.patch.object(
            workspace_registry,
            "current_git_branch",
            return_value=BRANCH,
        )
        branch_patcher.start()
        self.addCleanup(branch_patcher.stop)

    def package(
        self,
        instruction="읽기 전용 검토를 수행해",
        worker="athena",
        actions=None,
        project_id=PROJECT_ID,
    ):
        return {
            "task_id": "dispatch-001",
            "project_id": project_id,
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

    def write_workspaces(self, root: Path, project_root: Path) -> Path:
        path = root / "workspaces.json"
        path.write_text(
            json.dumps(
                {
                    "workspaces": {
                        PROJECT_ID: {
                            "root": str(project_root),
                            "branch": BRANCH,
                            "status": "ACTIVE",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def dispatch(self, package, *, project_root=None):
        return task_dispatch.dispatch(
            package,
            "auth.json",
            self.agents_path,
            "policy.json",
            project_root if project_root is not None else self.project_root,
            Path("runs.jsonl"),
        )

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
                    self.project_root,
                    Path("runs.jsonl"),
                )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "UNKNOWN_AGENT:missing")
        broker.assert_not_called()
        gateway.assert_not_called()

    def test_valid_package_checks_workspace_auth_and_hands_off_to_gateway(self):
        package = self.package()
        auth = {"broker_called": True, "allow": ["read_only_review"], "worker": "athena"}
        gateway_result = {"status": "PASS", "task_id": "dispatch-001"}
        with (
            mock.patch.object(task_dispatch, "load_task_authorization", return_value=auth) as broker,
            mock.patch.object(task_dispatch, "fast_gateway") as gateway,
        ):
            gateway.merge_policy.return_value = {}
            gateway.run.return_value = gateway_result
            result = self.dispatch(package)
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
        self.assertEqual(gateway.run.call_args.args[3], self.project_root.resolve())
        gateway.load_json.assert_not_called()

    def test_identity_fields_are_forwarded_to_gateway_request(self):
        package = self.package()
        package.update(
            {
                "run_id": "run-dispatch-001",
                "correlation_id": "corr-dispatch-001",
                "external_reference": {
                    "source": "war_room",
                    "project_id": "test-project",
                    "external_task_id": "war-task-001",
                },
            }
        )
        auth = {"broker_called": True, "allow": ["read_only_review"], "worker": "athena"}
        with (
            mock.patch.object(task_dispatch, "load_task_authorization", return_value=auth),
            mock.patch.object(task_dispatch, "fast_gateway") as gateway,
        ):
            gateway.merge_policy.return_value = {}
            gateway.run.return_value = {"status": "PASS", "task_id": package["task_id"]}

            self.dispatch(package)

        request = gateway.run.call_args.args[0]
        self.assertEqual(request["run_id"], "run-dispatch-001")
        self.assertEqual(request["correlation_id"], "corr-dispatch-001")
        self.assertEqual(request["external_reference"], package["external_reference"])

    def test_missing_or_rejected_auth_blocks_before_gateway(self):
        package = self.package()
        with (
            mock.patch.object(task_dispatch, "load_task_authorization", side_effect=ValueError("authorization not found")),
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = self.dispatch(package)
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
            result = self.dispatch(package)
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
            result = self.dispatch(package)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("action not authorized", result["reason"])
        gateway.assert_not_called()

    def test_old_c_repo_blocks_before_broker_and_gateway(self):
        with (
            mock.patch.object(task_dispatch, "load_task_authorization") as broker,
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = self.dispatch(
                self.package(),
                project_root=Path(r"C:\Users\egomine2\PLACHEM-Agent-Control"),
            )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("WORKSPACE_PATH_MISMATCH", result["reason"])
        broker.assert_not_called()
        gateway.assert_not_called()

    def test_appdata_temp_worktree_blocks_before_broker_and_gateway(self):
        with (
            mock.patch.object(task_dispatch, "load_task_authorization") as broker,
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = self.dispatch(
                self.package(),
                project_root=Path(
                    r"C:\Users\egomine2\AppData\Local\Temp\PLACHEM-CORE4-Agent-Registry"
                ),
            )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("WORKSPACE_PATH_MISMATCH", result["reason"])
        broker.assert_not_called()
        gateway.assert_not_called()

    def test_wrong_branch_blocks_before_broker_and_gateway(self):
        with (
            mock.patch.object(workspace_registry, "current_git_branch", return_value="wrong-branch"),
            mock.patch.object(task_dispatch, "load_task_authorization") as broker,
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = self.dispatch(self.package())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("WORKSPACE_BRANCH_MISMATCH", result["reason"])
        broker.assert_not_called()
        gateway.assert_not_called()

    def test_unknown_workspace_blocks_before_broker_and_gateway(self):
        with (
            mock.patch.object(task_dispatch, "load_task_authorization") as broker,
            mock.patch.object(task_dispatch, "run_gateway") as gateway,
        ):
            result = self.dispatch(self.package(project_id="missing"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "UNKNOWN_WORKSPACE:missing")
        broker.assert_not_called()
        gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
