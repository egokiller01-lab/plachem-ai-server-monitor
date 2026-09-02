from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

from agent_registry import AgentRegistry
from task_intake import create_task_package
from workspace_registry import WorkspaceRegistry


_REQUIRED_FIELDS = {"war_project_id", "war_task_id", "scope"}
_RAW_WORKSPACE_FIELDS = {"project_root", "workspace_root", "workspace_path", "path"}
_WORKFLOW_ROLES = {"implementation", "review", "observer"}


class DuplicateExternalReference(ValueError):
    """Raised when a War Room task already has a Command Task."""

    def __init__(self, project_id: str, task_id: str, command_task_id: str) -> None:
        self.project_id = project_id
        self.external_task_id = task_id
        self.task_id = command_task_id
        super().__init__(
            "DUPLICATE_EXTERNAL_REFERENCE:"
            f"war_room:{project_id}:{task_id}:command_task={command_task_id}"
        )


class CompilationConflict(ValueError):
    """Raised when a War Room task is submitted with a new agent set."""

    def __init__(self, project_id: str, task_id: str) -> None:
        self.project_id = project_id
        self.external_task_id = task_id
        super().__init__(
            "COMPILATION_REVISION_REQUIRED:"
            f"war_room:{project_id}:{task_id}"
        )


@dataclass(frozen=True)
class WarRoomExternalReference:
    project_id: str
    task_id: str

    @property
    def key(self) -> tuple[str, str]:
        return self.project_id, self.task_id


