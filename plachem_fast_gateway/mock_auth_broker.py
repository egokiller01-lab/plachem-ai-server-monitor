from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_task_authorization(config_path: Path, task_id: str) -> dict[str, Any]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("mock broker tasks must be an object")
    raw = tasks.get(task_id)
    if not isinstance(raw, dict):
        raise ValueError(f"authorization not found for task: {task_id}")

    allow = raw.get("allow", [])
    deny = raw.get("deny", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise ValueError("authorization allow must be a string list")
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        raise ValueError("authorization deny must be a string list")

    return {
        "broker_called": True,
        "task_id": task_id,
        "allow": sorted(set(allow)),
        "deny": sorted(set(deny)),
        "git_push_target": raw.get("git_push_target"),
        "git_push_ref": raw.get("git_push_ref"),
    }
