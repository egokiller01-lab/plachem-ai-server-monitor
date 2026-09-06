from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.core_engine import AgentRegistry, CoreEngine, ModelRegistry, RunRegistry
from plachem_fast_gateway.openclaw_adapter import MemoryRunBindingStore, OpenClawAdapter


BLOCK = "bounded local output repeats this verified operation"


class Secret:
    def resolve(self):
        return "test-secret"


class EventSocket:
    def __init__(self, event_payloads):
        self.event_payloads = list(event_payloads)
        self.responses = []
        self.calls = []

    def send(self, raw):
        frame = json.loads(raw)
        self.calls.append(frame)
        method = frame["method"]
        if method == "connect":
            payload = {
                "type": "hello-ok", "auth": {"role": "operator", "scopes": ["operator.write"]},
                "features": {"methods": ["agent", "agent.wait", "chat.history", "chat.abort"]},
            }
        elif method == "agent":
            payload = {
                "status": "accepted", "runId": "oc-1", "sessionKey": "agent:local:main",
                "sessionId": "session-1",
            }
        elif method == "agent.wait":
            for item in self.event_payloads:
                self.responses.append(json.dumps({"type": "event", "event": "agent", "payload": item}))
            payload = {"status": "pending"}
        else:
            raise AssertionError(f"unexpected RPC: {method}")
        self.responses.append(json.dumps({
            "type": "res", "id": frame["id"], "ok": True, "payload": payload,
        }))

    def recv(self, timeout=None):
        if not self.responses:
            raise TimeoutError
        return self.responses.pop(0)

    def close(self):
        pass


class AgentEventLoopObservationTests(unittest.TestCase):
    def make_engine(self, event_payloads, *, runtime_class="LOCAL"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        agents = root / "agents.json"
        models = root / "models.json"
        agents.write_text(json.dumps({"local": {
            "enabled": True, "capabilities": [], "runtime_model_id": "local/test",
            "allowed_model_ids": ["local/test"], "allowed_policy_profiles": ["LOCAL_STANDARD"],
        }}), encoding="utf-8")
        model = {
            "runtime_class": runtime_class, "policy_profile": "LOCAL_STANDARD", "max_runtime": 300,
            "max_retries": 1, "max_tool_calls": 20, "loop_guard": {"consecutive_threshold": 3},
            "context_policy": "FRESH_ON_LOOP", "fallback_policy": "NONE",
        }
        if runtime_class == "LOCAL":
            model.update({"execution_budget": 240, "finalization_recovery_budget": 60})
        models.write_text(json.dumps({"models": {"local/test": model}}), encoding="utf-8")
        socket = EventSocket(event_payloads)
        adapter = OpenClawAdapter(
            Secret(), MemoryRunBindingStore(), socket_factory=lambda *_: socket,
        )
        engine = CoreEngine(
            RunRegistry(root / "runs.jsonl"), AgentRegistry.load(agents), ModelRegistry.load(models), adapter,
        )
        engine.dispatch(
            core_run_id="core-1", agent_id="local", message="bounded test", timeout_seconds=10,
            idempotency_key="idem-1",
        )
        timer = engine._deadline_timers.pop("core-1")
        timer.cancel()
        return engine, socket

    @staticmethod
    def event(**updates):
        payload = {
            "runId": "oc-1", "seq": 1, "stream": "assistant", "ts": 1,
            "data": {"delta": BLOCK * 4}, "sessionKey": "agent:local:main", "agentId": "local",
        }
        payload.update(updates)
        return payload

    def test_correlated_agent_delta_reaches_detector_without_lifecycle_action(self):
        engine, socket = self.make_engine([self.event()])
        record = engine.wait("core-1", timeout_seconds=0.01)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual("LOOP_SUSPECTED", record["policy_events"][-1]["code"])
        methods = [call["method"] for call in socket.calls]
        self.assertNotIn("chat.abort", methods)
        self.assertNotIn("sessions.abort", methods)
        self.assertNotIn("llama-server.restart", methods)

    def test_foreign_and_incomplete_events_are_discarded(self):
        engine, socket = self.make_engine([
            self.event(runId="oc-other"), self.event(sessionKey="agent:other:main"),
            self.event(agentId="other"), self.event(seq=None), self.event(stream="tool"),
            self.event(data={"toolCall": "not assistant output"}),
        ])
        record = engine.wait("core-1", timeout_seconds=0.01)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([], record["policy_events"])
        self.assertEqual(1, [call["method"] for call in socket.calls].count("agent.wait"))

    def test_cloud_assistant_event_is_ignored(self):
        engine, _ = self.make_engine([self.event()], runtime_class="CLOUD")
        record = engine.wait("core-1", timeout_seconds=0.01)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([], record["policy_events"])

    def test_observer_exception_does_not_change_wait_result(self):
        engine, _ = self.make_engine([self.event()])
        engine.adapter.set_output_observer(
            lambda _binding, _delta: (_ for _ in ()).throw(RuntimeError("observer failed")),
        )
        record = engine.wait("core-1", timeout_seconds=0.01)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([], record["policy_events"])

    def test_cumulative_text_field_is_not_accepted_as_delta(self):
        engine, _ = self.make_engine([self.event(data={"text": BLOCK * 4})])
        record = engine.wait("core-1", timeout_seconds=0.01)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([], record["policy_events"])


if __name__ == "__main__":
    unittest.main()
