"""Core Engine wiring for OpenClaw-backed Fast Gateway runs.

The Core owns agent admission, run identity, lifecycle, and durable status.
The OpenClaw adapter owns only its verified transport/session contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .worker_transport import WorkerTransport
from .openclaw_adapter import (
    AdapterError,
    AdapterOutcome,
    CompositeResultValidator,
    CoreRunStatus,
    RunBinding,
    TransportError,
)
from .runtime_policy import (
    GoalContract,
    ModelRegistry,
    PolicyResolutionError,
    RuntimeClass,
    RuntimeModelProfile,
    RuntimePolicyEngine,
    normalize_goal_contract,
    normalize_progress_checkpoint,
)


_TERMINAL = {
    CoreRunStatus.PASS,
    CoreRunStatus.FAIL,
    CoreRunStatus.BLOCKED,
    CoreRunStatus.TIMEOUT,
    CoreRunStatus.CANCELLED,
}
_TRANSITIONS = {
    CoreRunStatus.QUEUED: {CoreRunStatus.RUNNING, CoreRunStatus.FAIL, CoreRunStatus.BLOCKED, CoreRunStatus.CANCELLED},
    CoreRunStatus.RUNNING: _TERMINAL,
    CoreRunStatus.PASS: set(),
    CoreRunStatus.FAIL: set(),
    CoreRunStatus.BLOCKED: set(),
    CoreRunStatus.TIMEOUT: set(),
    CoreRunStatus.CANCELLED: set(),
}
_GOAL_INPUT_FIELDS = (
    "primary_objective", "allowed_scope", "forbidden_scope", "expected_result", "completion_conditions",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _request_hash(agent_id: str, message: str, timeout_seconds: float, goal_id: str) -> str:
    raw = json.dumps(
        {"agent_id": agent_id, "message": message, "timeout_seconds": timeout_seconds, "goal_id": goal_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    enabled: bool
    capabilities: tuple[str, ...]
    runtime_model_id: str
    allowed_model_ids: tuple[str, ...]
    allowed_policy_profiles: tuple[str, ...]


class AgentRegistry:
    """Allow-list registry; runtime metadata is never sent to OpenClaw RPC."""

    def __init__(self, registrations: Mapping[str, AgentRegistration]) -> None:
        self._registrations = dict(registrations)

    @classmethod
    def load(cls, path: str | Path) -> "AgentRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("INVALID_AGENT_REGISTRY")
        registrations: dict[str, AgentRegistration] = {}
        for agent_id, value in raw.items():
            if not isinstance(agent_id, str) or not isinstance(value, Mapping):
                raise ValueError("INVALID_AGENT_REGISTRY")
            enabled = value.get("enabled", True)
            capabilities = value.get("capabilities", [])
            runtime_model_id = value.get("runtime_model_id")
            allowed_model_ids = value.get("allowed_model_ids")
            allowed_policy_profiles = value.get("allowed_policy_profiles")
            if not isinstance(enabled, bool):
                raise ValueError(f"INVALID_AGENT_ENABLED:{agent_id}")
            if not isinstance(capabilities, list) or not all(isinstance(item, str) and item for item in capabilities):
                raise ValueError(f"INVALID_AGENT_CAPABILITIES:{agent_id}")
            if not isinstance(runtime_model_id, str) or not runtime_model_id:
                raise ValueError(f"INVALID_RUNTIME_MODEL:{agent_id}")
            if not isinstance(allowed_model_ids, list) or not all(
                isinstance(item, str) and item for item in allowed_model_ids
            ):
                raise ValueError(f"INVALID_ALLOWED_MODELS:{agent_id}")
            if not isinstance(allowed_policy_profiles, list) or not all(
                isinstance(item, str) and item for item in allowed_policy_profiles
            ):
                raise ValueError(f"INVALID_ALLOWED_PROFILES:{agent_id}")
            registrations[agent_id] = AgentRegistration(
                agent_id,
                enabled,
                tuple(capabilities),
                runtime_model_id,
                tuple(allowed_model_ids),
                tuple(allowed_policy_profiles),
            )
        return cls(registrations)

    def require(self, agent_id: str) -> AgentRegistration:
        registration = self._registrations.get(agent_id)
        if registration is None or not registration.enabled:
            raise ValueError(f"UNKNOWN_AGENT:{agent_id}")
        return registration


class RunRegistry:
    """Append-only Core run registry imported from the phase2 state model."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utcnow,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._run_id_factory = run_id_factory or (lambda: f"core-{uuid.uuid4().hex}")
        self._lock = threading.RLock()

    def create(
        self,
        *,
        core_run_id: str | None,
        agent_id: str,
        idempotency_key: str,
        request_hash: str,
        policy: Mapping[str, Any],
        goal_contract: GoalContract,
        parent_core_run_id: str | None = None,
        context_reset_count: int = 0,
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            actual_id = core_run_id or self._run_id_factory()
            existing = self.get(actual_id)
            if existing is not None:
                if existing["idempotency_key"] != idempotency_key or existing["request_hash"] != request_hash:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return existing, False
            duplicate = self.get_by_idempotency(idempotency_key)
            if duplicate is not None:
                if duplicate["core_run_id"] == actual_id and duplicate["request_hash"] == request_hash:
                    return duplicate, False
                raise ValueError("DUPLICATE_DISPATCH")
            now = self._clock().astimezone(timezone.utc).isoformat()
            record = {
                "core_run_id": actual_id,
                "agent_id": agent_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "status": CoreRunStatus.QUEUED.value,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "reason": "",
                "result": None,
                "openclaw_binding": None,
                "runtime_class": policy.get("runtime_class", RuntimeClass.UNKNOWN.value),
                "model_profile": policy.get("model_profile"),
                "policy_profile": policy.get("policy_profile", "UNKNOWN"),
                "max_runtime": policy.get("max_runtime"),
                "max_retries": policy.get("max_retries"),
                "max_tool_calls": policy.get("max_tool_calls"),
                "context_policy": policy.get("context_policy", "MANUAL"),
                "fallback_policy": policy.get("fallback_policy", "NONE"),
                "runtime_seconds": 0.0,
                "retry_count": 0,
                "tool_call_count": None,
                "tool_call_metric": "UNSUPPORTED",
                "policy_status": "NORMAL",
                "policy_events": [],
                "cancel_reason": "",
                "policy_state": {"retry_count": 0, "tool_call_count": None},
                "goal_contract": goal_contract.as_dict(),
                "goal_status": "NORMAL",
                "goal_reinjection_ready": False,
                "progress_checkpoint": None,
                "verified_progress": {
                    "completed_conditions": [], "artifacts": [], "evidence": [], "scope": [],
                },
                "context_reset_count": context_reset_count,
                "max_context_resets": policy.get("max_context_resets", 0),
                "parent_core_run_id": parent_core_run_id,
                "escalation_required": False,
                "escalation_reason": "",
                "escalation_package": None,
            }
            self._append(record)
            return copy.deepcopy(record), True

    def transition(
        self,
        core_run_id: str,
        status: CoreRunStatus,
        *,
        binding: RunBinding | None = None,
        outcome: AdapterOutcome | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            record = self.get(core_run_id)
            if record is None:
                raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
            current = CoreRunStatus(record["status"])
            if status == current:
                return record
            if status not in _TRANSITIONS[current]:
                raise ValueError(f"INVALID_RUN_TRANSITION:{current.value}->{status.value}")
            now = self._clock().astimezone(timezone.utc).isoformat()
            record["status"] = status.value
            record["updated_at"] = now
            if status == CoreRunStatus.RUNNING:
                record["started_at"] = now
            if binding is not None:
                record["openclaw_binding"] = {
                    "core_run_id": binding.core_run_id,
                    "openclaw_run_id": binding.openclaw_run_id,
                    "agent_id": binding.agent_id,
                    "session_key": binding.session_key,
                    "session_id": binding.session_id,
                }
            if status in _TERMINAL:
                record["completed_at"] = now
                record["reason"] = reason or (outcome.reason if outcome is not None else "")
                record["result"] = copy.deepcopy(dict(outcome.result)) if outcome and outcome.result is not None else None
                if status == CoreRunStatus.CANCELLED:
                    record["cancel_reason"] = record["reason"]
                    record["policy_status"] = "CANCELLED"
            self._append(record)
            return copy.deepcopy(record)

    def update_goal_state(
        self,
        core_run_id: str,
        *,
        checkpoint: Mapping[str, Any] | None = None,
        verified_progress: Mapping[str, Any] | None = None,
        goal_state: Mapping[str, Any] | None = None,
        goal_status: str | None = None,
        reinjection_ready: bool | None = None,
        event_code: str | None = None,
        event_details: Mapping[str, Any] | None = None,
        escalation_reason: str | None = None,
        escalation_package: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self.get(core_run_id)
            if record is None:
                raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
            immutable_contract = copy.deepcopy(record.get("goal_contract"))
            if checkpoint is not None:
                record["progress_checkpoint"] = copy.deepcopy(dict(checkpoint))
            if verified_progress is not None:
                record["verified_progress"] = copy.deepcopy(dict(verified_progress))
            if goal_state is not None:
                record.setdefault("policy_state", {})["goal"] = copy.deepcopy(dict(goal_state))
            if goal_status is not None:
                if goal_status not in {"NORMAL", "GUARDED", "DRIFT"}:
                    raise ValueError("INVALID_GOAL_STATUS")
                record["goal_status"] = goal_status
            if reinjection_ready is not None:
                record["goal_reinjection_ready"] = bool(reinjection_ready)
            if event_code:
                events = list(record.get("policy_events") or [])
                events.append({
                    "code": event_code,
                    "at": self._clock().astimezone(timezone.utc).isoformat(),
                    "details": copy.deepcopy(dict(event_details or {})),
                })
                record["policy_events"] = events
                record["policy_status"] = "GUARDED"
            if escalation_reason is not None:
                record["escalation_required"] = True
                record["escalation_reason"] = escalation_reason
            if escalation_package is not None:
                record["escalation_package"] = copy.deepcopy(dict(escalation_package))
            if record.get("goal_contract") != immutable_contract:
                raise ValueError("GOAL_CONTRACT_IMMUTABLE")
            record["updated_at"] = self._clock().astimezone(timezone.utc).isoformat()
            self._append(record)
            return copy.deepcopy(record)

    def update_policy(
        self,
        core_run_id: str,
        *,
        runtime_seconds: float | None = None,
        retry_count: int | None = None,
        tool_call_count: int | None = None,
        tool_call_metric: str | None = None,
        policy_state: Mapping[str, Any] | None = None,
        event_code: str | None = None,
        event_details: Mapping[str, Any] | None = None,
        cancel_reason: str | None = None,
        policy_status: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self.get(core_run_id)
            if record is None:
                raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
            if runtime_seconds is not None:
                record["runtime_seconds"] = round(max(0.0, float(runtime_seconds)), 3)
            if retry_count is not None:
                record["retry_count"] = max(0, int(retry_count))
            if tool_call_metric is not None:
                if tool_call_metric not in {"SUPPORTED", "UNSUPPORTED"}:
                    raise ValueError("INVALID_TOOL_CALL_METRIC")
                record["tool_call_metric"] = tool_call_metric
            if tool_call_count is not None:
                if tool_call_metric != "SUPPORTED" and record.get("tool_call_metric") != "SUPPORTED":
                    raise ValueError("UNOBSERVABLE_TOOL_COUNT")
                record["tool_call_count"] = max(0, int(tool_call_count))
            if policy_state is not None:
                record["policy_state"] = copy.deepcopy(dict(policy_state))
            if event_code is not None:
                events = list(record.get("policy_events") or [])
                events.append(
                    {
                        "code": event_code,
                        "at": self._clock().astimezone(timezone.utc).isoformat(),
                        "details": copy.deepcopy(dict(event_details or {})),
                    }
                )
                record["policy_events"] = events
                record["policy_status"] = policy_status or "GUARDED"
            elif policy_status is not None:
                record["policy_status"] = policy_status
            if cancel_reason is not None:
                record["cancel_reason"] = cancel_reason
            record["updated_at"] = self._clock().astimezone(timezone.utc).isoformat()
            self._append(record)
            return copy.deepcopy(record)

    def get(self, core_run_id: str) -> dict[str, Any] | None:
        latest = None
        for record in self._records():
            if record.get("core_run_id") == core_run_id:
                latest = record
        return copy.deepcopy(latest) if latest is not None else None

    def get_by_idempotency(self, key: str) -> dict[str, Any] | None:
        latest = None
        for record in self._records():
            if record.get("idempotency_key") == key:
                latest = record
        return copy.deepcopy(latest) if latest is not None else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("INVALID_LIMIT")
        latest: dict[str, dict[str, Any]] = {}
        for record in self._records():
            run_id = record.get("core_run_id")
            if isinstance(run_id, str):
                latest[run_id] = record
        return [copy.deepcopy(item) for item in reversed(list(latest.values()))][:limit]

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"MALFORMED_RUN_REGISTRY:line={line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"MALFORMED_RUN_REGISTRY:line={line_number}")
            records.append(value)
        return records

    def _append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def production_result_validator() -> CompositeResultValidator:
    """Build the fail-closed validator used by the Core production path."""

    claim_patterns = {
        "tool": re.compile(r"(?i)(\btool(?:\s+(?:call|execution|use)|s?\s+(?:were|was)\s+used)\b|\bused\s+(?:a\s+)?tool\b|\binvoked\s+via\b|\bprocess\b.{0,40}\b(?:exited|exit\s+code|ran|executed)\b|도구.{0,12}(?:실행|사용))"),
        "file": re.compile(r"(?i)(\b(?:file|files)\b.{0,40}\b(?:read|writ\w*|modif\w*|creat\w*|edit\w*|access\w*)\b|\b(?:read|wrote|written|modified|created|edited|accessed|reads?)\b.{0,40}\b(?:file|files|jsonl)\b|파일.{0,20}(?:읽|쓰|수정|생성|접근))"),
        "memory": re.compile(r"(?i)(\bmemory\b.{0,30}\b(?:read|writ\w*|sav\w*|updat\w*)\b|\b(?:read|wrote|written|saved|updated|write)\b.{0,30}\bmemory\b|메모리.{0,20}(?:읽|쓰|저장|수정))"),
        "db": re.compile(r"(?i)(\b(?:db|database|sql|sqlite|postgres|supabase)\b.{0,30}\b(?:quer\w*|read|writ\w*|updat\w*|insert\w*|execut\w*|access\w*)\b|(?:DB|데이터베이스).{0,20}(?:조회|수정|실행|접근))"),
        "git": re.compile(r"(?i)(\bgit\b.{0,30}\b(?:status|diff|commit|push|pull|checkout|merge|execut\w*|ran|run)\b|Git.{0,20}(?:실행|커밋|푸시|조회))"),
        "deploy": re.compile(r"(?i)(\bdeploy(?:ed|ment|ing)?\b|배포(?:함|했다|실행|완료))"),
        "artifact": re.compile(r"(?i)(\bartifact\b.{0,30}\b(?:creat\w*|produc\w*|writ\w*|exist\w*)\b|산출물.{0,20}(?:생성|작성|존재))"),
    }
    denial_patterns = {
        "tool": re.compile(r"(?i)(\b(?:no|without|zero)\s+tools?\b|도구.{0,12}(?:없|않|미사용))"),
        "file": re.compile(r"(?i)(\b(?:no|without|zero)\s+(?:file|files)\b|파일.{0,12}(?:없|않|미사용))"),
        "memory": re.compile(r"(?i)(\b(?:no|without|zero)\s+memory\b|메모리.{0,12}(?:없|않|미사용))"),
        "db": re.compile(r"(?i)(\b(?:no|without|zero)\s+(?:db|database|sql)\b|(?:DB|데이터베이스).{0,12}(?:없|않|미사용))"),
        "git": re.compile(r"(?i)(\b(?:no|without|zero)\s+git\b|Git.{0,12}(?:없|않|미사용))"),
        "deploy": re.compile(r"(?i)(\b(?:no|without|zero)\s+deploy(?:ment)?\b|배포.{0,12}(?:없|않|미실행))"),
    }

    def observed_actions(envelope: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, bool]:
        observed = {key: False for key in claim_patterns}
        observed["artifact"] = any(
            isinstance(item, Mapping)
            and isinstance(item.get("path"), str)
            and Path(item["path"]).expanduser().exists()
            for item in result.get("artifacts", [])
            if isinstance(result.get("artifacts"), list)
        )
        history = envelope.get("history")
        messages = history.get("messages") if isinstance(history, Mapping) else None
        if not isinstance(messages, list):
            return observed
        user_indexes = [index for index, item in enumerate(messages)
                        if isinstance(item, Mapping) and item.get("role") == "user"]
        if not user_indexes:
            # A bounded history page can contain only stale messages from an
            # earlier turn.  Without the current user boundary, no action on
            # that page is attributable to this run.
            return observed
        start = user_indexes[-1]
        for message in messages[start:]:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                block_type = str(block.get("type") or "").lower().replace("_", "")
                if block_type not in {"toolcall", "tooluse"}:
                    continue
                observed["tool"] = True
                name = str(block.get("name") or block.get("toolName") or "").lower()
                arguments = json.dumps(block.get("arguments") or block.get("input") or {}, ensure_ascii=False).lower()
                joined = f"{name} {arguments}"
                if name in {"read", "write", "edit", "apply_patch", "view_image"}:
                    observed["file"] = True
                if name in {"write", "edit", "apply_patch"} or re.search(r"(?:^|\s)(?:tee|touch|cp|mv)\s|(?<![0-9])>(?![>&])", joined):
                    observed["file"] = True
                if name in {"read", "view_image"} or re.search(r"(?:^|\s)(?:cat|sed|rg|head|tail|stat)\s", joined):
                    observed["file"] = True
                if "memory" in name or "/memory/" in arguments or "memory.md" in arguments:
                    observed["memory"] = True
                if re.search(r"\b(?:sqlite3|psql|supabase|postgres|mysql)\b", joined):
                    observed["db"] = True
                if re.search(r"\bgit\b", joined):
                    observed["git"] = True
                if re.search(r"\bdeploy(?:ed|ment|ing)?\b|\bwrangler\s+(?:deploy|pages)\b", joined):
                    observed["deploy"] = True
        return observed

    def schema(result: Mapping[str, Any], _envelope: Mapping[str, Any]) -> str | None:
        required = {"status", "summary", "evidence", "artifacts", "scope"}
        if not required.issubset(result):
            return "MISSING_REQUIRED_FIELD"
        if result.get("status") not in {"completed", "blocked", "failed"}:
            return "INVALID_STATUS"
        if not isinstance(result.get("summary"), str) or not result["summary"].strip():
            return "INVALID_SUMMARY"
        return None

    def evidence(result: Mapping[str, Any], envelope: Mapping[str, Any]) -> str | None:
        items = result.get("evidence")
        if not isinstance(items, list) or not items:
            return "MISSING_EVIDENCE"
        if not all(isinstance(item, Mapping) and isinstance(item.get("type"), str) for item in items):
            return "INVALID_EVIDENCE"
        observed = observed_actions(envelope, result)
        for item in items:
            claim = f"{item.get('type', '')} {item.get('detail', '')}"
            for action, pattern in claim_patterns.items():
                if pattern.search(claim) and not observed[action]:
                    return "EVIDENCE_UNVERIFIED"
            for action, pattern in denial_patterns.items():
                if pattern.search(claim) and observed[action]:
                    return "EVIDENCE_CONTRADICTION"
        return None

    def artifacts(result: Mapping[str, Any], _envelope: Mapping[str, Any]) -> str | None:
        items = result.get("artifacts")
        if not isinstance(items, list):
            return "INVALID_ARTIFACTS"
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                return "INVALID_ARTIFACT"
            if not Path(item["path"]).expanduser().exists():
                return "ARTIFACT_NOT_FOUND"
        return None

    def scope(result: Mapping[str, Any], _envelope: Mapping[str, Any]) -> str | None:
        value = result.get("scope")
        if not isinstance(value, Mapping) or value.get("compliant") is not True:
            return "NON_COMPLIANT"
        violations = value.get("violations", [])
        if not isinstance(violations, list) or violations:
            return "SCOPE_VIOLATION"
        return None

    return CompositeResultValidator(
        result_schema=schema,
        evidence=evidence,
        artifacts=artifacts,
        scope=scope,
    )


class CoreEngine:
    """Wire Core dispatch/wait/cancel to the production OpenClaw adapter."""

    def __init__(
        self,
        registry: RunRegistry,
        agents: AgentRegistry,
        models: ModelRegistry,
        adapter: WorkerTransport,
        *,
        policy_engine: RuntimePolicyEngine | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.registry = registry
        self.agents = agents
        self.models = models
        self.adapter = adapter
        self.policy_engine = policy_engine or RuntimePolicyEngine()
        self._clock = clock
        self._lock = threading.RLock()
        self._deadline_timers: dict[str, threading.Timer] = {}

    def dispatch(
        self,
        *,
        agent_id: str,
        message: str,
        timeout_seconds: float,
        core_run_id: str | None = None,
        idempotency_key: str | None = None,
        goal_contract: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._dispatch(
            agent_id=agent_id, message=message, timeout_seconds=timeout_seconds,
            core_run_id=core_run_id, idempotency_key=idempotency_key,
            goal_contract=goal_contract, session_key=None, parent_core_run_id=None, context_reset_count=0,
        )

    def _dispatch(
        self,
        *,
        agent_id: str,
        message: str,
        timeout_seconds: float,
        core_run_id: str | None,
        idempotency_key: str | None,
        goal_contract: Mapping[str, Any] | None,
        session_key: str | None,
        parent_core_run_id: str | None,
        context_reset_count: int,
    ) -> dict[str, Any]:
        with self._lock:
            registration = self.agents.require(agent_id)
            if not isinstance(message, str) or not message.strip():
                raise ValueError("INVALID_MESSAGE")
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
                raise ValueError("INVALID_TIMEOUT")
            actual_id = core_run_id or f"core-{uuid.uuid4().hex}"
            key = idempotency_key or actual_id
            contract = normalize_goal_contract(goal_contract)
            digest = _request_hash(agent_id, message, float(timeout_seconds), contract.goal_id)
            try:
                profile = self.models.resolve(registration)
                policy = self._policy_metadata(profile)
                resolution_error = ""
            except PolicyResolutionError:
                profile = None
                policy = {
                    "runtime_class": RuntimeClass.UNKNOWN.value,
                    "model_profile": registration.runtime_model_id,
                    "policy_profile": "UNKNOWN",
                    "context_policy": "MANUAL",
                    "fallback_policy": "NONE",
                }
                resolution_error = "MODEL_PROFILE_MISMATCH"
            record, created = self.registry.create(
                core_run_id=actual_id,
                agent_id=agent_id,
                idempotency_key=key,
                request_hash=digest,
                policy=policy,
                goal_contract=contract,
                parent_core_run_id=parent_core_run_id,
                context_reset_count=context_reset_count,
            )
            if not created:
                return record
            if resolution_error:
                self.registry.update_policy(
                    actual_id,
                    event_code="MODEL_PROFILE_MISMATCH",
                    event_details={"model_profile": registration.runtime_model_id},
                    policy_status="BLOCKED",
                )
                return self.registry.transition(actual_id, CoreRunStatus.BLOCKED, reason=resolution_error)
            assert profile is not None
            try:
                submit_payload = {
                    "message": message,
                    "agentId": agent_id,
                    "idempotencyKey": key,
                    "timeout": timeout_seconds,
                }
                if session_key is not None:
                    submit_payload["sessionKey"] = session_key
                binding = self.adapter.submit(
                    actual_id,
                    submit_payload,
                )
                if session_key is not None and binding.session_key != session_key:
                    return self.registry.transition(
                        actual_id, CoreRunStatus.FAIL, reason="FRESH_SESSION_IDENTITY_UNVERIFIED"
                    )
            except (AdapterError, TimeoutError) as exc:
                reason = "TRANSPORT_FAILURE" if isinstance(exc, (TransportError, TimeoutError)) else "DISPATCH_REJECTED"
                return self.registry.transition(actual_id, CoreRunStatus.FAIL, reason=reason)
            running = self.registry.transition(actual_id, CoreRunStatus.RUNNING, binding=binding)
            self._schedule_runtime_deadline(actual_id, profile)
            return running

    def observe_progress_checkpoint(self, core_run_id: str, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate verified execution metadata without storing agent reasoning."""

        with self._lock:
            record = self._require(core_run_id)
            if CoreRunStatus(record["status"]) in _TERMINAL:
                return record
            contract = normalize_goal_contract({
                key: record["goal_contract"][key]
                for key in ("primary_objective", "allowed_scope", "forbidden_scope", "expected_result", "completion_conditions")
            })
            normalized = normalize_progress_checkpoint(checkpoint, contract)
            goal_state = dict((record.get("policy_state") or {}).get("goal") or {})
            previous = dict(record.get("verified_progress") or {})
            completed = sorted(set(previous.get("completed_conditions", [])) | set(normalized["completed_conditions"]))
            artifacts = sorted(set(previous.get("artifacts", [])) | set(normalized["artifact_delta"]))
            evidence = sorted(set(previous.get("evidence", [])) | set(normalized["evidence_delta"]))
            scopes = sorted(set(previous.get("scope", [])) | set(normalized["scope_delta"]))
            verified = {"completed_conditions": completed, "artifacts": artifacts, "evidence": evidence, "scope": scopes}
            event_code = None
            if normalized["goal_id"] != contract.goal_id:
                event_code = "OBJECTIVE_REPLACEMENT"
            elif any(item in contract.forbidden_scope or item not in contract.allowed_scope for item in normalized["scope_delta"]):
                event_code = "SCOPE_EXPANSION"
            else:
                changed = verified != previous
                signature_source = {
                    "current_step": normalized["current_step"],
                    "blocking_reason": normalized["blocking_reason"],
                    "remaining_conditions": normalized["remaining_conditions"],
                    "verified": verified,
                }
                signature = hashlib.sha256(json.dumps(signature_source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if not changed and signature == goal_state.get("last_checkpoint_signature"):
                    event_code = "NO_PROGRESS"
                goal_state["last_checkpoint_signature"] = signature
            if event_code is None:
                goal_state["drift_count"] = 0
                return self.registry.update_goal_state(
                    core_run_id, checkpoint=normalized, verified_progress=verified,
                    goal_state=goal_state, goal_status="NORMAL", reinjection_ready=False,
                )
            drift_count = int(goal_state.get("drift_count", 0)) + 1
            goal_state["drift_count"] = drift_count
            self.registry.update_goal_state(
                core_run_id, checkpoint=normalized, verified_progress=verified, goal_state=goal_state,
                goal_status="GUARDED" if drift_count == 1 else "DRIFT", reinjection_ready=True,
                event_code=event_code, event_details={"occurrence": drift_count},
            )
            if drift_count >= 2:
                profile = self._profile_for_record(self._require(core_run_id))
                return self._cancel_for_policy(
                    core_run_id, profile, event_code, reason="LOCAL_LLM_GOAL_DRIFT", event_already_recorded=True,
                )
            return self._require(core_run_id)

    def fresh_context(self, core_run_id: str) -> dict[str, Any]:
        """Start one bounded child run with a new verified OpenClaw session identity."""

        with self._lock:
            source = self._require(core_run_id)
            if CoreRunStatus(source["status"]) in _TERMINAL:
                raise ValueError("SOURCE_RUN_TERMINAL")
            profile = self._profile_for_record(source)
            if profile.context_policy not in {"FRESH_ON_LOOP", "FRESH_ON_RETRY"}:
                raise ValueError("FRESH_CONTEXT_NOT_ALLOWED")
            if profile.context_policy == "FRESH_ON_LOOP" and source.get("goal_reinjection_ready") is not True:
                raise ValueError("FRESH_CONTEXT_NOT_READY")
            reset_count = int(source.get("context_reset_count", 0))
            if reset_count >= profile.max_context_resets:
                package = self._escalation_package(source, "CONTEXT_RESET_FAILED")
                self.registry.update_goal_state(
                    core_run_id, event_code="ESCALATION_REQUIRED", goal_status="DRIFT",
                    escalation_reason="CONTEXT_RESET_FAILED", escalation_package=package,
                )
                try:
                    binding = self.adapter.cancel(core_run_id)
                except (AdapterError, TimeoutError):
                    binding = None
                self._clear_runtime_deadline(core_run_id)
                return self.registry.transition(
                    core_run_id, CoreRunStatus.BLOCKED, binding=binding, reason="ESCALATION_REQUIRED",
                )
            contract = dict(source["goal_contract"])
            verified = dict(source.get("verified_progress") or {})
            remaining_conditions = sorted(
                set(contract["completion_conditions"])
                - set(verified.get("completed_conditions", []))
            )
            execution_package = {
                "goal_contract": contract,
                "verified_progress": verified,
                "needed_artifacts": {
                    "expected_result": contract["expected_result"],
                    "instruction": "Supply only artifacts actually produced for the original goal.",
                },
                "needed_evidence": {
                    "completion_conditions": remaining_conditions,
                    "instruction": "Supply only evidence actually established while executing the original goal.",
                },
            }
            result_field_contract = {
                "status": "required string: use completed only when the actual goal work completed; otherwise use failed or blocked",
                "summary": "required non-empty string: concise actual outcome",
                "evidence": (
                    "required non-empty array: one or more actual verifiable evidence objects, each with string fields "
                    "type and detail. For text-only work with no actions, use exactly an observed runtime fact such as "
                    '{"type":"runtime_observation","detail":"Structured assistant result returned successfully"}. '
                    "Never claim a tool call, file read or write, memory operation, artifact, database action, Git action, "
                    "or deployment unless that action actually occurred in this run"
                ),
                "artifacts": "required array: use [] when none exist; every item must be an object with a string path",
                "scope": {
                    "compliant": "required boolean: true",
                    "violations": "required array: []",
                },
                "progress_checkpoint": {
                    "goal_id": "string: must equal goal_contract.goal_id",
                    "current_step": "string: actual current execution step code",
                    "completed_conditions": "array: only conditions actually verified",
                    "remaining_conditions": "array: conditions not yet verified",
                    "blocking_reason": "string: actual blocker code, or NONE when absent",
                    "artifact_delta": "array: newly verified artifact codes",
                    "scope_delta": "array: scopes actually used",
                    "evidence_delta": "array: newly verified evidence codes",
                },
            }
            message = (
                "Execute the immutable original goal from this verified execution package: "
                + json.dumps(execution_package, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                + " Do not replace the objective or extend its allowed scope. Perform the goal work before "
                + "reporting its outcome. The final assistant response must be exactly one raw JSON object. "
                + "Do not use Markdown code fences and do not add prose before or after the JSON object. "
                + "The top-level fields status, summary, evidence, artifacts, and scope are mandatory. "
                + "Use this output field contract; populate every value from the actual result and never claim a condition, "
                + "artifact, or evidence that was not verified: "
                + json.dumps(result_field_contract, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            )
            child_id = f"{core_run_id}-ctx-{reset_count + 1}-{uuid.uuid4().hex[:8]}"
            session_key = f"agent:{source['agent_id']}:fast-gateway-{uuid.uuid4().hex}"
            source_goal_state = dict((source.get("policy_state") or {}).get("goal") or {})
            binding = None if source_goal_state.get("openclaw_completed") is True else self.adapter.cancel(core_run_id)
            self._clear_runtime_deadline(core_run_id)
            self.registry.transition(
                core_run_id, CoreRunStatus.CANCELLED, binding=binding, reason="FRESH_CONTEXT_RESET",
            )
            child = self._dispatch(
                agent_id=source["agent_id"], message=message,
                timeout_seconds=min(float(source.get("max_runtime") or profile.max_runtime), profile.max_runtime),
                core_run_id=child_id, idempotency_key=f"{source['idempotency_key']}:ctx:{reset_count + 1}",
                goal_contract={key: contract[key] for key in _GOAL_INPUT_FIELDS},
                session_key=session_key, parent_core_run_id=core_run_id, context_reset_count=reset_count + 1,
            )
            return self.registry.update_goal_state(
                child["core_run_id"],
                goal_state={"drift_count": int(source_goal_state.get("drift_count", 0))},
            )

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        with self._lock:
            record = self._require(core_run_id)
            if CoreRunStatus(record["status"]) in _TERMINAL:
                return record
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
                raise ValueError("INVALID_TIMEOUT")
            profile = self._profile_for_record(record)
            requested_wait = float(timeout_seconds)
            while True:
                record = self._require(core_run_id)
                elapsed_before = self._runtime_seconds(record)
                self.registry.update_policy(core_run_id, runtime_seconds=elapsed_before)
                remaining = profile.max_runtime - elapsed_before
                if remaining <= 0:
                    return self._cancel_for_policy(core_run_id, profile, "RUNTIME_LIMIT")
                bounded_wait = min(requested_wait, remaining)
                try:
                    outcome = self.adapter.wait(core_run_id, timeout_seconds=bounded_wait)
                except TimeoutError:
                    outcome = AdapterOutcome(CoreRunStatus.TIMEOUT, "TRANSPORT_TIMEOUT")
                except AdapterError as exc:
                    reason = "TRANSPORT_FAILURE" if isinstance(exc, TransportError) else "WAIT_REJECTED"
                    outcome = AdapterOutcome(CoreRunStatus.FAIL, reason)
                elapsed = self._runtime_seconds(self._require(core_run_id))
                self.registry.update_policy(core_run_id, runtime_seconds=elapsed)
                if elapsed >= profile.max_runtime and outcome.status in {CoreRunStatus.RUNNING, CoreRunStatus.TIMEOUT}:
                    return self._cancel_for_policy(core_run_id, profile, "RUNTIME_LIMIT")
                if outcome.status != CoreRunStatus.TIMEOUT or profile.runtime_class != RuntimeClass.LOCAL:
                    break
                if outcome.reason != "OPENCLAW_TIMEOUT":
                    break
                # A LOCAL agent.wait window is not the run deadline. Continue
                # only while the absolute dispatch-based policy budget remains.
                # If a gateway returns timeout immediately, yield RUNNING to
                # avoid an unbounded busy loop; the deadline timer remains live.
                meaningful_wait = min(1.0, bounded_wait / 2.0)
                if elapsed - elapsed_before < meaningful_wait:
                    return self._require(core_run_id)
            if outcome.status == CoreRunStatus.RUNNING:
                return self._require(core_run_id)
            if outcome.status == CoreRunStatus.TIMEOUT:
                # A bounded wait timeout is terminal for Core, but the
                # underlying OpenClaw run may still be alive.  Best-effort
                # abort it on the adapter's independent connection before
                # recording TIMEOUT, so no worker is left orphaned.
                try:
                    self.adapter.cancel(core_run_id)
                except (AdapterError, TimeoutError):
                    self.registry.update_policy(
                        core_run_id,
                        event_code="TIMEOUT_ABORT_FAILED",
                        cancel_reason="TIMEOUT_ABORT_FAILED",
                        policy_status="GUARDED",
                    )
                self._clear_runtime_deadline(core_run_id)
                return self.registry.transition(core_run_id, CoreRunStatus.TIMEOUT, outcome=outcome)
            if outcome.result is not None and isinstance(outcome.result.get("progress_checkpoint"), Mapping):
                try:
                    observed = self.observe_progress_checkpoint(core_run_id, outcome.result["progress_checkpoint"])
                except ValueError:
                    self.registry.update_goal_state(
                        core_run_id, event_code="GOAL_CHECKPOINT_INVALID", goal_status="DRIFT",
                    )
                    self._clear_runtime_deadline(core_run_id)
                    return self.registry.transition(
                        core_run_id, CoreRunStatus.BLOCKED, reason="GOAL_CHECKPOINT_INVALID",
                    )
                if CoreRunStatus(observed["status"]) in _TERMINAL:
                    self._clear_runtime_deadline(core_run_id)
                    return observed
                if observed.get("goal_status") in {"GUARDED", "DRIFT"}:
                    goal_state = dict((observed.get("policy_state") or {}).get("goal") or {})
                    goal_state["openclaw_completed"] = True
                    self.registry.update_goal_state(core_run_id, goal_state=goal_state)
                    self._clear_runtime_deadline(core_run_id)
                    return self._require(core_run_id)
            self._clear_runtime_deadline(core_run_id)
            return self.registry.transition(core_run_id, outcome.status, outcome=outcome)

    def observe_runtime_event(self, core_run_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """Trusted hook for verified runtime events; it is intentionally not an HTTP endpoint."""

        with self._lock:
            record = self._require(core_run_id)
            if CoreRunStatus(record["status"]) in _TERMINAL:
                return record
            if not isinstance(event, Mapping):
                raise ValueError("INVALID_RUNTIME_EVENT")
            profile = self._profile_for_record(record)
            decision = self.policy_engine.evaluate_event(profile, record.get("policy_state"), event)
            metric = "SUPPORTED" if event.get("kind") == "tool_call" and event.get("observable") is True else None
            event_code = decision.event_code
            if event.get("kind") == "retry" and event_code is None:
                event_code = "RETRY_RECORDED"
            raw_reason = str(event.get("reason_code") or "")
            reason_details = (
                {"reason_signature": hashlib.sha256(raw_reason.encode("utf-8")).hexdigest()}
                if raw_reason else {}
            )
            self.registry.update_policy(
                core_run_id,
                retry_count=decision.retry_count,
                tool_call_count=decision.tool_call_count if metric == "SUPPORTED" else None,
                tool_call_metric=metric,
                policy_state=decision.state,
                event_code=event_code,
                event_details=reason_details if event_code else None,
                cancel_reason=decision.cancel_reason or None,
            )
            if decision.cancel_reason:
                return self._cancel_for_policy(
                    core_run_id,
                    profile,
                    event_code or "LOOP_SUSPECTED",
                    reason=decision.cancel_reason,
                    event_already_recorded=True,
                )
            return self._require(core_run_id)

    def cancel(self, core_run_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._require(core_run_id)
            status = CoreRunStatus(record["status"])
            if status == CoreRunStatus.CANCELLED:
                return record
            if status in _TERMINAL:
                # A prior timeout/policy failure may have finalized Core
                # while the OpenClaw worker remained active.  Permit one
                # idempotent cleanup attempt without reopening Core state.
                if status in {CoreRunStatus.TIMEOUT, CoreRunStatus.FAIL} and record.get("openclaw_binding"):
                    try:
                        self.adapter.cancel(core_run_id)
                    except (AdapterError, TimeoutError):
                        pass
                    return self._require(core_run_id)
                raise ValueError(f"RUN_ALREADY_TERMINAL:{status.value}")
            if status == CoreRunStatus.QUEUED:
                return self.registry.transition(core_run_id, CoreRunStatus.CANCELLED, reason="CORE_CANCELLED")
            binding = self.adapter.cancel(core_run_id)
            self._clear_runtime_deadline(core_run_id)
            return self.registry.transition(
                core_run_id,
                CoreRunStatus.CANCELLED,
                binding=binding,
                reason="OPENCLAW_ABORTED",
            )

    def status(self, core_run_id: str) -> dict[str, Any]:
        return self._require(core_run_id)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.registry.recent(limit)

    def _require(self, core_run_id: str) -> dict[str, Any]:
        record = self.registry.get(core_run_id)
        if record is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        return record

    @staticmethod
    def _policy_metadata(profile: RuntimeModelProfile) -> dict[str, Any]:
        return {
            "runtime_class": profile.runtime_class.value,
            "model_profile": profile.model_id,
            "policy_profile": profile.policy_profile,
            "max_runtime": profile.max_runtime,
            "max_retries": profile.max_retries,
            "max_tool_calls": profile.max_tool_calls,
            "context_policy": profile.context_policy,
            "fallback_policy": profile.fallback_policy,
            "max_context_resets": profile.max_context_resets,
        }

    @staticmethod
    def _escalation_package(record: Mapping[str, Any], reason: str) -> dict[str, Any]:
        contract = record.get("goal_contract") or {}
        verified = record.get("verified_progress") or {}
        remaining = list((record.get("progress_checkpoint") or {}).get("remaining_conditions") or contract.get("completion_conditions") or [])
        return {
            "goal_contract": copy.deepcopy(dict(contract)),
            "verified_progress": copy.deepcopy(dict(verified)),
            "original_goal": contract.get("primary_objective"),
            "verified_completed_work": list(verified.get("completed_conditions") or []),
            "remaining_work": remaining,
            "known_blocker": reason,
            "allowed_scope": list(contract.get("allowed_scope") or []),
            "forbidden_scope": list(contract.get("forbidden_scope") or []),
            "relevant_evidence": list(verified.get("evidence") or []),
            "source_agent": record.get("agent_id"),
            "source_runtime_class": record.get("runtime_class"),
            "failure_reason": reason,
        }

    def _profile_for_record(self, record: Mapping[str, Any]) -> RuntimeModelProfile:
        model_id = record.get("model_profile")
        if not isinstance(model_id, str):
            raise ValueError("MODEL_PROFILE_MISMATCH")
        profile = self.models.require(model_id)
        if profile.policy_profile != record.get("policy_profile"):
            raise ValueError("MODEL_PROFILE_MISMATCH")
        return profile

    def _runtime_seconds(self, record: Mapping[str, Any]) -> float:
        started_at = record.get("started_at")
        if not isinstance(started_at, str):
            return 0.0
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return 0.0
        return max(0.0, (self._clock().astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())

    def _cancel_for_policy(
        self,
        core_run_id: str,
        profile: RuntimeModelProfile,
        event_code: str,
        *,
        reason: str | None = None,
        event_already_recorded: bool = False,
    ) -> dict[str, Any]:
        cancel_reason = reason or RuntimePolicyEngine._reason(profile, event_code)
        if not event_already_recorded:
            self.registry.update_policy(
                core_run_id,
                event_code=event_code,
                cancel_reason=cancel_reason,
                policy_status="GUARDED",
            )
        record = self._require(core_run_id)
        if (
            cancel_reason == "LOCAL_LLM_GOAL_DRIFT"
            and int(record.get("context_reset_count", 0)) >= int(record.get("max_context_resets", 0))
        ):
            self.registry.update_goal_state(
                core_run_id,
                event_code="ESCALATION_REQUIRED",
                goal_status="DRIFT",
                escalation_reason=cancel_reason,
                escalation_package=self._escalation_package(record, cancel_reason),
            )
        try:
            binding = self.adapter.cancel(core_run_id)
        except (AdapterError, TimeoutError):
            self._clear_runtime_deadline(core_run_id)
            return self.registry.transition(core_run_id, CoreRunStatus.FAIL, reason="POLICY_ABORT_FAILED")
        self._clear_runtime_deadline(core_run_id)
        return self.registry.transition(
            core_run_id,
            CoreRunStatus.CANCELLED,
            binding=binding,
            reason=cancel_reason,
        )

    def _schedule_runtime_deadline(self, core_run_id: str, profile: RuntimeModelProfile) -> None:
        timer = threading.Timer(profile.max_runtime, self._enforce_runtime_deadline, args=(core_run_id,))
        timer.daemon = True
        old = self._deadline_timers.pop(core_run_id, None)
        if old is not None:
            old.cancel()
        self._deadline_timers[core_run_id] = timer
        timer.start()

    def _clear_runtime_deadline(self, core_run_id: str) -> None:
        timer = self._deadline_timers.pop(core_run_id, None)
        if timer is not None:
            timer.cancel()

    def _enforce_runtime_deadline(self, core_run_id: str) -> None:
        with self._lock:
            record = self.registry.get(core_run_id)
            if record is None or CoreRunStatus(record["status"]) in _TERMINAL:
                self._clear_runtime_deadline(core_run_id)
                return
            try:
                profile = self._profile_for_record(record)
            except (PolicyResolutionError, ValueError):
                self.registry.update_policy(
                    core_run_id,
                    event_code="MODEL_PROFILE_MISMATCH",
                    cancel_reason="MODEL_PROFILE_MISMATCH",
                    policy_status="BLOCKED",
                )
                self._clear_runtime_deadline(core_run_id)
                self.registry.transition(core_run_id, CoreRunStatus.BLOCKED, reason="MODEL_PROFILE_MISMATCH")
                return
            self.registry.update_policy(core_run_id, runtime_seconds=profile.max_runtime)
            self._cancel_for_policy(core_run_id, profile, "RUNTIME_LIMIT")
