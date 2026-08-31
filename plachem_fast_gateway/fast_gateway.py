#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mock_auth_broker import consume_task_authorization, load_task_authorization

ROOT = Path(__file__).resolve().parent

DEFAULT_POLICY = {
    "max_context_files": 20,
    "max_context_bytes": 250_000,
    "max_artifacts": 8,
    "max_file_bytes": 250_000,
    "max_total_artifact_bytes": 1_000_000,
    "max_retries": 1,
    "timeout_seconds": 300,
    "total_timeout_seconds": 420,
    "exclude_dirs": [".git", ".venv", "venv", "node_modules", "__pycache__", "runtime", "logs"],
    "exclude_suffixes": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".7z", ".exe", ".dll", ".bin", ".gguf"],
    "blocked_actions": ["git push", "github pr", "merge", "deploy", "production", "remote modify", "reset --hard", "git clean"],
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return obj


def save_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def merge_policy(path: Path | None) -> dict[str, Any]:
    p = dict(DEFAULT_POLICY)
    if path and path.exists():
        p.update(load_json(path))
    return p


def normalize_workspace(project_root: Path, workspace_value: str) -> Path:
    workspace = (project_root / workspace_value).resolve()
    if not workspace.is_relative_to(project_root):
        raise ValueError("workspace escapes project root")
    if not workspace.exists() or not workspace.is_dir():
        raise ValueError(f"workspace not found: {workspace}")
    return workspace


def detect_explicit_blocked_action(task: str, blocked: list[str]) -> str | None:
    # Only blocks affirmative/imperative-looking requests. Phrases such as
    # "do not git push" or "git push 금지" are treated as restrictions, not requests.
    low = task.lower()
    negators = (
        "do not ", "don't ", "must not ", "without ",
        "금지", "하지 마", "하지마", "건드리지 마", "건드리지마", "금한다",
    )
    for action in blocked:
        idx = low.find(action.lower())
        if idx < 0:
            continue
        window = low[max(0, idx - 24): idx + len(action) + 24]
        if any(n in window for n in negators):
            continue
        return action
    return None


def load_mock_authorization(
    config_path: Path,
    task_id: str,
    worker: str | None = None,
    requested_actions: list[str] | None = None,
) -> dict[str, Any]:
    return load_task_authorization(
        config_path,
        task_id,
        worker,
        requested_actions,
        consume=False,
    )


def _affirmative_phrase(task: str, phrase: str) -> bool:
    low = task.lower()
    phrase_low = phrase.lower()
    start = 0
    negators = (
        "do not ", "don't ", "must not ", "without ", "not authorized",
        "금지", "하지 마", "하지마", "건드리지 마", "건드리지마", "금한다", "없으므로",
    )
    while True:
        idx = low.find(phrase_low, start)
        if idx < 0:
            return False
        window = low[max(0, idx - 32): idx + len(phrase_low) + 32]
        if not any(n in window for n in negators):
            return True
        start = idx + len(phrase_low)


def detect_requested_actions(task: str) -> list[str]:
    patterns = [
        (
            "read_only_review",
            (
                "read-only review",
                "read-only code review",
                "read_only_review",
                "code_review",
                "읽기 전용 검토",
            ),
        ),
        ("git_commit", ("git commit",)),
        ("git_push", ("git push",)),
        ("production_migration", ("production migration", "production 마이그레이션")),
        ("business_data_change", ("업무 데이터 변경", "business data change")),
        ("production_deploy", ("production deploy", "production 배포", "production에 배포")),
    ]
    requested: list[str] = []
    for action, phrases in patterns:
        if any(_affirmative_phrase(task, phrase) for phrase in phrases):
            requested.append(action)
    return requested


