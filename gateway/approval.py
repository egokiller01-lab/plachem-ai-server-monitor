from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from gateway.models import TaskSpec
from gateway.path_security import reject_reparse_points


def task_sha256(task: TaskSpec) -> str:
    canonical = json.dumps(
        task.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ApprovalReceipt:
    task_id: str
    task_sha256: str
    approved_by: str
    approved_at: str


class ApprovalStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.approvals_dir = runtime_dir / "approvals"

    def _path(self, task_id: str) -> Path:
        return self.approvals_dir / f"{task_id}.json"

    def approve(self, task: TaskSpec, *, approved_by: str) -> ApprovalReceipt:
        if not approved_by.strip():
            raise ValueError("approved_by is required")
        reject_reparse_points(self.runtime_dir)
        reject_reparse_points(self.approvals_dir)
        self.approvals_dir.mkdir(parents=True, exist_ok=True)
        reject_reparse_points(self.approvals_dir)
        path = self._path(task.task_id)
        reject_reparse_points(path)
        receipt = ApprovalReceipt(
            task_id=task.task_id,
            task_sha256=task_sha256(task),
            approved_by=approved_by.strip(),
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        serialized = json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True, indent=2)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(stat.S_IREAD)
        return receipt

    def is_approved(self, task: TaskSpec) -> bool:
        path = self._path(task.task_id)
        reject_reparse_points(self.approvals_dir)
        reject_reparse_points(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or set(payload) != {
            "task_id", "task_sha256", "approved_by", "approved_at"
        }:
            return False
        return (
            payload["task_id"] == task.task_id
            and payload["task_sha256"] == task_sha256(task)
            and isinstance(payload["approved_by"], str)
            and bool(payload["approved_by"].strip())
            and isinstance(payload["approved_at"], str)
        )
