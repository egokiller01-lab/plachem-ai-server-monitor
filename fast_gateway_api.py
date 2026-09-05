"""Feature-gated Command Center HTTP boundary for Fast Gateway Core runs."""

from __future__ import annotations

import hmac
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from plachem_fast_gateway import CoreEngine
from plachem_fast_gateway.durable_core_store import DurableCoreStore
from plachem_fast_gateway.production_runtime import create_ubuntu_core_engine


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


def _core_db_path() -> Path:
    return Path(os.environ.get("PLACHEM_FAST_GATEWAY_CORE_DB", ROOT / "runtime" / "fast-gateway-core.sqlite3"))


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


@lru_cache(maxsize=1)
def _engine() -> CoreEngine:
    return create_ubuntu_core_engine(
        core_db_path=_core_db_path(),
        agents_path=Path(os.environ.get(
            "PLACHEM_FAST_GATEWAY_AGENTS", ROOT / "plachem_fast_gateway" / "agents.json",
        )),
        models_path=Path(os.environ.get(
            "PLACHEM_FAST_GATEWAY_MODELS", ROOT / "plachem_fast_gateway" / "models.json",
        )),
        bindings_path=Path(os.environ.get(
            "PLACHEM_FAST_GATEWAY_BINDINGS", ROOT / "runtime" / "fast-gateway-bindings.sqlite3",
        )),
    )


@router.get("/runs")
def recent_runs(limit: int = 50) -> dict[str, Any]:
    # Keep the historical run-file seam for callers/tests while production
    # uses the durable SQLite projection.
    path = _run_path()
    if path.exists() and path.suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return {"runs": [_present(item) for item in reversed(rows)]}
    return {"runs": [_present(item) for item in DurableCoreStore(_core_db_path()).recent(limit)]}


@router.get("/runs/{core_run_id}")
def run_status(core_run_id: str) -> dict[str, Any]:
    record = DurableCoreStore(_core_db_path()).get(core_run_id)
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
