from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway import OpenClawAdapter, WorkerTransport
from plachem_fast_gateway.core_engine import AgentRegistry, CoreEngine, RunRegistry
from plachem_fast_gateway.runtime_policy import ModelRegistry
from plachem_fast_gateway.openclaw_adapter import (
    AdapterOutcome,
    CoreRunStatus,
    RunBinding,
)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def connect(self):
        return {"protocol": 4}

    def set_output_observer(self, observer):
        self.calls.append(("observer", observer))

    def submit(self, core_run_id, payload):
        copied = dict(payload)
        self.calls.append(("submit", copied))
        return RunBinding(
            core_run_id, "oc-run", copied["agentId"],
            f"agent:{copied['agentId']}:main", "session-id",
            copied["idempotencyKey"], CoreRunStatus.RUNNING,
        )

    def wait(self, core_run_id, *, timeout_seconds):
        self.calls.append(("wait", (core_run_id, timeout_seconds)))
        return AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")

    def cancel(self, core_run_id):
        self.calls.append(("cancel", core_run_id))
        return RunBinding(
            core_run_id, "oc-run", "qwentest", "agent:qwentest:main",
            "session-id", "idem", CoreRunStatus.CANCELLED,
        )

    def close(self):
        self.calls.append(("close", None))


class WorkerTransportWiringTests(unittest.TestCase):
    def test_openclaw_adapter_implements_worker_transport(self):
        required = {"connect", "submit", "wait", "cancel", "close"}
        self.assertTrue(required.issubset(set(dir(OpenClawAdapter))))
        self.assertTrue(issubclass(OpenClawAdapter, WorkerTransport))

    def test_core_sends_only_allowlisted_worker_fields_and_preserves_idempotency(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agents = root / "agents.json"
            models = root / "models.json"
            agents.write_text(
                '{"qwentest":{"enabled":true,"capabilities":[],"runtime_model_id":"cloud/test",'
                '"allowed_model_ids":["cloud/test"],"allowed_policy_profiles":["CLOUD_STANDARD"]}}',
                encoding="utf-8",
            )
            models.write_text(
                '{"models":{"cloud/test":{"runtime_class":"CLOUD","policy_profile":"CLOUD_STANDARD",'
                '"max_runtime":100,"max_retries":1,"max_tool_calls":10,'
                '"loop_guard":{"consecutive_threshold":5},"context_policy":"REUSE",'
                '"fallback_policy":"NONE"}}}', encoding="utf-8",
            )
            engine = CoreEngine(
                RunRegistry(root / "runs.jsonl"),
                AgentRegistry.load(agents), ModelRegistry.load(models), transport,
            )
            first = engine.dispatch(
                core_run_id="core-1", agent_id="qwentest", message="work",
                timeout_seconds=5, idempotency_key="idem-1",
            )
            duplicate = engine.dispatch(
                core_run_id="core-1", agent_id="qwentest", message="work",
                timeout_seconds=5, idempotency_key="idem-1",
            )

        submits = [payload for name, payload in transport.calls if name == "submit"]
        self.assertEqual(1, len(submits))
        self.assertEqual(
            {"message", "agentId", "idempotencyKey", "timeout"}, set(submits[0]),
        )
        self.assertEqual("qwentest", submits[0]["agentId"])
        self.assertEqual("idem-1", submits[0]["idempotencyKey"])
        for forbidden in ("model", "provider", "endpoint", "workspace", "correlation_id"):
            self.assertNotIn(forbidden, submits[0])
        self.assertEqual(first["openclaw_binding"], duplicate["openclaw_binding"])


if __name__ == "__main__":
    unittest.main()
