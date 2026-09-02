from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import task_dispatch
from result_evidence import normalize_result
from run_registry import RunRegistry


def _failure_reason(gateway_result: dict[str, Any]) -> str:
    if "failure_reason" in gateway_result:
        return str(gateway_result.get("failure_reason") or "")
    if "reason" in gateway_result:
        return str(gateway_result.get("reason") or "")
    result = gateway_result.get("result")
    if isinstance(result, dict):
        return str(result.get("reason") or "")
    return ""


def run_sequence(
    task_packages: Iterable[dict[str, Any]],
    *,
    dispatch_kwargs: Mapping[str, Any],
    run_registry: RunRegistry,
    dispatcher: Callable[..., dict[str, Any]] = task_dispatch.dispatch,
) -> dict[str, Any]:
    """Run task packages sequentially and collect normalized Gateway results."""
    results: list[dict[str, Any]] = []
    sequence_status = "PASS"
    for package in task_packages:
        task_id = package["task_id"]
        run = run_registry.create(
            task_id,
            package.get("workspace_id", package.get("project_id", "")),
            package["requested_worker"],
            correlation_id=package.get("correlation_id"),
            workspace_id=package.get("workspace_id", package.get("project_id", "")),
            external_reference=package.get("external_reference"),
        )
        run_id = run["run_id"]
        correlation_id = run["correlation_id"]
        run_registry.transition(run_id, "DISPATCHING")
        run_registry.transition(run_id, "RUNNING")
        identity_enabled = "correlation_id" in package or "external_reference" in package
        dispatch_package = dict(package)
        if identity_enabled:
            dispatch_package.update(
                {
                    "run_id": run_id,
                    "correlation_id": correlation_id,
                }
            )
        gateway_result = dispatcher(dispatch_package, **dict(dispatch_kwargs))
        gateway_result = dict(gateway_result)
        if identity_enabled:
            gateway_result.update(
                {
                    "run_id": run_id,
                    "correlation_id": correlation_id,
                }
            )
            if "external_reference" in package:
                gateway_result["external_reference"] = package["external_reference"]
        gateway_status = gateway_result.get("status")
        run_registry.transition(
            run_id,
            gateway_status,
            gateway_result=gateway_result,
            failure_reason=_failure_reason(gateway_result),
        )
        results.append(normalize_result(gateway_result))
        if gateway_status != "PASS":
            sequence_status = gateway_status
            break
    return {"status": sequence_status, "results": results}
