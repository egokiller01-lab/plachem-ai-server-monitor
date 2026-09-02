from __future__ import annotations

from typing import Any, Callable

from dispatch_selection import DispatchCandidateSelector


class DispatchBoundaryError(ValueError):
    """Raised when an explicit dispatch request fails closed."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)


class ExplicitDispatchBoundary:
    """Pass one currently READY task to the existing Dispatcher only."""

    def __init__(
        self,
        selector: DispatchCandidateSelector,
        dispatcher: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._selector = selector
        self._dispatcher = dispatcher

    def dispatch_selected(
        self,
        task_id: str,
        tasks: list[dict[str, Any]],
        *,
        authorization_factory: Callable[..., Any] | None = None,
    ) -> Any:
        if not isinstance(task_id, str) or not task_id:
            raise DispatchBoundaryError("TASK_NOT_FOUND")
        if not isinstance(tasks, list):
            raise DispatchBoundaryError("TASK_NOT_FOUND")
        if not any(isinstance(task, dict) and task.get("task_id") == task_id for task in tasks):
            raise DispatchBoundaryError("TASK_NOT_FOUND")

        # Human approval metadata never creates Task Auth Broker authorization.
        del authorization_factory
        first = self._selector.select(tasks)
        self._require_candidate(task_id, first)

        # Re-evaluate immediately before handoff to close the READY-to-dispatch race.
        current = self._selector.select(tasks)
        self._require_candidate(task_id, current)

        selected = next(task for task in tasks if task["task_id"] == task_id)
        return self._dispatcher(selected)

    @staticmethod
    def _require_candidate(
        task_id: str,
        selection: dict[str, list[dict[str, Any]]],
    ) -> None:
        if any(candidate.get("task_id") == task_id for candidate in selection["candidates"]):
            return
        excluded = next(
            (item for item in selection["excluded"] if item.get("task_id") == task_id),
            None,
        )
        if excluded is None:
            raise DispatchBoundaryError("TASK_NOT_FOUND")
        reason = excluded.get("reason", "")
        if reason == "task_already_active":
            code = "TASK_ALREADY_ACTIVE"
        elif reason == "task_already_terminal":
            code = "TASK_ALREADY_TERMINAL"
        else:
            code = "TASK_NOT_READY"
        raise DispatchBoundaryError(code, reason)
