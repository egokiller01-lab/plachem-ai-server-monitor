from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plachem_fast_gateway.core_engine import AgentRegistry, CoreEngine, RunRegistry
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from plachem_fast_gateway.runtime_policy import ModelRegistry


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Timer:
    instances = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class Adapter:
    def __init__(self, clock):
        self.clock = clock
        self.submits = []
        self.wait_calls = []
        self.wait_steps = []
        self.cancel_calls = []

    def submit(self, core_run_id, payload):
        self.submits.append((core_run_id, dict(payload)))
        return RunBinding(
            core_run_id, f"oc-{core_run_id}", payload["agentId"],
            f"agent:{payload['agentId']}:main", None, payload["idempotencyKey"],
            CoreRunStatus.RUNNING,
        )

    def wait(self, core_run_id, *, timeout_seconds):
        self.wait_calls.append(timeout_seconds)
        if not self.wait_steps:
            return AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")
        advance, outcome = self.wait_steps.pop(0)
        self.clock.advance(advance(timeout_seconds) if callable(advance) else advance)
        return outcome

    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        _, payload = next(item for item in self.submits if item[0] == core_run_id)
        return RunBinding(
            core_run_id, f"oc-{core_run_id}", payload["agentId"],
            f"agent:{payload['agentId']}:main", None, payload["idempotencyKey"],
            CoreRunStatus.CANCELLED,
        )


def completed_outcome(*, recovered=False):
    return AdapterOutcome(
        CoreRunStatus.PASS,
        result={
            "status": "completed", "summary": "done",
            "evidence": [{"type": "runtime_observation", "detail": "response observed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        },
        format_error="RESULT_FORMAT_ERROR" if recovered else "",
        format_recovery_attempts=1 if recovered else 0,
    )


class ExecutionRecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "agents.json").write_text(json.dumps({
            "local": {
                "enabled": True, "capabilities": [], "runtime_model_id": "local/model",
                "allowed_model_ids": ["local/model"], "allowed_policy_profiles": ["LOCAL_STANDARD"],
            },
            "cloud": {
                "enabled": True, "capabilities": [], "runtime_model_id": "cloud/model",
                "allowed_model_ids": ["cloud/model"], "allowed_policy_profiles": ["CLOUD_STANDARD"],
            },
        }), encoding="utf-8")
        (root / "models.json").write_text(json.dumps({"models": {
            "local/model": {
                "runtime_class": "LOCAL", "policy_profile": "LOCAL_STANDARD",
                "max_runtime": 300, "execution_budget": 240,
                "finalization_recovery_budget": 60, "max_retries": 1,
                "max_tool_calls": 20, "loop_guard": {"consecutive_threshold": 3},
                "context_policy": "FRESH_ON_LOOP", "fallback_policy": "MANUAL",
            },
            "cloud/model": {
                "runtime_class": "CLOUD", "policy_profile": "CLOUD_STANDARD",
                "max_runtime": 1800, "max_retries": 1, "max_tool_calls": 100,
                "loop_guard": {"consecutive_threshold": 5},
                "context_policy": "REUSE", "fallback_policy": "NONE",
            },
        }}), encoding="utf-8")
        self.clock = Clock()
        self.adapter = Adapter(self.clock)
        self.models = ModelRegistry.load(root / "models.json")
        self.engine = CoreEngine(
            RunRegistry(root / "runs.jsonl", clock=self.clock),
            AgentRegistry.load(root / "agents.json"), self.models, self.adapter,
            clock=self.clock,
        )
        Timer.instances.clear()
        timer_patch = patch("plachem_fast_gateway.core_engine.threading.Timer", Timer)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)

    def dispatch(self, *, run="run-local", agent="local", timeout=1):
        return self.engine.dispatch(
            agent_id=agent, message="safe", timeout_seconds=timeout,
            core_run_id=run, idempotency_key=run,
        )

    def test_local_execution_and_recovery_budgets_are_separate(self):
        profile = self.models.require("local/model")
        self.assertEqual((240.0, 60.0, 300.0), (
            profile.execution_budget, profile.finalization_recovery_budget, profile.max_runtime,
        ))
        record = self.dispatch()
        self.assertEqual((240.0, 60.0, 300.0), (
            record["execution_budget"], record["finalization_recovery_budget"], record["max_runtime"],
        ))
        self.assertEqual(240.0, Timer.instances[-1].interval)

    def test_execution_boundary_preserves_finalization_time(self):
        self.adapter.wait_steps = [
            (lambda timeout: timeout, AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")),
            (10, completed_outcome()),
        ]
        self.dispatch()
        record = self.engine.wait("run-local", timeout_seconds=999)
        self.assertEqual("PASS", record["status"])
        self.assertEqual([240.0, 10.0], self.adapter.wait_calls)
        self.assertLessEqual(record["runtime_seconds"], 300.0)
        self.assertIn("EXECUTION_BUDGET_EXHAUSTED", [event["code"] for event in record["policy_events"]])

    def test_total_hard_deadline_is_not_exceeded(self):
        self.adapter.wait_steps = [
            (lambda timeout: timeout, AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")),
            (10, AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")),
        ]
        self.dispatch()
        record = self.engine.wait("run-local", timeout_seconds=999)
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual("LOCAL_LLM_RUNTIME_LIMIT", record["cancel_reason"])
        self.assertLessEqual(record["runtime_seconds"], 300.0)
        self.assertEqual(["run-local"], self.adapter.cancel_calls)

    def test_one_second_caller_timeout_cannot_override_local_policy(self):
        first = self.dispatch(timeout=1)
        second = self.dispatch(timeout=999)
        self.assertEqual(first, second)
        self.assertEqual(300.0, self.adapter.submits[0][1]["timeout"])
        self.assertEqual(1, len(self.adapter.submits))

    def test_one_second_poll_is_running_not_failure(self):
        self.dispatch()
        record = self.engine.wait("run-local", timeout_seconds=1)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([1.0], self.adapter.wait_calls)
        self.assertEqual([], self.adapter.cancel_calls)

    def test_result_recovery_runs_inside_reserved_budget(self):
        self.adapter.wait_steps = [
            (lambda timeout: timeout, AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")),
            (10, completed_outcome(recovered=True)),
        ]
        self.dispatch()
        record = self.engine.wait("run-local", timeout_seconds=999)
        self.assertEqual("PASS", record["status"])
        self.assertEqual("RESULT_FORMAT_ERROR", record["format_error"])
        self.assertEqual(1, record["format_recovery_attempts"])
        self.assertLessEqual(record["runtime_seconds"], 300.0)

    def test_fast_local_and_cloud_runtime_regression(self):
        self.adapter.wait_steps = [(1, completed_outcome())]
        self.dispatch()
        local = self.engine.wait("run-local", timeout_seconds=1)
        self.assertEqual("PASS", local["status"])
        self.assertEqual(300.0, self.adapter.submits[0][1]["timeout"])

        self.adapter.wait_steps = [(1, completed_outcome())]
        self.dispatch(run="run-cloud", agent="cloud", timeout=1)
        cloud = self.engine.wait("run-cloud", timeout_seconds=1)
        self.assertEqual("PASS", cloud["status"])
        self.assertEqual(300.0, self.adapter.submits[1][1]["timeout"])


if __name__ == "__main__":
    unittest.main()
