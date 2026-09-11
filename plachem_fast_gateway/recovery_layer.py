"""FastGateway Recovery Layer v0.1 (post-terminal, FAIL-only).

Design contract
---------------
- Primary dispatch path, PASS results, auth, policy and validators are
  untouched.  The Recovery Layer is a post-terminal module: it reads a run
  that already reached the FAIL terminal state and attempts at most one
  recovery dispatch through the same trusted Core path.
- 5-state outcome per recovered run:
    PASS_PRIMARY      (pre-existing terminal, never touched)
    PASS_RECOVERED    (recovery run finished PASS)
    POLICY_BLOCKED    (recovery dispatch rejected by auth/policy)
    HARD_FAIL         (recovery run reached a terminal non-PASS state)
    NEEDS_REVIEW      (no terminal verdict inside the bounded window,
                       or the recovery run could not be correlated)
- FAIL classification (4 classes):
    RESULT_RECOVERABLE   result/evidence/artifact validation failures
    SESSION_RECOVERABLE  transport/session failures
    POLICY_BLOCKED       policy abort / auth / user cancel
    UNKNOWN              anything else
- MAX_RECOVERY_ATTEMPTS = 1: the parent FAIL record is never mutated.
  Recovery state is appended to the child run record, which is the
  durable unit of recovery accounting.
- Read-only evidence: the agent-stall-detector scan is consumed as
  external evidence only; it is never mutated by this layer.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from plachem_fast_gateway.core_engine import CoreEngine, RunRegistry
from plachem_fast_gateway.openclaw_adapter import CoreRunStatus
from plachem_fast_gateway.runtime_policy import normalize_goal_contract

MAX_RECOVERY_ATTEMPTS = 1

RECOVERY_EVENT_CODE = "RECOVERY_ATTEMPTED"

RESULT_RECOVERABLE = "RESULT_RECOVERABLE"
SESSION_RECOVERABLE = "SESSION_RECOVERABLE"
POLICY_BLOCKED = "POLICY_BLOCKED"
UNKNOWN = "UNKNOWN"

RESULT_RECOVERABLE_REASONS = {
    "MISSING_RESULT",
    "OPENCLAW_RUN_MISMATCH",
    "HISTORY_WATERMARK_UNVERIFIABLE",
    "UNKNOWN_OPENCLAW_STATUS",
}
SESSION_RECOVERABLE_REASONS = {
    "TRANSPORT_FAILURE",
    "WAIT_REJECTED",
    "WORKER_FAILED",
    "TRANSPORT_TIMEOUT",
    "OPENCLAW_ERROR",
}
POLICY_BLOCKED_REASONS = {
    "POLICY_ABORT_FAILED",
    "USER_CANCEL",
}
_RESULT_VALIDATION_PREFIXES = (
    "EVIDENCE_VALIDATION_FAILED:",
    "SCOPE_VALIDATION_FAILED:",
    "ARTIFACT_VALIDATION_FAILED:",
    "RESULT_SCHEMA_VALIDATION_FAILED:",
)


def classify_fail(reason: str) -> str:
    """Rule-based classification of a FAIL reason into one of 4 classes."""

    if not isinstance(reason, str):
        return UNKNOWN
    value = reason.strip()
    if value in POLICY_BLOCKED_REASONS:
        return POLICY_BLOCKED
    if value in SESSION_RECOVERABLE_REASONS:
        return SESSION_RECOVERABLE
    if value in RESULT_RECOVERABLE_REASONS:
        return RESULT_RECOVERABLE
    for prefix in _RESULT_VALIDATION_PREFIXES:
        if value.startswith(prefix):
            return RESULT_RECOVERABLE
    return UNKNOWN


def is_recoverable_class(fail_class: str) -> bool:
    """Only result and session failures are eligible for recovery dispatch."""

    return fail_class in {RESULT_RECOVERABLE, SESSION_RECOVERABLE}


class RecoveryDispatcher(Protocol):
    def dispatch(
        self, *, agent_id: str, message: str, timeout_seconds: float,
        core_run_id: str | None = None, idempotency_key: str | None = None,
        goal_contract: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class RecoveryVerdict:
    fail_class: str
    eligible: bool
    reason: str
    detail: str = ""


def evaluate_recovery(record: Mapping[str, Any]) -> RecoveryVerdict:
    """Decide whether one terminal run record is eligible for recovery."""

    status = str(record.get("status") or "")
    reason = str(record.get("reason") or "")
    if status != CoreRunStatus.FAIL.value:
        return RecoveryVerdict(
            fail_class=classify_fail(reason),
            eligible=False,
            reason="NOT_FAIL",
            detail=f"status={status}",
        )
    fail_class = classify_fail(reason)
    if not is_recoverable_class(fail_class):
        return RecoveryVerdict(
            fail_class=fail_class,
            eligible=False,
            reason="CLASS_NOT_RECOVERABLE",
            detail=reason,
        )
    attempts = int(record.get("recovery_attempts") or 0)
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        return RecoveryVerdict(
            fail_class=fail_class,
            eligible=False,
            reason="MAX_RECOVERY_ATTEMPTS",
            detail=f"attempts={attempts}",
        )
    return RecoveryVerdict(
        fail_class=fail_class,
        eligible=True,
        reason="ELIGIBLE",
        detail=reason,
    )


def build_recovery_message(
    source: Mapping[str, Any],
    *,
    fail_class: str,
    verdict_detail: str,
    prior_evidence: Mapping[str, Any] | None = None,
) -> str:
    """Build the immutable recovery execution package for a child run."""

    contract = dict(source.get("goal_contract") or {})
    verified = dict(source.get("verified_progress") or {})
    remaining_conditions = sorted(
        set(contract.get("completion_conditions") or [])
        - set(verified.get("completed_conditions") or [])
    )
    if not remaining_conditions:
        remaining_conditions = list(contract.get("completion_conditions") or [])
    execution_package = {
        "goal_contract": contract,
        "verified_progress": verified,
        "prior_run": {
            "core_run_id": source.get("core_run_id"),
            "status": source.get("status"),
            "reason": source.get("reason"),
            "fail_class": fail_class,
            "format_error": source.get("format_error"),
        },
        "recovery_instruction": (
            "This is one recovery attempt after a terminal FAIL. "
            "Complete only the remaining unverified completion conditions. "
            "Reuse already verified progress; do not restate it as new work. "
            "Never claim evidence or artifacts that were not actually produced "
            "or verified in this run."
        ),
        "needed_evidence": {
            "completion_conditions": remaining_conditions,
            "instruction": "Supply only evidence actually established in this run.",
        },
    }
    prior_block = (
        " Prior run evidence (read-only, do not re-claim): "
        + json.dumps(prior_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if prior_evidence
        else ""
    )
    message = (
        "Execute the immutable original goal from this verified execution package: "
        + json.dumps(execution_package, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + f" Do not replace the objective or extend its allowed scope.{prior_block} "
        + "The final assistant response must be exactly one raw JSON object. "
        + "Do not use Markdown code fences and do not add prose before or after the JSON object. "
        + 'Top-level fields status, summary, evidence, artifacts, and scope are mandatory. '
        + 'status: "completed" only when all remaining conditions are actually verified, '
        'otherwise "failed" or "blocked". summary: concise actual outcome. '
        + 'evidence: non-empty array of {"type","detail"} objects for this run only. '
        + 'artifacts: array of {"path"} for outputs newly produced by this run. '
        + 'scope: {"compliant": true, "violations": []}.'
    )
    return message


def recovery_goal_contract(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project the parent goal contract; recovery never rewrites it."""

    contract = source.get("goal_contract")
    if not isinstance(contract, Mapping) or not contract:
        return None
    return {
        key: contract[key]
        for key in (
            "primary_objective", "allowed_scope", "forbidden_scope",
            "expected_result", "completion_conditions",
        )
        if key in contract
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


class RecoveryLayer:
    """Post-terminal Recovery Layer: FAIL runs get at most one recovery run."""

    def __init__(
        self,
        engine: CoreEngine,
        registry: RunRegistry,
        *,
        dispatcher: RecoveryDispatcher | None = None,
        max_attempts: int = MAX_RECOVERY_ATTEMPTS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self._dispatcher = dispatcher or engine
        self.max_attempts = max(1, int(max_attempts))
        self._clock = clock

    # -- classification -------------------------------------------------
    def classify(self, core_run_id: str) -> RecoveryVerdict:
        record = self.registry.get(core_run_id)
        if record is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        return evaluate_recovery(record)

    # -- recovery dispatch ----------------------------------------------
    def attempt_recovery(self, core_run_id: str, *, prior_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Attempt one bounded recovery for a terminal FAIL run.

        The parent record is never mutated.  All recovery accounting is
        appended to the child run record, so a repeated attempt on the same
        parent is fail-closed via the child idempotency key.
        """
        source = self.registry.get(core_run_id)
        if source is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        verdict = evaluate_recovery(source)
        if not verdict.eligible:
            return self._rejected(verdict, core_run_id,
                                  final_state=self._final_state_of(source))
        child_id = f"{core_run_id}-rec-{uuid.uuid4().hex[:8]}"
        idempotency_key = f"{source.get('idempotency_key') or core_run_id}:rec:1"
        message = build_recovery_message(
            source,
            fail_class=verdict.fail_class,
            verdict_detail=verdict.detail,
            prior_evidence=prior_evidence,
        )
        goal_contract = recovery_goal_contract(source)
        try:
            dispatched = self._dispatcher.dispatch(
                agent_id=source["agent_id"],
                message=message,
                timeout_seconds=10.0,
                core_run_id=child_id,
                idempotency_key=idempotency_key,
                goal_contract=goal_contract,
            )
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            detail = code if code in _DISPATCH_REJECT_CODES else "DISPATCH_REJECTED"
            return self._rejected(
                RecoveryVerdict(verdict.fail_class, False, detail, code),
                core_run_id,
                final_state="POLICY_BLOCKED",
            )
        # Dispatch is asynchronous: the child is RUNNING until observed.
        # Mark the recovery source on the child now; the final 5-state
        # verdict is resolved in recover() after a bounded wait.
        record = self.registry.update_goal_state(
            child_id,
            goal_state={"recovery_source_run_id": core_run_id, "recovery_attempt": 1},
            event_code=RECOVERY_EVENT_CODE,
            event_details={
                "source_run_id": core_run_id,
                "source_reason": source.get("reason"),
                "fail_class": verdict.fail_class,
                "attempt": 1,
                "max_attempts": self.max_attempts,
                "dispatched_status": dispatched.get("status"),
                "dispatched_reason": dispatched.get("reason"),
            },
        )
        return {
            "status": "DISPATCHED",
            "verdict": {
                "fail_class": verdict.fail_class,
                "eligible": True,
                "reason": verdict.reason,
                "detail": verdict.detail,
            },
            "source_run_id": core_run_id,
            "recovery_run_id": child_id,
            "final_state": None,
            "classified_at": record.get("updated_at"),
        }

    # -- verdict ----------------------------------------------------------
    def recover(self, core_run_id: str, *, wait_seconds: float = 30.0) -> dict[str, Any]:
        """Classify, attempt (if eligible), and return the final 5-state verdict."""

        attempt = self.attempt_recovery(core_run_id)
        if attempt["status"] == "REJECTED":
            return attempt
        child_id = attempt["recovery_run_id"]
        if attempt["final_state"] is None:
            try:
                child = self.engine.wait(child_id, timeout_seconds=wait_seconds)
            except ValueError:
                child = self.registry.get(child_id) or {}
            if CoreRunStatus(str(child.get("status") or "")) in _TERMINAL_STATES:
                final_state = self._final_state_of(child)
            else:
                final_state = "NEEDS_REVIEW"
        else:
            final_state = attempt["final_state"]
        result = dict(attempt)
        result["final_state"] = final_state
        return result

    @staticmethod
    def _rejected(verdict: RecoveryVerdict, source_run_id: str, *, final_state: str) -> dict[str, Any]:
        return {
            "status": "REJECTED",
            "verdict": {
                "fail_class": verdict.fail_class,
                "eligible": False,
                "reason": verdict.reason,
                "detail": verdict.detail,
            },
            "source_run_id": source_run_id,
            "recovery_run_id": None,
            "final_state": final_state,
            "classified_at": _utcnow_iso(),
        }

    # -- bounded wait on a recovery child ---------------------------------
    def wait_recovery(self, recovery_run_id: str, *, wait_seconds: float = 30.0) -> dict[str, Any]:
        """Bounded wait on a recovery child run, resolving the 5-state verdict.

        If no terminal verdict is reached inside the window, the verdict is
        NEEDS_REVIEW; the underlying worker is never silently cancelled by
        this layer (Core's own runtime deadline remains authoritative).
        """
        try:
            child = self.engine.wait(recovery_run_id, timeout_seconds=wait_seconds)
        except ValueError:
            child = self.registry.get(recovery_run_id) or {}
        if CoreRunStatus(str(child.get("status") or "")) in _TERMINAL_STATES:
            final_state = self._final_state_of(child)
        else:
            final_state = "NEEDS_REVIEW"
        return {
            "status": "WAITED",
            "recovery_run_id": recovery_run_id,
            "child_status": str(child.get("status") or ""),
            "child_reason": str(child.get("reason") or ""),
            "final_state": final_state,
            "classified_at": _utcnow_iso(),
        }

    # -- stats ------------------------------------------------------------
    def stats(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Aggregate recovery accounting over a set of run records."""

        counts = {
            "PASS_PRIMARY": 0,
            "PASS_RECOVERED": 0,
            "POLICY_BLOCKED": 0,
            "HARD_FAIL": 0,
            "NEEDS_REVIEW": 0,
            "FAIL_UNRECOVERED": 0,
        }
        classes = {
            RESULT_RECOVERABLE: 0,
            SESSION_RECOVERABLE: 0,
            POLICY_BLOCKED: 0,
            UNKNOWN: 0,
        }
        for record in records:
            status = str(record.get("status") or "")
            reason = str(record.get("reason") or "")
            fail_class = classify_fail(reason)
            is_recovery = self._is_recovery_child(record)
            if status == CoreRunStatus.PASS.value:
                if is_recovery:
                    counts["PASS_RECOVERED"] += 1
                else:
                    counts["PASS_PRIMARY"] += 1
                continue
            if status == CoreRunStatus.FAIL.value:
                if fail_class in {RESULT_RECOVERABLE, SESSION_RECOVERABLE, POLICY_BLOCKED, UNKNOWN}:
                    classes[fail_class] = classes.get(fail_class, 0) + 1
                if not is_recovery:
                    counts["FAIL_UNRECOVERED"] += 1
            elif status == CoreRunStatus.BLOCKED.value and is_recovery:
                counts["POLICY_BLOCKED"] += 1
            elif status == CoreRunStatus.TIMEOUT.value and is_recovery:
                counts["HARD_FAIL"] += 1
            elif status == CoreRunStatus.CANCELLED.value and is_recovery:
                counts["HARD_FAIL"] += 1
        return {
            "states": counts,
            "fail_classes": {
                key: value for key, value in classes.items() if value
            },
            "total_scanned": len(records),
        }

    @staticmethod
    def _is_recovery_child(record: Mapping[str, Any]) -> bool:
        goal_state = (record.get("policy_state") or {}).get("goal") or {}
        return bool(goal_state.get("recovery_source_run_id"))

    @staticmethod
    def _final_state_of(record: Mapping[str, Any]) -> str:
        status = str(record.get("status") or "")
        if status == CoreRunStatus.PASS.value:
            return "PASS_RECOVERED" if RecoveryLayer._is_recovery_child(record) else "PASS_PRIMARY"
        if status == CoreRunStatus.FAIL.value:
            return "HARD_FAIL"
        if status == CoreRunStatus.BLOCKED.value:
            return "POLICY_BLOCKED"
        if status in {CoreRunStatus.TIMEOUT.value, CoreRunStatus.CANCELLED.value}:
            return "HARD_FAIL"
        return "NEEDS_REVIEW"


_DISPATCH_REJECT_CODES = {
    "INVALID_MESSAGE", "INVALID_TIMEOUT", "IDEMPOTENCY_CONFLICT",
    "UNKNOWN_AGENT", "AUTH_TOKEN_MISMATCH", "FORBIDDEN_FIELD",
}

_TERMINAL_STATES = {
    CoreRunStatus.PASS,
    CoreRunStatus.FAIL,
    CoreRunStatus.BLOCKED,
    CoreRunStatus.TIMEOUT,
    CoreRunStatus.CANCELLED,
}
