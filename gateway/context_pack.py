from __future__ import annotations

import json

from gateway.models import TaskSpec

MAX_CONTEXT_BYTES = 1024 * 1024
MAX_QUERY_BYTES = 2 * 1024 * 1024


def validate_authoritative_context(authoritative_context: str) -> None:
    if len(authoritative_context.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ValueError("authoritative context exceeds the configured byte limit")


def build_context_pack(task: TaskSpec, authoritative_context: str = "") -> str:
    validate_authoritative_context(authoritative_context)
    contract = task.model_dump(mode="json")
    scoped_write = "workspace_write_scoped" in task.permissions
    if scoped_write:
        result_shape = {
            "task_id": task.task_id,
            "status": "completed|blocked|failed|partial",
            "summary": "short factual result",
            "artifacts": [
                {"path": path, "content": f"complete UTF-8 content for {path}"}
                for path in task.scope.include
            ],
            "changes": list(task.scope.include),
            "checks": ["factual self-check performed for generated artifacts"],
            "evidence": list(task.evidence),
            "permission_use": list(task.permissions),
            "production_changes": 0,
            "remaining_risks": [],
            "next_action": "review",
        }
        work_instructions = (
            "Return every requested file as an artifact with complete UTF-8 content.\n"
            "Do not write files directly; the Gateway validates and promotes artifacts.\n"
            "Artifact paths must exactly match TaskSpec scope.include, with no additional files.\n"
        )
    else:
        result_shape = {
            "task_id": task.task_id,
            "status": "completed|blocked|failed|partial",
            "summary": "short factual result",
            "artifacts": [],
            "changes": [],
            "checks": [f"{task.scope.include[0]}:1|exact authoritative line text"],
            "evidence": [f"{task.scope.include[0]}:1"],
            "permission_use": ["repo_read"],
            "production_changes": 0,
            "remaining_risks": [],
            "next_action": "review",
        }
        work_instructions = (
            "All required source content is already included under AUTHORITATIVE CONTEXT.\n"
            "Analyze that supplied content directly. Do not search for files.\n"
        )
    context_pack = (
        "You are Achilles, the bounded worker. Follow only this contract.\n"
        "Only the profile-local todo planning tool is permitted; use it only for planning if needed.\n"
        "No filesystem, terminal, network, or external tools are permitted. Do not output tool requests.\n"
        + work_instructions
        + "Do not expand scope. Stop and report when blocked.\n\n"
        f"TASKSPEC\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n\n"
        "AUTHORITATIVE CONTEXT\n"
        + authoritative_context
        + "\nEND AUTHORITATIVE CONTEXT\n\n"
        "Return exactly one JSON object and no markdown fences. "
        "Evidence items must be exact file:line strings such as README.md:7. "
        "Every checks item must be <path>:<line>|<exact line text> and match the authoritative context exactly.\n"
        "Match this shape:\n"
        f"{json.dumps(result_shape, ensure_ascii=False, indent=2)}\n"
    )
    if len(context_pack.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("worker query exceeds the configured byte limit")
    return context_pack
