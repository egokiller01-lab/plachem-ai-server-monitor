import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_registry import AgentRegistry
from dispatch_selection import DispatchCandidateSelector
from dependency_readiness import DependencyReadinessEvaluator
from run_query import RunQuery
from runtime_profile import RuntimeProfileResolver
import task_dispatch
import workspace_registry


class DynamicWorkerRoutingTests(unittest.TestCase):
    def registry_path(self, root: Path) -> Path:
        path = root / "agents.json"
        path.write_text(json.dumps({
            "ERPcoder": {
                "enabled": True,
                "priority": 10,
                "capabilities": ["coding", "erp"],
                "runtime_profile": "erpcoder",
            },
            "Achilles": {
                "enabled": True,
                "priority": 20,
                "capabilities": ["coding", "erp", "review"],
                "runtime_profile": "achilles",
            },
            "Athena": {
                "enabled": True,
                "priority": 30,
                "capabilities": ["review", "code_review"],
                "runtime_profile": "athena",
            },
        }), encoding="utf-8")
        return path

    def test_preferred_available_worker_is_selected(self):
        with tempfile.TemporaryDirectory() as td:
            registry = AgentRegistry.load(self.registry_path(Path(td)))
            selected = registry.select_worker({
                "preferred_worker": "ERPcoder",
                "required_capabilities": ["coding", "erp"],
                "workspace_id": "workspace-1",
            }, RunQuery(Path(td) / "runs.jsonl"))
        self.assertEqual(selected.agent_id, "ERPcoder")
        self.assertEqual(selected.reason, "preferred_worker")

    def test_busy_preferred_worker_falls_back_to_capability_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs.jsonl"
            runs.write_text(json.dumps({"run_id": "run-1", "task_id": "task-1", "worker": "ERPcoder", "status": "RUNNING"}) + "\n", encoding="utf-8")
            registry = AgentRegistry.load(self.registry_path(root))
            selected = registry.select_worker({
                "preferred_worker": "ERPcoder",
                "required_capabilities": ["coding", "erp"],
                "workspace_id": "workspace-1",
            }, RunQuery(runs))
        self.assertEqual(selected.agent_id, "Achilles")
        self.assertEqual(selected.reason, "capability_fallback")

    def test_capability_mismatch_and_no_available_worker_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            registry = AgentRegistry.load(self.registry_path(Path(td)))
            with self.assertRaisesRegex(ValueError, "NO_AVAILABLE_WORKER"):
                registry.select_worker({
                    "required_capabilities": ["database"],
                    "workspace_id": "workspace-1",
                }, RunQuery(Path(td) / "runs.jsonl"))

    def test_disabled_worker_is_not_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self.registry_path(root)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["ERPcoder"]["enabled"] = False
            path.write_text(json.dumps(data), encoding="utf-8")
            registry = AgentRegistry.load(path)
            with self.assertRaisesRegex(ValueError, "NO_AVAILABLE_WORKER"):
                registry.select_worker({
                    "preferred_worker": "ERPcoder",
                    "required_capabilities": ["coding", "erp"],
                    "worker_selection_mode": "strict",
                    "workspace_id": "workspace-1",
                }, RunQuery(root / "runs.jsonl"))

    def test_runtime_profile_is_resolved_from_profile_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "profiles" / "achilles" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "model:\n  provider: openai-codex\n  base_url: https://chatgpt.com/backend-api/codex\n  default: gpt-5.6-luna\n",
                encoding="utf-8",
            )
            profile = RuntimeProfileResolver(root).resolve("achilles")
        self.assertEqual(profile.provider, "openai-codex")
        self.assertEqual(profile.model, "gpt-5.6-luna")
        self.assertEqual(profile.base_url, "https://chatgpt.com/backend-api/codex")

    def test_fallback_authorization_targets_selected_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            agents = self.registry_path(root)
            workspaces = root / "workspaces.json"
            workspaces.write_text(json.dumps({"workspaces": {
                "workspace-1": {"root": str(project), "branch": "phase2-worker-identity", "status": "ACTIVE"}
            }}), encoding="utf-8")
            package = {
                "task_id": "dispatch-dynamic-1",
                "project_id": "workspace-1",
                "original_instruction": "검토",
                "instruction_sha256": __import__("hashlib").sha256("검토".encode()).hexdigest(),
                "requested_worker": "ERPcoder",
                "preferred_worker": "ERPcoder",
                "required_capabilities": ["coding", "erp"],
                "worker_selection_mode": "preferred",
                "requested_actions": ["read_only_review"],
                "created_at": "2026-09-01T00:00:00+00:00",
                "status": "CREATED",
            }
            runs = root / "runs.jsonl"
            runs.write_text(json.dumps({"run_id": "r1", "task_id": "busy", "worker": "ERPcoder", "status": "RUNNING"}) + "\n", encoding="utf-8")
            auth = {"broker_called": True, "allow": ["read_only_review"], "worker": "Achilles"}
            with (
                mock.patch.object(task_dispatch, "_WORKSPACE_REGISTRY_PATH", workspaces),
                mock.patch.object(workspace_registry, "current_git_branch", return_value="phase2-worker-identity"),
                mock.patch.object(task_dispatch, "load_task_authorization", return_value=auth) as broker,
                mock.patch.object(task_dispatch, "run_gateway", return_value={"status": "PASS"}) as gateway,
            ):
                task_dispatch.dispatch(package, root / "auth.json", agents, root / "policy.json", project, root / "log.jsonl", runs)
            self.assertEqual(broker.call_args.args[2], "Achilles")
            self.assertEqual(gateway.call_args.args[0]["requested_worker"], "Achilles")


if __name__ == "__main__":
    unittest.main()
