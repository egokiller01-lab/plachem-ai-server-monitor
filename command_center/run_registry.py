from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TRANSITIONS = {
    "CREATED": {"DISPATCHING"},
    "DISPATCHING": {"RUNNING"},
    "RUNNING": {"PASS", "FAIL", "BLOCKED"},
    "PASS": set(),
    "FAIL": set(),
    "BLOCKED": set(),
}
_TERMINAL = {"PASS", "FAIL", "BLOCKED"}


class RunRegistry:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def create(self, task_id: str, project_id: str, worker: str) -> dict[str, Any]:
        if self.get(task_id) is not None:
            raise ValueError(f"RUN_ALREADY_EXISTS:{task_id}")
        record = {
            "task_id": task_id,
            "project_id": project_id,
            "worker": worker,
            "status": "CREATED",
            "started_at": None,
            "completed_at": None,
            "gateway_result": None,
            "failure_reason": "",
        }
        self._append(record)
        return dict(record)

    def transition(
        self,
        task_id: str,
        status: str,
        *,
        gateway_result: dict[str, Any] | None = None,
        failure_reason: str = "",
    ) -> dict[str, Any]:
        record = self.get(task_id)
        if record is None:
            raise ValueError(f"UNKNOWN_RUN:{task_id}")
        current_status = str(record["status"])
        if status not in _TRANSITIONS.get(current_status, set()):
            raise ValueError(f"INVALID_RUN_TRANSITION:{current_status}->{status}")
        record["status"] = status
        if status == "RUNNING":
            record["started_at"] = self._clock().astimezone(timezone.utc).isoformat()
        if status in _TERMINAL:
            record["completed_at"] = self._clock().astimezone(timezone.utc).isoformat()
            record["gateway_result"] = gateway_result
            record["failure_reason"] = failure_reason
        self._append(record)
        return dict(record)

    def get(self, task_id: str) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        latest: dict[str, Any] | None = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict) and record.get("task_id") == task_id:
                latest = record
        return dict(latest) if latest is not None else None