class WarRoomTaskAdapter:
    """Translate a War Room task into an unapproved Command Task package."""

    def __init__(
        self,
        agent_registry: AgentRegistry,
        workspace_registry: WorkspaceRegistry,
        *,
        workspace_map: dict[str, str] | None = None,
        existing_tasks: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._agents = agent_registry
        self._workspaces = workspace_registry
        self._workspace_map = dict(workspace_map or {})
        self._external_tasks = existing_tasks if existing_tasks is not None else {}

    def lookup(self, payload: dict[str, Any]) -> str | None:
        reference = self._reference(payload)
        return self._external_tasks.get(reference.key)

    def to_task_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("War Room task payload must be an object")
        missing = _REQUIRED_FIELDS - payload.keys()
        if missing:
            raise ValueError(f"missing War Room task fields: {sorted(missing)}")
        if _RAW_WORKSPACE_FIELDS & payload.keys():
            raise ValueError("raw workspace path is not accepted")

        reference = self._reference(payload)
        existing = self._external_tasks.get(reference.key)
        if existing is not None:
            raise DuplicateExternalReference(
                reference.project_id,
                reference.task_id,
                existing,
            )

        workspace_id = self._workspace_id(payload, reference.project_id)
        self._workspaces.resolve(workspace_id)
        requested_agents = self._requested_agents(payload)
        for agent_id in requested_agents:
            self._agents.resolve(agent_id)
        requested_worker = requested_agents[0]

        instruction = self._instruction(payload["scope"])
        package = create_task_package(
            instruction,
            requested_worker=requested_worker,
            requested_actions=list(payload.get("requested_actions") or []),
            correlation_id=payload.get("correlation_id"),
            source="war_room",
            project_id=reference.project_id,
            external_task_id=reference.task_id,
        )
        package["workspace_id"] = workspace_id
        package["war_room_metadata"] = {
            "war_project_id": reference.project_id,
            "war_task_id": reference.task_id,
            "approval": copy.deepcopy(payload.get("approval")),
            "metadata": copy.deepcopy(payload.get("metadata", {})),
        }
        self._external_tasks[reference.key] = package["task_id"]
        return package

    @staticmethod
    def _instruction(scope: Any) -> str:
        if isinstance(scope, str) and scope:
            return scope
        if isinstance(scope, dict) and scope:
            return json.dumps(scope, ensure_ascii=False, sort_keys=True)
        raise ValueError("scope must be a non-empty string or object")

    @staticmethod
    def _reference(payload: dict[str, Any]) -> WarRoomExternalReference:
        project_id = payload.get("war_project_id")
        task_id = payload.get("war_task_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("war_project_id must be a non-empty string")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("war_task_id must be a non-empty string")
        return WarRoomExternalReference(project_id, task_id)

    def _workspace_id(self, payload: dict[str, Any], war_project_id: str) -> str:
        workspace_id = payload.get("workspace_id", payload.get("command_workspace_id"))
        if workspace_id is None:
            workspace_id = self._workspace_map.get(war_project_id)
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("registered workspace_id is required")
        return workspace_id

    @staticmethod
    def _requested_agents(payload: dict[str, Any]) -> list[str]:
        requested_agents = payload.get("requested_agents")
        assignee = payload.get("assignee_agent_id")
        if requested_agents is None:
            requested_agents = [assignee] if assignee is not None else []
        if not isinstance(requested_agents, list) or not requested_agents:
            raise ValueError("requested_agents or assignee_agent_id is required")
        if not all(isinstance(agent_id, str) and agent_id for agent_id in requested_agents):
            raise ValueError("requested_agents must contain non-empty strings")
        if assignee is not None and assignee not in requested_agents:
            raise ValueError("assignee_agent_id must be included in requested_agents")
        return list(dict.fromkeys(requested_agents))


class WarRoomTaskCompiler:
    """Compile one War Room task into independently identified child tasks."""

    def __init__(self, adapter: WarRoomTaskAdapter) -> None:
        self._adapter = adapter
        self._compilations: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}

    @property
    def compilations(self) -> dict[tuple[str, str, tuple[str, ...]], dict[str, Any]]:
        return copy.deepcopy(self._compilations)

    def compile(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("War Room task payload must be an object")
        if _RAW_WORKSPACE_FIELDS & payload.keys():
            raise ValueError("raw workspace path is not accepted")

        reference = self._adapter._reference(payload)
        agents = self._adapter._requested_agents(payload)
        agent_key = tuple(sorted(agents))
        key = (reference.project_id, reference.task_id, agent_key)
        existing = self._compilations.get(key)
        if existing is not None:
            return copy.deepcopy(existing)
        if any(existing_key[:2] == key[:2] for existing_key in self._compilations):
            raise CompilationConflict(reference.project_id, reference.task_id)

        workspace_id = self._adapter._workspace_id(payload, reference.project_id)
        self._adapter._workspaces.resolve(workspace_id)
        for agent_id in agents:
            self._adapter._agents.resolve(agent_id)
        workflow = self._workflow(payload, agents)

        correlation_id = payload.get("correlation_id") or f"corr-{uuid.uuid4().hex}"
        if not isinstance(correlation_id, str) or not correlation_id:
            raise TypeError("correlation_id must be a non-empty string or None")

        command_tasks: list[dict[str, Any]] = []
        for index, agent_id in enumerate(agents):
            child_payload = dict(payload)
            child_payload["requested_agents"] = [agent_id]
            child_payload["assignee_agent_id"] = agent_id
            child_payload["correlation_id"] = correlation_id
            child_adapter = WarRoomTaskAdapter(
                self._adapter._agents,
                self._adapter._workspaces,
                workspace_map=self._adapter._workspace_map,
                existing_tasks={},
            )
            package = child_adapter.to_task_package(child_payload)
            package["compile_index"] = index
            package["workflow_role"] = workflow[agent_id]["role"]
            package["depends_on_task_ids"] = []
            package["dependency_mode"] = "all_success"
            package["war_room_metadata"]["requested_agent"] = agent_id
            command_tasks.append(package)

        task_ids = {agent_id: package["task_id"] for agent_id, package in zip(agents, command_tasks)}
        workflow_graph = []
        for agent_id, package in zip(agents, command_tasks):
            dependencies = [task_ids[dependency] for dependency in workflow[agent_id]["depends_on"]]
            package["depends_on_task_ids"] = dependencies
            workflow_graph.append(
                {
                    "task_id": package["task_id"],
                    "role": package["workflow_role"],
                    "depends_on_task_ids": list(dependencies),
                    "dependency_mode": package["dependency_mode"],
                }
            )

        result = {
            "war_project_id": reference.project_id,
            "war_task_id": reference.task_id,
            "correlation_id": correlation_id,
            "external_reference": copy.deepcopy(command_tasks[0]["external_reference"]),
            "command_tasks": command_tasks,
            "workflow_graph": workflow_graph,
        }
        self._compilations[key] = copy.deepcopy(result)
        return result

    @staticmethod
    def _workflow(payload: dict[str, Any], agents: list[str]) -> dict[str, dict[str, Any]]:
        raw_workflow = payload.get("workflow")
        if raw_workflow is None:
            return {
                agent_id: {"role": "implementation", "depends_on": []}
                for agent_id in agents
            }
        if not isinstance(raw_workflow, list):
            raise ValueError("workflow must be a list")

        agent_set = set(agents)
        workflow: dict[str, dict[str, Any]] = {}
        for item in raw_workflow:
            if not isinstance(item, dict):
                raise ValueError("workflow entries must be objects")
            agent_id = item.get("agent_id")
            role = item.get("role")
            dependencies = item.get("depends_on", [])
            if agent_id in workflow:
                raise ValueError(f"DUPLICATE_WORKFLOW_AGENT:{agent_id}")
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("workflow agent_id must be a non-empty string")
            if role not in _WORKFLOW_ROLES:
                raise ValueError(f"INVALID_WORKFLOW_ROLE:{agent_id}")
            if not isinstance(dependencies, list) or not all(
                isinstance(dependency, str) and dependency for dependency in dependencies
            ):
                raise ValueError(f"invalid workflow dependencies:{agent_id}")
            if agent_id not in agent_set:
                raise ValueError(f"WORKFLOW_AGENT_SET_MISMATCH:{agent_id}")
            workflow[agent_id] = {
                "role": role,
                "depends_on": list(dict.fromkeys(dependencies)),
            }

        if set(workflow) != agent_set:
            raise ValueError("WORKFLOW_AGENT_SET_MISMATCH")
        for agent_id, item in workflow.items():
            for dependency in item["depends_on"]:
                if dependency not in agent_set:
                    raise ValueError(f"UNKNOWN_WORKFLOW_DEPENDENCY:{dependency}")
                if dependency == agent_id:
                    raise ValueError(f"SELF_DEPENDENCY:{agent_id}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(agent_id: str) -> None:
            if agent_id in visiting:
                raise ValueError("CYCLE_DETECTED")
            if agent_id in visited:
                return
            visiting.add(agent_id)
            for dependency in workflow[agent_id]["depends_on"]:
                visit(dependency)
            visiting.remove(agent_id)
            visited.add(agent_id)

        for agent_id in agents:
            visit(agent_id)
        return workflow