def authorize_requested_actions(
    task: str,
    blocked_actions: list[str],
    authorization: dict[str, Any] | None,
) -> dict[str, Any]:
    requested = detect_requested_actions(task)
    if authorization is None:
        if "read_only_review" in requested:
            return {
                "allowed": False,
                "reason": "AUTH_REQUIRED_ACTION:read_only_review",
                "requested": requested,
            }
        blocked = detect_explicit_blocked_action(task, blocked_actions)
        if blocked:
            return {"allowed": False, "reason": f"BLOCKED_ACTION:{blocked}", "requested": requested}
        if "git_commit" in requested:
            return {"allowed": False, "reason": "BLOCKED_ACTION:git commit", "requested": requested}
        return {"allowed": True, "reason": "", "requested": requested}

    allowed = set(authorization.get("allow", []))
    denied = set(authorization.get("deny", []))
    for action in requested:
        if action in denied or action not in allowed:
            return {"allowed": False, "reason": f"UNAUTHORIZED_ACTION:{action}", "requested": requested}
    return {"allowed": True, "reason": "", "requested": requested}


def _run_git(args: list[str], project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def execute_authorized_git_actions(
    project_root: Path,
    workspace_value: str,
    changes: list[dict[str, Any]],
    task_id: str,
    requested_actions: list[str],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {"commit": False, "push": False, "commit_sha": "", "push_ref": ""}
    wants_commit = "git_commit" in requested_actions
    wants_push = "git_push" in requested_actions
    if not wants_commit and not wants_push:
        return result

    push_target: Path | None = None
    push_ref = ""
    if wants_push:
        if "git_push" not in authorization.get("allow", []):
            raise ValueError("git_push is not authorized")
        target_value = authorization.get("git_push_target")
        push_ref = str(authorization.get("git_push_ref") or "")
        if not isinstance(target_value, str) or not target_value:
            raise ValueError("authorized local git push target is required")
        if "://" in target_value or target_value.startswith("git@"):
            raise ValueError("auth broker permits local git push targets only")
        push_target = Path(target_value)
        if not push_target.is_absolute():
            push_target = (project_root / push_target).resolve()
        if not push_target.is_dir() or not (push_target / "HEAD").is_file():
            raise ValueError("authorized local git push target is not a bare repository")
        if not push_ref.startswith("refs/heads/test10-"):
            raise ValueError("authorized git push ref must use refs/heads/test10-")

    if wants_commit:
        if "git_commit" not in authorization.get("allow", []):
            raise ValueError("git_commit is not authorized")
        if not changes:
            raise ValueError("git commit requires applied changes")
        workspace = normalize_workspace(project_root, workspace_value)
        paths: list[str] = []
        for change in changes:
            rel = Path(str(change["path"]))
            target = (workspace / rel).resolve()
            if not target.is_relative_to(workspace):
                raise ValueError("git path escapes workspace")
            paths.append(target.relative_to(project_root).as_posix())
        _run_git(["add", "--", *paths], project_root)
        safe_task_id = re.sub(r"[^A-Za-z0-9._-]", "-", task_id)[:128]
        _run_git(["commit", "-m", f"test: {safe_task_id}", "--", *paths], project_root)
        result["commit"] = True
        result["commit_sha"] = _run_git(["rev-parse", "HEAD"], project_root).stdout.strip()
    elif wants_push:
        if changes:
            raise ValueError("push-only action cannot include artifacts")
        commit_sha = str(authorization.get("git_push_commit") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha):
            raise ValueError("push-only action requires an authorized 40-character commit SHA")
        _run_git(["cat-file", "-e", f"{commit_sha}^{{commit}}"], project_root)
        result["commit_sha"] = commit_sha.lower()

    if wants_push:
        if push_target is None:
            raise ValueError("authorized local git push target is required")
        _run_git(["push", str(push_target), f"{result['commit_sha']}:{push_ref}"], project_root)
        result["push"] = True
        result["push_ref"] = push_ref
    return result


def collect_context(workspace: Path, policy: dict[str, Any]) -> list[dict[str, Any]]:
    max_files = int(policy["max_context_files"])
    max_bytes = int(policy["max_context_bytes"])
    excluded_dirs = set(policy["exclude_dirs"])
    excluded_suffixes = {s.lower() for s in policy["exclude_suffixes"]}

    items: list[dict[str, Any]] = []
    total = 0
    for path in sorted(workspace.rglob("*")):
        if len(items) >= max_files or total >= max_bytes:
            break
        if not path.is_file():
            continue
        rel_parts = path.relative_to(workspace).parts
        if any(part in excluded_dirs for part in rel_parts):
            continue
        if path.suffix.lower() in excluded_suffixes:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        remaining = max_bytes - total
        if remaining <= 0:
            break
        data = data[:remaining]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        total += len(data)
        items.append({
            "path": path.relative_to(workspace).as_posix(),
            "sha256": sha256_bytes(data),
            "content": text,
        })
    return items


def build_worker_prompt(
    task: str,
    workspace_name: str,
    context: list[dict[str, Any]],
    policy: dict[str, Any],
    authorization: dict[str, Any] | None = None,
    requested_actions: list[str] | None = None,
) -> str:
    contract = {
        "result_type": "artifact|action_only|read_only",
        "status": "completed|blocked|failed",
        "summary": "short factual summary",
        "artifacts": [{"path": "path relative to workspace", "content": "complete file content"}],
        "reason": "empty when completed; concise reason otherwise",
        "review_result": "PASS|FAIL; required only for completed read_only results",
        "findings": ["finding strings; required only for completed read_only results"],
    }
    authorized_gateway_actions = sorted(
        set(requested_actions or []) & set((authorization or {}).get("allow", []))
    )
    forbidden = list(policy["blocked_actions"])
    if "git_push" in authorized_gateway_actions:
        forbidden = [x for x in forbidden if x.lower() != "git push"]
    worker_packet = {
        "task": task,
        "workspace": workspace_name,
        "rules": {
            "filesystem_access": "none; return artifacts only",
            "allowed_scope": "files inside the supplied workspace only",
            "forbidden": forbidden,
            "authorized_gateway_actions": authorized_gateway_actions,
            "git_execution": "The Worker only returns artifacts; the Gateway performs any authorized commit/push after validation.",
            "preserve_existing_behavior": True,
            "do_not_expand_scope": True,
        },
        "context_files": context,
        "result_contract": contract,
    }
    return (
        "/no_think\n"
        "You are Achilles, a bounded implementation worker. The Gateway already handled policy. "
        "Do the requested work using only the supplied context. Do not use tools, filesystem, git, network, "
        "or external code. Return exactly one JSON object and nothing else. "
        "For every file you change or create, return its COMPLETE contents in artifacts. "
        "Artifact paths are relative to the workspace, never absolute and never use '..'. "
        "If the task cannot be completed within the supplied workspace/context, return status='blocked'.\n\n"
        + json.dumps(worker_packet, ensure_ascii=False)
    )


def call_worker(agent: dict[str, Any], prompt: str, timeout_seconds: int) -> dict[str, Any]:
    payload = {
        "model": agent["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": int(agent.get("max_tokens", 8000)),
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        agent["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"].strip()
    # Strict: whole response must be exactly one JSON object.
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("worker result must be one JSON object")
    return result


def validate_result(workspace: Path, result: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, list[str], list[dict[str, Any]]]:
    failures: list[str] = []
    result_type = result.get("result_type", "artifact")
    if result_type not in {"artifact", "action_only", "read_only"}:
        failures.append("invalid_result_type")
    status = result.get("status")
    if status not in {"completed", "blocked", "failed"}:
        failures.append("invalid_status")

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return False, sorted(set(failures + ["artifacts_not_list"])), []

    max_artifacts = int(policy["max_artifacts"])
    max_file = int(policy["max_file_bytes"])
    max_total = int(policy["max_total_artifact_bytes"])
    if len(artifacts) > max_artifacts:
        failures.append("too_many_artifacts")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for item in artifacts:
        if not isinstance(item, dict):
            failures.append("invalid_artifact_shape")
            continue
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            failures.append("invalid_artifact_shape")
            continue
        rel = path.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel_path = Path(rel)
        if not rel or rel.startswith("/") or ".." in rel_path.parts or rel in seen:
            failures.append("invalid_artifact_path")
            continue
        target = (workspace / rel_path).resolve()
        if not target.is_relative_to(workspace):
            failures.append("scope_escape")
            continue
        data = content.encode("utf-8")
        if len(data) > max_file:
            failures.append("artifact_too_large")
            continue
        total += len(data)
        if total > max_total:
            failures.append("artifact_bundle_too_large")
            continue
        seen.add(rel)
        normalized.append({"path": rel, "content": content, "sha256": sha256_bytes(data), "bytes": len(data)})

    if result_type == "artifact" and status == "completed" and not normalized:
        failures.append("completed_without_artifacts")
    if result_type == "action_only" and normalized:
        failures.append("action_only_with_artifacts")
    if result_type == "read_only":
        if normalized:
            failures.append("read_only_with_artifacts")
        if status == "completed":
            if result.get("review_result") not in {"PASS", "FAIL"}:
                failures.append("invalid_review_result")
            findings = result.get("findings")
            if (
                not isinstance(findings, list)
                or len(findings) > 64
                or not all(isinstance(item, str) and len(item) <= 2000 for item in findings)
            ):
                failures.append("invalid_review_findings")
    if status in {"blocked", "failed"} and normalized:
        failures.append("noncompleted_with_artifacts")
    return not failures, sorted(set(failures)), normalized


def atomic_apply(workspace: Path, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not artifacts:
        return []
    temp_root = Path(tempfile.mkdtemp(prefix="plachem-gateway-"))
    backups = temp_root / "backup"
    staged = temp_root / "staged"
    backups.mkdir(parents=True)
    staged.mkdir(parents=True)
    changed: list[dict[str, Any]] = []
    applied_targets: list[tuple[Path, Path | None, bool]] = []
    try:
        # Stage all outputs first.
        for art in artifacts:
            rel = Path(art["path"])
            stage_path = staged / rel
            stage_path.parent.mkdir(parents=True, exist_ok=True)
            stage_path.write_text(art["content"], encoding="utf-8", newline="\n")

        # Backup then atomically replace each file.
        for art in artifacts:
            rel = Path(art["path"])
            target = (workspace / rel).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            backup_path: Path | None = None
            before_sha = None
            if existed:
                before_data = target.read_bytes()
                before_sha = sha256_bytes(before_data)
                backup_path = backups / rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_path)
            staged_path = staged / rel
            replace_tmp = target.with_name(target.name + ".gateway-tmp")
            shutil.copy2(staged_path, replace_tmp)
            os.replace(replace_tmp, target)
            applied_targets.append((target, backup_path, existed))
            after_data = target.read_bytes()
            changed.append({
                "path": rel.as_posix(),
                "created": not existed,
                "before_sha256": before_sha,
                "after_sha256": sha256_bytes(after_data),
                "bytes": len(after_data),
            })
        return changed
    except Exception:
        # Best-effort rollback of everything already applied.
        for target, backup_path, existed in reversed(applied_targets):
            try:
                if existed and backup_path and backup_path.exists():
                    shutil.copy2(backup_path, target)
                elif not existed and target.exists():
                    target.unlink()
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def run(
    task_request: dict[str, Any],
    agents: dict[str, Any],
    policy: dict[str, Any],
    project_root: Path,
    log_path: Path,
    auth_broker_path: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    task_id = str(task_request.get("task_id") or f"task-{int(time.time())}")
    agent_name = str(task_request.get("agent") or "achilles")
    task = str(task_request.get("task") or "").strip()
    workspace_value = str(task_request.get("workspace") or ".")

    def blocked_record(reason: str, auth: dict[str, Any] | None, requested: list[str]) -> dict[str, Any]:
        record = {
            "timestamp": utcnow(),
            "task_id": task_id,
            "agent": agent_name,
            "workspace": workspace_value,
            "task_sha256": sha256_bytes(task.encode("utf-8")),
            "auth": {
                "broker_called": bool(auth and auth.get("broker_called")),
                "result": "AUTHORIZED" if auth else "NO_AUTH",
                "allow": list((auth or {}).get("allow", [])),
                "deny": list((auth or {}).get("deny", [])),
                "requested": requested,
                "authorization_id": str((auth or {}).get("authorization_id") or ""),
                "worker": str((auth or {}).get("worker") or ""),
            },
            "attempts": [],
            "result": {"status": "blocked", "reason": reason, "changes": []},
            "git": {"commit": False, "push": False, "commit_sha": "", "push_ref": ""},
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "status": "BLOCKED",
        }
        save_jsonl(log_path, record)
        return record

    if not task:
        return blocked_record("EMPTY_TASK", None, [])
    if agent_name not in agents:
        return blocked_record("UNKNOWN_AGENT", None, [])

    authorization: dict[str, Any] | None = None
    detected_actions = detect_requested_actions(task)
    if auth_broker_path is not None:
        try:
            authorization = load_mock_authorization(
                auth_broker_path,
                task_id,
                agent_name,
                detected_actions,
            )
        except Exception as exc:
            return blocked_record(f"AUTH_FAILED:{type(exc).__name__}:{str(exc)[:160]}", None, [])

    decision = authorize_requested_actions(task, list(policy["blocked_actions"]), authorization)
    requested_actions = list(decision["requested"])
    if not decision["allowed"]:
        return blocked_record(str(decision["reason"]), authorization, requested_actions)

    workspace = normalize_workspace(project_root, workspace_value)
    context = collect_context(workspace, policy)
    if not context:
        return blocked_record("NO_CONTEXT_FILES", authorization, requested_actions)

    prompt = build_worker_prompt(
        task,
        workspace_value,
        context,
        policy,
        authorization,
        requested_actions,
    )
    attempts: list[dict[str, Any]] = []
    max_attempts = int(policy["max_retries"]) + 1
    previous_error: str | None = None
    result: dict[str, Any] | None = None
    git_result: dict[str, Any] = {"commit": False, "push": False, "commit_sha": "", "push_ref": ""}

    for attempt_no in range(1, max_attempts + 1):
        if time.monotonic() - started > int(policy["total_timeout_seconds"]):
            break
        astart = time.monotonic()
        try:
            raw = call_worker(agents[agent_name], prompt, int(policy["timeout_seconds"]))
            valid, failures, normalized = validate_result(workspace, raw, policy)
            attempts.append({
                "attempt": attempt_no,
                "elapsed_seconds": round(time.monotonic() - astart, 3),
                "valid": valid,
                "failures": failures,
            })
            if not valid:
                error_fp = "|".join(failures) or "VALIDATION_FAILED"
                if previous_error == error_fp:
                    result = {"status": "failed", "reason": "REPEATED_FAILURE", "artifacts": []}
                    break
                previous_error = error_fp
                continue

            result_type = raw.get("result_type", "artifact")
            if "read_only_review" in requested_actions and result_type != "read_only":
                raise ValueError("read-only review cannot return artifacts or executable actions")
            if result_type == "read_only" and set(requested_actions) != {"read_only_review"}:
                raise ValueError("read-only result requires only the authorized read_only_review action")
            if result_type == "action_only":
                implemented_actions = {"git_commit", "git_push"}
                if not requested_actions or not set(requested_actions).issubset(implemented_actions):
                    raise ValueError("action-only result requires an implemented authorized action")

            if raw["status"] == "completed":
                changes = [] if result_type == "read_only" else atomic_apply(workspace, normalized)
                result = {
                    "status": "completed",
                    "summary": str(raw.get("summary") or "completed"),
                    "reason": "",
                    "changes": changes,
                }
                if result_type == "read_only":
                    result["review_result"] = str(raw["review_result"])
                    result["findings"] = list(raw["findings"])
                if requested_actions:
                    if authorization is None:
                        raise ValueError("authorization required for requested actions")
                    if result_type != "read_only":
                        git_result = execute_authorized_git_actions(
                            project_root,
                            workspace_value,
                            changes,
                            task_id,
                            requested_actions,
                            authorization,
                        )
                authorization_id = str((authorization or {}).get("authorization_id") or "")
                if auth_broker_path is not None and authorization_id:
                    consume_task_authorization(
                        auth_broker_path,
                        authorization_id=authorization_id,
                        task_id=task_id,
                        worker=agent_name,
                        requested_actions=requested_actions,
                    )
            else:
                result = {
                    "status": raw["status"],
                    "summary": str(raw.get("summary") or ""),
                    "reason": str(raw.get("reason") or raw["status"]),
                    "changes": [],
                }
            break
        except Exception as exc:
            error_fp = f"{type(exc).__name__}:{str(exc)[:200]}"
            attempts.append({
                "attempt": attempt_no,
                "elapsed_seconds": round(time.monotonic() - astart, 3),
                "valid": False,
                "failures": [error_fp],
            })
            if previous_error == error_fp:
                result = {"status": "failed", "reason": "REPEATED_FAILURE", "changes": []}
                break
            previous_error = error_fp

    if result is None:
        result = {"status": "failed", "reason": "TIMEOUT_OR_ATTEMPTS_EXHAUSTED", "changes": []}

    final_status = {"completed": "PASS", "blocked": "BLOCKED", "failed": "FAIL"}.get(result["status"], "FAIL")
    record = {
        "timestamp": utcnow(),
        "task_id": task_id,
        "agent": agent_name,
        "workspace": workspace_value,
        "task_sha256": sha256_bytes(task.encode("utf-8")),
        "auth": {
            "broker_called": bool(authorization and authorization.get("broker_called")),
            "result": "AUTHORIZED" if authorization else "NO_AUTH",
            "allow": list((authorization or {}).get("allow", [])),
            "deny": list((authorization or {}).get("deny", [])),
            "requested": requested_actions,
            "authorization_id": str((authorization or {}).get("authorization_id") or ""),
            "worker": str((authorization or {}).get("worker") or ""),
        },
        "context_files": [{"path": x["path"], "sha256": x["sha256"]} for x in context],
        "attempts": attempts,
        "result": result,
        "git": git_result,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "status": final_status,
    }
    save_jsonl(log_path, record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PLACHEM Fast Delegation Gateway")
    parser.add_argument("--request", type=Path, help="JSON: task_id, agent, task, workspace")
    parser.add_argument("--task", help="Natural-language task text")
    parser.add_argument("--agent", default="achilles")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--task-id")
    parser.add_argument("--project-root", type=Path, default=ROOT.parent)
    parser.add_argument("--agents", type=Path, default=ROOT / "agents.json")
    parser.add_argument("--policy", type=Path, default=ROOT / "policy.json")
    parser.add_argument("--log", type=Path, default=ROOT / "runs.jsonl")
    parser.add_argument("--auth-broker", type=Path, help="Task-scoped Mock Auth Broker JSON")
    args = parser.parse_args(argv)

    if args.request:
        request = load_json(args.request)
    else:
        request = {
            "task_id": args.task_id,
            "agent": args.agent,
            "task": args.task,
            "workspace": args.workspace,
        }

    agents = load_json(args.agents)
    policy = merge_policy(args.policy)
    record = run(
        request,
        agents,
        policy,
        args.project_root.resolve(),
        args.log,
        args.auth_broker.resolve() if args.auth_broker else None,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
