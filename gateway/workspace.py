from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gateway.models import TaskSpec, WorkerResult
from gateway.path_security import reject_reparse_points


class WorkspaceViolation(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StagedWorkspace:
    root: Path
    hashes: dict[str, str]


@dataclass(frozen=True)
class PromotionOutcome:
    promoted_paths: list[Path]
    manifest_path: Path


@dataclass(frozen=True)
class BaselineSnapshot:
    started_at: str
    files: tuple[dict[str, object], ...]
    path: Path


class ScopedWorkspace:
    def __init__(self, project_root: Path, runtime_dir: Path) -> None:
        self.project_root = project_root.resolve()
        self.runtime_dir = runtime_dir.resolve()
        if not self.runtime_dir.is_relative_to(self.project_root):
            raise WorkspaceViolation("runtime must remain inside project root")

    def _project_path(self, relative: str) -> Path:
        target = self.project_root / relative
        reject_reparse_points(target)
        resolved = target.resolve()
        if not resolved.is_relative_to(self.project_root):
            raise WorkspaceViolation("artifact path escapes project root")
        return target

    def create_baseline(self, task: TaskSpec) -> BaselineSnapshot:
        paths = list(task.scope.include)
        for relative in task.scope.exclude:
            candidate = self._project_path(relative)
            if candidate.is_file():
                paths.append(relative)
        records: list[dict[str, object]] = []
        for relative in dict.fromkeys(paths):
            target = self._project_path(relative)
            if not target.is_file():
                raise WorkspaceViolation(f"baseline file is missing: {relative}")
            data = target.read_bytes()
            records.append({
                "path": relative.replace("\\", "/"),
                "size": len(data),
                "sha256": _sha256(data),
            })
        payload = {
            "task_id": task.task_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "files": records,
        }
        baseline_dir = self.runtime_dir / "baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        path = baseline_dir / f"{task.task_id}.json"
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return BaselineSnapshot(
            started_at=str(payload["started_at"]), files=tuple(records), path=path
        )

    def build_source_context(self, task: TaskSpec) -> str:
        sections: list[str] = []
        for relative in task.scope.include:
            target = self._project_path(relative)
            if not target.is_file():
                raise WorkspaceViolation(f"context file is missing: {relative}")
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise WorkspaceViolation(f"context file is not UTF-8: {relative}") from exc
            sections.append(
                f"BEGIN ALLOWED FILE {relative.replace(chr(92), '/')}\n{text}\n"
                f"END ALLOWED FILE {relative.replace(chr(92), '/')}"
            )
        return "\n\n".join(sections)

    def verify_protected_unchanged(
        self, task: TaskSpec, baseline: BaselineSnapshot
    ) -> None:
        allowed = {path.replace("\\", "/") for path in task.scope.include}
        for item in baseline.files:
            relative = str(item["path"])
            if relative in allowed:
                continue
            target = self._project_path(relative)
            if not target.is_file() or _sha256(target.read_bytes()) != item["sha256"]:
                raise WorkspaceViolation(f"protected baseline file changed: {relative}")

    def stage(self, task: TaskSpec, result: WorkerResult) -> StagedWorkspace:
        if result.task_id != task.task_id:
            raise WorkspaceViolation("result task_id does not match")
        allowed = {path.replace("\\", "/") for path in task.scope.include}
        artifact_paths = {artifact.path for artifact in result.artifacts}
        change_paths = {path.replace("\\", "/") for path in result.changes}
        if artifact_paths != allowed or change_paths != allowed:
            raise WorkspaceViolation("artifact and change paths must exactly match TaskSpec scope")
        reject_reparse_points(self.project_root)
        reject_reparse_points(self.runtime_dir)
        staging_root = self.runtime_dir / "workspaces" / task.task_id
        reject_reparse_points(staging_root.parent)
        staging_root.mkdir(parents=True, exist_ok=False)
        hashes: dict[str, str] = {}
        for artifact in result.artifacts:
            self._project_path(artifact.path)
            destination = staging_root / artifact.path
            reject_reparse_points(destination.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            reject_reparse_points(destination.parent)
            data = artifact.content.encode("utf-8")
            with destination.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if destination.read_bytes() != data:
                raise WorkspaceViolation("staged artifact integrity verification failed")
            hashes[artifact.path] = _sha256(data)
        return StagedWorkspace(root=staging_root, hashes=hashes)

    def promote(self, task: TaskSpec, staged: StagedWorkspace) -> PromotionOutcome:
        expected_root = self.runtime_dir / "workspaces" / task.task_id
        if staged.root != expected_root or not staged.root.is_dir():
            raise WorkspaceViolation("staged workspace identity does not match task")
        backups_root = self.runtime_dir / "backups" / task.task_id
        manifests_root = self.runtime_dir / "manifests"
        backups_root.mkdir(parents=True, exist_ok=False)
        manifests_root.mkdir(parents=True, exist_ok=True)

        prepared: list[tuple[str, bytes, Path, str | None, Path | None]] = []
        for relative in sorted(staged.hashes):
            source = staged.root / relative
            data = source.read_bytes()
            if _sha256(data) != staged.hashes[relative]:
                raise WorkspaceViolation("staged artifact changed before promotion")
            target = self._project_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            reject_reparse_points(target.parent)
            before_hash: str | None = None
            backup: Path | None = None
            if target.exists():
                if not target.is_file():
                    raise WorkspaceViolation("declared artifact target is not a regular file")
                previous = target.read_bytes()
                before_hash = _sha256(previous)
                backup = backups_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                with backup.open("xb") as handle:
                    handle.write(previous)
                    handle.flush()
                    os.fsync(handle.fileno())
            prepared.append((relative, data, target, before_hash, backup))

        promoted: list[tuple[Path, Path | None]] = []
        try:
            for relative, data, target, _before_hash, backup in prepared:
                temporary = target.with_name(f".{target.name}.{task.task_id}.tmp")
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                reject_reparse_points(target.parent)
                reject_reparse_points(target)
                os.replace(temporary, target)
                promoted.append((target, backup))
                if _sha256(target.read_bytes()) != staged.hashes[relative]:
                    raise WorkspaceViolation("promoted artifact integrity verification failed")
        except Exception as exc:
            for target, backup in reversed(promoted):
                try:
                    if backup is None:
                        target.unlink(missing_ok=True)
                    else:
                        rollback = target.with_name(f".{target.name}.{task.task_id}.rollback")
                        rollback.write_bytes(backup.read_bytes())
                        os.replace(rollback, target)
                except OSError:
                    pass
            for relative, _data, target, _before_hash, _backup in prepared:
                target.with_name(f".{target.name}.{task.task_id}.tmp").unlink(missing_ok=True)
            raise WorkspaceViolation("artifact promotion failed and rollback was attempted") from exc

        manifest_items = [
            {
                "path": relative,
                "before_sha256": before_hash,
                "after_sha256": staged.hashes[relative],
            }
            for relative, _data, _target, before_hash, _backup in prepared
        ]
        manifest = {"task_id": task.task_id, "artifacts": manifest_items}
        manifest_path = manifests_root / f"{task.task_id}.json"
        with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return PromotionOutcome(
            promoted_paths=[target for target, _backup in promoted],
            manifest_path=manifest_path,
        )

    def rollback(self, task: TaskSpec, promotion: PromotionOutcome) -> None:
        manifest = json.loads(promotion.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("task_id") != task.task_id:
            raise WorkspaceViolation("rollback manifest task_id does not match")
        backups_root = self.runtime_dir / "backups" / task.task_id
        for item in reversed(manifest.get("artifacts", [])):
            relative = item["path"]
            target = self._project_path(relative)
            before_hash = item["before_sha256"]
            if before_hash is None:
                target.unlink(missing_ok=True)
                continue
            backup = backups_root / relative
            data = backup.read_bytes()
            if _sha256(data) != before_hash:
                raise WorkspaceViolation(f"rollback backup integrity failed: {relative}")
            temporary = target.with_name(f".{target.name}.{task.task_id}.rollback")
            temporary.write_bytes(data)
            os.replace(temporary, target)
            if _sha256(target.read_bytes()) != before_hash:
                raise WorkspaceViolation(f"rollback verification failed: {relative}")
