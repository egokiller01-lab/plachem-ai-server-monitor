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
- Attempt semantics:
    recovery_dispatch_attempt   incremented for every dispatch call
    recovery_execution_attempt  incremented only when the worker actually
                                started (status != BLOCKED with an auth
                                pre-execution reason)
  MAX_RECOVERY_ATTEMPTS = 1 applies to recovery_execution_attempt: the
  parent FAIL record is never mutated.  Pre-execution blocks do not
  consume the execution budget, but they are bounded by a hard cap on
  dispatch attempts to prevent infinite retry loops.
- Auth grant registration reuses the existing DirectIngressGrantAuthorizer
  path: the recovery child inherits the parent's goal contract verbatim,
  guaranteeing Recovery Scope <= Original Source Scope.
- Read-only evidence: the agent-stall-detector scan is consumed as
  external evidence only; it is never mutated by this layer.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from plachem_fast_gateway.auth_broker import execution_auth_scope
from plachem_fast_gateway.core_engine import CoreEngine, RunRegistry
from plachem_fast_gateway.openclaw_adapter import CoreRunStatus
from plachem_fast_gateway.runtime_policy import normalize_goal_contract

MAX_RECOVERY_ATTEMPTS = 1
MAX_RECOVERY_DISPATCH_ATTEMPTS = 3

RECOVERY_EVENT_CODE = "RECOVERY_ATTEMPTED"
RECOVERY_EXECUTION_EVENT_CODE = "RECOVERY_EXECUTION_STARTED"
WATCHDOG_RECOVERY_EVENT_CODE = "WATCHDOG_RECOVERY_DISPATCHED"
WATCHDOG_REQUEUE_EVENT_CODE = "WATCHDOG_REQUEUE_REQUIRED"

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

# Auth pre-execution block reasons: the worker never started.
_PRE_EXECUTION_BLOCK_REASONS = {
    "AUTH_AUTH_REQUIRED",
    "AUTH_BROKER_UNAVAILABLE",
    "AUTH_BINDING_MISMATCH",
    "AUTH_FORBIDDEN_FIELD",
    "AUTH_EXPIRED",
    "AUTH_CONFLICT",
}


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


def _is_pre_execution_block(status: str, reason: str) -> bool:
    """True when the child was blocked before the worker ever started."""

    return (status == CoreRunStatus.BLOCKED.value
            and reason in _PRE_EXECUTION_BLOCK_REASONS)


def _recovery_attempt_counts(record: Mapping[str, Any]) -> tuple[int, int]:
    """Return (dispatch_attempts, execution_attempts) from child goal_state."""

    goal_state = (record.get("policy_state") or {}).get("goal") or {}
    return (
        int(goal_state.get("recovery_dispatch_attempt") or 0),
        int(goal_state.get("recovery_execution_attempt") or 0),
    )


