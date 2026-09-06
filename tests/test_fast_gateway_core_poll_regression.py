import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plachem_fast_gateway.core_engine import AgentRegistration, AgentRegistry, CoreEngine, RunRegistry
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from plachem_fast_gateway.runtime_policy import ModelRegistry, RuntimeClass, RuntimeModelProfile


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class BoundedTimeoutAdapter:
    def __init__(self):
        self.cancelled = []

    def submit(self, core_run_id, payload):
        return RunBinding(core_run_id, "openclaw-" + core_run_id, payload["agentId"], "agent:" + payload["agentId"] + ":main", None, payload["idempotencyKey"], CoreRunStatus.RUNNING)

    def wait(self, core_run_id, *, timeout_seconds):
        return AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")

    def cancel(self, core_run_id):
        self.cancelled.append(core_run_id)
        return self.submit(core_run_id, {"agentId": "worker", "idempotencyKey": core_run_id})


def make_engine(runtime_class, clock, max_runtime=300.0):
    registration = AgentRegistration("worker", True, ("test",), "test-model", ("test-model",), ("TEST",))
    profile = RuntimeModelProfile("test-model", runtime_class, "TEST", max_runtime, 0, None, {"consecutive_threshold": 3}, "MANUAL", "NONE", 0)
    return CoreEngine(RunRegistry(Path(tempfile.mktemp()), clock=clock), AgentRegistry({"worker": registration}), ModelRegistry({"test-model": profile}), BoundedTimeoutAdapter(), clock=clock)


class FastGatewayCorePollRegressionTests(unittest.TestCase):
    def test_bounded_openclaw_timeout_preserves_cloud_running(self):
        clock = Clock(); engine = make_engine(RuntimeClass.CLOUD, clock)
        record = engine.dispatch(agent_id="worker", message="poll", timeout_seconds=1, core_run_id="cloud-poll")
        observed = engine.wait(record["core_run_id"], timeout_seconds=1)
        self.assertEqual("RUNNING", observed["status"])
        self.assertEqual([], engine.adapter.cancelled)

    def test_bounded_openclaw_timeout_preserves_local_running(self):
        clock = Clock(); engine = make_engine(RuntimeClass.LOCAL, clock)
        record = engine.dispatch(agent_id="worker", message="poll", timeout_seconds=1, core_run_id="local-poll")
        observed = engine.wait(record["core_run_id"], timeout_seconds=1)
        self.assertEqual("RUNNING", observed["status"])
        self.assertEqual([], engine.adapter.cancelled)

    def test_absolute_runtime_deadline_still_cancels_after_bounded_timeout(self):
        clock = Clock(); engine = make_engine(RuntimeClass.CLOUD, clock, max_runtime=5.0)
        record = engine.dispatch(agent_id="worker", message="deadline", timeout_seconds=1, core_run_id="cloud-deadline")
        clock.value += timedelta(seconds=6)
        observed = engine.wait(record["core_run_id"], timeout_seconds=1)
        self.assertEqual("CANCELLED", observed["status"])
        self.assertEqual("RUNTIME_POLICY_RUNTIME_LIMIT", observed["cancel_reason"])
        self.assertEqual(["cloud-deadline"], engine.adapter.cancelled)


if __name__ == "__main__":
    unittest.main()
