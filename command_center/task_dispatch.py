from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY_DIR = _ROOT / "plachem_fast_gateway"
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_DIR))

import fast_gateway
from agent_registry import AgentRegistry
from mock_auth_broker import load_task_authorization
from run_query import RunQuery
from workspace_registry import WorkspaceRegistry


_WORKSPACE_REGISTRY_PATH = Path(__file__).resolve().with_name("workspaces.json")


_REQUIRED_FIELDS = {
    "task_id",
    "original_instruction",
    "instruction_sha256",
    "requested_worker",
    "requested_actions",
    "created_at",
    "status",
}


def validate_task_package(package: dict[str, Any]) -> None:
    if not isinstance(package, dict) or not _REQUIRED_FIELDS.issubset(package):
        raise ValueError("invalid task package")
    instruction = package["original_instruction"]
    if not isinstance(instruction, str):
        raise ValueError("original_instruction must be a string")
    expected = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if package["instruction_sha256"] != expected:
        raise ValueError("INSTRUCTION_SHA256_MISMATCH")
    if not isinstance(package["task_id"], str) or not package["task_id"]:
        raise ValueError("task_id is required")
    if package["requested_worker"] is not None and (
        not isinstance(package["requested_worker"], str) or not package["requested_worker"]
    ):
        raise ValueError("requested_worker must be a non-empty string or None")
    if package["requested_worker"] is None and not package.get("preferred_worker") and not package.get("required_capabilities"):
        raise ValueError("worker selection hint is required")
    if not isinstance(package["requested_actions"], list) or not all(
        isinstance(action, str) for action in package["requested_actions"]
    ):
        raise ValueError("requested_actions must be a list of strings")
    if package["status"] != "CREATED":
        raise ValueError("task package is not CREATED")


def run_gateway(
    package: dict[str, Any],
    auth_path: Path,
    agents: dict[str, Any],
    policy_path: Path,
    project_root: Path,
    log_path: Path,
) -> dict[str, Any]:
    request = {
        "task_id": package["task_id"],
        "agent": package["requested_worker"],
        "task": package["original_instruction"],
        "workspace": package.get("workspace", "."),
        "requested_actions": list(package["requested_actions"]),
    }
    for identity_field in ("run_id", "correlation_id", "external_reference"):
        if identity_field in package:
            request[identity_field] = package[identity_field]
    policy = fast_gateway.merge_policy(policy_path)
    return fast_gateway.run(
        request,
        agents,
        policy,
        project_root.resolve(),
        log_path,
        auth_path.resolve(),
    )


def dispatch(
    package: dict[str, Any],
    auth_path: str | Path,
    agents_path: str | Path,
    policy_path: str | Path,
    project_root: str | Path,
    log_path: str | Path,
    run_registry_path: str | Path | None = None,
) -> dict[str, Any]:
    task_id = package.get("task_id", "") if isinstance(package, dict) else ""
    try:
        validate_task_package(package)
        workspace_registry = WorkspaceRegistry.load(_WORKSPACE_REGISTRY_PATH)
        workspace = workspace_registry.validate(
            package.get("workspace_id", package.get("project_id", "")),
            project_root,
        )
        registry = AgentRegistry.load(Path(agents_path))
        requested_worker = package["requested_worker"]
        if requested_worker is not None and requested_worker not in registry.gateway_agents():
            registry.resolve(requested_worker)
        dynamic_request = any(
            key in package for key in ("required_capabilities", "preferred_worker", "worker_selection_mode")
        )
        if dynamic_request:
            selection_task = dict(package)
            selection_task["preferred_worker"] = package.get("preferred_worker", requested_worker)
            selection = registry.select_worker(
                selection_task,
                RunQuery(Path(run_registry_path) if run_registry_path else Path("__missing_runs__.jsonl")),
            )
            selected_worker = selection.agent_id
        else:
            selected_worker = requested_worker
            registry.resolve(selected_worker)
        dispatch_package = dict(package)
        dispatch_package["requested_worker"] = selected_worker
        auth = load_task_authorization(
            Path(auth_path),
            package["task_id"],
            selected_worker,
            list(package["requested_actions"]),
            consume=False,
        )
        if not auth.get("broker_called"):
            raise ValueError("authorization broker was not called")
        return run_gateway(
            dispatch_package,
            Path(auth_path),
            registry.gateway_agents(),
            Path(policy_path),
            workspace.canonical_root,
            Path(log_path),
        )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return {
            "task_id": task_id,
            "status": "BLOCKED",
            "reason": str(exc),
            "worker_calls": 0,
        }
