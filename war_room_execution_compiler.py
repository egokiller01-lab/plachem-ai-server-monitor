"""War Room Phase 2 execution compiler.

Compiles one existing War Room task multi-agent intent into independent
execution units with execution_id, agent_id, shared correlation_id, and
depends_on_execution_ids.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Mapping


def _validate_agent_id(agent_id: Any) -> str:
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("INVALID_AGENT_ID")
    return agent_id


def _validate_task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("INVALID_TASK_ID")
    return task_id


def _validate_project_id(project_id: Any) -> str:
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("INVALID_PROJECT_ID")
    return project_id


def _validate_correlation_id(correlation_id: Any) -> str:
    if not isinstance(correlation_id, str) or not correlation_id:
        raise ValueError("INVALID_CORRELATION_ID")
    return correlation_id


def compile_task_intent(
    *,
    war_project_id: Any,
    war_task_id: Any,
    agents: list[str] | tuple[str, ...],
    correlation_id: str | None = None,
    workflow: dict[str, dict[str, Any]] | None = None,
    execution_id_factory: Any = None,
) -> dict[str, Any]:
    """Compile one War Room task multi-agent intent into execution units.

    Args:
        war_project_id: War Room project ID.
        war_task_id: War Room task ID.
        agents: List of agent IDs to execute.
        correlation_id: Optional correlation ID; auto-generated if None.
        workflow: Optional workflow definition mapping agent_id to
            {"role": str, "depends_on": list[str]}.
        execution_id_factory: Optional callable that returns execution IDs.

    Returns:
        dict with keys:
            - war_project_id
            - war_task_id
            - correlation_id
            - execution_units: list of execution unit dicts
            - workflow_graph: list of graph nodes

    Raises:
        TypeError: If war_project_id, war_task_id, or agents is not a valid type.
        ValueError: If any required field is invalid or workflow is malformed.
    """
    project_id = _validate_project_id(war_project_id)
    task_id = _validate_task_id(war_task_id)

    if agents is None or not isinstance(agents, (list, tuple)):
        raise TypeError("agents must be a list or tuple")
    if len(agents) == 0:
        raise ValueError("agents must be non-empty")

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered_agents: list[str] = []
    for agent_id in agents:
        normalized = _validate_agent_id(agent_id)
        if normalized not in seen:
            seen.add(normalized)
            ordered_agents.append(normalized)

    if correlation_id is None:
        correlation_id = f"corr-{uuid.uuid4().hex}"
    else:
        correlation_id = _validate_correlation_id(correlation_id)

    if execution_id_factory is None:
        execution_id_factory = lambda: f"exec-{uuid.uuid4().hex}"

    # Build execution units
    execution_units: list[dict[str, Any]] = []
    agent_to_execution_id: dict[str, str] = {}

    for index, agent_id in enumerate(ordered_agents):
        execution_id = str(execution_id_factory())
        if not execution_id:
            raise ValueError("execution_id_factory must return non-empty string")
        if not isinstance(execution_id, str):
            raise TypeError("execution_id_factory must return str")
        if any(unit["execution_id"] == execution_id for unit in execution_units):
            raise ValueError(f"DUPLICATE_EXECUTION_ID:{execution_id}")

        execution_units.append({
            "execution_id": execution_id,
            "agent_id": agent_id,
            "correlation_id": correlation_id,
            "compile_index": index,
        })
        agent_to_execution_id[agent_id] = execution_id

    # Resolve workflow
    if workflow is None:
        workflow_map: dict[str, dict[str, Any]] = {
            agent_id: {"role": "implementation", "depends_on": [],
                       "message": None, "timeout_seconds": None, "goal_contract": None}
            for agent_id in ordered_agents
        }
    else:
        if not isinstance(workflow, Mapping):
            raise TypeError("workflow must be a Mapping")

        workflow_map = {}
        for agent_id in ordered_agents:
            if agent_id not in workflow:
                raise ValueError(f"WORKFLOW_MISSING_AGENT:{agent_id}")
            entry = workflow[agent_id]
            if not isinstance(entry, Mapping):
                raise TypeError(f"WORKFLOW_INVALID_ENTRY:{agent_id}")

            role = entry.get("role", "implementation")
            if not isinstance(role, str) or not role:
                raise ValueError(f"WORKFLOW_INVALID_ROLE:{agent_id}")

            depends_on = entry.get("depends_on", [])
            if not isinstance(depends_on, list):
                raise ValueError(f"WORKFLOW_INVALID_DEPENDS_ON:{agent_id}")

            normalized_deps: list[str] = []
            for dep in depends_on:
                dep_id = _validate_agent_id(dep)
                if dep_id == agent_id:
                    raise ValueError(f"WORKFLOW_SELF_DEPENDENCY:{agent_id}")
                if dep_id not in ordered_agents:
                    raise ValueError(f"WORKFLOW_UNKNOWN_DEPENDENCY:{agent_id}->{dep_id}")
                if dep_id not in normalized_deps:
                    normalized_deps.append(dep_id)

            workflow_map[agent_id] = {
                "role": role,
                "depends_on": normalized_deps,
                "message": entry.get("message", entry.get("dispatch_message", (entry.get("dispatch_input") or {}).get("message"))),
                "timeout_seconds": entry.get("timeout_seconds", (entry.get("dispatch_input") or {}).get("timeout_seconds")),
                "goal_contract": entry.get("goal_contract", (entry.get("dispatch_input") or {}).get("goal_contract")),
            }

    # Detect cycles in dependency graph
    visited: set[str] = set()
    visiting: set[str] = set()

    def _visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"WORKFLOW_CYCLE:{node}")
        if node in visited:
            return
        visiting.add(node)
        for dep in workflow_map[node]["depends_on"]:
            _visit(dep)
        visiting.discard(node)
        visited.add(node)

    for agent_id in ordered_agents:
        _visit(agent_id)

    # Build workflow_graph with execution IDs
    workflow_graph: list[dict[str, Any]] = []
    for agent_id in ordered_agents:
        workflow_graph.append({
            "agent_id": agent_id,
            "execution_id": agent_to_execution_id[agent_id],
            "role": workflow_map[agent_id]["role"],
            "depends_on_execution_ids": [
                agent_to_execution_id[dep] for dep in workflow_map[agent_id]["depends_on"]
            ],
        })

    # Attach depends_on_execution_ids to execution units
    for unit in execution_units:
        agent_id = unit["agent_id"]
        unit["depends_on_execution_ids"] = [
            agent_to_execution_id[dep] for dep in workflow_map[agent_id]["depends_on"]
        ]
        unit["workflow_role"] = workflow_map[agent_id]["role"]
        unit["dispatch_message"] = workflow_map[agent_id]["message"]
        unit["dispatch_timeout_seconds"] = workflow_map[agent_id]["timeout_seconds"]
        unit["dispatch_goal_contract"] = workflow_map[agent_id]["goal_contract"]

    return {
        "war_project_id": project_id,
        "war_task_id": task_id,
        "correlation_id": correlation_id,
        "execution_units": execution_units,
        "workflow_graph": workflow_graph,
    }
