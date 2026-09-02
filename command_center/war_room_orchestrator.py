from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import task_dispatch
from agent_registry import AgentRegistry
from dependency_readiness import DependencyReadinessEvaluator
from dispatch_boundary import ExplicitDispatchBoundary
from dispatch_selection import DispatchCandidateSelector
from run_query import RunQuery
from war_room_adapter import WarRoomTaskAdapter, WarRoomTaskCompiler
from workspace_registry import WorkspaceRegistry


_SELECTION_MODES = {"strict", "fallback"}
_ACTIVE = {"CREATED", "DISPATCHING", "RUNNING"}
_TERMINAL = {"PASS", "FAIL", "BLOCKED"}


class WarRoomOrchestrator:
    """Compose existing War Room and Command Center components."""

    def __init__(
        self,
        agent_registry: AgentRegistry,
        workspace_registry: WorkspaceRegistry,
        *,
        run_registry_path: str | Path,
        dispatcher: Callable[[dict[str, Any]], Any] | None = None,
        dispatch_kwargs: Mapping[str, Any] | None = None,
        workspace_map: dict[str, str] | None = None,
    ) -> None:
        self._agents = agent_registry
        self._workspaces = workspace_registry
        self._runs = RunQuery(run_registry_path)
        self._workspace_map = dict(workspace_map or {})
        self._adapter = WarRoomTaskAdapter(
            agent_registry,
            workspace_registry,
            workspace_map=self._workspace_map,
        )
        self._compiler = WarRoomTaskCompiler(self._adapter)
        self._dispatch_kwargs = dict(dispatch_kwargs or {})
        self._dispatcher = dispatcher or self._default_dispatcher

    def _default_dispatcher(self, task: dict[str, Any]) -> Any:
        return task_dispatch.dispatch(task, **self._dispatch_kwargs)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("War Room task payload must be an object")
        selection_mode = payload.get("selection_mode", "strict")
        if selection_mode not in _SELECTION_MODES:
            raise ValueError(f"INVALID_SELECTION_MODE:{selection_mode}")
        # Compiler performs validation, raw-path rejection, workspace/agent checks,
        # duplicate detection, revision protection, and dependency compilation.
        compiled = self._compiler.compile(payload)
        result = copy.deepcopy(compiled)
        result["selection_mode"] = selection_mode
        preferred = payload.get("preferred_worker")
        for task in result["command_tasks"]:
            requested = task["requested_worker"]
            task["preferred_worker"] = preferred or requested
            task["required_capabilities"] = list(payload.get("required_capabilities") or [])
            task["worker_selection_mode"] = "strict" if selection_mode == "strict" else "fallback"
        return result

    def _compilation(self, war_project_id: str, war_task_id: str) -> dict[str, Any]:
        for compilation in self._compiler.compilations.values():
            if (
                compilation.get("war_project_id") == war_project_id
                and compilation.get("war_task_id") == war_task_id
            ):
                return compilation
        raise ValueError(f"UNKNOWN_WORKFLOW:war_room:{war_project_id}:{war_task_id}")

    @staticmethod
    def _task(compilation: dict[str, Any], task_id: str) -> dict[str, Any]:
        for task in compilation["command_tasks"]:
            if task.get("task_id") == task_id:
                return task
        raise ValueError(f"UNKNOWN_TASK:{task_id}")

    def _components(self, compilation: dict[str, Any]) -> tuple[DependencyReadinessEvaluator, DispatchCandidateSelector, ExplicitDispatchBoundary]:
        tasks = compilation["command_tasks"]
        evaluator = DependencyReadinessEvaluator(
            self._runs,
            known_task_ids={task["task_id"] for task in tasks},
        )
        selector = DispatchCandidateSelector(evaluator)
        boundary = ExplicitDispatchBoundary(selector, self._dispatcher)
        return evaluator, selector, boundary

    def status(self, war_project_id: str, war_task_id: str) -> dict[str, Any]:
        compilation = self._compilation(war_project_id, war_task_id)
        evaluator, _, _ = self._components(compilation)
        tasks: list[dict[str, Any]] = []
        for task in compilation["command_tasks"]:
            records = self._runs.by_task_id(task["task_id"])
            latest = records[0] if records else {}
            tasks.append({
                "task_id": task["task_id"],
                "requested_agent": task.get("requested_worker"),
                "selected_worker": latest.get("worker") or task.get("requested_worker"),
                "workflow_role": task.get("workflow_role"),
                "depends_on_task_ids": list(task.get("depends_on_task_ids", [])),
                "readiness": evaluator.evaluate(task)["readiness"],
                "run_id": latest.get("run_id"),
                "run_status": latest.get("status"),
                "result_available": bool(latest.get("gateway_result") or latest.get("result")),
            })
        return {
            "war_project_id": war_project_id,
            "war_task_id": war_task_id,
            "correlation_id": compilation["correlation_id"],
            "tasks": tasks,
        }

    def candidates(self, war_project_id: str, war_task_id: str) -> dict[str, list[dict[str, Any]]]:
        compilation = self._compilation(war_project_id, war_task_id)
        _, selector, _ = self._components(compilation)
        return selector.select(compilation["command_tasks"])

    def dispatch(self, task_id: str, *, war_project_id: str | None = None, war_task_id: str | None = None) -> Any:
        if war_project_id is None or war_task_id is None:
            matches = [
                compilation
                for compilation in self._compiler.compilations.values()
                if any(task.get("task_id") == task_id for task in compilation["command_tasks"])
            ]
            if len(matches) != 1:
                raise ValueError(f"UNKNOWN_TASK:{task_id}")
            compilation = matches[0]
        else:
            compilation = self._compilation(war_project_id, war_task_id)
        _, _, boundary = self._components(compilation)
        return boundary.dispatch_selected(task_id, compilation["command_tasks"])

    def next_ready(self, war_project_id: str, war_task_id: str) -> list[dict[str, Any]]:
        return self.candidates(war_project_id, war_task_id)["candidates"]

    def summary(self, war_project_id: str, war_task_id: str) -> dict[str, Any]:
        compilation = self._compilation(war_project_id, war_task_id)
        evaluator, _, _ = self._components(compilation)
        required = [task for task in compilation["command_tasks"] if task.get("workflow_role") != "observer"]
        readiness = [evaluator.evaluate(task) for task in required]
        records = [
            self._runs.by_task_id(task["task_id"])[0]
            for task in required
            if self._runs.by_task_id(task["task_id"])
        ]
        statuses = {record.get("status") for record in records}
        if required and all(record.get("status") == "PASS" for record in records) and len(records) == len(required):
            overall = "COMPLETED"
        elif statuses & _ACTIVE:
            overall = "IN_PROGRESS"
        elif any(item["readiness"] == "READY" for item in readiness):
            overall = "READY"
        elif any(item["readiness"] == "WAITING" for item in readiness):
            overall = "PENDING"
        elif statuses & {"FAIL", "BLOCKED"} or any(item["readiness"] == "BLOCKED" for item in readiness):
            overall = "FAILED" if statuses & {"FAIL", "BLOCKED"} else "BLOCKED"
        else:
            overall = "PENDING"
        return {
            "war_project_id": war_project_id,
            "war_task_id": war_task_id,
            "correlation_id": compilation["correlation_id"],
            "overall": overall,
            "tasks": self.status(war_project_id, war_task_id)["tasks"],
        }
