from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from plachem_fast_gateway.core_engine import AgentRegistry, CoreEngine, RunRegistry
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from plachem_fast_gateway.runtime_policy import ModelRegistry, normalized_action_signature


def model_value(runtime_class="LOCAL", profile="LOCAL_STANDARD", *, max_runtime=300, retries=1, tools=20):
    value = {
        "runtime_class": runtime_class,
        "policy_profile": profile,
        "max_runtime": max_runtime,
        "max_retries": retries,
        "max_tool_calls": tools,
        "loop_guard": {"consecutive_threshold": 3 if runtime_class == "LOCAL" else 5},
        "context_policy": "FRESH_ON_LOOP" if runtime_class == "LOCAL" else "REUSE",
        "fallback_policy": "MANUAL" if runtime_class == "LOCAL" else "NONE",
    }
    if runtime_class == "LOCAL":
        finalization = min(60.0, float(max_runtime) / 5.0)
        value["execution_budget"] = float(max_runtime) - finalization
        value["finalization_recovery_budget"] = finalization
    return value


def agent_value(model_id, profile):
    return {
        "enabled": True,
        "capabilities": [],
        "runtime_model_id": model_id,
        "allowed_model_ids": [model_id],
        "allowed_policy_profiles": [profile],
    }


class FakeAdapter:
    def __init__(self):
        self.submit_calls = []
        self.cancel_calls = []
        self.cancel_event = threading.Event()

    def submit(self, core_run_id, payload):
        self.submit_calls.append((core_run_id, dict(payload)))
        return RunBinding(
            core_run_id, "oc-" + core_run_id, payload["agentId"],
            f"agent:{payload['agentId']}:main", None, payload["idempotencyKey"], CoreRunStatus.RUNNING,
        )

    def wait(self, core_run_id, *, timeout_seconds):
        return AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")

    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        self.cancel_event.set()
        submitted = next(payload for run_id, payload in self.submit_calls if run_id == core_run_id)
        return RunBinding(
            core_run_id, "oc-" + core_run_id, submitted["agentId"],
            f"agent:{submitted['agentId']}:main", None, submitted["idempotencyKey"], CoreRunStatus.CANCELLED,
        )


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, agents, models):
        agents_path = self.root / "agents.json"
        models_path = self.root / "models.json"
        agents_path.write_text(json.dumps(agents), encoding="utf-8")
        models_path.write_text(json.dumps({"models": models}), encoding="utf-8")
        adapter = FakeAdapter()
        registry = RunRegistry(self.root / "runs.jsonl", clock=lambda: datetime.now(timezone.utc))
        engine = CoreEngine(
            registry, AgentRegistry.load(agents_path), ModelRegistry.load(models_path), adapter
        )
        return engine, adapter, registry, AgentRegistry.load(agents_path), ModelRegistry.load(models_path)

    @staticmethod
    def dispatch(engine, agent_id="worker", run_id="run-1"):
        return engine.dispatch(
            agent_id=agent_id, message="safe", timeout_seconds=10,
            core_run_id=run_id, idempotency_key="idem-" + run_id,
        )

    def test_01_local_profile_resolution(self):
        engine, _, _, agents, models = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")},
            {"local/x": model_value()},
        )
        profile = models.resolve(agents.require("worker"))
        self.assertEqual("LOCAL", profile.runtime_class.value)
        self.assertEqual((300.0, 240.0, 60.0), (profile.max_runtime, profile.execution_budget, profile.finalization_recovery_budget))
        self.assertEqual((1, 20), (profile.max_retries, profile.max_tool_calls))
        self.assertEqual("FRESH_ON_LOOP", profile.context_policy)
        self.assertEqual("MANUAL", profile.fallback_policy)

    def test_02_cloud_profile_resolution_and_separation(self):
        _, _, _, agents, models = self.build(
            {"worker": agent_value("cloud/x", "CLOUD_STANDARD")},
            {"cloud/x": model_value("CLOUD", "CLOUD_STANDARD", max_runtime=1800, tools=100)},
        )
        profile = models.resolve(agents.require("worker"))
        self.assertEqual("CLOUD", profile.runtime_class.value)
        self.assertEqual("CLOUD_STANDARD", profile.policy_profile)
        self.assertGreater(profile.max_runtime, 300)

    def test_03_unknown_model_fails_closed(self):
        engine, adapter, _, _, _ = self.build(
            {
                "unknown": agent_value("missing/x", "LOCAL_STANDARD"),
            },
            {"local/x": model_value()},
        )
        for index, agent_id in enumerate(("unknown",), 1):
            record = self.dispatch(engine, agent_id, f"run-{index}")
            self.assertEqual("RUNNING", record["status"])
            self.assertEqual("UNKNOWN", record["runtime_class"])
            self.assertEqual("MODEL_PROFILE_MISMATCH", record["policy_events"][-1]["code"])
        self.assertEqual([], adapter.submit_calls)

    def test_04_retry_limit_stops_second_retry_and_records_reason(self):
        engine, adapter, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        self.dispatch(engine)
        first = engine.observe_runtime_event("run-1", {"kind": "retry", "reason_code": "SAME_CAUSE"})
        self.assertEqual(1, first["retry_count"])
        final = engine.observe_runtime_event("run-1", {"kind": "retry", "reason_code": "SAME_CAUSE"})
        self.assertEqual("CANCELLED", final["status"])
        self.assertEqual("LOCAL_LLM_RETRY_LIMIT", final["cancel_reason"])
        self.assertEqual(["run-1"], adapter.cancel_calls)

    def test_04a_runtime_deadline_auto_cancels_without_wait_poll(self):
        engine, adapter, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STRICT")},
            {"local/x": model_value(profile="LOCAL_STRICT", max_runtime=0.05)},
        )
        self.dispatch(engine)
        self.assertTrue(adapter.cancel_event.wait(1.0))
        record = engine.status("run-1")
        for _ in range(50):
            if record["status"] == "CANCELLED":
                break
            time.sleep(0.01)
            record = engine.status("run-1")
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual("LOCAL_LLM_RUNTIME_LIMIT", record["cancel_reason"])

    def test_05_duplicate_normalized_action_loop_detection(self):
        engine, adapter, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        self.dispatch(engine)
        event = {
            "kind": "tool_call", "observable": True, "tool_name": "read",
            "action_type": "inspect", "target": "/safe/file", "arguments": {"line": 1},
        }
        engine.observe_runtime_event("run-1", event)
        engine.observe_runtime_event("run-1", {**event, "arguments": {"line": 1}})
        final = engine.observe_runtime_event("run-1", event)
        self.assertEqual("CANCELLED", final["status"])
        self.assertEqual("LOCAL_LLM_LOOP_GUARD", final["cancel_reason"])
        self.assertEqual("LOOP_SUSPECTED", final["policy_events"][-1]["code"])
        self.assertEqual(["run-1"], adapter.cancel_calls)

    def test_06_no_progress_loop_detection(self):
        engine, _, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        self.dispatch(engine)
        event = {"kind": "error", "error_code": "EAGAIN", "state_changed": False}
        engine.observe_runtime_event("run-1", event)
        engine.observe_runtime_event("run-1", event)
        final = engine.observe_runtime_event("run-1", event)
        self.assertEqual("CANCELLED", final["status"])
        self.assertEqual("LOCAL_LLM_LOOP_GUARD", final["cancel_reason"])

    def test_07_observable_tool_budget_enforced_but_default_metric_is_unsupported(self):
        engine, _, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")},
            {"local/x": model_value(tools=2)},
        )
        initial = self.dispatch(engine)
        self.assertIsNone(initial["tool_call_count"])
        self.assertEqual("UNSUPPORTED", initial["tool_call_metric"])
        unobservable = engine.observe_runtime_event("run-1", {
            "kind": "tool_call", "observable": False, "tool_name": "unverified",
        })
        self.assertIsNone(unobservable["tool_call_count"])
        self.assertEqual("UNSUPPORTED", unobservable["tool_call_metric"])
        for target in ("a", "b"):
            current = engine.observe_runtime_event("run-1", {
                "kind": "tool_call", "observable": True, "tool_name": "read",
                "action_type": "inspect", "target": target, "arguments": {},
            })
            self.assertEqual("RUNNING", current["status"])
        final = engine.observe_runtime_event("run-1", {
            "kind": "tool_call", "observable": True, "tool_name": "read",
            "action_type": "inspect", "target": "c", "arguments": {},
        })
        self.assertEqual("CANCELLED", final["status"])
        self.assertEqual(3, final["tool_call_count"])
        self.assertEqual("SUPPORTED", final["tool_call_metric"])
        self.assertEqual("LOCAL_LLM_TOOL_BUDGET", final["cancel_reason"])

    def test_08_policy_event_recording_and_command_metadata(self):
        engine, _, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        record = self.dispatch(engine)
        self.assertEqual("NORMAL", record["policy_status"])
        record = engine.observe_runtime_event("run-1", {"kind": "retry", "reason_code": "SAME_CAUSE"})
        self.assertEqual("GUARDED", record["policy_status"])
        self.assertEqual("RETRY_RECORDED", record["policy_events"][-1]["code"])
        self.assertEqual("FRESH_ON_LOOP", record["context_policy"])
        self.assertEqual("MANUAL", record["fallback_policy"])

    def test_09_signature_is_deterministic_redacted_and_registry_has_no_secret(self):
        first = normalized_action_signature({
            "tool_name": "fetch", "action_type": "get", "target": "resource",
            "arguments": {"token": "secret-one", "nested": {"password": "hidden", "page": 1}},
        })
        second = normalized_action_signature({
            "action_type": "get", "tool_name": "fetch", "target": "resource",
            "arguments": {"nested": {"page": 1, "password": "different"}, "token": "secret-two"},
        })
        self.assertEqual(first, second)
        engine, _, registry, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        self.dispatch(engine)
        engine.observe_runtime_event("run-1", {
            "kind": "tool_call", "observable": True, "tool_name": "fetch",
            "action_type": "get", "target": "resource", "arguments": {"token": "DO_NOT_STORE"},
        })
        engine.observe_runtime_event("run-1", {"kind": "retry", "reason_code": "SECRET_REASON_VALUE"})
        self.assertNotIn("DO_NOT_STORE", registry.path.read_text(encoding="utf-8"))
        self.assertNotIn("SECRET_REASON_VALUE", registry.path.read_text(encoding="utf-8"))

    def test_10_agent_name_change_preserves_model_policy(self):
        _, _, _, agents, models = self.build(
            {
                "old-name": agent_value("local/x", "LOCAL_STANDARD"),
                "renamed": agent_value("local/x", "LOCAL_STANDARD"),
            },
            {"local/x": model_value()},
        )
        old = models.resolve(agents.require("old-name"))
        renamed = models.resolve(agents.require("renamed"))
        self.assertEqual(old, renamed)

    def test_11_researcher_local_profile(self):
        engine, _, _, _, _ = self.build(
            {"researcher": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        record = self.dispatch(engine, "researcher")
        self.assertEqual(("LOCAL", "LOCAL_STANDARD"), (record["runtime_class"], record["policy_profile"]))

    def test_12_researcher_cloud_profile(self):
        engine, _, _, _, _ = self.build(
            {"researcher": agent_value("cloud/x", "CLOUD_STANDARD")},
            {"cloud/x": model_value("CLOUD", "CLOUD_STANDARD", max_runtime=1800, tools=100)},
        )
        record = self.dispatch(engine, "researcher")
        self.assertEqual(("CLOUD", "CLOUD_STANDARD"), (record["runtime_class"], record["policy_profile"]))

    def test_13_caller_model_provider_override_is_not_in_core_dispatch_contract(self):
        engine, _, _, _, _ = self.build(
            {"worker": agent_value("local/x", "LOCAL_STANDARD")}, {"local/x": model_value()}
        )
        with self.assertRaises(TypeError):
            engine.dispatch(
                agent_id="worker", message="safe", timeout_seconds=1,
                core_run_id="run-x", idempotency_key="idem-x", model="cloud/x",
            )


if __name__ == "__main__":
    unittest.main()
