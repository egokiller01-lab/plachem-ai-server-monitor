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
        run_registry.create(
            task_id,
            package["project_id"],
            package["requested_worker"],
        )
        run_registry.transition(task_id, "DISPATCHING")
        run_registry.transition(task_id, "RUNNING")
        gateway_result = dispatcher(package, **dict(dispatch_kwargs))
        gateway_status = gateway_result.get("status")
        run_registry.transition(
            task_id,
            gateway_status,
            gateway_result=gateway_result,
            failure_reason=_failure_reason(gateway_result),
        )
        results.append(normalize_result(gateway_result))
        if gateway_status != "PASS":
            sequence_status = gateway_status
            break
    return {"status": sequence_status, "results": results}
