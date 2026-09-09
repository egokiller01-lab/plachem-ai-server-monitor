"""Feature-gated Command Center HTTP boundary for Fast Gateway Core runs."""

from __future__ import annotations

import hmac
import hashlib
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from plachem_fast_gateway import CoreEngine, TaskRuntimeClass
from plachem_fast_gateway.core_engine import RunRegistry
from plachem_fast_gateway.auth_broker import AuthBrokerError
from plachem_fast_gateway.auth_broker import execution_auth_scope
from plachem_fast_gateway.runtime_policy import normalize_goal_contract
from fast_gateway_service import get_persistent_harness


ROOT = Path(__file__).resolve().parent
_run_path = lambda: Path(os.environ.get("PLACHEM_FAST_GATEWAY_RUNS", ROOT / "runtime" / "fast-gateway-runs.jsonl"))
router = APIRouter(prefix="/api/fast-gateway", tags=["fast-gateway"])


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_run_id: str | None = Field(default=None, min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=100_000)
    timeout_seconds: float = Field(gt=0, le=3600)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    goal_contract: "GoalContractRequest | None" = None
    auth_token: str | None = Field(default=None, min_length=1, max_length=512)
    action: str = Field(default="dispatch", min_length=1, max_length=128)
    workspace_id: str = Field(default="command-center", min_length=1, max_length=256)
    project_id: str = Field(default="fast-gateway", min_length=1, max_length=256)
    task_runtime_class: TaskRuntimeClass = TaskRuntimeClass.STANDARD


class GoalContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_objective: str = Field(min_length=1, max_length=256)
    allowed_scope: list[str] = Field(min_length=1, max_length=100)
    forbidden_scope: list[str] = Field(min_length=1, max_length=100)
    expected_result: str = Field(min_length=1, max_length=256)
    completion_conditions: list[str] = Field(min_length=1, max_length=100)


class WaitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(gt=0, le=3600)


class DirectDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=100_000)
    source_run_id: str = Field(min_length=8, max_length=256)
    source_session_key: str = Field(min_length=8, max_length=512)
    task_runtime_class: TaskRuntimeClass = TaskRuntimeClass.STANDARD


def _present(record: dict[str, Any]) -> dict[str, Any]:
    """Command Center projection; includes CANCELLED but omits session identity."""
    return {
        key: record.get(key)
        for key in (
            "core_run_id",
            "agent_id",
            "status",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "reason",
            "result",
            "format_error",
            "format_recovery_attempts",
            "format_recovery_rejection",
            "runtime_class",
            "task_class",
            "model_profile",
            "policy_profile",
            "max_runtime",
            "execution_budget",
            "finalization_recovery_budget",
            "runtime_seconds",
            "retry_count",
            "tool_call_count",
            "tool_call_metric",
            "policy_status",
            "policy_events",
            "cancel_reason",
            "context_policy",
            "fallback_policy",
            "goal_status",
            "context_reset_count",
            "max_context_resets",
            "escalation_required",
            "escalation_reason",
        )
    }


def _require_write(secret: str | None) -> None:
    if os.environ.get("PLACHEM_FAST_GATEWAY_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="FAST_GATEWAY_DISABLED")
    expected = os.environ.get("PLACHEM_FAST_GATEWAY_ADMIN_SECRET", "")
    if not expected or not secret or not hmac.compare_digest(expected, secret):
        raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")


def _require_ingress(secret: str | None) -> None:
    if os.environ.get("PLACHEM_FAST_GATEWAY_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="FAST_GATEWAY_DISABLED")
    expected = os.environ.get("PLACHEM_FAST_GATEWAY_INGRESS_SECRET", "")
    if not expected or not secret or not hmac.compare_digest(expected, secret):
        raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")


def _direct_goal(agent_id: str) -> dict[str, Any]:
    return {
        "primary_objective": f"Fulfill the owner's direct request through {agent_id}",
        "allowed_scope": [f"Exact direct request within the {agent_id} role and workspace"],
        "forbidden_scope": ["Unrequested work", "Direct subagent spawning", "Fast Gateway bypass"],
        "expected_result": "Verified result with a user-facing summary",
        "completion_conditions": ["Requested work is completed or an exact blocker is reported"],
    }


def _direct_message(message: str) -> str:
    contract = (
        'Return exactly one raw JSON object with fields: '
        '"status" (completed|failed|blocked), "summary" (complete user-facing answer), '
        '"evidence" (non-empty array of {"type","detail"}), "artifacts" (array of {"path"}), '
        'and "scope" ({"compliant":true,"violations":[]}). Do not use Markdown fences.'
    )
    return f"Owner direct request routed by Fast Gateway. Execute exactly this request:\n{message}\n\n{contract}"


def _engine() -> CoreEngine:
    # Dispatch, observation, cancellation and projection use the same JSONL
    # registry and owner as War Room. Never create a second lifecycle owner.
    legacy_path = os.environ.get("PLACHEM_FAST_GATEWAY_CORE_DB")
    if legacy_path and Path(legacy_path).resolve() != _run_path().resolve():
        raise ValueError("LEGACY_RUN_STORE_CONFIG_REQUIRES_REVIEW")
    try:
        return get_persistent_harness().engine
    except AuthBrokerError as exc:
        raise HTTPException(status_code=503, detail="AUTH_BROKER_UNAVAILABLE") from exc


@router.get("/runs")
def recent_runs(limit: int = 50) -> dict[str, Any]:
    registry = RunRegistry(_run_path())
    try:
        return {"runs": [_present(item) for item in registry.recent(limit)]}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc).split(":", 1)[0]) from exc


