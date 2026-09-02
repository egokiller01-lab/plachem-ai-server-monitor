from __future__ import annotations

import json
import uuid
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


def _run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def _correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex}"


class RunRegistry:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        correlation_id_factory: Callable[[], str] | None = None,
    ):
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._run_id_factory = run_id_factory or _run_id
        self._correlation_id_factory = correlation_id_factory or _correlation_id

    def _now(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def create(
        self,
        task_id: str,
        project_id: str,
        worker: str,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
        workspace_id: str | None = None,
        external_reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual_run_id = run_id or self._run_id_factory()
        if actual_run_id == task_id:
            raise ValueError("RUN_ID_MUST_DIFFER_FROM_TASK_ID")
        if self.get_run(actual_run_id) is not None:
            raise ValueError(f"RUN_ALREADY_EXISTS:{actual_run_id}")
        created_at = datetime.now(timezone.utc).isoformat()
        record = {
            "run_id": actual_run_id,
            "task_id": task_id,
            "correlation_id": correlation_id or self._correlation_id_factory(),
            "project_id": project_id,
            "workspace_id": workspace_id or project_id,
            "worker": worker,
            "status": "CREATED",
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "completed_at": None,
            "gateway_result": None,
            "failure_reason": "",
            "external_reference": dict(external_reference) if external_reference is not None else None,
        }
        self._append(record)
        return dict(record)

    def transition(
        self,
        run_or_task_id: str,
        status: str,
        *,
        gateway_result: dict[str, Any] | None = None,
        failure_reason: str = "",
    ) -> dict[str, Any]:
        record = self.get_run(run_or_task_id) or self.get(run_or_task_id)
        if record is None:
            raise ValueError(f"UNKNOWN_RUN:{run_or_task_id}")
        current_status = str(record["status"])
        if status not in _TRANSITIONS.get(current_status, set()):
            raise ValueError(f"INVALID_RUN_TRANSITION:{current_status}->{status}")
        updated_at = (
            str(record.get("updated_at") or record.get("created_at") or "")
            if status == "DISPATCHING"
            else self._now()
        )
        record["status"] = status
        record["updated_at"] = updated_at
        if status == "RUNNING":
            record["started_at"] = updated_at
        if status in _TERMINAL:
            record["completed_at"] = updated_at
            record["gateway_result"] = gateway_result
            record["failure_reason"] = failure_reason
        self._append(record)
        return dict(record)

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        return records

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for record in self._records():
            if record.get("run_id") == run_id:
                latest = record
        return dict(latest) if latest is not None else None

    def get(self, task_id: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for record in self._records():
            if record.get("task_id") == task_id:
                latest = record
        return dict(latest) if latest is not None else None
