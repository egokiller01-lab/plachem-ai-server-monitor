from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.core_engine import AgentRegistry, CoreEngine, ModelRegistry, RunRegistry
from plachem_fast_gateway.openclaw_adapter import CoreRunStatus, RunBinding


BLOCK = "bounded local worker emits the same sufficiently long output"


class ObservationAdapter:
    def __init__(self):
        self.cancel_calls = []
        self.rpc_calls = []

    def submit(self, core_run_id, payload):
        return RunBinding(
            core_run_id, "oc-1", payload["agentId"], "agent:local:main", "session-1",
            payload["idempotencyKey"], CoreRunStatus.RUNNING,
        )

    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        raise AssertionError("observation must never cancel")


class CoreLoopObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        agents = root / "agents.json"
        models = root / "models.json"
        agents.write_text(json.dumps({"local": {
            "enabled": True, "capabilities": [], "runtime_model_id": "local/test",
            "allowed_model_ids": ["local/test"], "allowed_policy_profiles": ["LOCAL_STANDARD"],
        }}), encoding="utf-8")
        models.write_text(json.dumps({"models": {"local/test": {
            "runtime_class": "LOCAL", "policy_profile": "LOCAL_STANDARD", "max_runtime": 300,
            "execution_budget": 240, "finalization_recovery_budget": 60,
            "max_retries": 1, "max_tool_calls": 20, "loop_guard": {"consecutive_threshold": 3},
            "context_policy": "FRESH_ON_LOOP", "fallback_policy": "NONE",
        }}}), encoding="utf-8")
        self.adapter = ObservationAdapter()
        self.engine = CoreEngine(
            RunRegistry(root / "runs.jsonl"), AgentRegistry.load(agents), ModelRegistry.load(models), self.adapter,
        )
        self.record = self.engine.dispatch(
            core_run_id="core-1", agent_id="local", message="bounded test",
            timeout_seconds=10, idempotency_key="idem-1",
        )

    def tearDown(self):
        timer = self.engine._deadline_timers.pop("core-1", None)
        if timer is not None:
            timer.cancel()

    def observe(self, output):
        return self.engine.observe_local_run_output(
            core_run_id="core-1", openclaw_run_id="oc-1", session_key="agent:local:main",
            agent_id="local", output=output,
        )

    def test_event_is_observation_only_and_contains_no_output(self):
        record = self.observe(BLOCK * 4)
        self.assertEqual("RUNNING", record["status"])
        event = record["policy_events"][-1]
        self.assertEqual("LOOP_SUSPECTED", event["code"])
        self.assertEqual(4, event["details"]["repeat_count"])
        self.assertNotIn("output", event["details"])
        self.assertNotIn("prompt", event["details"])
        self.assertEqual([], self.adapter.cancel_calls)
        self.assertEqual([], self.adapter.rpc_calls)  # no sessions.abort/chat.abort/restart transport

    def test_all_correlation_fields_must_match(self):
        with self.assertRaisesRegex(ValueError, "RUN_CORRELATION_MISMATCH"):
            self.engine.observe_local_run_output(
                core_run_id="core-1", openclaw_run_id="different",
                session_key="agent:local:main", agent_id="local", output=BLOCK * 4,
            )
        self.assertEqual([], self.engine.status("core-1")["policy_events"])


if __name__ == "__main__":
    unittest.main()
