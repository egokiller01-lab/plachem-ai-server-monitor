from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


MAX_STRING_BYTES = 256 * 1024
MAX_ITEM_BYTES = 4 * 1024
MAX_ITEMS = 100


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def enforce_practical_payload_bounds(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, str) and len(value.encode("utf-8")) > MAX_STRING_BYTES:
                raise ValueError(f"{name} exceeds the UTF-8 byte limit")
            if isinstance(value, list):
                if len(value) > MAX_ITEMS:
                    raise ValueError(f"{name} exceeds the item limit")
                for item in value:
                    if isinstance(item, str) and len(item.encode("utf-8")) > MAX_ITEM_BYTES:
                        raise ValueError(f"{name} contains an oversized item")
        return self


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionMode(str, Enum):
    AUTONOMOUS = "autonomous"
    BOUNDED = "bounded"
    SUPERVISED = "supervised"
    APPROVAL_REQUIRED = "approval_required"


class Environment(str, Enum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Scope(StrictModel):
    include: list[StrictStr] = Field(min_length=1)
    exclude: list[StrictStr] = Field(default_factory=list)

    @field_validator("include")
    @classmethod
    def validate_document_paths(cls, value: list[str]) -> list[str]:
        for item in value:
            normalized = item.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                not item
                or normalized.startswith("/")
                or path.is_absolute()
                or ":" in path.parts[0]
                or any(part in ("", ".", "..") for part in path.parts)
            ):
                raise ValueError("scope.include must contain relative non-traversing document paths")
        return value


class Limits(StrictModel):
    max_steps: StrictInt = Field(ge=1, le=100)
    max_retries: StrictInt = Field(ge=0, le=10)
    timeout_seconds: StrictInt = Field(ge=30, le=86400)


class CompletionRules(StrictModel):
    max_summary_sentences: StrictInt = Field(ge=1, le=10)
    min_evidence: StrictInt = Field(ge=1, le=100)
    no_changes: StrictBool


class ValidationRules(StrictModel):
    required_text: list[StrictStr] = Field(default_factory=list)
    required_regex: list[StrictStr] = Field(default_factory=list)
    javascript_syntax: StrictBool = False

    @field_validator("required_regex")
    @classmethod
    def validate_regexes(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid validation regex: {pattern}") from exc
        return value


class TaskSpec(StrictModel):
    task_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    agent: StrictStr = Field(min_length=1)
    objective: StrictStr = Field(min_length=1)
    risk: RiskLevel
    execution: ExecutionMode
    environment: Environment
    scope: Scope
    permissions: list[StrictStr] = Field(default_factory=list)
    deny: list[StrictStr] = Field(min_length=1)
    limits: Limits
    completion: CompletionRules
    evidence: list[StrictStr] = Field(min_length=1)
    validation: ValidationRules = Field(default_factory=ValidationRules)


class ResultStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    PARTIAL = "partial"


class Artifact(StrictModel):
    path: StrictStr = Field(min_length=1)
    content: StrictStr

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            normalized.startswith("/")
            or path.is_absolute()
            or ":" in path.parts[0]
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise ValueError("artifact path must be relative and non-traversing")
        return normalized


class WorkerResult(StrictModel):
    task_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    status: ResultStatus
    summary: StrictStr = Field(min_length=1)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=MAX_ITEMS)
    changes: list[StrictStr] = Field(default_factory=list)
    checks: list[StrictStr] = Field(default_factory=list)
    evidence: list[StrictStr] = Field(default_factory=list)
    permission_use: list[StrictStr] = Field(default_factory=list)
    production_changes: StrictInt = Field(default=0, ge=0)
    remaining_risks: list[StrictStr] = Field(default_factory=list)
    next_action: StrictStr = "review"

    @model_validator(mode="after")
    def reject_duplicate_artifact_paths(self) -> Self:
        paths = [artifact.path.casefold() for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self
