"""Fail-closed runtime-model policy resolution and deterministic loop guards."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class RuntimeClass(StrEnum):
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"
    UNKNOWN = "UNKNOWN"


class PolicyResolutionError(ValueError):
    """A registered agent/model/profile relationship could not be proven."""


@dataclass(frozen=True)
class RuntimeModelProfile:
    model_id: str
    runtime_class: RuntimeClass
    policy_profile: str
    max_runtime: float
    max_retries: int
    max_tool_calls: int | None
    loop_guard: Mapping[str, Any]
    context_policy: str
    fallback_policy: str
    max_context_resets: int
    execution_budget: float | None = None
    finalization_recovery_budget: float | None = None

    def __post_init__(self) -> None:
        if self.execution_budget is None and self.finalization_recovery_budget is None:
            if self.runtime_class is RuntimeClass.LOCAL:
                recovery = float(self.max_runtime) / 5.0
                object.__setattr__(self, "execution_budget", float(self.max_runtime) - recovery)
                object.__setattr__(self, "finalization_recovery_budget", recovery)
            else:
                object.__setattr__(self, "execution_budget", float(self.max_runtime))
                object.__setattr__(self, "finalization_recovery_budget", 0.0)


_CONTEXT_POLICIES = {"REUSE", "FRESH_ON_RETRY", "FRESH_ON_LOOP", "MANUAL"}
_FALLBACK_POLICIES = {"NONE", "MANUAL", "ELIGIBLE"}
_SENSITIVE_KEYS = {
    "token", "credential", "credentials", "api_key", "apikey", "password", "secret",
    "authorization", "cookie", "prompt", "message", "content",
}
_CONTRACT_FIELDS = {
    "primary_objective", "allowed_scope", "forbidden_scope", "expected_result", "completion_conditions",
}
_SAFE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_.:/-]{0,255}$")
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+\S+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+|sk-[a-z0-9_-]{8,})")


@dataclass(frozen=True)
class GoalContract:
    goal_id: str
    primary_objective: str
    allowed_scope: tuple[str, ...]
    forbidden_scope: tuple[str, ...]
    expected_result: str
    completion_conditions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "primary_objective": self.primary_objective,
            "allowed_scope": list(self.allowed_scope),
            "forbidden_scope": list(self.forbidden_scope),
            "expected_result": self.expected_result,
            "completion_conditions": list(self.completion_conditions),
        }


def _normalized_code(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"INVALID_GOAL_CONTRACT:{field}")
    normalized = value.strip().upper().replace(" ", "_")
    if not _SAFE_CODE.fullmatch(normalized):
        raise ValueError(f"INVALID_GOAL_CONTRACT:{field}")
    return normalized


def _normalized_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"INVALID_GOAL_CONTRACT:{field}")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 512 or _SECRET_TEXT.search(normalized):
        raise ValueError(f"INVALID_GOAL_CONTRACT:{field}")
    return normalized


def _normalized_code_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"INVALID_GOAL_CONTRACT:{field}")
    result = tuple(dict.fromkeys(_normalized_code(item, field) for item in value))
    return result


def normalize_goal_contract(value: Mapping[str, Any] | None) -> GoalContract:
    """Create a compact immutable execution contract without preserving a prompt."""

    if value is None:
        value = {
            "primary_objective": "LEGACY_VALIDATED_REQUEST",
            "allowed_scope": ["REQUESTED_SCOPE"],
            "forbidden_scope": ["UNREQUESTED_SCOPE"],
            "expected_result": "VALIDATED_RESULT",
            "completion_conditions": ["RESULT_VALIDATED"],
        }
    if not isinstance(value, Mapping) or set(value) != _CONTRACT_FIELDS:
        raise ValueError("INVALID_GOAL_CONTRACT:FIELDS")
    canonical = {
        "primary_objective": _normalized_text(value["primary_objective"], "primary_objective"),
        "allowed_scope": list(_normalized_code_list(value["allowed_scope"], "allowed_scope")),
        "forbidden_scope": list(_normalized_code_list(value["forbidden_scope"], "forbidden_scope")),
        "expected_result": _normalized_text(value["expected_result"], "expected_result"),
        "completion_conditions": list(
            _normalized_code_list(value["completion_conditions"], "completion_conditions")
        ),
    }
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return GoalContract(goal_id="goal-" + hashlib.sha256(encoded.encode()).hexdigest()[:24], **canonical)


def normalize_progress_checkpoint(value: Mapping[str, Any], contract: GoalContract) -> dict[str, Any]:
    """Accept only verifiable, bounded checkpoint metadata; never free-form reasoning."""

    allowed = {
        "goal_id", "current_step", "completed_conditions", "remaining_conditions", "blocking_reason",
        "artifact_delta", "scope_delta", "evidence_delta",
    }
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError("INVALID_PROGRESS_CHECKPOINT:FIELDS")
    goal_id = value.get("goal_id")
    if not isinstance(goal_id, str) or not goal_id:
        raise ValueError("INVALID_PROGRESS_CHECKPOINT:goal_id")
    completed = _normalized_code_list(value.get("completed_conditions", []), "completed_conditions") if value.get("completed_conditions") else ()
    remaining = _normalized_code_list(value.get("remaining_conditions", []), "remaining_conditions") if value.get("remaining_conditions") else ()
    artifacts = _normalized_code_list(value.get("artifact_delta", []), "artifact_delta") if value.get("artifact_delta") else ()
    scopes = _normalized_code_list(value.get("scope_delta", []), "scope_delta") if value.get("scope_delta") else ()
    evidence = _normalized_code_list(value.get("evidence_delta", []), "evidence_delta") if value.get("evidence_delta") else ()
    if not set(completed).issubset(contract.completion_conditions):
        raise ValueError("UNVERIFIED_COMPLETION_CONDITION")
    if not set(remaining).issubset(contract.completion_conditions):
        raise ValueError("UNVERIFIED_REMAINING_CONDITION")
    return {
        "goal_id": goal_id,
        "current_step": _normalized_code(value.get("current_step", "UNSPECIFIED"), "current_step"),
        "completed_conditions": list(completed),
        "remaining_conditions": list(remaining),
        "blocking_reason": _normalized_code(value.get("blocking_reason", "NONE"), "blocking_reason"),
        "artifact_delta": list(artifacts),
        "scope_delta": list(scopes),
        "evidence_delta": list(evidence),
    }


class ModelRegistry:
    """Model-id to runtime policy mapping, independent from agent identity."""

    def __init__(self, models: Mapping[str, RuntimeModelProfile]) -> None:
        self._models = dict(models)

    @classmethod
    def load(cls, path: str | Path) -> "ModelRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or not isinstance(raw.get("models"), Mapping):
            raise ValueError("INVALID_MODEL_REGISTRY")
        models: dict[str, RuntimeModelProfile] = {}
        for model_id, value in raw["models"].items():
            if not isinstance(model_id, str) or not model_id or not isinstance(value, Mapping):
                raise ValueError("INVALID_MODEL_REGISTRY")
            runtime_class_raw = value.get("runtime_class")
            profile_name = value.get("policy_profile")
            try:
                runtime_class = RuntimeClass(runtime_class_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"INVALID_RUNTIME_CLASS:{model_id}") from exc
            if runtime_class is RuntimeClass.UNKNOWN:
                raise ValueError(f"UNKNOWN_RUNTIME_CLASS_NOT_REGISTRABLE:{model_id}")
            if not isinstance(profile_name, str) or not profile_name:
                raise ValueError(f"INVALID_POLICY_PROFILE:{model_id}")
            max_runtime = value.get("max_runtime")
            execution_budget = value.get("execution_budget")
            finalization_recovery_budget = value.get("finalization_recovery_budget")
            max_retries = value.get("max_retries")
            max_tool_calls = value.get("max_tool_calls")
            loop_guard = value.get("loop_guard")
            context_policy = value.get("context_policy")
            fallback_policy = value.get("fallback_policy")
            max_context_resets = value.get("max_context_resets", 1)
            if isinstance(max_runtime, bool) or not isinstance(max_runtime, (int, float)) or max_runtime <= 0:
                raise ValueError(f"INVALID_MAX_RUNTIME:{model_id}")
            if runtime_class is RuntimeClass.LOCAL:
                if (
                    isinstance(execution_budget, bool)
                    or not isinstance(execution_budget, (int, float))
                    or execution_budget <= 0
                ):
                    raise ValueError(f"INVALID_EXECUTION_BUDGET:{model_id}")
                if (
                    isinstance(finalization_recovery_budget, bool)
                    or not isinstance(finalization_recovery_budget, (int, float))
                    or finalization_recovery_budget <= 0
                ):
                    raise ValueError(f"INVALID_FINALIZATION_RECOVERY_BUDGET:{model_id}")
                if abs(float(execution_budget) + float(finalization_recovery_budget) - float(max_runtime)) > 1e-9:
                    raise ValueError(f"INVALID_RUNTIME_BUDGET_SUM:{model_id}")
            else:
                if execution_budget is not None or finalization_recovery_budget is not None:
                    raise ValueError(f"CLOUD_RUNTIME_BUDGET_OVERRIDE_FORBIDDEN:{model_id}")
                execution_budget = max_runtime
                finalization_recovery_budget = 0.0
            if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
                raise ValueError(f"INVALID_MAX_RETRIES:{model_id}")
            if max_tool_calls is not None and (
                isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls < 1
            ):
                raise ValueError(f"INVALID_MAX_TOOL_CALLS:{model_id}")
            if not isinstance(loop_guard, Mapping):
                raise ValueError(f"INVALID_LOOP_GUARD:{model_id}")
            threshold = loop_guard.get("consecutive_threshold")
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 2:
                raise ValueError(f"INVALID_LOOP_THRESHOLD:{model_id}")
            if context_policy not in _CONTEXT_POLICIES:
                raise ValueError(f"INVALID_CONTEXT_POLICY:{model_id}")
            if fallback_policy not in _FALLBACK_POLICIES:
                raise ValueError(f"INVALID_FALLBACK_POLICY:{model_id}")
            if isinstance(max_context_resets, bool) or not isinstance(max_context_resets, int) or max_context_resets < 0:
                raise ValueError(f"INVALID_CONTEXT_RESET_LIMIT:{model_id}")
            models[model_id] = RuntimeModelProfile(
                model_id=model_id,
                runtime_class=runtime_class,
                policy_profile=profile_name,
                max_runtime=float(max_runtime),
                execution_budget=float(execution_budget),
                finalization_recovery_budget=float(finalization_recovery_budget),
                max_retries=max_retries,
                max_tool_calls=max_tool_calls,
                loop_guard=dict(loop_guard),
                context_policy=context_policy,
                fallback_policy=fallback_policy,
                max_context_resets=max_context_resets,
            )
        return cls(models)

    def require(self, model_id: str) -> RuntimeModelProfile:
        profile = self._models.get(model_id)
        if profile is None:
            raise PolicyResolutionError("UNKNOWN_MODEL")
        return profile

    def resolve(self, registration: Any) -> RuntimeModelProfile:
        """Resolve only configured metadata; callers never supply a model/profile."""

        model_id = getattr(registration, "runtime_model_id", None)
        allowed_models = getattr(registration, "allowed_model_ids", ())
        allowed_profiles = getattr(registration, "allowed_policy_profiles", ())
        if not isinstance(model_id, str) or not model_id:
            raise PolicyResolutionError("MODEL_PROFILE_MISMATCH")
        profile = self.require(model_id)
        if model_id not in allowed_models or profile.policy_profile not in allowed_profiles:
            raise PolicyResolutionError("MODEL_PROFILE_MISMATCH")
        return profile


def _redacted_normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redacted_normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_redacted_normalize(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def normalized_action_signature(event: Mapping[str, Any]) -> str:
    """Hash a deterministic, redacted action representation for safe storage."""

    canonical = {
        "tool_name": event.get("tool_name"),
        "action_type": event.get("action_type"),
        "target": event.get("target") or event.get("resource"),
        "arguments": _redacted_normalize(event.get("arguments", {})),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalized_progress_signature(event: Mapping[str, Any]) -> str:
    canonical = {
        "error_code": event.get("error_code"),
        "result_code": event.get("result_code"),
        "state_changed": bool(event.get("state_changed", False)),
    }
    encoded = json.dumps(_redacted_normalize(canonical), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolicyDecision:
    event_code: str | None = None
    cancel_reason: str = ""
    retry_count: int = 0
    tool_call_count: int | None = None
    state: Mapping[str, Any] | None = None


class RuntimePolicyEngine:
    """Evaluate trusted observable events; it never invents unavailable metrics."""

    def evaluate_event(
        self,
        profile: RuntimeModelProfile,
        state: Mapping[str, Any] | None,
        event: Mapping[str, Any],
    ) -> PolicyDecision:
        current = dict(state or {})
        kind = event.get("kind")
        retry_count = int(current.get("retry_count", 0))
        tool_count = current.get("tool_call_count")
        if tool_count is not None:
            tool_count = int(tool_count)
        threshold = int(profile.loop_guard["consecutive_threshold"])

        if kind == "retry":
            retry_count += 1
            current["retry_count"] = retry_count
            if retry_count > profile.max_retries:
                return PolicyDecision(
                    "RETRY_LIMIT", self._reason(profile, "RETRY_LIMIT"), retry_count, tool_count, current
                )
            return PolicyDecision(retry_count=retry_count, tool_call_count=tool_count, state=current)

        if kind == "tool_call":
            if event.get("observable") is not True:
                return PolicyDecision(retry_count=retry_count, tool_call_count=tool_count, state=current)
            tool_count = int(tool_count or 0) + 1
            current["tool_call_count"] = tool_count
            signature = normalized_action_signature(event)
            previous = current.get("last_action_signature")
            repeats = int(current.get("action_repeats", 0)) + 1 if previous == signature else 1
            current.update(last_action_signature=signature, action_repeats=repeats)
            if profile.max_tool_calls is not None and tool_count > profile.max_tool_calls:
                return PolicyDecision(
                    "TOOL_BUDGET", self._reason(profile, "TOOL_BUDGET"), retry_count, tool_count, current
                )
            if repeats >= threshold:
                return PolicyDecision(
                    "LOOP_SUSPECTED", self._reason(profile, "LOOP_GUARD"), retry_count, tool_count, current
                )
            return PolicyDecision(retry_count=retry_count, tool_call_count=tool_count, state=current)

        if kind in {"error", "result"} and event.get("state_changed") is not True:
            signature = normalized_progress_signature(event)
            previous = current.get("last_progress_signature")
            repeats = int(current.get("progress_repeats", 0)) + 1 if previous == signature else 1
            current.update(last_progress_signature=signature, progress_repeats=repeats)
            if repeats >= threshold:
                return PolicyDecision(
                    "LOOP_SUSPECTED", self._reason(profile, "LOOP_GUARD"), retry_count, tool_count, current
                )
        elif event.get("state_changed") is True:
            current.pop("last_progress_signature", None)
            current["progress_repeats"] = 0
        return PolicyDecision(retry_count=retry_count, tool_call_count=tool_count, state=current)

    @staticmethod
    def _reason(profile: RuntimeModelProfile, suffix: str) -> str:
        prefix = "LOCAL_LLM" if profile.runtime_class is RuntimeClass.LOCAL else "RUNTIME_POLICY"
        return f"{prefix}_{suffix}"
