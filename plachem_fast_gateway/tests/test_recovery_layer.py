"""Tests for the FastGateway Recovery Layer v0.1 (post-terminal, FAIL-only)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.core_engine import (
    AgentRegistry,
    CoreEngine,
    ModelRegistry,
    RunRegistry,
)
from plachem_fast_gateway.openclaw_adapter import (
    AdapterOutcome,
    CoreRunStatus,
    RunBinding,
)
from plachem_fast_gateway.recovery_layer import (
    MAX_RECOVERY_ATTEMPTS,
    POLICY_BLOCKED,
    RESULT_RECOVERABLE,
    SESSION_RECOVERABLE,
    UNKNOWN,
    RecoveryLayer,
    build_recovery_message,
    classify_fail,
    evaluate_recovery,
)


PASS_RESULT = {
    "status": "completed",
    "summary": "done",
    "evidence": [{"type": "response"}],
    "artifacts": [],
    "scope": {"compliant": True, "violations": []},
}


class ScriptedAdapter:
    """Adapter that replays one scripted terminal outcome per submitted run."""

    def __init__(self, outcomes: dict[str, AdapterOutcome] | None = None) -> None:
        self.outcomes: dict[str, AdapterOutcome] = dict(outcomes or {})
        self.submit_calls = []
        self.cancel_calls = []
        self._terminal: set[str] = set()

    def submit(self, core_run_id, payload):
        self.submit_calls.append((core_run_id, dict(payload)))
        # Mirrors live adapter: when the agent response omits sessionId, the
        # adapter resolves it via a sessions.list RPC. The fake returns a
        # deterministic session_id so the recovery layer can be exercised.
        return RunBinding(
            core_run_id, f"oc-{core_run_id}", payload["agentId"],
            f"agent:{payload['agentId']}:main", "session-1",
            payload["idempotencyKey"], CoreRunStatus.RUNNING,
        )

    def request(self, method, params, timeout_ms=15000):
        """Minimal fake RPC bridge for sessions.list lookups (mirrors live bridge)."""
        if method == "sessions.list":
            rows = [{"key": f"agent:{a}:main", "sessionId": f"session-{a}"} for a in ("erpmanager", "erpcoder", "erpqa", "secretary")]
            return ({"sessions": rows, "count": len(rows)}, "fake-connection")
        raise AssertionError(f"unexpected RPC {method}")

    def wait(self, core_run_id, *, timeout_seconds):
        if core_run_id in self.outcomes:
            outcome = self.outcomes[core_run_id]
            if core_run_id not in self._terminal:
                self._terminal.add(core_run_id)
                return outcome
            # A terminal outcome is delivered exactly once; afterwards the
            # run is already terminal in Core and this must not happen.
            raise AssertionError(f"adapter polled after terminal for {core_run_id}")
        return AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")

    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        return RunBinding(core_run_id, f"oc-{core_run_id}", "qwentest",
                          "agent:qwentest:main", "session-1", "idem-x",
                          CoreRunStatus.CANCELLED)

    def close(self) -> None:
        return None


def _pass_outcome():
    return AdapterOutcome(CoreRunStatus.PASS, result=dict(PASS_RESULT))


def _fail_outcome(reason: str):
    return AdapterOutcome(CoreRunStatus.FAIL, reason)


def _seed(adapter: ScriptedAdapter, core_run_id: str, outcome: AdapterOutcome) -> None:
    adapter.outcomes[core_run_id] = outcome


class RecoveryLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        agents_path = root / "agents.json"
        agents_path.write_text(
            json.dumps({
                "qwentest": {
                    "enabled": True,
                    "capabilities": ["safe-smoke"],
                    "runtime_model_id": "local/test",
                    "allowed_model_ids": ["local/test"],
                    "allowed_policy_profiles": ["LOCAL_STANDARD"],
                },
            }),
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
            }}),
            encoding="utf-8",
        )
        self.registry = RunRegistry(root / "runs.jsonl")
        self.agents = AgentRegistry.load(agents_path)
        self.models = ModelRegistry.load(models_path)

    def _engine_with(self, adapter: ScriptedAdapter) -> tuple[CoreEngine, RecoveryLayer]:
        engine = CoreEngine(self.registry, self.agents, self.models, adapter)
        return engine, RecoveryLayer(engine, self.registry)

    def _dispatch_fail(self, engine: CoreEngine, adapter: ScriptedAdapter, reason: str,
                       core_run_id: str = "core-fail-1", idempotency_key: str = "idem-fail-1") -> dict:
        _seed(adapter, core_run_id, _fail_outcome(reason))
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id=core_run_id, idempotency_key=idempotency_key,
            goal_contract={
                "primary_objective": "Ship the feature",
                "allowed_scope": ["code changes in repo"],
                "forbidden_scope": ["production deploy"],
                "expected_result": "verified result",
                "completion_conditions": ["code written", "tests pass"],
            },
        )
        record = engine.wait(core_run_id, timeout_seconds=10)
        self.assertEqual(CoreRunStatus.FAIL.value, record["status"])
        self.assertEqual(reason, record["reason"])
        return record

    # -- 1. classification rules -----------------------------------------
    def test_fail_classification_rules(self):
        self.assertEqual("RESULT_RECOVERABLE", classify_fail("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED"))
        self.assertEqual("RESULT_RECOVERABLE", classify_fail("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT"))
        self.assertEqual("RESULT_RECOVERABLE", classify_fail("MISSING_RESULT"))
        self.assertEqual("SESSION_RECOVERABLE", classify_fail("TRANSPORT_FAILURE"))
        self.assertEqual("SESSION_RECOVERABLE", classify_fail("WORKER_FAILED"))
        self.assertEqual(POLICY_BLOCKED, classify_fail("POLICY_ABORT_FAILED"))
        self.assertEqual(POLICY_BLOCKED, classify_fail("USER_CANCEL"))
        self.assertEqual(UNKNOWN, classify_fail("DISPATCH_REJECTED"))
        self.assertEqual(UNKNOWN, classify_fail(""))

    def test_pass_run_is_never_recovered(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_with(adapter)
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id="core-pass-1", idempotency_key="idem-pass-1",
        )
        _seed(adapter, "core-pass-1", _pass_outcome())
        record = engine.wait("core-pass-1", timeout_seconds=10)
        self.assertEqual(CoreRunStatus.PASS.value, record["status"])
        verdict = layer.classify("core-pass-1")
        self.assertFalse(verdict.eligible)
        self.assertEqual("NOT_FAIL", verdict.reason)
        result = layer.recover("core-pass-1", wait_seconds=1)
        self.assertEqual("REJECTED", result["status"])
        self.assertEqual(1, len(adapter.submit_calls))  # no recovery dispatch

    # -- 2. recoverable FAIL gets one recovery and PASS_RECOVERED ---------
    def test_result_fail_recovered_to_pass(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_with(adapter)
        self._dispatch_fail(engine, adapter, "EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED")
        before = self.registry.get("core-fail-1")

        attempt = layer.attempt_recovery("core-fail-1")
        self.assertEqual("DISPATCHED", attempt["status"])
        self.assertEqual(RESULT_RECOVERABLE, attempt["verdict"]["fail_class"])
        _seed(adapter, attempt["recovery_run_id"], _pass_outcome())
        result = layer.wait_recovery(attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("PASS_RECOVERED", result["final_state"])
        child = self.registry.get(attempt["recovery_run_id"])
        self.assertEqual(CoreRunStatus.PASS.value, child["status"])
        goal_state = (child.get("policy_state") or {}).get("goal") or {}
        self.assertEqual("core-fail-1", goal_state["recovery_source_run_id"])
        self.assertEqual(1, goal_state["recovery_attempt"])
        # parent FAIL record is immutable
        after = self.registry.get("core-fail-1")
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(CoreRunStatus.FAIL.value, after["status"])

    # -- 3. policy-blocked FAIL is rejected, no dispatch ------------------
    def test_policy_blocked_fail_not_recovered(self):
        adapter = ScriptedAdapter([])
        engine, layer = self._engine_with(adapter)
        self._dispatch_fail(engine, adapter, "POLICY_ABORT_FAILED")
        result = layer.recover("core-fail-1", wait_seconds=1)
        self.assertEqual("REJECTED", result["status"])
        self.assertEqual(POLICY_BLOCKED, result["verdict"]["fail_class"])
        self.assertEqual("CLASS_NOT_RECOVERABLE", result["verdict"]["reason"])
        self.assertEqual(1, len(adapter.submit_calls))
        self.assertEqual("core-fail-1", adapter.submit_calls[0][0])  # parent only, no recovery dispatch

    # -- 4. second attempt is fail-closed (MAX_RECOVERY_ATTEMPTS = 1) -----
    def test_second_recovery_attempt_is_blocked(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_with(adapter)
        self._dispatch_fail(engine, adapter, "MISSING_RESULT")
        first_attempt = layer.attempt_recovery("core-fail-1")
        _seed(adapter, first_attempt["recovery_run_id"], _pass_outcome())
        first = layer.wait_recovery(first_attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("PASS_RECOVERED", first["final_state"])
        # A second recovery against the same parent must be rejected: the
        # idempotency key collides with the first recovery child.
        second = layer.attempt_recovery("core-fail-1")
        self.assertEqual("REJECTED", second["status"])
        self.assertEqual("DISPATCH_REJECTED", second["verdict"]["reason"])
        self.assertEqual(1, len([c for c in adapter.submit_calls if c[0] != "core-fail-1"]))
        self.assertEqual(MAX_RECOVERY_ATTEMPTS, 1)

    # -- 5. recovery run FAILs -> HARD_FAIL --------------------------------
    def test_recovery_run_fail_is_hard_fail(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_with(adapter)
        self._dispatch_fail(engine, adapter, "EVIDENCE_VALIDATION_FAILED:EVIDENCE_CONTRADICTION")
        attempt = layer.attempt_recovery("core-fail-1")
        _seed(adapter, attempt["recovery_run_id"], _fail_outcome("WORKER_FAILED"))
        result = layer.wait_recovery(attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("DISPATCHED", attempt["status"])
        self.assertEqual("HARD_FAIL", result["final_state"])
        child = self.registry.get(result["recovery_run_id"])
        self.assertEqual(CoreRunStatus.FAIL.value, child["status"])
        self.assertEqual("WORKER_FAILED", child["reason"])

    def test_recovery_needs_review_when_no_terminal_verdict(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_with(adapter)
        self._dispatch_fail(engine, adapter, "TRANSPORT_FAILURE")
        result = layer.recover("core-fail-1", wait_seconds=1)
        # No outcome seeded for the recovery child: adapter keeps reporting RUNNING.
        self.assertEqual("DISPATCHED", result["status"])
        self.assertEqual("NEEDS_REVIEW", result["final_state"])

    # -- recovery message contract ----------------------------------------
    def test_recovery_message_preserves_goal_contract(self):
        source = {
            "core_run_id": "core-fail-1",
            "status": "FAIL",
            "reason": "MISSING_RESULT",
            "format_error": "",
            "idempotency_key": "idem-fail-1",
            "agent_id": "qwentest",
            "goal_contract": {
                "goal_id": "goal-1",
                "primary_objective": "Ship the feature",
                "allowed_scope": ["code changes in repo"],
                "forbidden_scope": ["production deploy"],
                "expected_result": "verified result",
                "completion_conditions": ["code written", "tests pass"],
            },
            "verified_progress": {"completed_conditions": ["code written"], "artifacts": [], "evidence": [], "scope": []},
        }
        message = build_recovery_message(source, fail_class=RESULT_RECOVERABLE, verdict_detail="MISSING_RESULT")
        self.assertIn('"primary_objective":"Ship the feature"', message)
        self.assertIn('"core_run_id":"core-fail-1"', message)
        needed = message.split("needed_evidence")[1].split("}")[0]
        self.assertIn("tests pass", needed)           # only the remaining condition is requested
        self.assertNotIn("code written", needed)      # verified condition not re-requested
        self.assertIn("recovery_instruction", message)

    # -- stats over historical records -------------------------------------
    def test_stats_counts_five_states(self):
        _, layer = self._engine_with(ScriptedAdapter([]))
        records = [
            {"core_run_id": "a", "status": "PASS", "reason": ""},
            {"core_run_id": "b", "status": "PASS", "reason": "", "policy_state": {"goal": {"recovery_source_run_id": "x"}}},
            {"core_run_id": "c", "status": "FAIL", "reason": "EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED"},
            {"core_run_id": "d", "status": "FAIL", "reason": "WORKER_FAILED"},
            {"core_run_id": "e", "status": "FAIL", "reason": "POLICY_ABORT_FAILED"},
            {"core_run_id": "f", "status": "FAIL", "reason": "DISPATCH_REJECTED"},
            {"core_run_id": "g", "status": "BLOCKED", "reason": "", "policy_state": {"goal": {"recovery_source_run_id": "c"}}},
            {"core_run_id": "h", "status": "FAIL", "reason": "WORKER_FAILED", "policy_state": {"goal": {"recovery_source_run_id": "d"}}},
        ]
        stats = layer.stats(records)
        self.assertEqual(1, stats["states"]["PASS_PRIMARY"])
        self.assertEqual(1, stats["states"]["PASS_RECOVERED"])
        self.assertEqual(1, stats["states"]["POLICY_BLOCKED"])
        self.assertEqual(4, stats["states"]["FAIL_UNRECOVERED"])
        self.assertEqual(8, stats["total_scanned"])
        self.assertEqual(1, stats["fail_classes"][RESULT_RECOVERABLE])
        self.assertEqual(2, stats["fail_classes"][SESSION_RECOVERABLE])
        self.assertEqual(1, stats["fail_classes"][POLICY_BLOCKED])
        self.assertEqual(1, stats["fail_classes"][UNKNOWN])

    # -- unknown run ---------------------------------------------------------
    def test_unknown_run_raises(self):
        _, layer = self._engine_with(ScriptedAdapter([]))
        with self.assertRaises(ValueError):
            layer.classify("core-missing")


if __name__ == "__main__":
    unittest.main()
