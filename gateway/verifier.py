from __future__ import annotations

import re
from enum import Enum

from pydantic import Field

from gateway.models import ResultStatus, StrictModel, TaskSpec, WorkerResult


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    NEEDS_APPROVAL = "needs_approval"


class Verification(StrictModel):
    status: VerificationStatus
    reasons: list[str] = Field(default_factory=list)


_LINE_REFERENCE = re.compile(r"^(?P<path>[^:\r\n|]+):(?P<line>[1-9][0-9]*)$")
_CHECK_REFERENCE = re.compile(
    r"^(?P<path>[^:\r\n|]+):(?P<line>[1-9][0-9]*)\|(?P<text>[^\r\n]*)$"
)
_SENTENCE_END = re.compile(r"[.!?。！？]+[\"')\]]*(?=\s|$|[A-Z]|[^\x00-\x7f])")
_COMMON_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
}


def _is_abbreviation(line: str, match: re.Match[str]) -> bool:
    if not match.group().startswith("."):
        return False
    token_start = line.rfind(" ", 0, match.start()) + 1
    if line[token_start:match.start()].casefold().startswith(("http://", "https://")):
        return False
    if (
        match.end() < len(line)
        and line[match.end()].isupper()
        and len(line[token_start:match.start()]) == 1
        and line[token_start:match.start()].isupper()
    ):
        return True
    prefix = line[: match.start() + 1]
    token_match = re.search(r"([A-Za-z](?:[A-Za-z.]*)?)\.$", prefix)
    if not token_match:
        return False
    token = token_match.group(1).casefold().rstrip(".")
    return token in _COMMON_ABBREVIATIONS or bool(re.fullmatch(r"(?:[a-z]\.)+[a-z]", token))


def _sentence_count(summary: str) -> int:
    total = 0
    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        boundaries = [match for match in _SENTENCE_END.finditer(line) if not _is_abbreviation(line, match)]
        total += len(boundaries)
        tail_start = boundaries[-1].end() if boundaries else 0
        if line[tail_start:].strip():
            total += 1
    return total


def verify_result(
    task: TaskSpec,
    result: WorkerResult,
    authoritative_context: str = "",
) -> Verification:
    reasons: list[str] = []
    completed = result.status is ResultStatus.COMPLETED
    if result.task_id != task.task_id:
        reasons.append("task_id does not match")
    if result.production_changes != 0:
        reasons.append("production changes must be zero")
    scoped_write = "workspace_write_scoped" in task.permissions
    if completed and task.completion.no_changes and result.changes:
        reasons.append("completed result changes must be empty")
    if completed and not result.evidence:
        reasons.append("completed result requires evidence")

    if completed and not scoped_write and result.permission_use != ["repo_read"]:
        reasons.append("completed result permission_use must be exactly ['repo_read']")
    if completed and not result.checks:
        reasons.append("completed result requires checks")
    sentence_count = _sentence_count(result.summary)
    if completed and sentence_count > task.completion.max_summary_sentences:
        reasons.append("summary exceeds completion max_summary_sentences")

    if scoped_write:
        allowed = {path.replace("\\", "/") for path in task.scope.include}
        artifact_by_path = {artifact.path: artifact for artifact in result.artifacts}
        changes = {path.replace("\\", "/") for path in result.changes}
        if set(artifact_by_path) != allowed:
            reasons.append("artifact paths must exactly match TaskSpec scope.include")
        if changes != allowed:
            reasons.append("change paths must exactly match TaskSpec scope.include")
        if result.permission_use != task.permissions:
            reasons.append("completed result permission_use must exactly match TaskSpec permissions")
        grounded_evidence: set[tuple[str, int]] = set()
        for item in result.evidence:
            match = _LINE_REFERENCE.fullmatch(item.strip())
            if not match:
                reasons.append(f"evidence lacks an exact file:line reference: {item}")
                continue
            artifact_path = match.group("path").replace("\\", "/")
            line = int(match.group("line"))
            artifact = artifact_by_path.get(artifact_path)
            if artifact is None:
                reasons.append(f"evidence references outside artifact bundle: {item}")
                continue
            lines = artifact.content.splitlines()
            if line > len(lines):
                reasons.append(f"evidence line is absent from generated artifact: {item}")
                continue
            grounded_evidence.add((artifact_path, line))
        required = {
            (match.group("path").replace("\\", "/"), int(match.group("line")))
            for requirement in task.evidence
            if (match := _LINE_REFERENCE.fullmatch(requirement.strip()))
        }
        for requirement in sorted(required - grounded_evidence):
            reasons.append(
                f"TaskSpec evidence requirement is not satisfied: {requirement[0]}:{requirement[1]}"
            )
        if completed and len(grounded_evidence) < task.completion.min_evidence:
            reasons.append(
                "completed result requires at least "
                f"{task.completion.min_evidence} unique grounded evidence items"
            )
        if not completed:
            reasons.append(f"worker status is {result.status.value}")
        if reasons:
            return Verification(status=VerificationStatus.REJECTED, reasons=reasons)
        return Verification(status=VerificationStatus.VERIFIED)

    context_lines = authoritative_context.splitlines()
    allowed_artifact = task.scope.include[0].replace("\\", "/")
    grounded_checks: set[tuple[str, int]] = set()
    for check in result.checks:
        match = _CHECK_REFERENCE.fullmatch(check.strip())
        if not match:
            reasons.append(f"check is not grounded in <path>:<line>|<exact line text> format: {check}")
            continue
        artifact = match.group("path").replace("\\", "/")
        line = int(match.group("line"))
        if artifact != allowed_artifact or line > len(context_lines):
            reasons.append(f"check is not grounded in authoritative context: {check}")
        elif match.group("text") != context_lines[line - 1]:
            reasons.append(f"check does not contain exact line text: {check}")
        else:
            grounded_checks.add((artifact, line))

    grounded_evidence: set[tuple[str, int]] = set()
    for item in result.evidence:
        match = _LINE_REFERENCE.fullmatch(item.strip())
        if not match:
            reasons.append(f"evidence lacks an exact file:line reference: {item}")
            continue
        artifact = match.group("path").replace("\\", "/")
        line = int(match.group("line"))
        if artifact != allowed_artifact:
            reasons.append(f"evidence references outside scope: {item}")
        elif line > len(context_lines):
            reasons.append(f"evidence line is absent from authoritative context: {item}")
        else:
            grounded_evidence.add((artifact, line))
            if (artifact, line) not in grounded_checks:
                reasons.append(f"evidence has no matching grounded check: {item}")

    required_evidence: set[tuple[str, int]] = set()
    for requirement in task.evidence:
        match = _LINE_REFERENCE.fullmatch(requirement.strip())
        if match:
            required_evidence.add(
                (match.group("path").replace("\\", "/"), int(match.group("line")))
            )
        else:
            reasons.append(f"unsupported deterministic TaskSpec evidence requirement: {requirement}")
    for requirement in sorted(required_evidence - grounded_evidence):
        reasons.append(
            f"TaskSpec evidence requirement is not satisfied: {requirement[0]}:{requirement[1]}"
        )

    if completed and len(grounded_evidence) < task.completion.min_evidence:
        reasons.append(
            "completed result requires at least "
            f"{task.completion.min_evidence} unique grounded evidence items"
        )

    if not completed:
        reasons.append(f"worker status is {result.status.value}")
    if reasons:
        return Verification(status=VerificationStatus.REJECTED, reasons=reasons)
    return Verification(status=VerificationStatus.VERIFIED)
