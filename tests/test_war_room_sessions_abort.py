import sqlite3
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.openclaw_adapter import GatewayRPCClient, REQUIRED_SCOPE
from war_room_fast_gateway import FastGatewayWarRoomAdapter, GatewayControlRPC


class FakeControlRPC:
    def __init__(self, status="aborted", on_abort=None):
        self.status = status
        self.on_abort = on_abort
        self.calls = []

    def abort(self, *, session_key):
        self.calls.append(session_key)
        if self.on_abort:
            self.on_abort()
        return self.status


class FakeCore:
    def __init__(self, status="RUNNING"):
        self.record = {"core_run_id": "core-child", "status": status, "cancel_reason": ""}
        self.marked = []
        self.cancelled = []

    def status(self, core_run_id):
        if core_run_id != self.record["core_run_id"]:
            raise ValueError("UNKNOWN_CORE_RUN")
        return dict(self.record)

    def mark_user_cancelled(self, core_run_id):
        self.marked.append(core_run_id)
        self.record.update(status="CANCELLED", cancel_reason="USER_CANCEL")
        return dict(self.record)

    def mark_user_cancelled_after_abort(self, core_run_id):
        if self.record.get("status") == "FAIL" and self.record.get("reason") == "OPENCLAW_ERROR":
            self.marked.append(core_run_id)
            self.record.update(status="CANCELLED", cancel_reason="USER_CANCEL")
        elif self.record.get("status") == "RUNNING":
            self.marked.append(core_run_id)
            self.record.update(status="CANCELLED", cancel_reason="USER_CANCEL")
        return dict(self.record)

    def reconcile_external_completion(self, core_run_id):
        return dict(self.record)

    def cancel(self, core_run_id):
        self.cancelled.append(core_run_id)
        raise AssertionError("policy cancel must not be used by War Room Stop")


class NegotiatingSecret:
    def resolve(self):
        return "opaque-test-secret"


class NegotiatingSocket:
    def __init__(self):
        self.calls = []
        self.closed = False

    def send(self, frame):
        self.calls.append(__import__("json").loads(frame))

    def recv(self, timeout):
        request = self.calls[-1]
        scope = request["params"]["scopes"][0]
        return __import__("json").dumps({
            "type": "res", "id": request["id"], "ok": True,
            "payload": {"type": "hello-ok", "auth": {"role": "operator", "scopes": [scope]}, "features": {"methods": ["agent"]}},
        })

    def close(self):
        self.closed = True


