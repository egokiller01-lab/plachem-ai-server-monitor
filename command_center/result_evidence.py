from __future__ import annotations

from typing import Any


_FIELDS = (
    "task_id",
    "worker",
    "requested_actions",
    "gateway_result",
    "worker_attempts",
    "result_type",
    "artifacts",
    "files_modified",
    "authorization_consumed",
    "execution_evidence",
    "validation_result",
    "failure_reason",
    "completed_at",
)


def _present(mapping: dict[str, Any], key: str) -> tuple[bool, Any]:
    return key in mapping, mapping.get(key)


def normalize_result(gateway_result: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Gateway result without re-evaluating or mutating it."""
    if not isinstance(gateway_result, dict):
        raise TypeError("gateway_result must be a dictionary")

    auth = gateway_result.get("auth")
    auth = auth if isinstance(auth, dict) else {}
    result = gateway_result.get("result")
    result = result if isinstance(result, dict) else {}

    has_actions, actions = _present(gateway_result, "requested_actions")
    if not has_actions and "requested" in auth:
        actions = auth.get("requested")

    has_attempts, attempts = _present(gateway_result, "attempts")
    worker_attempts = len(attempts) if has_attempts and isinstance(attempts, list) else None

    has_type, result_type = _present(gateway_result, "result_type")
    if not has_type:
        result_type = gateway_result.get("worker_result_type")
        if result_type is None:
            result_type = result.get("result_type") if "result_type" in result else None

    has_artifacts, artifacts = _present(gateway_result, "artifacts")
    if not has_artifacts:
        artifacts = result.get("artifacts") if "artifacts" in result else None

    has_files, files_modified = _present(gateway_result, "files_modified")
    if not has_files:
        files_modified = result.get("changes") if "changes" in result else None

    has_validation, validation_result = _present(gateway_result, "validation_result")
    if not has_validation:
        validation_result = result.get("validation_result") if "validation_result" in result else None

    has_failure, failure_reason = _present(gateway_result, "failure_reason")
    if not has_failure:
        failure_reason = result.get("reason") if "reason" in result else None

    normalized = {
        "task_id": gateway_result.get("task_id"),
        "worker": gateway_result.get("worker", gateway_result.get("agent")),
        "requested_actions": actions,
        "gateway_result": gateway_result.get("status"),
        "worker_attempts": worker_attempts,
        "result_type": result_type,
        "artifacts": artifacts,
        "files_modified": files_modified,
        "authorization_consumed": gateway_result.get("authorization_consumed"),
        "execution_evidence": gateway_result.get("execution_evidence"),
        "validation_result": validation_result,
        "failure_reason": failure_reason,
        "completed_at": gateway_result.get("completed_at", gateway_result.get("timestamp")),
    }
    for identity_field in ("run_id", "correlation_id", "external_reference"):
        if identity_field in gateway_result:
            normalized[identity_field] = gateway_result[identity_field]
    return normalized
