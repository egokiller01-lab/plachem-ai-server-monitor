from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_ACTIVE = {"CREATED", "DISPATCHING", "RUNNING"}
_TERMINAL = {"PASS", "FAIL", "BLOCKED"}


class RunQuery:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        text = self.path.read_text(encoding="utf-8")
        if not text:
            return []
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise ValueError(f"MALFORMED_RUN_JSONL:line={line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"MALFORMED_RUN_JSONL:line={line_number}") from exc
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("task_id"), str)
                or not record.get("task_id")
                or not isinstance(record.get("status"), str)
                or not record.get("status")
            ):
                raise ValueError(f"MALFORMED_RUN_JSONL:line={line_number}")
            records.append(record)
        return records

    def _latest_runs(self) -> list[dict[str, Any]]:
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, record in enumerate(self._records()):
            latest[record["task_id"]] = (index, record)
        return [
            dict(record)
            for _, record in sorted(latest.values(), key=lambda item: item[0], reverse=True)
        ]

    @staticmethod
    def _limit(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("INVALID_RUN_QUERY_LIMIT")
        return records[:limit]

    def get(self, task_id: str) -> dict[str, Any] | None:
        for record in self._latest_runs():
            if record["task_id"] == task_id:
                return record
        return None

    def recent(self, limit: int) -> list[dict[str, Any]]:
        return self._limit(self._latest_runs(), limit)

    def active(self) -> list[dict[str, Any]]:
        return [record for record in self._latest_runs() if record["status"] in _ACTIVE]

    def recent_terminal(self, limit: int) -> list[dict[str, Any]]:
        terminal = [record for record in self._latest_runs() if record["status"] in _TERMINAL]
        return self._limit(terminal, limit)

    def worker_statuses(self) -> dict[str, str]:
        workers: dict[str, str] = {}
        for record in self._latest_runs():
            worker = record.get("worker")
            if isinstance(worker, str) and worker and worker not in workers:
                workers[worker] = record["status"]
        return workers

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._latest_runs():
            status = record["status"]
            counts[status] = counts.get(status, 0) + 1
        return counts

    def summary(self, *, recent_limit: int = 10) -> dict[str, Any]:
        return {
            "active": self.active(),
            "recent": self.recent(recent_limit),
            "workers": self.worker_statuses(),
            "counts": self.counts(),
        }
