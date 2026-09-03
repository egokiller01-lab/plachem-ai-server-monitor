import os
import unittest

from war_room_adapter import OpenClawSessionAdapter


class FakeGatewayWs:
    """Deterministic stand-in for the raw WebSocket RPC bridge."""

    connection_id = "fake-connection"

    def __init__(self, responses=None, error=None):
        self.calls = []
        self.responses = responses or {}
        self.error = error

    def request(self, method, params, timeout_ms=15000):
        self.calls.append((method, params, timeout_ms))
        if self.error:
            raise self.error
        response = self.responses.get(method)
        if callable(response):
            response = response(params)
        if response is None:
            raise AssertionError(f"unexpected RPC {method}")
        return response, self.connection_id


class OpenClawGatewayAdapterTests(unittest.TestCase):
    def setUp(self):
        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"

    def tearDown(self):
        os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)

    @staticmethod
    def bind(adapter, delivery="delivery-1", agent="ERPcoder", suffix="one"):
        adapter.bind_delivery(
            delivery,
            session_key=f"agent:{agent.lower()}:war-room-test:{suffix}",
            session_id=f"session-{suffix}",
            disposable=True,
            purpose="test",
            agent_id=agent,
        )

    def test_submit_uses_only_allowed_agent_fields_and_stable_idempotency(self):
        bridge = FakeGatewayWs({"agent": lambda p: {"runId": "run-1", "status": "accepted", "sessionKey": p["sessionKey"]}, "chat.abort": {"aborted": True}})
        adapter = OpenClawSessionAdapter(bridge=bridge)
        self.bind(adapter)
        first = adapter.deliver(delivery_id="delivery-1", agent_id="ERPcoder", instruction_id="ignored", body="hello")
        second = adapter.deliver(delivery_id="delivery-1", agent_id="ERPcoder", instruction_id="ignored", body="hello")
        self.assertEqual("received", first.status)
        self.assertEqual("run-1", second.run_id)
        params = bridge.calls[0][1]
        self.assertEqual({"message", "agentId", "idempotencyKey", "timeout", "sessionKey"}, set(params))
        self.assertEqual("delivery-1", params["idempotencyKey"])
        self.assertNotIn("model", params)
        self.assertNotIn("provider", params)
        self.assertNotIn("credential", params)
        self.assertNotIn("cwd", params)
        self.assertNotIn("sessionId", params)

    def test_wait_timeout_and_terminal_result_are_bounded(self):
        bridge = FakeGatewayWs({
            "agent.wait": {"status": "timeout"},
            "chat.history": {"sessionInfo": {"hasActiveRun": False, "activeRunIds": []}, "messages": []},
        })
        adapter = OpenClawSessionAdapter(bridge=bridge)
        adapter.bind_run("run-timeout", session_key="agent:erpcoder:war-room-test:timeout", session_id="session-timeout", disposable=True, purpose="test", started_at=100, agent_id="ERPcoder")
        receipt = adapter.poll(run_id="run-timeout", agent_id="ERPcoder")
        self.assertEqual("received", receipt.status)
        self.assertEqual(["agent.wait", "chat.history"], [call[0] for call in bridge.calls])
        self.assertEqual(1, bridge.calls[0][1]["timeoutMs"])

    def test_invalid_agent_and_session_binding_fail_closed(self):
        bridge = FakeGatewayWs({"agent": {"runId": "run"}})
        adapter = OpenClawSessionAdapter(bridge=bridge)
        self.bind(adapter)
        invalid = adapter.deliver(delivery_id="delivery-1", agent_id="unknown", instruction_id="i", body="no")
        self.assertEqual("openclaw_invalid_agent_id", invalid.error_code)
        with self.assertRaisesRegex(ValueError, "only explicit disposable"):
            adapter.bind_delivery("bad", session_key="agent:erpqa:war-room-test:bad", session_id="s", disposable=True, purpose="test", agent_id="ERPcoder")

    def test_transport_failure_is_not_a_success(self):
        adapter = OpenClawSessionAdapter(bridge=FakeGatewayWs(error=OSError("socket closed")))
        self.bind(adapter)
        receipt = adapter.deliver(delivery_id="delivery-1", agent_id="ERPcoder", instruction_id="i", body="no")
        self.assertEqual("failed", receipt.status)
        self.assertEqual("openclaw_gateway_rejected", receipt.error_code)

    def test_abort_preserves_exact_run_binding(self):
        bridge = FakeGatewayWs({"agent": {"runId": "run-exact", "status": "accepted"}, "chat.abort": {"aborted": True, "runIds": ["run-exact"]}, "bridge.status": {"connected": True}})
        adapter = OpenClawSessionAdapter(bridge=bridge)
        self.bind(adapter)
        adapter.deliver(delivery_id="delivery-1", agent_id="ERPcoder", instruction_id="i", body="stop me")
        receipt = adapter.stop(delivery_id="delivery-1", agent_id="ERPcoder")
        self.assertEqual("stopped", receipt.status)
        abort = next(params for method, params, _ in bridge.calls if method == "chat.abort")
        self.assertEqual("run-exact", abort["runId"])

    def test_bridge_script_contains_protocol4_connect_and_no_hashed_import(self):
        from pathlib import Path
        script = Path(__file__).parents[1].joinpath("war_room_gateway_bridge.mjs").read_text(encoding="utf-8")
        self.assertIn('request("connect"', script)
        self.assertIn("minProtocol: 4", script)
        self.assertIn('method, params', script)
        self.assertNotIn("/dist/", script)


if __name__ == "__main__":
    unittest.main()
