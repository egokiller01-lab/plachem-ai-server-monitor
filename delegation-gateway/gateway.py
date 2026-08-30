#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent


RESULT_FIELDS = frozenset({
    "task_id", "status", "steps_used", "summary", "artifacts",
    "files_created", "files_modified", "functional_test", "scope_violation",
    "permission_violation", "repeated_failure", "remaining_issue", "gateway_used",
    "direct_delegation_attempted", "policy_applied", "final_result",
})


def parse_result_text(text: str, *, max_bytes: int) -> dict[str, Any]:
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Achilles result exceeds byte limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        result = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ValueError("Achilles did not return exactly one JSON object") from exc
    if not isinstance(result, dict):
        raise ValueError("Achilles result must be an object")
    return result


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _normalized_capability(value: str) -> str:
    return value.strip().lower().replace(".", "_").replace("-", "_")


def _bounded_limit(request: dict[str, Any], policy: dict[str, Any], name: str, default: int) -> int:
    value = request.get(name, default)
    maximum = policy[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    if value > maximum:
        raise ValueError(f"{name} exceeds policy maximum")
    return value


def build_taskspec(request: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    required = ("task_id", "objective", "scope", "completion")
    missing = [name for name in required if not request.get(name)]
    if missing:
        raise ValueError("missing request fields: " + ", ".join(missing))
    permissions = request.get("permission_package", policy["default_permission_package"])
    if not isinstance(permissions, list) or not permissions or not all(
        isinstance(item, str) and item.strip() for item in permissions
    ):
        raise ValueError("permission_package must be a non-empty string list")
    deny = sorted(set(policy["deny"] + request.get("deny", [])))
    normalized_denials = {_normalized_capability(item) for item in deny}
    for permission in permissions:
        normalized = _normalized_capability(permission)
        if any(normalized == denied or normalized.startswith(denied + "_") for denied in normalized_denials):
            raise ValueError(f"permission conflicts with deny: {permission}")
        if permission not in policy["allowed_permissions"]:
            raise ValueError(f"permission is not allowed: {permission}")
    limits = {
        "max_steps": _bounded_limit(request, policy, "max_steps", policy["max_steps"]),
        "max_retries": _bounded_limit(request, policy, "max_retries", policy["max_retries"]),
        "max_files": _bounded_limit(request, policy, "max_files", policy["max_files"]),
        "timeout_seconds": _bounded_limit(
            request, policy, "timeout_seconds", policy["timeout_seconds"]
        ),
        "max_response_bytes": policy["max_response_bytes"],
    }
    validation = request.get("validation", {"required_text": [], "forbidden_text": []})
    scope = request["scope"]
    if not isinstance(scope, dict) or not isinstance(scope.get("include"), list):
        raise ValueError("scope.include must be a list")
    if not all(isinstance(item, str) and item.strip() for item in scope["include"]):
        raise ValueError("scope.include must contain non-empty paths")
    if len(scope["include"]) > limits["max_files"]:
        raise ValueError("scope exceeds max_files")
    requirements_text = "\n".join(
        [str(request["objective"])] + [str(item) for item in request["completion"]]
    )
    if "../" in requirements_text or "..\\" in requirements_text:
        raise ValueError("path traversal requested outside scope")
    if not isinstance(validation, dict):
        raise ValueError("validation must be an object")
    for name in ("required_text", "forbidden_text"):
        values = validation.get(name, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"validation.{name} must be a string list")
    return {
        "task_id": request["task_id"],
        "delegator": "odyssey",
        "worker": "achilles",
        "objective": request["objective"],
        "scope": request["scope"],
        "permission_package": permissions,
        "limits": limits,
        "deny": deny,
        "work_sequence": request.get("work_sequence", []),
        "stopping_conditions": request.get("stopping_conditions", []),
        "completion": request["completion"],
        "validation": validation,
        "result_contract": {
            "task_id": request["task_id"],
            "status": "completed|blocked|failed",
            "steps_used": "integer",
            "summary": "short factual summary",
            "artifacts": [{"path": "allowed relative path", "content": "complete file content"}],
            "files_created": "list of artifact paths",
            "files_modified": "list of existing artifact paths",
            "functional_test": {"named_check": "PASS|FAIL"},
            "scope_violation": "NO|YES",
            "permission_violation": "NO|YES",
            "repeated_failure": "NO|YES",
            "remaining_issue": "string",
            "gateway_used": "YES",
            "direct_delegation_attempted": "NO",
            "policy_applied": "PASS|FAIL",
            "final_result": "PASS|FAIL"
        },
    }


class RetryExhausted(RuntimeError):
    def __init__(
        self, attempts: list[dict[str, Any]], *, stop_reason: str = "RETRY_LIMIT"
    ) -> None:
        super().__init__(f"Achilles stopped: {stop_reason}")
        self.attempts = attempts
        self.stop_reason = stop_reason


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "json" in message:
        return "INVALID_JSON"
    if "byte limit" in message:
        return "OUTPUT_LIMIT"
    return type(exc).__name__.upper()


def execute_with_retries(
    operation: Callable[[], dict[str, Any]], max_retries: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    previous_failure: tuple[str, str] | None = None
    for attempt_number in range(1, max_retries + 2):
        started_at = _utc_now()
        try:
            result = operation()
            attempts.append({
                "attempt": attempt_number,
                "started_at": started_at,
                "ended_at": _utc_now(),
                "status": "PASS",
                "error_code": None,
                "retry": False,
                "final_success": True,
            })
            return result, attempts
        except Exception as exc:
            code = _error_code(exc)
            fingerprint = (code, str(exc))
            repeated = previous_failure == fingerprint
            has_retry = attempt_number < max_retries + 1 and not repeated
            attempts.append({
                "attempt": attempt_number,
                "started_at": started_at,
                "ended_at": _utc_now(),
                "status": "FAIL",
                "error_code": code,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "retry": has_retry,
                "final_success": False,
            })
            if repeated:
                raise RetryExhausted(
                    attempts, stop_reason="REPEATED_FAILURE"
                ) from exc
            if not has_retry:
                raise RetryExhausted(attempts, stop_reason="RETRY_LIMIT") from exc
            previous_failure = fingerprint
    raise RetryExhausted(attempts, stop_reason="RETRY_LIMIT")


def call_achilles(spec: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "/no_think\n"
        "You are Achilles, a bounded worker called only by the PLACHEM Test "
        "Delegation Gateway. Execute the TaskSpec by generating complete file contents "
        "inside the artifacts array. Do not access the filesystem directly and do not "
        "use tools; the Gateway alone will validate and write approved artifacts. Return "
        "exactly one JSON object matching result_contract. Do not use markdown fences. "
        "Use final_result PASS only when every requested artifact satisfies the TaskSpec "
        "completion, validation, scope, permissions, sequence, and limits.\n\nTASKSPEC\n"
        + json.dumps(spec, ensure_ascii=False, indent=2)
    )
    payload = {
        "model": agent["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 7000,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        agent["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=int(spec["limits"]["timeout_seconds"])) as response:
        raw_body = response.read(spec["limits"]["max_response_bytes"] + 1)
    if len(raw_body) > spec["limits"]["max_response_bytes"]:
        raise ValueError("Achilles HTTP response exceeds byte limit")
    body = json.loads(raw_body.decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("Achilles result content must be text")
    return parse_result_text(content.strip(), max_bytes=spec["limits"]["max_response_bytes"])


def verify(spec: dict[str, Any], result: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if set(result) != RESULT_FIELDS:
        failures.append("result_fields")
    if not isinstance(result.get("summary"), str) or not result.get("summary", "").strip():
        failures.append("summary")
    if not isinstance(result.get("remaining_issue"), str):
        failures.append("remaining_issue")
    artifacts = result.get("artifacts")
    artifact_map: dict[str, str] = {}
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                failures.append("artifact_shape")
                continue
            path, content = item.get("path"), item.get("content")
            if not isinstance(path, str) or not isinstance(content, str) or not content.strip():
                failures.append("artifact_shape")
                continue
            if path in artifact_map:
                failures.append("duplicate_artifact")
            artifact_map[path] = content
    else:
        failures.append("artifacts")

    allowed = set(spec["scope"]["include"])
    if set(artifact_map) != allowed:
        failures.append("artifact_scope")
    files_created = result.get("files_created")
    files_modified = result.get("files_modified")
    if not isinstance(files_created, list) or not isinstance(files_modified, list):
        failures.append("file_lists")
    elif (
        not all(isinstance(item, str) for item in files_created + files_modified)
        or set(files_created) & set(files_modified)
        or set(files_created) | set(files_modified) != set(artifact_map)
    ):
        failures.append("file_lists")
    functional_test = result.get("functional_test")
    if not isinstance(functional_test, dict) or not all(
        isinstance(name, str) and value in {"PASS", "FAIL"}
        for name, value in functional_test.items()
    ):
        failures.append("functional_test")
    if len(artifact_map) > spec["limits"]["max_files"]:
        failures.append("max_files")
    combined = "\n".join(artifact_map.values())
    combined_folded = combined.casefold()
    validation = spec.get("validation", {})
    required_text = validation.get("required_text", [])
    forbidden_text = validation.get("forbidden_text", [])
    if any(text.casefold() not in combined_folded for text in required_text):
        failures.append("required_text")
    if any(text.casefold() in combined_folded for text in forbidden_text):
        failures.append("forbidden_text")

    checks = {
        "task_id": result.get("task_id") == spec["task_id"],
        "status": result.get("status") == "completed",
        "steps_used": isinstance(result.get("steps_used"), int)
        and 0 < result["steps_used"] <= spec["limits"]["max_steps"],
        "gateway_used": result.get("gateway_used") == "YES",
        "direct_delegation": result.get("direct_delegation_attempted") == "NO",
        "policy": result.get("policy_applied") == "PASS",
        "scope_violation": result.get("scope_violation") == "NO",
        "permission_violation": result.get("permission_violation") == "NO",
        "repeated_failure": result.get("repeated_failure") == "NO",
        "final_result": result.get("final_result") == "PASS",
    }
    failures.extend(name for name, passed in checks.items() if not passed)
    return not failures, sorted(set(failures))


class ArtifactApplyError(RuntimeError):
    def __init__(self, message: str, *, rollback_started: bool, rollback_success: bool) -> None:
        super().__init__(message)
        self.rollback_started = rollback_started
        self.rollback_success = rollback_success


def apply_artifacts(
    spec: dict[str, Any],
    result: dict[str, Any],
    *,
    project_root: Path | None = None,
    replace_file: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> list[str]:
    root = (project_root or ROOT.parent).resolve()
    allowed = set(spec["scope"]["include"])
    prepared: list[tuple[str, Path, str]] = []
    originals: dict[str, bytes | None] = {}

    for artifact in result["artifacts"]:
        relative = artifact["path"].replace("\\", "/")
        if relative not in allowed or relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError(f"artifact path is outside TaskSpec scope: {relative}")
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"artifact path escapes project root: {relative}")
        originals[relative] = target.read_bytes() if target.is_file() else None
        prepared.append((relative, target, artifact["content"]))

    written: list[str] = []
    with tempfile.TemporaryDirectory(prefix="delegation-gateway-stage-") as temp_name:
        stage_root = Path(temp_name)
        staged: dict[str, Path] = {}
        for relative, _target, content in prepared:
            stage_file = stage_root / "new" / relative
            stage_file.parent.mkdir(parents=True, exist_ok=True)
            stage_file.write_text(content, encoding="utf-8", newline="\n")
            staged[relative] = stage_file

        try:
            for relative, target, _content in prepared:
                target.parent.mkdir(parents=True, exist_ok=True)
                replace_file(staged[relative], target)
                written.append(relative)
        except Exception as exc:
            rollback_success = True
            for relative, target, _content in reversed(prepared):
                try:
                    original = originals[relative]
                    if original is None:
                        if target.exists():
                            target.unlink()
                    else:
                        rollback_file = stage_root / "rollback" / relative
                        rollback_file.parent.mkdir(parents=True, exist_ok=True)
                        rollback_file.write_bytes(original)
                        os.replace(rollback_file, target)
                except Exception:
                    rollback_success = False
            raise ArtifactApplyError(
                f"artifact apply failed: {type(exc).__name__}",
                rollback_started=True,
                rollback_success=rollback_success,
            ) from exc
    return written


def validate_and_apply(
    spec: dict[str, Any],
    result: dict[str, Any],
    *,
    apply_fn: Callable[[dict[str, Any], dict[str, Any]], list[str]] = apply_artifacts,
) -> tuple[bool, list[str], list[str]]:
    passed, failures = verify(spec, result)
    if not passed:
        return False, failures, []
    try:
        written = apply_fn(spec, result)
    except ArtifactApplyError as exc:
        failure = "apply_failure_rollback_pass" if exc.rollback_success else "apply_failure_rollback_fail"
        return False, [failure], []
    return True, [], written


def verify_written_files(spec: dict[str, Any]) -> list[str]:
    project_root = ROOT.parent.resolve()
    failures: list[str] = []
    for relative in spec["scope"]["include"]:
        target = project_root / relative
        if not target.is_file() or not target.read_text(encoding="utf-8").strip():
            failures.append(f"missing_or_empty:{relative}")
    return failures


def append_log(record: dict[str, Any]) -> None:
    with (ROOT / "runs.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PLACHEM Delegation Gateway V1")
    parser.add_argument("--task", type=Path, default=ROOT / "demo_task.json")
    args = parser.parse_args(argv)
    started = time.monotonic()
    request: dict[str, Any] = {}
    spec: dict[str, Any] = {}
    result: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    try:
        agents = load_json(ROOT / "agents.yaml")
        policy = load_json(ROOT / "policy.yaml")
        request = load_json(args.task)
        spec = build_taskspec(request, policy)
        print("PROCESSED TASKSPEC")
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        try:
            result, attempts = execute_with_retries(
                lambda: call_achilles(spec, agents["achilles"]),
                max_retries=spec["limits"]["max_retries"],
            )
        except RetryExhausted as exc:
            attempts = exc.attempts
            raise
        passed, failures, written_files = validate_and_apply(spec, result)
        if passed:
            failures.extend(verify_written_files(spec))
            passed = not failures
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": spec["task_id"],
            "route": "odyssey->gateway->achilles->gateway->odyssey",
            "taskspec": spec,
            "achilles_result": result,
            "attempts": attempts,
            "written_files": written_files,
            "verification_failures": failures,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "status": "PASS" if passed else "FAIL",
        }
        append_log(record)
        print("ACHILLES RESULT")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("GATEWAY RESULT", record["status"])
        return 0 if passed else 2
    except Exception as exc:
        append_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": spec.get("task_id") or request.get("task_id") or "unknown",
            "route": "odyssey->gateway->achilles",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "attempts": attempts,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "status": "FAIL",
        })
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
