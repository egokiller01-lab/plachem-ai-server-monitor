from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import task_dispatch
from result_evidence import normalize_result


def run_sequence(
    task_packages: Iterable[dict[str, Any]],
    *,
    dispatch_kwargs: Mapping[str, Any],
    dispatcher: Callable[..., dict[str, Any]] = task_dispatch.dispatch,
) -> dict[str, Any]:
    """Run task packages sequentially and collect normalized Gateway results."""
    results: list[dict[str, Any]] = []
    sequence_status = "PASS"
    for package in task_packages:
        gateway_result = dispatcher(package, **dict(dispatch_kwargs))
        results.append(normalize_result(gateway_result))
        gateway_status = gateway_result.get("status")
        if gateway_status != "PASS":
            sequence_status = gateway_status
            break
    return {"status": sequence_status, "results": results}
