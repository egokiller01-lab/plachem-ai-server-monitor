from __future__ import annotations

import json
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gateway.audit import AuditLog
from gateway.achilles_runner import RunnerParseError
from gateway.context_pack import build_context_pack, validate_authoritative_context
from gateway.local_validation import LocalValidationAdapter
from gateway.models import TaskSpec, WorkerResult
from gateway.path_security import reject_reparse_points
from gateway.policy import PolicyDecision, PolicyEngine
from gateway.verifier import Verification, VerificationStatus, verify_result
from gateway.workspace import ScopedWorkspace, StagedWorkspace


class Runner(Protocol):
    def run(self, task: TaskSpec, context_pack: str) -> WorkerResult: ...


class Guard(Protocol):
    def __enter__(self): ...
    def __exit__(self, *args: object): ...


@dataclass(frozen=True)
class GatewayOutcome:
    result: WorkerResult
    verification: Verification
    validation_checks: tuple[str, ...] = ()
    manifest_path: Path | None = None


class DelegationGateway:
    def __init__(
        self,
        runtime_dir: Path,
        runner: Runner,
        guard: Guard,
        audit: AuditLog,
        policy: PolicyEngine | None = None,
        workspace: ScopedWorkspace | None = None,
        validator: LocalValidationAdapter | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.runner = runner
        self.guard = guard
        self.audit = audit
        self.policy = policy or PolicyEngine.default()
        self.workspace = workspace
        self.validator = validator

    def execute(
        self,
        task: TaskSpec,
        authoritative_context: str = "",
        *,
        approved: bool = False,
    ) -> GatewayOutcome:
        task_payload = task.model_dump(mode="json")
        stage = "policy_failure"
        result_payload: dict[str, object] = {}
        audited = False
        try:
            decision = self.policy.evaluate(task, approved=approved)
            if decision is not PolicyDecision.READY:
                raise PermissionError(f"task is not ready: {decision.value}")
            scoped_write = "workspace_write_scoped" in task.permissions
            stage = "context_failure"
            validate_authoritative_context(authoritative_context)
            if not scoped_write and not authoritative_context.strip():
                raise ValueError("authoritative context is required for read-only execution")

            stage = "preflight_failure"
            self._preflight_artifacts(task.task_id)
            self._write_json("tasks", task.task_id, task_payload)
            baseline = None
            if scoped_write:
                if self.workspace is None or self.validator is None:
                    raise RuntimeError("scoped workspace execution is not configured")
                stage = "baseline_failure"
                baseline = self.workspace.create_baseline(task)
                stage = "context_failure"
                source_context = self.workspace.build_source_context(task)
                authoritative_context = (
                    authoritative_context + "\n\n" + source_context
                    if authoritative_context.strip() else source_context
                )
            context_pack = build_context_pack(task, authoritative_context)

            stage = "resource_failure"
            validation_checks: tuple[str, ...] = ()
            manifest_path: Path | None = None
            audit_payload: dict[str, object] = result_payload
            with self.guard:
                try:
                    stage = "runner_failure"
                    try:
                        result = self.runner.run(task, context_pack)
                    except RunnerParseError:
                        stage = "parse_failure"
                        raise
                    result_payload = result.model_dump(mode="json")
                    audit_payload = result_payload
                    stage = "result_write_failure"
                    self._write_json("results", task.task_id, result_payload)
                    stage = "verification_failure"
                    verification = verify_result(task, result, authoritative_context)

                    if scoped_write and verification.status is VerificationStatus.VERIFIED:
                        stage = "protected_baseline_failure"
                        self.workspace.verify_protected_unchanged(task, baseline)
                        stage = "workspace_stage_failure"
                        staged = self.workspace.stage(task, result)
                        stage = "local_validation_failure"
                        report = self.validator.validate(task, staged)
                        validation_checks = tuple(report.checks)
                        stage = "workspace_promotion_failure"
                        promotion = self.workspace.promote(task, staged)
                        manifest_path = promotion.manifest_path
                        stage = "post_validation_failure"
                        try:
                            applied = StagedWorkspace(
                                root=self.workspace.project_root,
                                hashes=staged.hashes,
                            )
                            post_report = self.validator.validate(task, applied)
                            validation_checks = validation_checks + tuple(
                                f"post-apply: {check}" for check in post_report.checks
                            )
                        except Exception:
                            self.workspace.rollback(task, promotion)
                            raise
                        manifest_sha256 = hashlib.sha256(
                            manifest_path.read_bytes()
                        ).hexdigest()
                        audit_payload = {
                            "worker_result": result_payload,
                            "validation_checks": list(validation_checks),
                            "manifest_sha256": manifest_sha256,
                        }
                except Exception as exc:
                    self.audit.append(
                        task.task_id, stage, task_payload,
                        audit_payload or {"error_type": type(exc).__name__},
                    )
                    audited = True
                    raise
                self.audit.append(
                    task.task_id, verification.status.value, task_payload, audit_payload
                )
                audited = True
            return GatewayOutcome(
                result=result,
                verification=verification,
                validation_checks=validation_checks,
                manifest_path=manifest_path,
            )
        except Exception as exc:
            if not audited:
                self.audit.append(
                    task.task_id,
                    stage,
                    task_payload,
                    result_payload or {"error_type": type(exc).__name__},
                )
            raise

    def _artifact_paths(self, task_id: str) -> tuple[Path, Path, Path]:
        return (
            self.runtime_dir / "tasks" / f"{task_id}.json",
            self.runtime_dir / "tasks" / f"{task_id}.md",
            self.runtime_dir / "results" / f"{task_id}.json",
            self.runtime_dir / "baselines" / f"{task_id}.json",
        )

    def _preflight_artifacts(self, task_id: str) -> None:
        reject_reparse_points(self.runtime_dir)
        reject_reparse_points(self.runtime_dir / "tasks")
        reject_reparse_points(self.runtime_dir / "results")
        for path in self._artifact_paths(task_id):
            if os.path.lexists(path):
                raise FileExistsError(f"task_id already exists: {task_id}")

    def _write_json(self, folder: str, task_id: str, payload: dict[str, object]) -> None:
        path = self.runtime_dir / folder / f"{task_id}.json"
        reject_reparse_points(path.parent)
        reject_reparse_points(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_points(path.parent)
        try:
            serialized = json.dumps(payload, ensure_ascii=False, indent=2)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(serialized)
            reject_reparse_points(path)
            readback = path.read_text(encoding="utf-8")
            decoded = json.loads(readback)
            canonical_expected = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            canonical_actual = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if (
                readback != serialized
                or hashlib.sha256(canonical_actual.encode("utf-8")).digest()
                != hashlib.sha256(canonical_expected.encode("utf-8")).digest()
            ):
                raise OSError("artifact integrity verification failed")
            path.chmod(stat.S_IREAD)
        except (FileExistsError, IsADirectoryError, PermissionError) as exc:
            raise FileExistsError(f"task_id already exists: {task_id}") from exc
