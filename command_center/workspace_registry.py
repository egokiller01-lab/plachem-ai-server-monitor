from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspaceEntry:
    project_id: str
    canonical_root: Path
    branch: str
    status: str
    configured_root: Path | None = None


def current_git_branch(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("WORKSPACE_GIT_BRANCH_UNAVAILABLE") from exc
    branch = completed.stdout.strip()
    if not branch:
        raise ValueError("WORKSPACE_GIT_BRANCH_UNAVAILABLE")
    return branch


def _canonical_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _reject_reparse_points(path: Path) -> None:
    absolute = path.absolute()
    components = [absolute, *absolute.parents]
    for component in reversed(components):
        if not os.path.lexists(component):
            continue
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"WORKSPACE_REPARSE_POINT:{component}")


class WorkspaceRegistry:
    def __init__(self, entries: dict[str, WorkspaceEntry]):
        self._entries = dict(entries)

    @classmethod
    def load(cls, path: str | Path) -> "WorkspaceRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_workspaces = data.get("workspaces") if isinstance(data, dict) else None
        if not isinstance(raw_workspaces, dict):
            raise ValueError("invalid workspace registry")
        entries: dict[str, WorkspaceEntry] = {}
        for project_id, raw in raw_workspaces.items():
            if not isinstance(project_id, str) or not project_id or not isinstance(raw, dict):
                raise ValueError("invalid workspace registry entry")
            root = raw.get("root")
            branch = raw.get("branch")
            status = raw.get("status")
            if not all(isinstance(value, str) and value for value in (root, branch, status)):
                raise ValueError(f"invalid workspace registry entry:{project_id}")
            configured_root = Path(root)
            if not configured_root.is_absolute():
                raise ValueError(f"workspace root must be absolute:{project_id}")
            if not configured_root.is_dir():
                raise ValueError(f"workspace root not found:{project_id}")
            entries[project_id] = WorkspaceEntry(
                project_id=project_id,
                canonical_root=configured_root.resolve(),
                branch=branch,
                status=status,
                configured_root=configured_root,
            )
        return cls(entries)

    def resolve(self, project_id: str) -> WorkspaceEntry:
        entry = self._entries.get(project_id)
        if entry is None:
            raise ValueError(f"UNKNOWN_WORKSPACE:{project_id}")
        return entry

    def validate(self, project_id: str, project_root: str | Path) -> WorkspaceEntry:
        entry = self.resolve(project_id)
        if entry.status != "ACTIVE":
            raise ValueError(f"WORKSPACE_INACTIVE:{project_id}")
        _reject_reparse_points(Path(project_root))
        _reject_reparse_points(entry.configured_root or entry.canonical_root)
        actual_root = Path(project_root).resolve()
        if _canonical_key(actual_root) != _canonical_key(entry.canonical_root):
            raise ValueError(f"WORKSPACE_PATH_MISMATCH:{project_id}")
        actual_branch = current_git_branch(actual_root)
        if actual_branch != entry.branch:
            raise ValueError(
                f"WORKSPACE_BRANCH_MISMATCH:expected={entry.branch}:actual={actual_branch}"
            )
        return entry
