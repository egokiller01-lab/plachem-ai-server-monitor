"""War Room adapter backed directly by the in-process Fast Gateway Core."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from plachem_fast_gateway import CoreEngine, CoreRunStatus
from war_room_adapter import DeliveryReceipt


class FastGatewayWarRoomAdapter:
    def __init__(self, engine: CoreEngine, db_path: str | Path, control_rpc: Any | None = None) -> None:
        self.engine = engine
        self.db_path = Path(db_path)
        owner_rpc = getattr(getattr(engine, "adapter", None), "rpc", None)
        self.control_rpc = control_rpc or GatewayControlRPC(owner_rpc)
        self._stop_lock = threading.Lock()

    @property
    def reserves_call_budget(self) -> bool:
        return getattr(self.engine, "auth_required", False) is True

    @staticmethod
    def core_id(delivery_id: str) -> str:
        return f"war-{delivery_id}"

    @staticmethod
    def _agent(agent_id: str) -> str:
        return agent_id.casefold()

    def deliver(self, *, delivery_id: str, agent_id: str, instruction_id: str, body: str) -> DeliveryReceipt:
        # Compose from the immutable stored original and grounding packet at
        # the final adapter boundary.  This keeps storage/display lossless and
        # prevents a caller-supplied replacement body from changing the
        # Broker-authorized task digest.
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                """SELECT m.body,t.execution_mode,g.packet_json
                   FROM war_deliveries d
                   JOIN war_messages m ON m.id=d.message_id
                   JOIN war_tasks t ON t.source_message_id=m.id
                   JOIN war_grounding_packets g ON g.task_id=t.id
                   WHERE d.id=? AND m.id=? AND lower(d.agent_id)=lower(?)""",
                (delivery_id, instruction_id, agent_id),
            ).fetchone()
        if row is None:
            return DeliveryReceipt(delivery_id, "failed", error_code="FAST_GATEWAY_APPROVED_SOURCE_MISSING")
        from war_room_actions import _grounded_instruction
        packet = json.loads(row[2])
        body = _grounded_instruction(row[0], packet, row[1])
        # The approved result-document paths come only from the War Room task
        # contract (immutable grounding packet).  They are never derived from
        # worker responses or request fields.
        # Fast Gateway READ-ONLY validation requires exact new-result
        # designations. Broad approved_paths remain the War Room filesystem
        # scope, while result_artifact_paths is the exact output contract.
        approved_paths = packet.get("result_artifact_paths")
        if not isinstance(approved_paths, list) or not approved_paths:
            # Backward compatibility for old tasks that already supplied exact
            # approved_paths.
            approved_paths = packet.get("approved_paths")
        if not isinstance(approved_paths, list):
            approved_paths = []
        approved_paths = [path for path in approved_paths
                          if isinstance(path, str) and path.strip()]
        dispatch_kwargs: dict[str, Any] = {}
        if approved_paths:
            dispatch_kwargs["approved_paths"] = approved_paths
        record = self.engine.dispatch(
            agent_id=self._agent(agent_id), message=body, timeout_seconds=300.0,
            core_run_id=self.core_id(delivery_id), idempotency_key=delivery_id,
            watchdog_managed=True,
            **dispatch_kwargs,
        )
        binding = record.get("openclaw_binding") or {}
        status = str(record.get("status") or "")
        if status == CoreRunStatus.RUNNING.value:
            return DeliveryReceipt(delivery_id, "received", session_id=binding.get("session_id"), run_id=binding.get("openclaw_run_id"))
        return DeliveryReceipt(delivery_id, "failed", error_code=str(record.get("reason") or status or "FAST_GATEWAY_DISPATCH_FAILED"))

    def _core_id_for_openclaw(self, run_id: str) -> str | None:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT core_run_id FROM war_execution_runs WHERE openclaw_run_id=?", (run_id,)).fetchone()
        return str(row[0]) if row else None

    def _binding_for_delivery(self, delivery_id: str, agent_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """SELECT er.core_run_id,er.agent_id,er.openclaw_run_id,er.session_key,er.run_status
                   FROM war_deliveries d
                   JOIN war_messages m ON m.id=d.message_id
                   JOIN war_tasks t ON t.source_message_id=m.id
                   JOIN war_execution_runs er ON er.war_project_id=m.project_id
                     AND er.war_task_id=t.id AND lower(er.agent_id)=lower(d.agent_id)
                   WHERE d.id=? AND d.agent_id=?
                   ORDER BY er.updated_at DESC LIMIT 1""",
                (delivery_id, agent_id),
            ).fetchone()
        return dict(row) if row else None

    def poll(self, *, run_id: str, agent_id: str) -> DeliveryReceipt:
        core_id = self._core_id_for_openclaw(run_id)
        if not core_id:
            return DeliveryReceipt(run_id, "failed", error_code="UNKNOWN_FAST_GATEWAY_RUN", run_id=run_id)
        # Polling is a read-only projection. The persistent harness observer is
        # solely responsible for collecting and reconciling terminal results.
        record = self.engine.status(core_id)
        status = str(record.get("status") or "")
        if status == CoreRunStatus.RUNNING.value:
            return DeliveryReceipt(run_id, "received", run_id=run_id)
        if status == CoreRunStatus.CANCELLED.value:
            return DeliveryReceipt(run_id, "stopped", run_id=run_id, error_code=record.get("cancel_reason") or "CORE_CANCELLED")
        if status in {CoreRunStatus.PASS.value, CoreRunStatus.FAIL.value, CoreRunStatus.BLOCKED.value, CoreRunStatus.TIMEOUT.value}:
            result = record.get("result")
            body = record.get("raw_response")
            if not isinstance(body, str) or not body.strip():
                body = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else None
            return DeliveryReceipt(run_id, "responded" if status == CoreRunStatus.PASS.value else "failed", run_id=run_id, response_body=body, error_code=None if status == CoreRunStatus.PASS.value else record.get("reason") or status)
        return DeliveryReceipt(run_id, "failed", run_id=run_id, error_code="FAST_GATEWAY_STATUS_INVALID")

    def stop(self, *, delivery_id: str, agent_id: str) -> DeliveryReceipt:
        with self._stop_lock:
            binding = self._binding_for_delivery(delivery_id, agent_id)
            if not binding or not binding.get("core_run_id") or not binding.get("session_key"):
                return DeliveryReceipt(delivery_id, "failed", error_code="FAST_GATEWAY_BINDING_MISSING")
            core_id = str(binding["core_run_id"])
            request_cancel = getattr(self.engine, "request_user_cancel", None)
            if callable(request_cancel):
                request_cancel(core_id)
            try:
                record = self.engine.status(core_id)
            except ValueError:
                return DeliveryReceipt(delivery_id, "failed", run_id=binding.get("openclaw_run_id"), error_code="FAST_GATEWAY_RUN_MISSING")
            terminal = {CoreRunStatus.PASS.value, CoreRunStatus.FAIL.value, CoreRunStatus.BLOCKED.value, CoreRunStatus.TIMEOUT.value, CoreRunStatus.CANCELLED.value}
            if str(record.get("status")) in terminal:
                return DeliveryReceipt(delivery_id, "stopped", run_id=binding.get("openclaw_run_id"))
            try:
                abort_status = self.control_rpc.abort(session_key=str(binding["session_key"]))
            except Exception:
                return DeliveryReceipt(delivery_id, "failed", run_id=binding.get("openclaw_run_id"), error_code="FAST_GATEWAY_USER_ABORT_FAILED")
            if abort_status == "no-active-run":
                record = self.engine.reconcile_external_completion(core_id)
                if str(record.get("status")) in terminal:
                    return DeliveryReceipt(delivery_id, "stopped", run_id=binding.get("openclaw_run_id"))
            if abort_status == "aborted":
                reconcile = getattr(self.engine, "mark_user_cancelled_after_abort", None)
                record = (reconcile or self.engine.mark_user_cancelled)(core_id)
            else:
                record = self.engine.mark_user_cancelled(core_id)
            return DeliveryReceipt(
                delivery_id,
                "stopped" if record.get("status") == CoreRunStatus.CANCELLED.value else "failed",
                run_id=binding.get("openclaw_run_id"),
                error_code=None if record.get("status") == CoreRunStatus.CANCELLED.value else "FAST_GATEWAY_CANCEL_RECONCILE_FAILED",
            )

    def stop_core_run(self, *, core_run_id: str) -> DeliveryReceipt:
        """Stop one persisted Core run using its live owner session binding."""
        with self._stop_lock:
            request_cancel = getattr(self.engine, "request_user_cancel", None)
            if callable(request_cancel):
                request_cancel(core_run_id)
            try:
                record = self.engine.status(core_run_id)
            except ValueError:
                return DeliveryReceipt(core_run_id, "failed", error_code="FAST_GATEWAY_RUN_MISSING")
            binding = record.get("openclaw_binding") or {}
            session_key = binding.get("session_key")
            if not session_key:
                return DeliveryReceipt(core_run_id, "failed", error_code="FAST_GATEWAY_BINDING_MISSING")
            run_id = binding.get("openclaw_run_id")
            terminal = {CoreRunStatus.PASS.value, CoreRunStatus.FAIL.value, CoreRunStatus.BLOCKED.value, CoreRunStatus.TIMEOUT.value, CoreRunStatus.CANCELLED.value}
            if str(record.get("status")) in terminal:
                return DeliveryReceipt(core_run_id, "stopped", run_id=run_id)
            try:
                abort_status = self.control_rpc.abort(session_key=str(session_key))
            except Exception:
                return DeliveryReceipt(core_run_id, "failed", run_id=run_id, error_code="FAST_GATEWAY_USER_ABORT_FAILED")
            if abort_status == "no-active-run":
                record = self.engine.reconcile_external_completion(core_run_id)
                if str(record.get("status")) in terminal:
                    return DeliveryReceipt(core_run_id, "stopped", run_id=run_id)
            if abort_status == "aborted":
                reconcile = getattr(self.engine, "mark_user_cancelled_after_abort", None)
                record = (reconcile or self.engine.mark_user_cancelled)(core_run_id)
            else:
                record = self.engine.mark_user_cancelled(core_run_id)
            return DeliveryReceipt(
                core_run_id,
                "stopped" if record.get("status") == CoreRunStatus.CANCELLED.value else "failed",
                run_id=run_id,
                error_code=None if record.get("status") == CoreRunStatus.CANCELLED.value else "FAST_GATEWAY_CANCEL_RECONCILE_FAILED",
            )

    def execution_snapshot(self, delivery_id: str) -> dict[str, Any] | None:
        record = self.engine.status(self.core_id(delivery_id))
        binding = record.get("openclaw_binding") or {}
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        rejected = record.get("rejected_result") if isinstance(record.get("rejected_result"), dict) else {}
        return {
            "core_run_id": record.get("core_run_id"), "openclaw_run_id": binding.get("openclaw_run_id"),
            "agent_id": record.get("agent_id"), "session_key": binding.get("session_key"),
            "run_status": record.get("status"), "runtime_seconds": record.get("runtime_seconds"),
            "result_summary": result.get("summary") or rejected.get("summary") or result.get("status") or rejected.get("status") or None,
            "result_json": json.dumps(record.get("result"), ensure_ascii=False) if record.get("result") is not None else None,
            "raw_response": record.get("raw_response") if isinstance(record.get("raw_response"), str) else None,
            "rejected_result_json": json.dumps(record.get("rejected_result"), ensure_ascii=False) if record.get("rejected_result") is not None else None,
            "validation_error": record.get("reason") if record.get("rejected_result") is not None else None,
            "evidence_json": json.dumps(result.get("evidence"), ensure_ascii=False) if result.get("evidence") is not None else None,
            "artifacts_json": json.dumps(result.get("artifacts"), ensure_ascii=False) if result.get("artifacts") is not None else None,
            "policy_status": record.get("policy_status"),
            "cancel_reason": record.get("cancel_reason") or record.get("reason"),
            "escalation_required": int(bool(record.get("escalation_required"))),
        }


class GatewayControlRPC:
    """Trusted owner-connection Gateway control for immediate cancellation."""

    def __init__(self, owner_rpc: Any | None) -> None:
        self.rpc = owner_rpc

    def abort(self, *, session_key: str) -> str:
        if self.rpc is None or not callable(getattr(self.rpc, "request_on_owner_connection", None)):
            raise RuntimeError("OPENCLAW_OWNER_CONNECTION_UNAVAILABLE")
        response = self.rpc.request_on_owner_connection(
            "sessions.abort", {"key": session_key}, timeout=15.0,
        )
        status = response.get("status") if isinstance(response, dict) else None
        if status not in {"aborted", "no-active-run"}:
            raise RuntimeError("OPENCLAW_SESSION_ABORT_UNCONFIRMED")
        return str(status)

    def close(self) -> None:
        closer = getattr(self.rpc, "close", None)
        if closer:
            closer()
