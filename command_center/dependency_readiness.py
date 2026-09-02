from __future__ import annotations

from typing import Any

from run_query import RunQuery


_ACTIVE_STATUSES = {"CREATED", "DISPATCHING", "RUNNING"}
_TERMINAL_STATUSES = {"PASS", "FAIL", "BLOCKED"}
_SUPPORTED_MODES = {"all_success"}


class DependencyReadinessEvaluator:
    """Derive execution readiness from task metadata and Run Registry records."""

    def __init__(
        self,
        run_query: RunQuery,
        *,
        known_task_ids: set[str] | None = None,
    ) -> None:
        self._runs = run_query
        self._known_task_ids = set(known_task_ids) if known_task_ids is not None else None

    def evaluate(self, task: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise TypeError("task must be an object")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        mode = task.get("dependency_mode", "all_success")
        if mode not in _SUPPORTED_MODES:
            raise ValueError(f"UNSUPPORTED_DEPENDENCY_MODE:{mode}")
        dependencies = task.get("depends_on_task_ids", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(dependency, str) and dependency for dependency in dependencies
        ):
            raise ValueError("depends_on_task_ids must be a list of non-empty strings")

        task_outcome, task_has_run = self._task_outcome(task_id)
        dependency_details = [
            self._dependency_detail(dependency_id)
            for dependency_id in dependencies
        ]
        result = {
            "task_id": task_id,
            "readiness": "READY",
            "dependency_mode": mode,
            "task_outcome": task_outcome,
            "dependencies": dependency_details,
            "reason": "no dependencies",
        }

        if task_has_run:
            result["readiness"] = "BLOCKED"
            result["reason"] = (
                "task_already_terminal"
                if task_outcome == "SUCCESS" or task_outcome == "FAILURE"
                else "task_already_active"
                if task_outcome == "ACTIVE"
                else "task_outcome_unknown"
            )
            return result

        for detail in dependency_details:
            if detail["outcome"] in {"FAILURE", "NOT_FOUND", "UNKNOWN"}:
                result["readiness"] = "BLOCKED"
                result["reason"] = (
                    f"dependency {detail['task_id']} ended in failure"
                    if detail["outcome"] == "FAILURE"
                    else f"dependency {detail['task_id']} is unavailable"
                )
                return result
        for detail in dependency_details:
            if detail["outcome"] in {"NOT_STARTED", "ACTIVE"}:
                result["readiness"] = "WAITING"
                result["reason"] = f"dependency {detail['task_id']} is still pending"
                return result
        if dependency_details:
            result["reason"] = "all dependencies succeeded"
        return result

    def _dependency_detail(self, task_id: str) -> dict[str, str]:
        outcome, has_run = self._task_outcome(task_id)
        if not has_run:
            if self._known_task_ids is not None and task_id not in self._known_task_ids:
                outcome = "NOT_FOUND"
                reason = f"dependency {task_id} is not a known Command Task"
            else:
                outcome = "NOT_STARTED"
                reason = f"dependency {task_id} has no Run Registry record"
        elif outcome == "ACTIVE":
            reason = f"dependency {task_id} has an active run"
        elif outcome == "SUCCESS":
            reason = f"dependency {task_id} has a successful authoritative run"
        elif outcome == "FAILURE":
            reason = f"dependency {task_id} has a failed authoritative run"
        else:
            reason = f"dependency {task_id} has an unsupported Run Registry status"
        return {"task_id": task_id, "outcome": outcome, "reason": reason}

    def _task_outcome(self, task_id: str) -> tuple[str, bool]:
        records = self._runs.by_task_id(task_id)
        if not records:
            return "NOT_STARTED", False
        if any(record.get("status") in _ACTIVE_STATUSES for record in records):
            return "ACTIVE", True
        latest = records[0]
        status = latest.get("status")
        if status == "PASS":
            return "SUCCESS", True
        if status in {"FAIL", "BLOCKED"}:
            return "FAILURE", True
        return "UNKNOWN", True
