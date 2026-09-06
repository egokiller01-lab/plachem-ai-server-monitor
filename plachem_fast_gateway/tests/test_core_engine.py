from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plachem_fast_gateway.core_engine import (
    AgentRegistry,
    CoreEngine,
    ModelRegistry,
    RunRegistry,
    production_result_validator,
)
from plachem_fast_gateway.openclaw_adapter import (
    AdapterOutcome,
    CoreRunStatus,
    RunBinding,
    TransportError,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.submit_calls = []
        self.wait_calls = []
        self.cancel_calls = []
        self.submit_error = None
        self.wait_error = None
        self.wait_outcome = AdapterOutcome(
            CoreRunStatus.PASS,
            result={
                "status": "completed",
                "summary": "done",
                "evidence": [{"type": "response"}],
                "artifacts": [],
                "scope": {"compliant": True, "violations": []},
            },
        )

    def submit(self, core_run_id, payload):
        self.submit_calls.append((core_run_id, dict(payload)))
        if self.submit_error:
            raise self.submit_error
        return RunBinding(
            core_run_id,
            "oc-1",
            payload["agentId"],
            f"agent:{payload['agentId']}:main",
            "session-1",
            payload["idempotencyKey"],
            CoreRunStatus.RUNNING,
        )

    def wait(self, core_run_id, *, timeout_seconds):
        self.wait_calls.append((core_run_id, timeout_seconds))
        if self.wait_error:
            raise self.wait_error
        return self.wait_outcome

    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        return RunBinding(
            core_run_id,
            "oc-1",
            "qwentest",
            "agent:qwentest:main",
            "session-1",
            core_run_id,
            CoreRunStatus.CANCELLED,
        )


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class CoreEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        agents_path = root / "agents.json"
        agents_path.write_text(
            json.dumps(
                {
                    "qwentest": {
                        "enabled": True,
                        "capabilities": ["safe-smoke"],
                        "runtime_model_id": "local/test",
                        "allowed_model_ids": ["local/test"],
                        "allowed_policy_profiles": ["LOCAL_STANDARD"],
                    },
                    "disabled": {
                        "enabled": False,
                        "capabilities": [],
                        "runtime_model_id": "local/test",
                        "allowed_model_ids": ["local/test"],
                        "allowed_policy_profiles": ["LOCAL_STANDARD"],
                    },
                    "unknown-model": {
                        "enabled": True,
                        "capabilities": [],
                        "runtime_model_id": "missing/model",
                        "allowed_model_ids": ["missing/model"],
                        "allowed_policy_profiles": ["LOCAL_STANDARD"],
                    },
                    "cloudtest": {
                        "enabled": True,
                        "capabilities": ["safe-smoke"],
                        "runtime_model_id": "cloud/test",
                        "allowed_model_ids": ["cloud/test"],
                        "allowed_policy_profiles": ["CLOUD_STANDARD"],
                    },
                }
            ),
            encoding="utf-8",
        )
        models_path = root / "models.json"
        models_path.write_text(
            json.dumps({"models": {
                "local/test": {
                    "runtime_class": "LOCAL", "policy_profile": "LOCAL_STANDARD",
                    "max_runtime": 300, "max_retries": 1, "max_tool_calls": 20,
                    "loop_guard": {"consecutive_threshold": 3},
                    "context_policy": "FRESH_ON_LOOP", "fallback_policy": "MANUAL",
                },
                "cloud/test": {
                    "runtime_class": "CLOUD", "policy_profile": "CLOUD_STANDARD",
                    "max_runtime": 1800, "max_retries": 2, "max_tool_calls": 100,
                    "loop_guard": {"consecutive_threshold": 5},
                    "context_policy": "REUSE", "fallback_policy": "ELIGIBLE",
                },
            }}),
            encoding="utf-8",
        )
        self.clock = FakeClock()
        self.registry = RunRegistry(root / "runs.jsonl", clock=self.clock)
        self.adapter = FakeAdapter()
        self.engine = CoreEngine(
            self.registry,
            AgentRegistry.load(agents_path),
            ModelRegistry.load(models_path),
            self.adapter,
            clock=self.clock,
        )

    def dispatch(self, **updates):
        values = {
            "core_run_id": "core-1",
            "agent_id": "qwentest",
            "message": "Return a safe response.",
            "timeout_seconds": 10,
            "idempotency_key": "idem-1",
        }
        values.update(updates)
        return self.engine.dispatch(**values)

    def test_dispatch_success_stores_full_binding_and_running(self):
        record = self.dispatch()
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual("oc-1", record["openclaw_binding"]["openclaw_run_id"])
        self.assertEqual("session-1", record["openclaw_binding"]["session_id"])
        sent = self.adapter.submit_calls[0][1]
        self.assertEqual({"message", "agentId", "idempotencyKey", "timeout"}, set(sent))
        self.assertNotIn("runtime_model_id", sent)
        self.assertEqual("LOCAL", record["runtime_class"])
        self.assertEqual("LOCAL_STANDARD", record["policy_profile"])

    def test_wait_success_transitions_to_pass(self):
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=2)
        self.assertEqual("PASS", record["status"])
        self.assertEqual("done", record["result"]["summary"])

    def test_result_validation_success_and_failure(self):
        validator = production_result_validator()
        good = {
            "status": "ok",
            "result": {
                "status": "completed",
                "summary": "safe response",
                "evidence": [{"type": "response"}],
                "artifacts": [],
                "scope": {"compliant": True, "violations": []},
            },
        }
        self.assertEqual(CoreRunStatus.PASS, validator(good).status)
        bad = {"status": "ok", "result": {"status": "completed", "summary": "unsafe"}}
        decision = validator(bad)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertIn("RESULT_SCHEMA_VALIDATION_FAILED", decision.reason)

    def test_false_tool_evidence_is_rejected_without_observed_tool_call(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_false_file_evidence_is_rejected_without_observed_file_operation(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "file_write", "detail": "A file was modified"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_text_only_runtime_observation_is_valid(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "runtime_observation",
                          "detail": "Structured assistant result returned successfully"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        self.assertEqual(CoreRunStatus.PASS, production_result_validator()(payload).status)

    def test_observed_tool_action_allows_matching_evidence(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [
            {"role": "user", "content": "run a tool"},
            {"role": "assistant", "content": [
                {"type": "toolCall", "name": "exec", "arguments": {"command": "date"}},
            ]},
        ]}}
        self.assertEqual(CoreRunStatus.PASS, production_result_validator()(payload).status)

    def test_stale_bounded_history_without_user_boundary_is_not_current_run_evidence(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [
            {"role": "assistant", "content": [
                {"type": "toolCall", "name": "exec", "arguments": {"command": "date"}},
            ]},
            {"role": "toolResult", "content": "stale"},
        ]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_unobserved_process_invocation_claim_is_rejected(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "runtime_observation",
                          "detail": "Parent process was invoked via a CLI and exited with code 0"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_cancel_calls_adapter_and_command_center_state_is_cancelled(self):
        self.dispatch()
        record = self.engine.cancel("core-1")
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)
        self.assertEqual("CANCELLED", self.engine.status("core-1")["status"])

    def test_local_wait_timeout_continues_within_absolute_runtime_budget(self):
        outcomes = iter([
            AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT"),
            self.adapter.wait_outcome,
        ])

        def wait(core_run_id, *, timeout_seconds):
            self.adapter.wait_calls.append((core_run_id, timeout_seconds))
            self.clock.advance(timeout_seconds if len(self.adapter.wait_calls) == 1 else 60)
            return next(outcomes)

        self.adapter.wait = wait
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=180)
        self.assertEqual("PASS", record["status"])
        self.assertEqual([180.0, 120.0], [call[1] for call in self.adapter.wait_calls])
        self.assertEqual([], self.adapter.cancel_calls)

    def test_local_wait_timeout_at_policy_deadline_aborts(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")

        def wait(core_run_id, *, timeout_seconds):
            self.adapter.wait_calls.append((core_run_id, timeout_seconds))
            self.clock.advance(timeout_seconds)
            return self.adapter.wait_outcome

        self.adapter.wait = wait
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=180)
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual("LOCAL_LLM_RUNTIME_LIMIT", record["cancel_reason"])
        self.assertEqual([180.0, 120.0], [call[1] for call in self.adapter.wait_calls])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)

    def test_cloud_wait_timeout_remains_terminal(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")
        self.dispatch(agent_id="cloudtest")
        record = self.engine.wait("core-1", timeout_seconds=180)
        self.assertEqual("TIMEOUT", record["status"])
        self.assertEqual("OPENCLAW_TIMEOUT", record["reason"])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)

    def test_runtime_policy_limit_aborts_and_records_distinct_cancel_reason(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")
        self.dispatch()
        self.clock.advance(301)
        record = self.engine.wait("core-1", timeout_seconds=10)
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual("LOCAL_LLM_RUNTIME_LIMIT", record["cancel_reason"])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)
        self.assertEqual("RUNTIME_LIMIT", record["policy_events"][-1]["code"])

    def test_missing_runtime_model_is_blocked_before_transport(self):
        record = self.dispatch(agent_id="unknown-model")
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual("UNKNOWN", record["runtime_class"])
        self.assertEqual("MODEL_PROFILE_MISMATCH", record["policy_events"][-1]["code"])
        self.assertEqual([], self.adapter.submit_calls)

    def test_invalid_agent_is_rejected_before_transport(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT"):
            self.dispatch(agent_id="missing")
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT"):
            self.dispatch(agent_id="disabled")
        self.assertEqual([], self.adapter.submit_calls)

    def test_transport_failure_maps_to_fail(self):
        self.adapter.submit_error = TransportError("closed")
        record = self.dispatch()
        self.assertEqual("FAIL", record["status"])
        self.assertEqual("TRANSPORT_FAILURE", record["reason"])

    def test_idempotent_retry_does_not_dispatch_twice(self):
        first = self.dispatch()
        second = self.dispatch()
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.adapter.submit_calls))

    def test_duplicate_dispatch_prevention_across_core_runs(self):
        self.dispatch()
        with self.assertRaisesRegex(ValueError, "DUPLICATE_DISPATCH"):
            self.dispatch(core_run_id="core-2")
        self.assertEqual(1, len(self.adapter.submit_calls))

    def test_same_core_run_changed_payload_is_idempotency_conflict(self):
        self.dispatch()
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            self.dispatch(message="changed")

    def test_run_registry_rejects_invalid_terminal_transition(self):
        self.dispatch()
        self.engine.wait("core-1", timeout_seconds=1)
        with self.assertRaisesRegex(ValueError, "RUN_ALREADY_TERMINAL"):
            self.engine.cancel("core-1")

    def test_error_plus_aborted_outcome_becomes_cancelled(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.CANCELLED, "OPENCLAW_ABORTED")
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=1)
        self.assertEqual("CANCELLED", record["status"])


if __name__ == "__main__":
    unittest.main()
