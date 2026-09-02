from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any


def create_task_package(
    original_instruction: str,
    *,
    requested_worker: str | None = None,
    requested_actions: list[str] | None = None,
    correlation_id: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    external_task_id: str | None = None,
) -> dict[str, Any]:
    """Create a CREATED package without approving or executing the task."""
    if not isinstance(original_instruction, str):
        raise TypeError("original_instruction must be a string")
    if requested_worker is not None and not isinstance(requested_worker, str):
        raise TypeError("requested_worker must be a string or None")
    if requested_actions is not None and (
        not isinstance(requested_actions, list)
        or not all(isinstance(action, str) for action in requested_actions)
    ):
        raise TypeError("requested_actions must be a list of strings or None")
    for name, value in (
        ("correlation_id", correlation_id),
        ("source", source),
        ("project_id", project_id),
        ("external_task_id", external_task_id),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise TypeError(f"{name} must be a non-empty string or None")

    package = {
        "task_id": f"task-{uuid.uuid4().hex}",
        "correlation_id": correlation_id or f"corr-{uuid.uuid4().hex}",
        "original_instruction": original_instruction,
        "instruction_sha256": hashlib.sha256(
            original_instruction.encode("utf-8")
        ).hexdigest(),
        "requested_worker": requested_worker,
        "requested_actions": list(requested_actions or []),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "CREATED",
    }
    external_reference = {
        key: value
        for key, value in (
            ("source", source),
            ("project_id", project_id),
            ("external_task_id", external_task_id),
        )
        if value is not None
    }
    if external_reference:
        package["external_reference"] = external_reference
    return package