@router.get("/runs/{core_run_id}")
def run_status(core_run_id: str) -> dict[str, Any]:
    record = RunRegistry(_run_path()).get(core_run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="UNKNOWN_CORE_RUN")
    return _present(record)


@router.post("/runs/dispatch")
def dispatch_run(
    request: DispatchRequest,
    x_fast_gateway_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_write(x_fast_gateway_secret)
    if not (os.environ.get("PLACHEM_AUTH_BROKER_DB") and os.environ.get("PLACHEM_AUTH_BROKER_KEY_ID")):
        raise HTTPException(status_code=503, detail="AUTH_BROKER_UNAVAILABLE")
    try:
        payload = request.model_dump()
        goal = payload.get("goal_contract")
        if goal is not None and hasattr(goal, "model_dump"):
            payload["goal_contract"] = goal.model_dump()
        return _present(_engine().dispatch(**payload))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc).split(":", 1)[0]) from exc


@router.post("/direct/runs/dispatch-and-wait")
def direct_dispatch_and_wait(
    request: DirectDispatchRequest,
    x_fast_gateway_ingress_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Run one owner-authenticated direct agent turn under Core protection."""
    _require_ingress(x_fast_gateway_ingress_secret)
    if request.agent_id == "main":
        raise HTTPException(status_code=409, detail="MAIN_DIRECT_SESSION_NOT_DELEGATED")
    seed = f"{request.agent_id}\0{request.source_session_key}\0{request.source_run_id}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:32]
    core_run_id = f"direct-{digest}"
    idempotency_key = f"direct:{digest}"
    goal = _direct_goal(request.agent_id)
    message = _direct_message(request.message)
    contract = normalize_goal_contract(goal)
    scope = execution_auth_scope(
        agent_id=request.agent_id,
        message=message,
        core_run_id=core_run_id,
        idempotency_key=idempotency_key,
        goal_contract=contract.as_dict(),
    )
    engine = _engine()
    try:
        engine.grant_authorizer.direct().register(core_run_id, scope)
        record = engine.dispatch(
            agent_id=request.agent_id,
            message=message,
            timeout_seconds=300,
            core_run_id=core_run_id,
            idempotency_key=idempotency_key,
            goal_contract=goal,
            task_runtime_class=request.task_runtime_class,
        )
        if record.get("status") not in {"PASS", "FAIL", "BLOCKED", "TIMEOUT", "CANCELLED"}:
            record = engine.wait(core_run_id, timeout_seconds=305)
        return _present(record)
    except (AuthBrokerError, ValueError) as exc:
        detail = exc.code if isinstance(exc, AuthBrokerError) else str(exc).split(":", 1)[0]
        raise HTTPException(status_code=409, detail=detail) from exc


@router.post("/runs/{core_run_id}/wait")
def wait_run(
    core_run_id: str,
    request: WaitRequest,
    x_fast_gateway_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_write(x_fast_gateway_secret)
    try:
        return _present(_engine().wait(core_run_id, timeout_seconds=request.timeout_seconds))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc).split(":", 1)[0]) from exc


@router.post("/runs/{core_run_id}/cancel")
def cancel_run(
    core_run_id: str,
    x_fast_gateway_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_write(x_fast_gateway_secret)
    try:
        return _present(_engine().cancel(core_run_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc).split(":", 1)[0]) from exc


@router.post("/runs/{core_run_id}/fresh-context")
def fresh_context_run(
    core_run_id: str,
    x_fast_gateway_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_write(x_fast_gateway_secret)
    try:
        return _present(_engine().fresh_context(core_run_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc).split(":", 1)[0]) from exc
