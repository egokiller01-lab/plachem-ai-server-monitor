"""War Room Phase 2 orchestration service.

In-process orchestration that composes ExecutionUnitStore (durable mapping),
compile_task_intent (compilation), ExecutionReadinessEvaluator (readiness),
and CoreEngine.dispatch/status (execution truth).

Design rules:
- READY-only dispatch with immediate revalidation.
- Deterministic core_run_id and idempotency_key from execution_id.
- core_run_id persisted to ExecutionUnitStore.
- Duplicate dispatch prevention (idempotent replay, no double-dispatch).
- Never dispatch WAITING/BLOCKED.
- No status duplication, no new RunRegistry.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Mapping


# Core run status sets (mirrors plachem_fast_gateway.CoreRunStatus values)
_ACTIVE_STATUSES = {"QUEUED", "RUNNING"}
_SUCCESS_STATUSES = {"PASS"}
_FAILURE_STATUSES = {"FAIL", "BLOCKED", "TIMEOUT", "CANCELLED"}
_PARENT_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "BLOCKED"}


def _core_run_id_for(execution_id: str) -> str:
    """Deterministic core_run_id from execution_id."""
    return f"core-exec-{execution_id}"


def _idempotency_key_for(execution_id: str) -> str:
    """Deterministic idempotency key from execution_id."""
    return f"war-exec-{execution_id}"


def _dispatch_contract(message: str, timeout_seconds: float, goal_contract: Mapping[str, Any] | None) -> str:
    payload = {
        "message": message,
        "timeout_seconds": float(timeout_seconds),
        "goal_contract": dict(goal_contract or {}),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DuplicateDispatchError(ValueError):
    """Raised when a dispatch attempt finds an active or already-dispatched execution."""


class NotReadyError(ValueError):
    """Raised when attempting to dispatch a WAITING or BLOCKED execution."""


class WarRoomOrchestrator:
    """In-process War Room Phase 2 orchestration service."""

    def __init__(
        self,
        *,
        core_engine: Any,
        execution_store: Any,
        readiness_evaluator: Any | None = None,
        stop_controller: Any | None = None,
    ) -> None:
        if core_engine is None:
            raise ValueError("core_engine is required")
        if execution_store is None:
            raise ValueError("execution_store is required")
        self.core_engine = core_engine
        self.store = execution_store
        from war_room_execution_readiness import ExecutionReadinessEvaluator
        self.readiness_evaluator = readiness_evaluator or ExecutionReadinessEvaluator(
            execution_store, core_engine
        )
        self._lock = threading.RLock()
        self._dispatch_contracts: dict[str, str] = {}
        if stop_controller is None:
            from war_room_fast_gateway import FastGatewayWarRoomAdapter
            stop_controller = FastGatewayWarRoomAdapter(core_engine, execution_store.db_path)
        self.stop_controller = stop_controller

    # ------------------------------------------------------------------
    # Compile & Persist
    # ------------------------------------------------------------------
    def compile_and_persist(
        self,
        *,
        war_project_id: str,
        war_task_id: str,
        agents: list[str] | tuple[str, ...],
        workflow: dict[str, dict[str, Any]] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Compile a multi-agent task intent and persist execution units.

        Returns:
            dict with keys:
                - correlation_id
                - execution_units: list of persisted unit dicts
                - workflow_graph: list of graph nodes

        Raises:
            ValueError: If any validation or persistence error occurs.
        """
        from war_room_execution_compiler import compile_task_intent

        with self._lock:
            if self.store.get_units_by_task(war_project_id, war_task_id):
                raise ValueError(f"DUPLICATE_COMPILE:{war_project_id}/{war_task_id}")
            for agent_id in agents:
                self.core_engine.agents.require(agent_id)
            compiled = compile_task_intent(
                war_project_id=war_project_id,
                war_task_id=war_task_id,
                agents=agents,
                workflow=workflow,
                correlation_id=correlation_id,
            )
            persisted: list[dict[str, Any]] = []
            for unit in compiled["execution_units"]:
                try:
                    row = self.store.create_unit(
                        execution_id=unit["execution_id"],
                        war_project_id=war_project_id,
                        war_task_id=war_task_id,
                        correlation_id=compiled["correlation_id"],
                        agent_id=unit["agent_id"],
                        workflow_role=unit.get("workflow_role", "implementation"),
                        depends_on_execution_ids=unit["depends_on_execution_ids"],
                        dispatch_message=unit.get("dispatch_message"),
                        dispatch_timeout_seconds=unit.get("dispatch_timeout_seconds"),
                        dispatch_goal_contract=unit.get("dispatch_goal_contract"),
                    )
                    persisted.append(row)
                except ValueError as exc:
                    if str(exc).startswith("EXECUTION_UNIT_EXISTS:"):
                        raise ValueError(
                            f"DUPLICATE_COMPILE:{war_project_id}/{war_task_id}"
                        ) from exc
                    raise

        return {
            "correlation_id": compiled["correlation_id"],
            "execution_units": persisted,
            "workflow_graph": compiled["workflow_graph"],
        }

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------
    def _project_unit(self, unit: Mapping[str, Any]) -> dict[str, Any]:
        """Project an execution unit with live readiness and run state."""
        projected: dict[str, Any] = dict(unit)
        try:
            readiness = self.readiness_evaluator.evaluate(unit)
            projected["readiness"] = readiness.get("readiness")
            projected["readiness_reason"] = readiness.get("reason")
        except (TypeError, ValueError):
            projected["readiness"] = "UNKNOWN"
            projected["readiness_reason"] = "EVALUATION_ERROR"

        core_run_id = unit.get("core_run_id")
        if core_run_id:
            try:
                record = self.core_engine.status(str(core_run_id))
                projected["run_status"] = str(record.get("status") or "UNKNOWN")
                result = record.get("result") if isinstance(record.get("result"), dict) else {}
                projected["runtime_class"] = record.get("runtime_class")
                projected["result_summary"] = result.get("summary") or result.get("status")
                projected["evidence"] = result.get("evidence") or []
                projected["policy_status"] = record.get("policy_status")
                projected["cancel_reason"] = record.get("cancel_reason")
                projected["escalation_required"] = bool(record.get("escalation_required"))
            except ValueError:
                projected["run_status"] = "NOT_FOUND"
        else:
            projected["run_status"] = "NOT_STARTED"
            projected["runtime_class"] = None
            projected["result_summary"] = None
            projected["evidence"] = []
            projected["policy_status"] = None
            projected["cancel_reason"] = None
            projected["escalation_required"] = False

        return projected

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        """Get a single execution unit with live projection."""
        unit = self.store.get_unit(execution_id)
        if unit is None:
            return None
        return self._project_unit(unit)

    def list_executions(self, *, war_project_id: str, war_task_id: str) -> list[dict[str, Any]]:
        """List all execution units for a task with live projection."""
        units = self.store.get_units_by_task(war_project_id, war_task_id)
        return [self._project_unit(u) for u in units]

    def list_by_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        """List all execution units for a correlation."""
        units = self.store.get_units_by_correlation(correlation_id)
        return [self._project_unit(u) for u in units]

    def workflow_summary(
        self,
        correlation_id: str,
        *,
        war_project_id: str | None = None,
        war_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Derive parent workflow state from live Child Core/Run truth.

        No Child outcome is copied into the execution-unit store.  A parent
        remains RUNNING while any Child is active or can still run.  Existing
        failure semantics are preserved: FAIL/TIMEOUT/CANCELLED are
        unsuccessful, while explicit BLOCKED leaves an exhausted graph
        BLOCKED.
        """
        if war_project_id is not None and war_task_id is not None:
            units = self.store.get_units_by_workflow(
                war_project_id, war_task_id, correlation_id
            )
        else:
            units = self.store.get_units_by_correlation(correlation_id)
        if not units:
            raise ValueError(f"UNKNOWN_WORKFLOW:{correlation_id}")

        children: list[dict[str, Any]] = []
        has_runnable = False
        has_unresolved = False
        statuses: list[str] = []
        for unit in units:
            required = unit.get("workflow_role") != "observer"
            core_run_id = unit.get("core_run_id")
            if core_run_id:
                try:
                    status = str(self.core_engine.status(str(core_run_id)).get("status") or "UNKNOWN")
                except ValueError:
                    status = "UNKNOWN"
            else:
                status = "NOT_STARTED"
            readiness = self.readiness_evaluator.evaluate(unit)
            readiness_state = str(readiness.get("readiness") or "UNKNOWN")
            if status in _ACTIVE_STATUSES or readiness_state in {"READY", "WAITING"}:
                has_runnable = True
            if status in {"UNKNOWN", "NOT_FOUND"} or readiness_state == "UNKNOWN":
                has_unresolved = True
            if required:
                statuses.append(status)
            children.append({
                "execution_id": unit["execution_id"],
                "core_run_id": core_run_id,
                "run_status": status,
                "readiness": readiness_state,
                "workflow_role": unit.get("workflow_role", "implementation"),
                "required": required,
            })

        if has_runnable or has_unresolved:
            status = "RUNNING"
        elif statuses and all(value == "PASS" for value in statuses):
            status = "COMPLETED"
        elif any(value in {"FAIL", "TIMEOUT", "CANCELLED"} for value in statuses):
            status = "FAILED"
        else:
            # No Child is active/runnable and the graph is not all-PASS.
            # An explicit Core BLOCKED state (or a dependency made unreachable
            # by it) leaves the exhausted parent BLOCKED.
            status = "BLOCKED"

        return {
            "correlation_id": correlation_id,
            "war_project_id": units[0]["war_project_id"],
            "war_task_id": units[0]["war_task_id"],
            "status": status,
            "terminal": status in _PARENT_TERMINAL_STATUSES,
            "children": children,
        }

    def parent_projection(
        self,
        correlation_id: str,
        *,
        war_project_id: str | None = None,
        war_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Return live summary plus any once-only durable parent finalization."""
        summary = self.workflow_summary(
            correlation_id,
            war_project_id=war_project_id,
            war_task_id=war_task_id,
        )
        projection = self.store.get_workflow_projection(summary["war_task_id"])
        if projection is None:
            raise ValueError(f"UNKNOWN_PARENT:{summary['war_task_id']}")
        return {
            **summary,
            "status": projection["status"] or summary["status"],
            "finalized": projection["finalized_at"] is not None,
            "finalized_at": projection["finalized_at"],
            "finalize_count": projection["finalize_count"],
        }

    def _finalize_parent_if_terminal(
        self, correlation_id: str, *, war_project_id: str, war_task_id: str
    ) -> dict[str, Any]:
        summary = self.workflow_summary(
            correlation_id,
            war_project_id=war_project_id,
            war_task_id=war_task_id,
        )
        if summary["terminal"]:
            self.store.finalize_workflow_projection(
                summary["war_task_id"], summary["status"]
            )
        return self.parent_projection(
            correlation_id,
            war_project_id=war_project_id,
            war_task_id=war_task_id,
        )

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------
    def readiness(self, execution_id: str) -> dict[str, Any]:
        """Evaluate readiness for an execution unit."""
        unit = self.store.get_unit(execution_id)
        if unit is None:
            raise ValueError(f"UNKNOWN_EXECUTION:{execution_id}")
        return self.readiness_evaluator.evaluate(unit)

    def stop_execution(self, execution_id: str) -> dict[str, Any]:
        """Stop one existing execution through the Fast Gateway owner path."""
        unit = self.store.get_unit(execution_id)
        if unit is None:
            raise ValueError(f"UNKNOWN_EXECUTION:{execution_id}")
        core_run_id = unit.get("core_run_id")
        if not core_run_id:
            raise ValueError(f"EXECUTION_NOT_DISPATCHED:{execution_id}")
        receipt = self.stop_controller.stop_core_run(core_run_id=str(core_run_id))
        return {"execution_id": execution_id, "core_run_id": core_run_id, "status": receipt.status, "error_code": receipt.error_code}

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def dispatch_execution(
        self,
        *,
        execution_id: str,
        message: str,
        timeout_seconds: float,
        goal_contract: Mapping[str, Any] | None = None,
        _claimed: bool = False,
    ) -> dict[str, Any]:
        """Dispatch under the same lock used by terminal continuation."""
        with self._lock:
            return self._dispatch_execution_locked(
                execution_id=execution_id,
                message=message,
                timeout_seconds=timeout_seconds,
                goal_contract=goal_contract,
                _claimed=_claimed,
            )

    def _dispatch_execution_locked(
        self,
        *,
        execution_id: str,
        message: str,
        timeout_seconds: float,
        goal_contract: Mapping[str, Any] | None = None,
        _claimed: bool = False,
    ) -> dict[str, Any]:
        """Dispatch an execution unit to the CoreEngine.

        Rules:
        - Only READY units may be dispatched.
        - Revalidate immediately before dispatch (race protection).
        - Deterministic core_run_id and idempotency_key from execution_id.
        - Persist core_run_id to ExecutionUnitStore.
        - Duplicate dispatch is prevented: idempotent replay if same request,
          error if conflicting request or already active.

        Returns:
            dict with keys:
                - core_run_id
                - status
                - replayed (True if this was an idempotent replay)

        Raises:
            ValueError: If the execution is unknown or invalid.
            NotReadyError: If the execution is WAITING or BLOCKED.
            DuplicateDispatchError: If the execution is already active
                or dispatched with a conflicting request.
        """
        if not isinstance(message, str) or not message:
            raise ValueError("message must be a non-empty string")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a positive number")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        unit = self.store.get_unit(execution_id)
        if unit is None:
            raise ValueError(f"UNKNOWN_EXECUTION:{execution_id}")

        core_run_id = _core_run_id_for(execution_id)
        idempotency_key = _idempotency_key_for(execution_id)
        contract = _dispatch_contract(message, timeout_seconds, goal_contract)

        # Persist the exact input so a terminal callback can dispatch this
        # unit without inventing a second source of execution intent.
        self.store.update_dispatch_input(
            execution_id, message=message, timeout_seconds=float(timeout_seconds),
            goal_contract=dict(goal_contract or {}) if goal_contract is not None else None,
        )

        # Check if already dispatched
        if unit.get("core_run_id"):
            # Already has a core_run_id - this is a replay or conflict
            try:
                record = self.core_engine.status(unit["core_run_id"])
                status = str(record.get("status") or "UNKNOWN")
            except ValueError:
                status = "UNKNOWN"

            if status in _ACTIVE_STATUSES or status in _SUCCESS_STATUSES:
                known_contract = self._dispatch_contracts.get(execution_id)
                if known_contract is not None and known_contract != contract:
                    raise DuplicateDispatchError(
                        f"DUPLICATE_DISPATCH_CONFLICT:{execution_id}"
                    )
                if known_contract is None:
                    try:
                        self.core_engine.dispatch(
                            agent_id=str(unit["agent_id"]), message=message,
                            timeout_seconds=float(timeout_seconds), core_run_id=core_run_id,
                            idempotency_key=idempotency_key, goal_contract=goal_contract,
                        )
                    except ValueError as exc:
                        if str(exc).startswith(("IDEMPOTENCY_CONFLICT", "DUPLICATE_DISPATCH")):
                            raise DuplicateDispatchError(
                                f"DUPLICATE_DISPATCH_CONFLICT:{execution_id}"
                            ) from exc
                        raise
                    self._dispatch_contracts[execution_id] = contract
                # Idempotent replay - return existing record
                return {
                    "execution_id": execution_id,
                    "core_run_id": unit["core_run_id"],
                    "status": status,
                    "replayed": True,
                }
            # Terminal failure or unknown - allow retry by clearing core_run_id
            self.store.update_core_run_id(execution_id, core_run_id)

        # Revalidate readiness immediately before dispatch
        current_unit = self.store.get_unit(execution_id)
        readiness = self.readiness_evaluator.evaluate(current_unit)
        if readiness.get("readiness") != "READY":
            raise NotReadyError(
                f"EXECUTION_NOT_READY:{execution_id}:{readiness.get('reason')}"
            )

        if not _claimed:
            claimed = self.store.claim_for_dispatch(execution_id)
        else:
            claimed = current_unit
        if claimed is None:
            existing = self.store.get_unit(execution_id)
            if existing and existing.get("core_run_id"):
                try:
                    record = self.core_engine.status(existing["core_run_id"])
                    return {"core_run_id": existing["core_run_id"], "status": str(record.get("status") or "UNKNOWN"), "replayed": True}
                except ValueError:
                    pass
            raise DuplicateDispatchError(f"DUPLICATE_DISPATCH:{execution_id}")

        # Dispatch
        try:
            record = self.core_engine.dispatch(
                agent_id=str(unit["agent_id"]),
                message=message,
                timeout_seconds=float(timeout_seconds),
                core_run_id=core_run_id,
                idempotency_key=idempotency_key,
                goal_contract=goal_contract,
            )
        except ValueError as exc:
            err = str(exc)
            if err.startswith("IDEMPOTENCY_CONFLICT"):
                raise DuplicateDispatchError(
                    f"DUPLICATE_DISPATCH_CONFLICT:{execution_id}"
                ) from exc
            if err.startswith("DUPLICATE_DISPATCH"):
                raise DuplicateDispatchError(
                    f"DUPLICATE_DISPATCH:{execution_id}"
                ) from exc
            raise

        # Persist core_run_id
        self.store.update_core_run_id(execution_id, core_run_id)
        self._dispatch_contracts[execution_id] = contract

        return {
            "execution_id": execution_id,
            "core_run_id": core_run_id,
            "status": str(record.get("status") or "UNKNOWN"),
            "replayed": False,
        }

    def on_core_run_terminal(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        """React to one Core terminal observation and dispatch READY children."""
        core_run_id = record.get("core_run_id")
        if not isinstance(core_run_id, str) or not core_run_id:
            return []
        results: list[dict[str, Any]] = []
        with self._lock:
            # A Core that terminates immediately after dispatch must wait until
            # its execution-unit mapping has been durably persisted.
            source = self.store.get_unit_by_core_run_id(core_run_id)
            if source is None:
                return []
            units = self.store.get_units_by_workflow(
                source["war_project_id"], source["war_task_id"], source["correlation_id"]
            )
            for unit in units:
                readiness = self.readiness_evaluator.evaluate(unit)
                if readiness.get("readiness") != "READY":
                    continue
                if (not isinstance(unit.get("dispatch_message"), str)
                        or not unit.get("dispatch_message")
                        or isinstance(unit.get("dispatch_timeout_seconds"), bool)
                        or not isinstance(unit.get("dispatch_timeout_seconds"), (int, float))
                        or unit.get("dispatch_timeout_seconds") <= 0):
                    continue
                # Claim first, durably and atomically. Only the winner dispatches.
                claimed = self.store.claim_for_dispatch(unit["execution_id"])
                if claimed is None:
                    continue
                try:
                    results.append(self.dispatch_execution(
                        execution_id=claimed["execution_id"],
                        message=str(claimed["dispatch_message"]),
                        timeout_seconds=float(claimed["dispatch_timeout_seconds"]),
                        goal_contract=claimed.get("dispatch_goal_contract"),
                        _claimed=True,
                    ))
                except Exception:
                    # The durable claim prevents duplicate dispatch on duplicate
                    # terminal notifications; the failed claim remains observable.
                    continue
            # STEP 1 continuation is complete before the parent snapshot, so a
            # newly READY Child is claimed/dispatched before terminal decision.
            self._finalize_parent_if_terminal(
                source["correlation_id"],
                war_project_id=source["war_project_id"],
                war_task_id=source["war_task_id"],
            )
        return results
