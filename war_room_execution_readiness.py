"""War Room Phase 2 execution readiness evaluator.

Derives WAITING/READY/BLOCKED status for execution units by querying the
existing Fast Gateway RunRegistry without storing duplicate execution status.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from war_room_execution_units import ExecutionUnitStore


# Fast Gateway CoreRunStatus values
_ACTIVE_STATUSES = {"QUEUED", "RUNNING"}
_SUCCESS_STATUSES = {"PASS"}
_FAILURE_STATUSES = {"FAIL", "BLOCKED", "TIMEOUT", "CANCELLED"}
_UNKNOWN_STATUSES = {"UNKNOWN"}


class ExecutionReadinessEvaluator:
    """Derive execution readiness from execution units and RunRegistry records.

    The evaluator does NOT store execution status. It queries the Fast Gateway
    RunRegistry in real-time to determine readiness.
    """

    def __init__(self, execution_store: ExecutionUnitStore, core_engine: Any) -> None:
        """Initialize with a CoreEngine instance.

        Args:
            execution_store: ExecutionUnitStore instance.
            core_engine: CoreEngine instance with .status() method.
        """
        self._store = execution_store
        self._engine = core_engine

    def _query_run_status(self, core_run_id: str) -> str:
        """Query the RunRegistry for a core run's status.

        Returns:
            Status string or "UNKNOWN" if not found.
        """
        try:
            record = self._engine.status(core_run_id)
            return str(record.get("status") or "UNKNOWN")
        except ValueError:
            return "UNKNOWN"

    def _execution_outcome(self, unit: Mapping[str, Any]) -> tuple[str, bool]:
        """Determine the outcome of a single execution unit.

        Returns:
            tuple of (outcome, has_run) where outcome is one of:
            - SUCCESS
            - FAILURE
            - ACTIVE
            - NOT_STARTED
            - UNKNOWN
        """
        core_run_id = unit.get("core_run_id")
        if not core_run_id:
            return "NOT_STARTED", False

        status = self._query_run_status(str(core_run_id))
        if status in _SUCCESS_STATUSES:
            return "SUCCESS", True
        if status in _FAILURE_STATUSES:
            return "FAILURE", True
        if status in _ACTIVE_STATUSES:
            return "ACTIVE", True
        if status in _UNKNOWN_STATUSES:
            return "UNKNOWN", True
        return "NOT_STARTED", True

    def evaluate(self, unit: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate readiness for a single execution unit.

        Args:
            unit: Execution unit dict with keys:
                - execution_id
                - agent_id
                - core_run_id
                - depends_on_execution_ids
                - dependency_mode (optional, defaults to "all_success")

        Returns:
            dict with keys:
                - execution_id
                - agent_id
                - readiness: WAITING|READY|BLOCKED
                - reason: str
                - dependencies: list of dependency details
        """
        if not isinstance(unit, Mapping):
            raise TypeError("unit must be a Mapping")

        execution_id = unit.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("execution_id is required")

        agent_id = unit.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id is required")

        mode = unit.get("dependency_mode", "all_success")
        if mode not in {"all_success"}:
            raise ValueError(f"UNSUPPORTED_DEPENDENCY_MODE:{mode}")

        dependencies = unit.get("depends_on_execution_ids", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(dep, str) and dep for dep in dependencies
        ):
            raise ValueError("depends_on_execution_ids must be a list of non-empty strings")

        # Check if this execution itself has a run
        execution_outcome, execution_has_run = self._execution_outcome(unit)
        dependency_details: list[dict[str, Any]] = []

        for dep_id in dependencies:
            dep_detail = self._dependency_detail(dep_id)
            dependency_details.append(dep_detail)

        result: dict[str, Any] = {
            "execution_id": execution_id,
            "agent_id": agent_id,
            "readiness": "READY",
            "dependency_mode": mode,
            "execution_outcome": execution_outcome,
            "dependencies": dependency_details,
            "reason": "no dependencies",
        }

        # Check if this execution itself is already terminal
        if execution_has_run:
            result["readiness"] = "BLOCKED"
            result["reason"] = (
                "execution_already_terminal"
                if execution_outcome in {"SUCCESS", "FAILURE"}
                else "execution_already_active"
                if execution_outcome == "ACTIVE"
                else "execution_outcome_unknown"
            )
            return result

        # Check dependencies
        for detail in dependency_details:
            if detail["outcome"] in {"FAILURE", "NOT_FOUND", "UNKNOWN"}:
                result["readiness"] = "BLOCKED"
                result["reason"] = (
                    f"dependency {detail['execution_id']} ended in failure"
                    if detail["outcome"] == "FAILURE"
                    else f"dependency {detail['execution_id']} is unavailable"
                )
                return result

        for detail in dependency_details:
            if detail["outcome"] in {"NOT_STARTED", "ACTIVE"}:
                result["readiness"] = "WAITING"
                result["reason"] = f"dependency {detail['execution_id']} is still pending"
                return result

        if dependency_details:
            result["reason"] = "all dependencies succeeded"

        return result

    def _dependency_detail(self, dep_id: str) -> dict[str, Any]:
        """Get detail for a dependency execution.

        Args:
            dep_id: Execution ID of the dependency.

        Returns:
            dict with keys:
                - execution_id
                - outcome: SUCCESS|FAILURE|ACTIVE|NOT_STARTED|UNKNOWN|NOT_FOUND
                - core_run_id
                - status
        """
        unit = self._store.get_unit(dep_id)
        if unit is None:
            return {
                "execution_id": dep_id,
                "outcome": "NOT_FOUND",
                "core_run_id": None,
                "status": "NOT_FOUND",
            }

        core_run_id = unit.get("core_run_id")
        if not core_run_id:
            return {
                "execution_id": dep_id,
                "outcome": "NOT_STARTED",
                "core_run_id": None,
                "status": "NOT_STARTED",
            }

        status = self._query_run_status(str(core_run_id))
        if status in _SUCCESS_STATUSES:
            outcome = "SUCCESS"
        elif status in _FAILURE_STATUSES:
            outcome = "FAILURE"
        elif status in _ACTIVE_STATUSES:
            outcome = "ACTIVE"
        else:
            outcome = "UNKNOWN"

        return {
            "execution_id": dep_id,
            "outcome": outcome,
            "core_run_id": core_run_id,
            "status": status,
        }

    def evaluate_many(self, units: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Evaluate readiness for multiple execution units.

        Args:
            units: List of execution unit dicts.

        Returns:
            List of readiness result dicts.
        """
        if not isinstance(units, list):
            raise TypeError("units must be a list")
        return [self.evaluate(unit) for unit in units]
