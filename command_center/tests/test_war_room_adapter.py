import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_registry import AgentRegistry
from war_room_adapter import (
    DuplicateExternalReference,
    CompilationConflict,
    WarRoomTaskCompiler,
    WarRoomTaskAdapter,
)
from workspace_registry import WorkspaceEntry, WorkspaceRegistry


class WarRoomTaskAdapterTests(unittest.TestCase):
    def setUp(self):
        self.agents = AgentRegistry(
            {
                "ERPcoder": {"provider": "test-worker"},
                "ERPqa": {"provider": "test-worker"},
                "ERPmanager": {"provider": "test-worker"},
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


class WarRoomTaskCompilerTests(unittest.TestCase):
    def setUp(self):
        agents = AgentRegistry(
            {
                "ERPcoder": {"provider": "test-worker"},
                "ERPqa": {"provider": "test-worker"},
                "ERPmanager": {"provider": "test-worker"},
            }
        )
        workspaces = WorkspaceRegistry(
            {
                "erp": WorkspaceEntry(
                    project_id="erp",
                    canonical_root=Path("E:/PLACHEM-Agent-Control/repo"),
                    branch="phase2-worker-identity",
                    status="ACTIVE",
                )
            }
        )
        adapter = WarRoomTaskAdapter(agents, workspaces)
        self.compiler = WarRoomTaskCompiler(adapter)

    def payload(self, agents=None, **overrides):
        value = {
            "war_project_id": "project-1",
            "war_task_id": "war-task-1",
            "scope": "ERP fix and review",
            "requested_agents": agents or ["ERPcoder", "ERPqa"],
            "workspace_id": "erp",
        }
        value.update(overrides)
        return value

    def test_compiles_two_independent_command_tasks(self):
        result = self.compiler.compile(self.payload())

        tasks = result["command_tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertNotEqual(tasks[0]["task_id"], tasks[1]["task_id"])
        self.assertEqual([task["requested_worker"] for task in tasks], ["ERPcoder", "ERPqa"])
        self.assertEqual([task["compile_index"] for task in tasks], [0, 1])

    def test_compiled_tasks_share_correlation_and_external_reference(self):
        result = self.compiler.compile(self.payload(correlation_id="war-corr-1"))

        tasks = result["command_tasks"]
        self.assertEqual(result["correlation_id"], "war-corr-1")
        self.assertEqual({task["correlation_id"] for task in tasks}, {"war-corr-1"})
        self.assertEqual(
            {tuple(sorted(task["external_reference"].items())) for task in tasks},
            {(
                ("external_task_id", "war-task-1"),
                ("project_id", "project-1"),
                ("source", "war_room"),
            )},
        )

    def test_unknown_agent_rejects_atomically(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT:missing"):
            self.compiler.compile(self.payload(agents=["ERPcoder", "missing"]))

        self.assertEqual(self.compiler.compilations, {})

    def test_same_agent_set_returns_existing_compilation(self):
        first = self.compiler.compile(self.payload())
        second = self.compiler.compile(self.payload())

        self.assertEqual(second, first)
        self.assertEqual(len(self.compiler.compilations), 1)

    def test_changed_agent_set_requires_revision(self):
        self.compiler.compile(self.payload())

        with self.assertRaises(CompilationConflict):
            self.compiler.compile(
                self.payload(agents=["ERPcoder", "ERPqa", "ERPmanager"])
            )

        self.assertEqual(len(self.compiler.compilations), 1)

    def test_single_agent_remains_compatible(self):
        result = self.compiler.compile(self.payload(agents=["ERPqa"]))

        self.assertEqual(len(result["command_tasks"]), 1)
        self.assertEqual(result["command_tasks"][0]["requested_worker"], "ERPqa")
        self.assertEqual(result["command_tasks"][0]["depends_on_task_ids"], [])

    def test_explicit_implementation_to_review_maps_agent_dependency_to_task_id(self):
        result = self.compiler.compile(
            self.payload(
                correlation_id="war-corr-1",
                workflow=[
                    {"agent_id": "ERPcoder", "role": "implementation", "depends_on": []},
                    {"agent_id": "ERPqa", "role": "review", "depends_on": ["ERPcoder"]},
                ],
            )
        )

        tasks = result["command_tasks"]
        self.assertEqual(tasks[0]["workflow_role"], "implementation")
        self.assertEqual(tasks[0]["depends_on_task_ids"], [])
        self.assertEqual(tasks[1]["workflow_role"], "review")
        self.assertEqual(tasks[1]["depends_on_task_ids"], [tasks[0]["task_id"]])
        self.assertEqual(
            result["workflow_graph"][1]["depends_on_task_ids"],
            [tasks[0]["task_id"]],
        )

    def test_unknown_dependency_rejects_atomically(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_WORKFLOW_DEPENDENCY:missing"):
            self.compiler.compile(
                self.payload(
                    workflow=[
                        {"agent_id": "ERPcoder", "role": "implementation", "depends_on": []},
                        {"agent_id": "ERPqa", "role": "review", "depends_on": ["missing"]},
                    ]
                )
            )

        self.assertEqual(self.compiler.compilations, {})

    def test_self_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "SELF_DEPENDENCY:ERPcoder"):
            self.compiler.compile(
                self.payload(
                    workflow=[
                        {"agent_id": "ERPcoder", "role": "implementation", "depends_on": ["ERPcoder"]},
                        {"agent_id": "ERPqa", "role": "review", "depends_on": []},
                    ]
                )
            )

    def test_cycle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "CYCLE_DETECTED"):
            self.compiler.compile(
                self.payload(
                    workflow=[
                        {"agent_id": "ERPcoder", "role": "implementation", "depends_on": ["ERPqa"]},
                        {"agent_id": "ERPqa", "role": "review", "depends_on": ["ERPcoder"]},
                    ]
                )
            )

    def test_duplicate_workflow_agent_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "DUPLICATE_WORKFLOW_AGENT:ERPcoder"):
            self.compiler.compile(
                self.payload(
                    workflow=[
                        {"agent_id": "ERPcoder", "role": "implementation", "depends_on": []},
                        {"agent_id": "ERPcoder", "role": "review", "depends_on": []},
                    ]
                )
            )

    def test_workflow_agent_set_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "WORKFLOW_AGENT_SET_MISMATCH"):
            self.compiler.compile(
                self.payload(
                    workflow=[
                        {"agent_id": "ERPcoder", "role": "implementation", "depends_on": []},
                        {"agent_id": "ERPqa", "role": "review", "depends_on": []},
                        {"agent_id": "ERPmanager", "role": "observer", "depends_on": []},
                    ]
                )
            )

    def test_unknown_workspace_rejects_before_child_creation(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_WORKSPACE:missing"):
            self.compiler.compile(self.payload(workspace_id="missing"))

        self.assertEqual(self.compiler.compilations, {})

    def test_raw_path_cannot_bypass_workspace_guard(self):
        with self.assertRaisesRegex(ValueError, "raw workspace path"):
            self.compiler.compile(self.payload(project_root="E:/PLACHEM-Agent-Control/repo"))


if __name__ == "__main__":
    unittest.main()