class RecoveryDispatcher(Protocol):
    def dispatch(
        self, *, agent_id: str, message: str, timeout_seconds: float,
        core_run_id: str | None = None, idempotency_key: str | None = None,
        goal_contract: Mapping[str, Any] | None = None,
        watchdog_managed: bool = False,
        task_runtime_class: str = "STANDARD",
        approved_paths: list[str] | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class RecoveryVerdict:
    fail_class: str
    eligible: bool
    reason: str
    detail: str = ""


def evaluate_recovery(
    record: Mapping[str, Any],
    *,
    existing_execution_attempts: int | None = None,
) -> RecoveryVerdict:
    """Decide whether one terminal run record is eligible for recovery.

    MAX_RECOVERY_ATTEMPTS applies to recovery_execution_attempt:
    pre-execution blocks do not consume the execution budget.

    The parent FAIL record is immutable, so recovery accounting lives on
    the child record.  When called from RecoveryLayer (which can scan
    children), ``existing_execution_attempts`` is passed explicitly.
    When called standalone with a child record, the record's own
    goal_state is used as fallback.
    """

    status = str(record.get("status") or "")
    reason = str(record.get("reason") or "")
    # Watchdog-managed FastGateway runs belong to the controlled recovery lane.
    # A later terminal FAIL must not fall through into the legacy automatic
    # RecoveryLayer; Main / Process Board decides whether to requeue them.
    if bool(record.get("watchdog_managed")):
        return RecoveryVerdict(
            fail_class=classify_fail(reason),
            eligible=False,
            reason="WATCHDOG_MANAGED_REQUIRES_CONTROLLED_REQUEUE",
            detail=reason,
        )
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
    if existing_execution_attempts is None:
        _, existing_execution_attempts = _recovery_attempt_counts(record)
    if existing_execution_attempts >= MAX_RECOVERY_ATTEMPTS:
        return RecoveryVerdict(
            fail_class=fail_class,
            eligible=False,
            reason="MAX_RECOVERY_ATTEMPTS",
            detail=f"execution_attempts={existing_execution_attempts}",
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
        + 'otherwise "failed" or "blocked". summary: concise actual outcome. '
        + 'evidence: non-empty array of {"type","detail"} objects for this run only. '
        + 'artifacts: array of {"path"} for outputs newly produced by this run. '
        + 'scope: {"compliant": true, "violations": []}.'
    )
    return message


def build_watchdog_recovery_message(
    source: Mapping[str, Any],
    *,
    prior_evidence: Mapping[str, Any] | None = None,
) -> str:
    """Build a bounded child package after an explicit JEV watchdog SALVAGE."""

    message = build_recovery_message(
        source,
        fail_class=SESSION_RECOVERABLE,
        verdict_detail="JEV_WATCHDOG_SALVAGE",
        prior_evidence=prior_evidence,
    )
    return message.replace(
        "This is one recovery attempt after a terminal FAIL. ",
        "This is one recovery attempt after JEV Watchdog stopped a non-progressing session. "
        "Do not repeat approaches recorded as failed or non-progressing. ",
        1,
    )


def recovery_goal_contract(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project the parent goal contract for the recovery child.

    Recovery never rewrites the contract: the child inherits the exact
    same objective, scope, and completion conditions as the parent.
    The goal_id field is intentionally omitted because
    CoreEngine._dispatch calls normalize_goal_contract() which
    deterministically regenerates the identical goal_id from the same
    content.  This guarantees Recovery Scope <= Original Source Scope
    and allows the auth grant digest to match the normalized contract
    that CoreEngine._dispatch computes internally.
    """

    contract = source.get("goal_contract")
    if not isinstance(contract, Mapping) or not contract:
        return None
    projected: dict[str, Any] = {}
    for key in (
        "primary_objective", "allowed_scope",
        "forbidden_scope", "expected_result", "completion_conditions",
    ):
        if key in contract:
            projected[key] = contract[key]
    return projected or None


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
        max_dispatch_attempts: int = MAX_RECOVERY_DISPATCH_ATTEMPTS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self._dispatcher = dispatcher or engine
        self.max_attempts = max(1, int(max_attempts))
        self.max_dispatch_attempts = max(1, int(max_dispatch_attempts))
        self._clock = clock

    # -- classification -------------------------------------------------
    def classify(self, core_run_id: str) -> RecoveryVerdict:
        record = self.registry.get(core_run_id)
        if record is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        # The parent FAIL record is immutable.  Recovery accounting lives
        # on child records, so scan children for existing execution
        # attempts before evaluating eligibility.
        existing_exec = self._max_execution_attempts(core_run_id)
        return evaluate_recovery(record, existing_execution_attempts=existing_exec)

    # -- recovery dispatch ----------------------------------------------
    def attempt_recovery(self, core_run_id: str, *, prior_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Attempt one bounded recovery for a terminal FAIL run.

        The parent record is never mutated.  All recovery accounting is
        appended to the child run record.  Auth grant registration
        reuses the existing DirectIngressGrantAuthorizer path: the
        recovery child inherits the parent's goal contract verbatim,
        guaranteeing Recovery Scope <= Original Source Scope.
        """
        source = self.registry.get(core_run_id)
        if source is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        existing_exec = self._max_execution_attempts(core_run_id)
        verdict = evaluate_recovery(source, existing_execution_attempts=existing_exec)
        if not verdict.eligible:
            return self._rejected(verdict, core_run_id,
                                  final_state=self._final_state_of(source))
        # Bounded dispatch attempts: prevent infinite pre-execution retry.
        existing_dispatches = self._count_existing_dispatches(core_run_id)
        if existing_dispatches >= self.max_dispatch_attempts:
            return self._rejected(
                RecoveryVerdict(
                    verdict.fail_class, False,
                    "MAX_DISPATCH_ATTEMPTS",
                    f"dispatch_attempts={existing_dispatches}",
                ),
                core_run_id,
                final_state="POLICY_BLOCKED",
            )
        child_id = f"{core_run_id}-rec-{uuid.uuid4().hex[:8]}"
        idempotency_key = f"{source.get('idempotency_key') or core_run_id}:rec:{existing_dispatches + 1}"
        message = build_recovery_message(
            source,
            fail_class=verdict.fail_class,
            verdict_detail=verdict.detail,
            prior_evidence=prior_evidence,
        )
        goal_contract = recovery_goal_contract(source)

        # -- Auth grant registration -----------------------------------
        # Reuse the existing trusted auth path.  The recovery child
        # inherits the parent's normalized goal contract, so the
        # task_digest computed here matches what CoreEngine._dispatch
        # will compute internally.  No new auth mechanism is created.
        if (self.engine.auth_broker is not None
                and self.engine.grant_authorizer is not None):
            if not self.engine.grant_authorizer.owns_run(child_id):
                return self._rejected(
                    RecoveryVerdict(
                        verdict.fail_class, False,
                        "AUTH_GRANT_UNAVAILABLE",
                        f"grant_authorizer does not own {child_id}",
                    ),
                    core_run_id,
                    final_state="POLICY_BLOCKED",
                )
            try:
                _normalized = normalize_goal_contract(goal_contract)
                scope = execution_auth_scope(
                    agent_id=source["agent_id"],
                    message=message,
                    core_run_id=child_id,
                    idempotency_key=idempotency_key,
                    goal_contract=_normalized.as_dict(),
                )
                self._register_auth_grant(
                    self.engine.grant_authorizer, child_id, scope,
                )
            except Exception as exc:
                return self._rejected(
                    RecoveryVerdict(
                        verdict.fail_class, False,
                        "AUTH_GRANT_REGISTRATION_FAILED",
                        f"{type(exc).__name__}: {exc}",
                    ),
                    core_run_id,
                    final_state="POLICY_BLOCKED",
                )

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

        # -- Recovery accounting on the child --------------------------
        dispatch_attempt = existing_dispatches + 1
        child_status = str(dispatched.get("status") or "")
        child_reason = str(dispatched.get("reason") or "")
        execution_started = not _is_pre_execution_block(child_status, child_reason)
        execution_attempt = 1 if execution_started else 0

        record = self.registry.update_goal_state(
            child_id,
            goal_state={
                "recovery_source_run_id": core_run_id,
                "recovery_dispatch_attempt": dispatch_attempt,
                "recovery_execution_attempt": execution_attempt,
            },
            event_code=RECOVERY_EVENT_CODE,
            event_details={
                "source_run_id": core_run_id,
                "source_reason": source.get("reason"),
                "fail_class": verdict.fail_class,
                "dispatch_attempt": dispatch_attempt,
                "execution_attempt": execution_attempt,
                "execution_started": execution_started,
                "max_attempts": self.max_attempts,
                "max_dispatch_attempts": self.max_dispatch_attempts,
                "dispatched_status": dispatched.get("status"),
                "dispatched_reason": dispatched.get("reason"),
            },
        )
        if execution_started:
            self.registry.update_goal_state(
                child_id,
                goal_state={
                    "recovery_source_run_id": core_run_id,
                    "recovery_dispatch_attempt": dispatch_attempt,
                    "recovery_execution_attempt": execution_attempt,
                },
                event_code=RECOVERY_EXECUTION_EVENT_CODE,
                event_details={
                    "source_run_id": core_run_id,
                    "dispatch_attempt": dispatch_attempt,
                    "execution_attempt": execution_attempt,
                },
            )
        result: dict[str, Any] = {
            "status": "DISPATCHED",
            "verdict": {
                "fail_class": verdict.fail_class,
                "eligible": True,
                "reason": verdict.reason,
                "detail": verdict.detail,
            },
            "source_run_id": core_run_id,
            "recovery_run_id": child_id,
            "recovery_dispatch_attempt": dispatch_attempt,
            "recovery_execution_attempt": execution_attempt,
            "execution_started": execution_started,
            "final_state": None,
            "classified_at": record.get("updated_at"),
        }
        # If the dispatch itself returned a pre-execution terminal
        # (BLOCKED with auth reason), resolve the final state now.
        if CoreRunStatus(child_status) in _TERMINAL_STATES:
            result["final_state"] = self._final_state_of(dispatched)
        return result

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

    def mark_watchdog_requeue(
        self,
        core_run_id: str,
        *,
        prior_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark any managed FastGateway run for controlled stop/re-approval."""

        source = self.registry.get(core_run_id)
        if source is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        if not source.get("watchdog_managed"):
            return {"status": "REJECTED", "reason": "WATCHDOG_NOT_MANAGED", "source_run_id": core_run_id}
        package = {
            "source_run_id": core_run_id,
            "source_status": source.get("status"),
            "agent_id": source.get("agent_id"),
            "goal_contract": source.get("goal_contract"),
            "verified_progress": source.get("verified_progress"),
            "watchdog_snapshot": dict(prior_evidence or {}),
            "required_action": "STOP_AND_REQUEUE_THROUGH_CONTROLLED_LANE",
        }
        self.registry.update_goal_state(
            core_run_id,
            event_code=WATCHDOG_REQUEUE_EVENT_CODE,
            event_details={"source_run_id": core_run_id},
            escalation_reason=WATCHDOG_REQUEUE_EVENT_CODE,
            escalation_package=package,
        )
        return {
            "status": "REQUEUE_REQUIRED",
            "reason": WATCHDOG_REQUEUE_EVENT_CODE,
            "source_run_id": core_run_id,
        }

    def recover_watchdog_cancelled(
        self,
        core_run_id: str,
        *,
        prior_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recover a watchdog-cancelled run without bypassing FastGateway auth.

        Direct runs get one fresh child through the same Core/Auth/validator path.
        War Room / Process Board runs are marked for controlled-lane requeue
        because their approval is bound to the original delivery/execution id.
        """

        source = self.registry.get(core_run_id)
        if source is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        if not source.get("watchdog_managed"):
            return {"status": "REJECTED", "reason": "WATCHDOG_NOT_MANAGED", "source_run_id": core_run_id}

        source_goal_state = (source.get("policy_state") or {}).get("goal") or {}
        if source_goal_state.get("watchdog_recovery_source_run_id"):
            return {
                "status": "REJECTED",
                "reason": "MAX_WATCHDOG_RECOVERY_GENERATION",
                "source_run_id": core_run_id,
            }

        if not core_run_id.startswith("direct-"):
            package = {
                "source_run_id": core_run_id,
                "source_status": source.get("status"),
                "agent_id": source.get("agent_id"),
                "goal_contract": source.get("goal_contract"),
                "verified_progress": source.get("verified_progress"),
                "watchdog_snapshot": dict(prior_evidence or {}),
                "required_action": "REQUEUE_THROUGH_CONTROLLED_LANE",
            }
            self.registry.update_goal_state(
                core_run_id,
                event_code=WATCHDOG_REQUEUE_EVENT_CODE,
                event_details={"source_run_id": core_run_id},
                escalation_reason=WATCHDOG_REQUEUE_EVENT_CODE,
                escalation_package=package,
            )
            return {
                "status": "REQUEUE_REQUIRED",
                "reason": WATCHDOG_REQUEUE_EVENT_CODE,
                "source_run_id": core_run_id,
            }

        if str(source.get("status") or "") != CoreRunStatus.CANCELLED.value:
            return {
                "status": "REJECTED",
                "reason": "SOURCE_NOT_CANCELLED",
                "source_run_id": core_run_id,
                "source_status": source.get("status"),
            }

        existing = 0
        for record in self.registry.recent(200):
            goal_state = (record.get("policy_state") or {}).get("goal") or {}
            if goal_state.get("watchdog_recovery_source_run_id") == core_run_id:
                existing += 1
        if existing >= 1:
            return {
                "status": "REJECTED",
                "reason": "MAX_WATCHDOG_RECOVERY_ATTEMPTS",
                "source_run_id": core_run_id,
            }

        child_id = f"direct-{uuid.uuid4().hex}"
        idempotency_key = f"direct:watchdog:{uuid.uuid4().hex}"
        goal_contract = recovery_goal_contract(source)
        message = build_watchdog_recovery_message(
            source,
            prior_evidence=prior_evidence,
        )

        if self.engine.auth_broker is not None and self.engine.grant_authorizer is not None:
            if not self.engine.grant_authorizer.owns_run(child_id):
                return {
                    "status": "POLICY_BLOCKED",
                    "reason": "AUTH_GRANT_UNAVAILABLE",
                    "source_run_id": core_run_id,
                }
            normalized = normalize_goal_contract(goal_contract)
            scope = execution_auth_scope(
                agent_id=source["agent_id"],
                message=message,
                core_run_id=child_id,
                idempotency_key=idempotency_key,
                goal_contract=normalized.as_dict(),
            )
            try:
                self._register_auth_grant(self.engine.grant_authorizer, child_id, scope)
            except Exception as exc:
                return {
                    "status": "POLICY_BLOCKED",
                    "reason": "AUTH_GRANT_REGISTRATION_FAILED",
                    "detail": type(exc).__name__,
                    "source_run_id": core_run_id,
                }

        try:
            dispatched = self._dispatcher.dispatch(
                agent_id=source["agent_id"],
                message=message,
                timeout_seconds=10.0,
                core_run_id=child_id,
                idempotency_key=idempotency_key,
                goal_contract=goal_contract,
                watchdog_managed=True,
                task_runtime_class=str(source.get("task_class") or "STANDARD"),
                approved_paths=list(source.get("approved_paths") or []),
            )
        except ValueError as exc:
            return {
                "status": "POLICY_BLOCKED",
                "reason": str(exc).split(":", 1)[0],
                "source_run_id": core_run_id,
            }

        child = self.registry.update_goal_state(
            child_id,
            goal_state={
                "watchdog_recovery_source_run_id": core_run_id,
                "watchdog_recovery_attempt": 1,
            },
            event_code=WATCHDOG_RECOVERY_EVENT_CODE,
            event_details={
                "source_run_id": core_run_id,
                "source_status": source.get("status"),
                "auth_rechecked": True,
                "approved_paths_preserved": list(source.get("approved_paths") or []),
            },
        )
        return {
            "status": "DISPATCHED",
            "source_run_id": core_run_id,
            "recovery_run_id": child_id,
            "recovery_status": dispatched.get("status"),
            "auth_rechecked": True,
            "goal_contract_preserved": child.get("goal_contract") == source.get("goal_contract"),
            "approved_paths_preserved": child.get("approved_paths") == source.get("approved_paths"),
        }

    @staticmethod
    def _register_auth_grant(grant_authorizer: Any, child_id: str, scope: Any) -> None:
        """Register an auth grant for a recovery child run.

        Reuses the existing DirectIngressGrantAuthorizer.register() path.
        Works with both CompositeGrantAuthorizer (production) and
        DirectIngressGrantAuthorizer (direct) by routing to the
        appropriate authorizer.
        """
        # CompositeGrantAuthorizer routes to the owning authorizer.
        if hasattr(grant_authorizer, "direct") and hasattr(grant_authorizer, "authorizers"):
            # Composite: route to the direct authorizer for direct-* runs
            for authorizer in grant_authorizer.authorizers:
                if hasattr(authorizer, "owns_run") and authorizer.owns_run(child_id):
                    if hasattr(authorizer, "register"):
                        authorizer.register(child_id, scope)
                        return
            raise ValueError(
                f"no authorizer with register() owns {child_id}"
            )
        # Direct authorizer
        if hasattr(grant_authorizer, "register"):
            grant_authorizer.register(child_id, scope)
            return
        raise ValueError(
            f"grant_authorizer {type(grant_authorizer).__name__} has no register()"
        )

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
            "recovery_dispatch_attempt": 0,
            "recovery_execution_attempt": 0,
            "execution_started": False,
            "final_state": final_state,
            "classified_at": _utcnow_iso(),
        }

    def _count_existing_dispatches(self, core_run_id: str) -> int:
        """Count existing recovery child dispatch attempts for a parent."""

        count = 0
        for record in self.registry.recent(limit=200):
            if not self._is_recovery_child(record):
                continue
            goal_state = (record.get("policy_state") or {}).get("goal") or {}
            if goal_state.get("recovery_source_run_id") == core_run_id:
                count += 1
        return count

    def _max_execution_attempts(self, core_run_id: str) -> int:
        """Return the max recovery_execution_attempt across children of a parent.

        The parent FAIL record is immutable, so all recovery accounting
        lives on child records.
        """
        max_exec = 0
        for record in self.registry.recent(limit=200):
            if not self._is_recovery_child(record):
                continue
            goal_state = (record.get("policy_state") or {}).get("goal") or {}
            if goal_state.get("recovery_source_run_id") == core_run_id:
                max_exec = max(max_exec, int(goal_state.get("recovery_execution_attempt") or 0))
        return max_exec

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
        goal_state = (child.get("policy_state") or {}).get("goal") or {}
        return {
            "status": "WAITED",
            "recovery_run_id": recovery_run_id,
            "child_status": str(child.get("status") or ""),
            "child_reason": str(child.get("reason") or ""),
            "recovery_dispatch_attempt": int(goal_state.get("recovery_dispatch_attempt") or 0),
            "recovery_execution_attempt": int(goal_state.get("recovery_execution_attempt") or 0),
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
