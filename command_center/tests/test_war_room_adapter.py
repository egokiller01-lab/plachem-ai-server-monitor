import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_registry import AgentRegistry
from war_room_adapter import (
    DuplicateExternalReference,
    WarRoomTaskAdapter,
)
from workspace_registry import WorkspaceEntry, WorkspaceRegistry


class WarRoomTaskAdapterTests(unittest.TestCase):
    def setUp(self):
        self.agents = AgentRegistry(
            {
                "ERPcoder": {"provider": "test-worker"},
                "ERPqa": {"provider": "test-worker"},
            }
        )
        self.workspaces = WorkspaceRegistry(
            {
                "plachem-agent-control": WorkspaceEntry(
                    project_id="plachem-agent-control",
                    canonical_root=Path("E:/PLACHEM-Agent-Control/repo"),
                    branch="phase2-worker-identity",
                    status="ACTIVE",
                )
            }
        )
        self.adapter = WarRoomTaskAdapter(
            self.agents,
            self.workspaces,
            workspace_map={"project-1": "plachem-agent-control"},
        )

    def payload(self, **overrides):
        value = {
            "war_project_id": "project-1",
            "war_task_id": "war-task-1",
            "scope": "Inspect the task",
            "assignee_agent_id": "ERPcoder",
            "workspace_id": "plachem-agent-control",
            "approval": {"status": "approved", "approved_by": "human"},
            "metadata": {"ticket": "WR-1"},
        }
        value.update(overrides)
        return value

    def test_basic_translation_preserves_external_reference(self):
        package = self.adapter.to_task_package(self.payload())

        self.assertEqual(
            package["external_reference"],
            {
                "source": "war_room",
                "project_id": "project-1",
                "external_task_id": "war-task-1",
            },
        )
        self.assertEqual(package["requested_worker"], "ERPcoder")
        self.assertEqual(package["workspace_id"], "plachem-agent-control")
        self.assertEqual(package["status"], "CREATED")

    def test_command_task_id_is_independent(self):
        package = self.adapter.to_task_package(self.payload())

        self.assertNotEqual(package["task_id"], "war-task-1")
        self.assertTrue(package["task_id"].startswith("task-"))

    def test_correlation_is_preserved_or_generated(self):
        supplied = self.adapter.to_task_package(self.payload(correlation_id="war-corr-1"))
        generated = self.adapter.to_task_package(self.payload(war_task_id="war-task-2"))

        self.assertEqual(supplied["correlation_id"], "war-corr-1")
        self.assertTrue(generated["correlation_id"].startswith("corr-"))

    def test_duplicate_external_reference_is_detectable_and_queryable(self):
        first = self.adapter.to_task_package(self.payload())

        with self.assertRaises(DuplicateExternalReference) as raised:
            self.adapter.to_task_package(self.payload())

        self.assertEqual(raised.exception.task_id, first["task_id"])
        self.assertEqual(
            self.adapter.lookup(self.payload()),
            first["task_id"],
        )

    def test_unknown_requested_agent_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT:missing"):
            self.adapter.to_task_package(
                self.payload(assignee_agent_id="missing")
            )

    def test_unknown_requested_agent_list_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT:missing"):
            self.adapter.to_task_package(
                self.payload(requested_agents=["ERPcoder", "missing"])
            )

    def test_raw_filesystem_path_cannot_bypass_workspace_registry(self):
        with self.assertRaisesRegex(ValueError, "raw workspace path"):
            self.adapter.to_task_package(
                self.payload(project_root="E:/PLACHEM-Agent-Control/repo")
            )

    def test_workspace_reference_must_be_registered(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_WORKSPACE:unregistered"):
            self.adapter.to_task_package(
                self.payload(workspace_id="unregistered")
            )

    def test_human_approval_is_metadata_not_gateway_authorization(self):
        package = self.adapter.to_task_package(self.payload())

        self.assertEqual(package["war_room_metadata"]["approval"]["status"], "approved")
        self.assertNotIn("authorization", package)
        self.assertNotIn("broker_authorization", package)

    def test_requested_agents_do_not_create_fanout(self):
        package = self.adapter.to_task_package(
            self.payload(requested_agents=["ERPcoder", "ERPqa"])
        )

        self.assertEqual(package["requested_worker"], "ERPcoder")
        self.assertNotIn("requested_agents", package)


if __name__ == "__main__":
    unittest.main()
