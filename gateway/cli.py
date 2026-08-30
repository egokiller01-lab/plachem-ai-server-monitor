from __future__ import annotations

import argparse
import json
import re
import sys
from functools import partial
from pathlib import Path

from gateway.achilles_runner import AchillesRunner
from gateway.approval import ApprovalStore
from gateway.audit import AuditLog
from gateway.context_pack import MAX_CONTEXT_BYTES, validate_authoritative_context
from gateway.models import TaskSpec
from gateway.path_security import reject_reparse_points, validate_pinned_file
from gateway.policy import PolicyDecision, PolicyEngine
from gateway.resource_guard import ResourceGuard, canonical_gpu_lock_path
from gateway.service import DelegationGateway
from gateway.system_probes import comfy_has_work, free_rtx3090_vram_mib, http_reachable
from gateway.local_validation import LocalValidationAdapter
from gateway.workspace import ScopedWorkspace


def _load_task(path: Path) -> TaskSpec:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    return TaskSpec.model_validate(payload)


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_task_id(path: Path) -> str | None:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return task_id if isinstance(task_id, str) and _SAFE_TASK_ID.fullmatch(task_id) else None


def _load_authoritative_context(task: TaskSpec, path: Path | None, project_root: Path) -> str:
    if path is None:
        return ""
    root = project_root.resolve()
    resolved = path.resolve()
    reject_reparse_points(path)
    if not resolved.is_relative_to(root):
        raise ValueError("context file is outside the project root")
    relative = resolved.relative_to(root).as_posix()
    allowed = {Path(item).as_posix() for item in task.scope.include}
    if relative not in allowed:
        raise ValueError(f"context file {relative!r} is not included in TaskSpec scope")
    if resolved.stat().st_size > MAX_CONTEXT_BYTES:
        raise ValueError("authoritative context exceeds the configured byte limit")
    raw = resolved.read_bytes()
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError("authoritative context exceeds the configured byte limit")
    context = raw.decode("utf-8")
    validate_authoritative_context(context)
    return context


def _validated_runtime(runtime: Path, policy: PolicyEngine) -> Path:
    expected = Path(policy.runtime_root)
    resolved = runtime.resolve()
    if str(resolved).casefold() != str(expected.resolve()).casefold():
        raise ValueError("--runtime must be the configured trusted runtime root")
    reject_reparse_points(expected)
    return expected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plachem-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("--task", type=Path, required=True)
        command.add_argument("--context", type=Path)
        command.add_argument("--runtime", type=Path, default=Path("runtime"))
    approve = subparsers.add_parser("approve")
    approve.add_argument("--task", type=Path, required=True)
    approve.add_argument("--runtime", type=Path, default=Path("runtime"))
    approve.add_argument("--approved-by", required=True)
    return parser


def _build_gateway(runtime: Path, policy: PolicyEngine) -> DelegationGateway:
    lock_path = Path(policy.lock_path)
    if lock_path != canonical_gpu_lock_path():
        raise ValueError("configured lock path is not canonical")
    guard = ResourceGuard(
        lock_path=lock_path,
        llm_reachable=partial(http_reachable, policy.model_endpoint + "/models"),
        comfy_running=comfy_has_work,
        free_vram_mib=partial(
            free_rtx3090_vram_mib,
            executable=policy.nvidia_smi_executable,
            attestor=partial(
                validate_pinned_file, Path(policy.nvidia_smi_executable),
                Path(policy.nvidia_smi_executable), policy.nvidia_smi_sha256,
            ),
        ),
        minimum_free_vram_mib=policy.minimum_free_vram_mib,
    )
    audit = AuditLog(runtime / "audit.jsonl")
    audit.prepare()
    return DelegationGateway(
        runtime_dir=runtime,
        runner=AchillesRunner(
            runtime, profile=policy.profile, toolset=policy.toolset, model=policy.model,
            executable=policy.hermes_executable,
            attestor=lambda: (
                validate_pinned_file(
                    Path(policy.hermes_executable), Path(policy.hermes_executable),
                    policy.hermes_sha256,
                ),
                validate_pinned_file(Path(policy.model), Path(policy.model), policy.model_sha256),
            ),
        ),
        guard=guard,
        audit=audit,
        policy=policy,
        workspace=ScopedWorkspace(Path.cwd(), runtime),
        validator=LocalValidationAdapter(),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime: Path | None = None
    safe_task_id = _safe_task_id(args.task)
    stage = "task_failure"
    task_payload: dict[str, object] = {"task_id": safe_task_id} if safe_task_id else {}
    try:
        task = _load_task(args.task)
        safe_task_id = task.task_id
        task_payload = task.model_dump(mode="json")
        stage = "config_failure"
        policy = PolicyEngine.default()
        runtime = _validated_runtime(args.runtime, policy)
        stage = "policy_failure"
        approvals = ApprovalStore(runtime)
        if args.command == "approve":
            if policy.evaluate(task) is not PolicyDecision.NEEDS_APPROVAL:
                raise PermissionError("task does not require an approval receipt")
            if policy.evaluate(task, approved=True) is not PolicyDecision.READY:
                raise PermissionError("task is not supported by an approved execution tier")
            receipt = approvals.approve(task, approved_by=args.approved_by)
            print(json.dumps({
                "status": "approved",
                "task_id": task.task_id,
                "task_sha256": receipt.task_sha256,
                "approved_by": receipt.approved_by,
            }, ensure_ascii=False))
            return 0
        decision = policy.evaluate(task, approved=approvals.is_approved(task))
        if args.command == "dry-run":
            print(json.dumps({"decision": decision.value, "task_id": task.task_id}))
            return 0
        stage = "context_failure"
        context = _load_authoritative_context(task, args.context, Path.cwd())
        stage = "gateway_build_failure"
        gateway = _build_gateway(runtime, policy)
        stage = "gateway"
        outcome = gateway.execute(
            task,
            authoritative_context=context,
            approved=approvals.is_approved(task),
        )
        print(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "worker_status": outcome.result.status.value,
                    "verification": outcome.verification.status.value,
                    "summary": outcome.result.summary,
                },
                ensure_ascii=False,
            )
        )
        return 0 if outcome.verification.status.value == "verified" else 2
    except Exception as exc:
        if safe_task_id and stage != "gateway" and runtime is not None:
            AuditLog(runtime / "audit.jsonl").append(
                safe_task_id,
                stage,
                task_payload,
                {"error_type": type(exc).__name__},
            )
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
