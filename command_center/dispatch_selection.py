from __future__ import annotations

import copy
from typing import Any

from dependency_readiness import DependencyReadinessEvaluator


_CANDIDATE_FIELDS = (
    "task_id",
    "requested_worker",
    "workspace_id",
    "correlation_id",
    "external_reference",
    "workflow_role",
    "readiness",
)


class DispatchCandidateSelector:
    """Select READY tasks without dispatching or changing execution state."""

    def __init__(self, evaluator: DependencyReadinessEvaluator) -> None:
        self._evaluator = evaluator

    def select(self, tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(tasks, list):
            raise TypeError("tasks must be a list")
        task_ids: set[str] = set()
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
                raise ValueError("invalid task")
            task_id = task["task_id"]
            if task_id in task_ids:
                raise ValueError(f"DUPLICATE_TASK_ID:{task_id}")
            task_ids.add(task_id)

        ordered_tasks = self._ordered(tasks)
        candidates: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for task in ordered_tasks:
            try:
                readiness = self._evaluator.evaluate(task)
            except (TypeError, ValueError) as exc:
                excluded.append(
                    {
                        "task_id": task.get("task_id", ""),
                        "readiness": "BLOCKED",
                        "reason": f"evaluation_error: {exc}",
                    }
                )
                continue

            if readiness["readiness"] == "READY":
                candidate = {
                    field: copy.deepcopy(task[field])
                    for field in _CANDIDATE_FIELDS
                    if field != "readiness" and field in task
                }
                candidate["readiness"] = "READY"
                candidate["reason"] = "dependencies satisfied"
                candidates.append(candidate)
            else:
                excluded.append(
                    {
                        "task_id": task["task_id"],
                        "readiness": readiness["readiness"],
                        "reason": readiness["reason"],
                    }
                )
        return {"candidates": candidates, "excluded": excluded}

    @staticmethod
    def _ordered(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        indexes = [task.get("compile_index") for task in tasks]
        if indexes and all(isinstance(index, int) and not isinstance(index, bool) for index in indexes):
            return sorted(tasks, key=lambda task: task["compile_index"])
        return list(tasks)