def binding_db(*, task_id="task-1", session_key="agent:erpmanager:war-room-child"):
    path = Path(tempfile.mktemp())
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE war_deliveries (id TEXT PRIMARY KEY, message_id TEXT, agent_id TEXT, status TEXT);
            CREATE TABLE war_messages (id TEXT PRIMARY KEY, project_id TEXT);
            CREATE TABLE war_tasks (id TEXT PRIMARY KEY, source_message_id TEXT);
            CREATE TABLE war_execution_runs (
                core_run_id TEXT PRIMARY KEY, war_project_id TEXT, war_task_id TEXT,
                agent_id TEXT, openclaw_run_id TEXT, session_key TEXT, run_status TEXT, updated_at INTEGER
            );
        """)
        con.execute("INSERT INTO war_messages VALUES ('message-1','project-1')")
        con.execute("INSERT INTO war_tasks VALUES (?, 'message-1')", (task_id,))
        con.execute("INSERT INTO war_deliveries VALUES ('delivery-1','message-1','ERPmanager','received')")
        con.execute("INSERT INTO war_execution_runs VALUES ('core-child','project-1',?,'erpmanager','openclaw-child',?,'RUNNING',1)", (task_id, session_key))
    return path


class WarRoomSessionsAbortTests(unittest.TestCase):
    def test_gateway_scope_negotiation_remains_operator_write(self):
        ordinary_socket = NegotiatingSocket()
        ordinary = GatewayRPCClient(NegotiatingSecret(), socket_factory=lambda *_: ordinary_socket)
        ordinary.connect()
        self.assertEqual([REQUIRED_SCOPE], ordinary_socket.calls[0]["params"]["scopes"])

    def test_stop_resolves_actual_child_session_and_reconciles_user_cancel(self):
        control = FakeControlRPC()
        core = FakeCore()
        adapter = FastGatewayWarRoomAdapter(core, binding_db(), control_rpc=control)
        receipt = adapter.stop(delivery_id="delivery-1", agent_id="ERPmanager")
        self.assertEqual("stopped", receipt.status)
        self.assertEqual(["agent:erpmanager:war-room-child"], control.calls)
        self.assertEqual(["core-child"], core.marked)
        self.assertEqual([], core.cancelled)
        self.assertEqual("USER_CANCEL", core.record["cancel_reason"])

    def test_missing_task_id_column_is_not_needed_for_binding_resolution(self):
        control = FakeControlRPC()
        adapter = FastGatewayWarRoomAdapter(FakeCore(), binding_db(), control_rpc=control)
        receipt = adapter.stop(delivery_id="delivery-1", agent_id="ERPmanager")
        self.assertEqual("stopped", receipt.status)

    def test_missing_binding_fails_without_fallback(self):
        path = binding_db()
        with sqlite3.connect(path) as con:
            con.execute("DELETE FROM war_execution_runs")
        control = FakeControlRPC()
        core = FakeCore()
        receipt = FastGatewayWarRoomAdapter(core, path, control_rpc=control).stop(delivery_id="delivery-1", agent_id="ERPmanager")
        self.assertEqual("failed", receipt.status)
        self.assertEqual("FAST_GATEWAY_BINDING_MISSING", receipt.error_code)
        self.assertEqual([], control.calls)
        self.assertEqual([], core.cancelled)

    def test_duplicate_stop_and_already_terminal_do_not_abort_again(self):
        control = FakeControlRPC()
        core = FakeCore()
        adapter = FastGatewayWarRoomAdapter(core, binding_db(), control_rpc=control)
        self.assertEqual("stopped", adapter.stop(delivery_id="delivery-1", agent_id="ERPmanager").status)
        self.assertEqual("stopped", adapter.stop(delivery_id="delivery-1", agent_id="ERPmanager").status)
        self.assertEqual(1, len(control.calls))
        self.assertEqual("CANCELLED", core.record["status"])

        terminal_control = FakeControlRPC()
        terminal_core = FakeCore(status="PASS")
        self.assertEqual("stopped", FastGatewayWarRoomAdapter(terminal_core, binding_db(), control_rpc=terminal_control).stop(delivery_id="delivery-1", agent_id="ERPmanager").status)
        self.assertEqual([], terminal_control.calls)

    def test_sessions_abort_uses_exact_control_rpc_shape_and_accepts_no_active_run(self):
        class RPC:
            def __init__(self):
                self.calls = []

            def request_on_owner_connection(self, method, params, *, timeout):
                self.calls.append((method, params, timeout))
                return {"status": "no-active-run"}

        rpc = RPC()
        self.assertEqual("no-active-run", GatewayControlRPC(rpc).abort(session_key="agent:erpmanager:war-room-child"))
        self.assertEqual(("sessions.abort", {"key": "agent:erpmanager:war-room-child"}, 15.0), rpc.calls[0])

    def test_no_active_run_race_preserves_completion(self):
        class CompletionWonCore(FakeCore):
            def reconcile_external_completion(self, core_run_id):
                self.record.update(status="PASS", cancel_reason="", result={"summary": "completed"})
                return dict(self.record)

        control = FakeControlRPC(status="no-active-run")
        core = CompletionWonCore()
        receipt = FastGatewayWarRoomAdapter(core, binding_db(), control_rpc=control).stop(
            delivery_id="delivery-1", agent_id="ERPmanager"
        )
        self.assertEqual("stopped", receipt.status)
        self.assertEqual(["agent:erpmanager:war-room-child"], control.calls)
        self.assertEqual("PASS", core.record["status"])
        self.assertEqual([], core.marked)
        self.assertEqual([], core.cancelled)

    def test_aborted_race_reconciles_only_abort_observation_failure(self):
        core = FakeCore()
        control = FakeControlRPC(
            status="aborted",
            on_abort=lambda: core.record.update(status="FAIL", reason="OPENCLAW_ERROR"),
        )
        receipt = FastGatewayWarRoomAdapter(core, binding_db(), control_rpc=control).stop(
            delivery_id="delivery-1", agent_id="ERPmanager"
        )
        self.assertEqual("stopped", receipt.status)
        self.assertEqual("CANCELLED", core.record["status"])
        self.assertEqual("USER_CANCEL", core.record["cancel_reason"])


if __name__ == "__main__":
    unittest.main()
