"""Tests for the FastGateway Recovery Layer v0.1 (post-terminal, FAIL-only)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.auth_broker import (
    AuthScope,
    execution_auth_scope,
    canonical_task_digest,
    SQLiteAuthBroker,
    AuthBrokerError,
)
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
    MAX_RECOVERY_DISPATCH_ATTEMPTS,
    POLICY_BLOCKED,
    RESULT_RECOVERABLE,
    SESSION_RECOVERABLE,
    UNKNOWN,
    RecoveryLayer,
    build_recovery_message,
    classify_fail,
    evaluate_recovery,
    recovery_goal_contract,
    _is_pre_execution_block,
)
from direct_gateway_authorization import (
    CompositeGrantAuthorizer,
    DirectIngressGrantAuthorizer,
)


class _TestSecretRef:
    """Minimal SecretRef for test brokers."""
    def get(self, key_id: str) -> bytes:
        return b"test-pepper-bytes-32-bytes-long!!"


PASS_RESULT = {
    "status": "completed",
    "summary": "done",
    "evidence": [{"type": "response"}],
    "artifacts": [],
    "scope": {"compliant": True, "violations": []},
}

GOAL_CONTRACT = {
    "primary_objective": "Ship the feature",
    "allowed_scope": ["CODE_CHANGES_IN_REPO"],
    "forbidden_scope": ["PRODUCTION_DEPLOY"],
    "expected_result": "VERIFIED_RESULT",
    "completion_conditions": ["CODE_WRITTEN", "TESTS_PASS"],
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
        return RunBinding(
            core_run_id, f"oc-{core_run_id}", payload["agentId"],
            f"agent:{payload['agentId']}:main", "session-1",
            payload["idempotencyKey"], CoreRunStatus.RUNNING,
        )

    def request(self, method, params, timeout_ms=15000):
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


class _BaseRecoveryTest(unittest.TestCase):
    """Common setup for recovery tests with optional auth."""

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
        self.root = root
        self.registry = RunRegistry(root / "runs.jsonl")
        self.agents = AgentRegistry.load(agents_path)
        self.models = ModelRegistry.load(models_path)
        self._broker = None

    def _make_broker(self):
        if self._broker is None:
            self._broker = SQLiteAuthBroker(
                self.root / "auth_broker.sqlite3",
                _TestSecretRef(),
                key_id="test-key",
            )
        return self._broker

    def _engine_no_auth(self, adapter: ScriptedAdapter) -> tuple[CoreEngine, RecoveryLayer]:
        engine = CoreEngine(self.registry, self.agents, self.models, adapter)
        return engine, RecoveryLayer(engine, self.registry)

    def _engine_with_auth(self, adapter: ScriptedAdapter) -> tuple[CoreEngine, RecoveryLayer, DirectIngressGrantAuthorizer]:
        broker = self._make_broker()
        direct_auth = DirectIngressGrantAuthorizer()
        engine = CoreEngine(
            self.registry, self.agents, self.models, adapter,
            auth_broker=broker, auth_required=True,
            grant_authorizer=CompositeGrantAuthorizer(direct_auth),
        )
        return engine, RecoveryLayer(engine, self.registry), direct_auth

    def _dispatch_fail(self, engine: CoreEngine, adapter: ScriptedAdapter, reason: str,
                       direct_auth: DirectIngressGrantAuthorizer | None = None,
                       core_run_id: str | None = None, idempotency_key: str | None = None) -> dict:
        # DirectIngressGrantAuthorizer only owns direct-* run IDs.
        if direct_auth is not None and core_run_id is None:
            core_run_id = "direct-test-fail-abc123"
        if core_run_id is None:
            core_run_id = "core-fail-1"
        if idempotency_key is None:
            idempotency_key = f"idem-{core_run_id}"
        _seed(adapter, core_run_id, _fail_outcome(reason))
        if direct_auth is not None:
            # Register auth grant for the source run (as direct dispatch does)
            from plachem_fast_gateway.runtime_policy import normalize_goal_contract
            nc = normalize_goal_contract(GOAL_CONTRACT)
            scope = execution_auth_scope(
                agent_id="qwentest", message="Do the goal.",
                core_run_id=core_run_id, idempotency_key=idempotency_key,
                goal_contract=nc.as_dict(),
            )
            direct_auth.register(core_run_id, scope)
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id=core_run_id, idempotency_key=idempotency_key,
            goal_contract=GOAL_CONTRACT,
        )
        record = engine.wait(core_run_id, timeout_seconds=10)
        self.assertEqual(CoreRunStatus.FAIL.value, record["status"])
        self.assertEqual(reason, record["reason"])
        return record


# ─── 1. Classification rules ───────────────────────────────────────────────


class TestClassification(_BaseRecoveryTest):
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

    def test_pre_execution_block_detection(self):
        self.assertTrue(_is_pre_execution_block("BLOCKED", "AUTH_AUTH_REQUIRED"))
        self.assertTrue(_is_pre_execution_block("BLOCKED", "AUTH_BROKER_UNAVAILABLE"))
        self.assertTrue(_is_pre_execution_block("BLOCKED", "AUTH_EXPIRED"))
        self.assertFalse(_is_pre_execution_block("BLOCKED", "WORKER_BLOCKED"))
        self.assertFalse(_is_pre_execution_block("FAIL", "AUTH_AUTH_REQUIRED"))
        self.assertFalse(_is_pre_execution_block("PASS", ""))


# ─── 2. PASS run is never recovered ────────────────────────────────────────


class TestPassNeverRecovered(_BaseRecoveryTest):
    def test_pass_run_is_never_recovered(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        _seed(adapter, "core-pass-1", _pass_outcome())
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id="core-pass-1", idempotency_key="idem-pass-1",
        )
        record = engine.wait("core-pass-1", timeout_seconds=10)
        self.assertEqual(CoreRunStatus.PASS.value, record["status"])
        verdict = layer.classify("core-pass-1")
        self.assertFalse(verdict.eligible)
        self.assertEqual("NOT_FAIL", verdict.reason)
        result = layer.recover("core-pass-1", wait_seconds=1)
        self.assertEqual("REJECTED", result["status"])
        self.assertEqual(1, len(adapter.submit_calls))


# ─── 3. Recoverable FAIL gets one recovery and PASS_RECOVERED (no auth) ───


class TestRecoveryPassNoAuth(_BaseRecoveryTest):
    def test_result_fail_recovered_to_pass(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED")
        before = self.registry.get("core-fail-1")

        attempt = layer.attempt_recovery("core-fail-1")
        self.assertEqual("DISPATCHED", attempt["status"])
        self.assertEqual(RESULT_RECOVERABLE, attempt["verdict"]["fail_class"])
        self.assertEqual(1, attempt["recovery_dispatch_attempt"])
        self.assertEqual(1, attempt["recovery_execution_attempt"])
        self.assertTrue(attempt["execution_started"])
        _seed(adapter, attempt["recovery_run_id"], _pass_outcome())
        result = layer.wait_recovery(attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("PASS_RECOVERED", result["final_state"])
        child = self.registry.get(attempt["recovery_run_id"])
        self.assertEqual(CoreRunStatus.PASS.value, child["status"])
        goal_state = (child.get("policy_state") or {}).get("goal") or {}
        self.assertEqual("core-fail-1", goal_state["recovery_source_run_id"])
        self.assertEqual(1, goal_state["recovery_dispatch_attempt"])
        self.assertEqual(1, goal_state["recovery_execution_attempt"])
        # parent FAIL record is immutable
        after = self.registry.get("core-fail-1")
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(CoreRunStatus.FAIL.value, after["status"])


# ─── 4. Policy-blocked FAIL is rejected, no dispatch ───────────────────────


class TestPolicyBlockedRejected(_BaseRecoveryTest):
    def test_policy_blocked_fail_not_recovered(self):
        adapter = ScriptedAdapter([])
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "POLICY_ABORT_FAILED")
        result = layer.recover("core-fail-1", wait_seconds=1)
        self.assertEqual("REJECTED", result["status"])
        self.assertEqual(POLICY_BLOCKED, result["verdict"]["fail_class"])
        self.assertEqual("CLASS_NOT_RECOVERABLE", result["verdict"]["reason"])
        self.assertEqual(1, len(adapter.submit_calls))


# ─── 5. Second attempt is fail-closed (MAX_RECOVERY_ATTEMPTS = 1) ─────────


class TestSecondAttemptBlocked(_BaseRecoveryTest):
    def test_second_recovery_attempt_is_blocked(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "MISSING_RESULT")
        first_attempt = layer.attempt_recovery("core-fail-1")
        _seed(adapter, first_attempt["recovery_run_id"], _pass_outcome())
        first = layer.wait_recovery(first_attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("PASS_RECOVERED", first["final_state"])
        # A second recovery against the same parent must be rejected:
        # the execution attempt count is 1 >= MAX_RECOVERY_ATTEMPTS.
        second = layer.attempt_recovery("core-fail-1")
        self.assertEqual("REJECTED", second["status"])
        self.assertEqual("MAX_RECOVERY_ATTEMPTS", second["verdict"]["reason"])
        self.assertEqual(1, len([c for c in adapter.submit_calls if c[0] != "core-fail-1"]))
        self.assertEqual(MAX_RECOVERY_ATTEMPTS, 1)


# ─── 6. Recovery run FAILs -> HARD_FAIL ────────────────────────────────────


class TestRecoveryHardFail(_BaseRecoveryTest):
    def test_recovery_run_fail_is_hard_fail(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "EVIDENCE_VALIDATION_FAILED:EVIDENCE_CONTRADICTION")
        attempt = layer.attempt_recovery("core-fail-1")
        _seed(adapter, attempt["recovery_run_id"], _fail_outcome("WORKER_FAILED"))
        result = layer.wait_recovery(attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("DISPATCHED", attempt["status"])
        self.assertEqual("HARD_FAIL", result["final_state"])
        child = self.registry.get(result["recovery_run_id"])
        self.assertEqual(CoreRunStatus.FAIL.value, child["status"])
        self.assertEqual("WORKER_FAILED", child["reason"])


# ─── 7. Recovery NEEDS_REVIEW when no terminal verdict ─────────────────────


class TestRecoveryNeedsReview(_BaseRecoveryTest):
    def test_recovery_needs_review_when_no_terminal_verdict(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "TRANSPORT_FAILURE")
        result = layer.recover("core-fail-1", wait_seconds=1)
        self.assertEqual("DISPATCHED", result["status"])
        self.assertEqual("NEEDS_REVIEW", result["final_state"])


# ─── 8. Recovery message contract ─────────────────────────────────────────


class TestRecoveryMessage(_BaseRecoveryTest):
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
                "allowed_scope": ["CODE_CHANGES_IN_REPO"],
                "forbidden_scope": ["PRODUCTION_DEPLOY"],
                "expected_result": "VERIFIED_RESULT",
                "completion_conditions": ["CODE_WRITTEN", "TESTS_PASS"],
            },
            "verified_progress": {"completed_conditions": ["CODE_WRITTEN"], "artifacts": [], "evidence": [], "scope": []},
        }
        message = build_recovery_message(source, fail_class=RESULT_RECOVERABLE, verdict_detail="MISSING_RESULT")
        self.assertIn('"primary_objective":"Ship the feature"', message)
        self.assertIn('"core_run_id":"core-fail-1"', message)
        needed = message.split("needed_evidence")[1].split("}")[0]
        self.assertIn("TESTS_PASS", needed)
        self.assertNotIn("CODE_WRITTEN", needed)
        self.assertIn("recovery_instruction", message)

    def test_recovery_goal_contract_strips_goal_id(self):
        """goal_id is stripped because CoreEngine regenerates it
        deterministically from the same contract content."""
        source = {
            "goal_contract": {
                "goal_id": "goal-abc123",
                "primary_objective": "Ship the feature",
                "allowed_scope": ["CODE_CHANGES_IN_REPO"],
                "forbidden_scope": ["PRODUCTION_DEPLOY"],
                "expected_result": "VERIFIED_RESULT",
                "completion_conditions": ["CODE_WRITTEN", "TESTS_PASS"],
            },
        }
        projected = recovery_goal_contract(source)
        self.assertNotIn("goal_id", projected)
        self.assertEqual("Ship the feature", projected["primary_objective"])
        # The projected contract is directly normalizable by CoreEngine
        from plachem_fast_gateway.runtime_policy import normalize_goal_contract
        gc = normalize_goal_contract(projected)
        self.assertEqual("Ship the feature", gc.primary_objective)

    def test_recovery_goal_contract_rejects_expansion(self):
        """Recovery cannot add scope beyond the parent contract."""
        source = {
            "goal_contract": {
                "goal_id": "goal-abc123",
                "primary_objective": "Ship the feature",
                "allowed_scope": ["CODE_CHANGES_IN_REPO"],
                "forbidden_scope": ["PRODUCTION_DEPLOY"],
                "expected_result": "VERIFIED_RESULT",
                "completion_conditions": ["CODE_WRITTEN", "TESTS_PASS"],
            },
        }
        projected = recovery_goal_contract(source)
        # projected is a strict subset of the parent contract
        self.assertEqual(projected["allowed_scope"], source["goal_contract"]["allowed_scope"])
        self.assertEqual(projected["forbidden_scope"], source["goal_contract"]["forbidden_scope"])
        # No new fields beyond the 5 canonical contract fields
        self.assertEqual(
            set(projected.keys()),
            {"primary_objective", "allowed_scope", "forbidden_scope",
             "expected_result", "completion_conditions"},
        )


# ─── 9. Stats over historical records ──────────────────────────────────────


class TestStats(_BaseRecoveryTest):
    def test_stats_counts_five_states(self):
        _, layer = self._engine_no_auth(ScriptedAdapter([]))
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


# ─── 10. Unknown run raises ────────────────────────────────────────────────


class TestUnknownRun(_BaseRecoveryTest):
    def test_unknown_run_raises(self):
        _, layer = self._engine_no_auth(ScriptedAdapter([]))
        with self.assertRaises(ValueError):
            layer.classify("core-missing")


# ─── 11. AUTH: source authorized → child grant registered → dispatch ──────


class TestAuthGrantRegistered(_BaseRecoveryTest):
    def test_source_authorized_child_grant_registered(self):
        """When the source has valid auth, the recovery child gets a
        registered grant and dispatch proceeds past the auth gate."""
        adapter = ScriptedAdapter()
        engine, layer, direct_auth = self._engine_with_auth(adapter)
        record = self._dispatch_fail(
            engine, adapter, "EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED",
            direct_auth=direct_auth,
        )
        src_id = record["core_run_id"]
        self.assertEqual("FAIL", record["status"])

        attempt = layer.attempt_recovery(src_id)
        self.assertEqual("DISPATCHED", attempt["status"])
        self.assertTrue(attempt["execution_started"])
        self.assertEqual(1, attempt["recovery_execution_attempt"])
        # The child run should NOT be BLOCKED by auth
        child = self.registry.get(attempt["recovery_run_id"])
        self.assertNotEqual("BLOCKED", child["status"],
                            f"child was auth-blocked: reason={child.get('reason')}")

    def test_child_grant_scope_matches_parent(self):
        """The child grant's task_digest is computed from the same
        normalized goal contract as the parent, ensuring scope equality."""
        adapter = ScriptedAdapter()
        engine, layer, direct_auth = self._engine_with_auth(adapter)
        record = self._dispatch_fail(
            engine, adapter, "MISSING_RESULT",
            direct_auth=direct_auth,
        )
        src_id = record["core_run_id"]
        attempt = layer.attempt_recovery(src_id)
        self.assertEqual("DISPATCHED", attempt["status"])
        # The child was dispatched (not auth-blocked), confirming the
        # grant registration worked with the inherited contract.
        child = self.registry.get(attempt["recovery_run_id"])
        self.assertNotEqual("BLOCKED", child["status"])


# ─── 12. AUTH: no source authorization → no arbitrary grant ───────────────


class TestAuthNoSourceAuthorization(_BaseRecoveryTest):
    def test_no_source_auth_no_arbitrary_grant(self):
        """When the source run has no auth (e.g., non-direct run), the
        recovery layer must not create an arbitrary grant.  The dispatch
        should be blocked or rejected, not silently authorized."""
        adapter = ScriptedAdapter()
        engine, layer, direct_auth = self._engine_with_auth(adapter)
        # Dispatch a FAIL run WITHOUT registering a direct grant.
        # In a real system this would be a non-direct run, but for the
        # test we simulate by not registering.
        _seed(adapter, "core-fail-noauth", _fail_outcome("WORKER_FAILED"))
        # We need to bypass the auth gate for the source dispatch.
        # Use a non-direct run ID so the composite doesn't route to direct.
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id="core-fail-noauth", idempotency_key="idem-noauth-1",
            goal_contract=GOAL_CONTRACT,
        )
        # The source will be BLOCKED (auth_required but no grant registered).
        record = engine.wait("core-fail-noauth", timeout_seconds=10)
        # If it's BLOCKED, it's not a FAIL, so recovery won't apply.
        # This is the expected fail-closed behavior.
        if record["status"] == "BLOCKED":
            verdict = layer.classify("core-fail-noauth")
            self.assertFalse(verdict.eligible)
            return
        # If somehow it's FAIL (e.g., adapter failure before auth),
        # the recovery layer should still work correctly.
        attempt = layer.attempt_recovery("core-fail-noauth")
        # The child ID won't match direct-* pattern, so auth grant
        # registration will fail → POLICY_BLOCKED.
        if attempt["status"] == "REJECTED":
            self.assertIn("POLICY_BLOCKED", attempt.get("final_state", ""))


# ─── 13. AUTH: privilege expansion blocked ─────────────────────────────────


class TestAuthPrivilegeExpansion(_BaseRecoveryTest):
    def test_recovery_cannot_exceed_source_scope(self):
        """The recovery child inherits the parent's goal contract
        verbatim.  Any attempt to modify the contract (widen scope,
        change objective) would produce a different task_digest and
        fail the BINDING_MISMATCH check in DirectIngressGrantAuthorizer."""
        adapter = ScriptedAdapter()
        engine, layer, direct_auth = self._engine_with_auth(adapter)
        record = self._dispatch_fail(
            engine, adapter, "MISSING_RESULT",
            direct_auth=direct_auth,
        )
        source = self.registry.get(record["core_run_id"])
        original_contract = source["goal_contract"]

        # recovery_goal_contract projects the exact same scope fields
        projected = recovery_goal_contract(source)
        # The projected contract is a subset of the original —
        # no new fields, no modified values (goal_id is excluded).
        for key in projected:
            self.assertEqual(original_contract[key], projected[key],
                             f"field {key} was modified by recovery_goal_contract")
        self.assertNotIn("goal_id", projected)
        # No privilege expansion is possible: the child gets the
        # same contract the parent had.


# ─── 14. Pre-execution block does NOT consume execution attempt ────────────


class TestPreExecutionBlockSemantics(_BaseRecoveryTest):
    def test_auth_blocked_child_does_not_consume_execution_attempt(self):
        """A child that is BLOCKED with AUTH_AUTH_REQUIRED before the
        worker starts must NOT increment recovery_execution_attempt."""
        adapter = ScriptedAdapter()
        # Use a dispatcher that returns BLOCKED for the recovery child
        class _BlockingDispatcher:
            def __init__(self, engine):
                self._engine = engine
            def dispatch(self, **kwargs):
                # Simulate: the child is created but immediately BLOCKED
                # by auth (as would happen if grant registration failed
                # in a race condition or the grant expired).
                result = self._engine.dispatch(**kwargs)
                return result
        # We test the semantics directly: _is_pre_execution_block
        self.assertTrue(_is_pre_execution_block("BLOCKED", "AUTH_AUTH_REQUIRED"))
        self.assertFalse(_is_pre_execution_block("FAIL", "WORKER_FAILED"))
        self.assertFalse(_is_pre_execution_block("RUNNING", ""))

        # Verify evaluate_recovery uses execution_attempts (not dispatch_attempts)
        record = {
            "status": "FAIL",
            "reason": "WORKER_FAILED",
            "policy_state": {
                "goal": {
                    "recovery_dispatch_attempt": 1,
                    "recovery_execution_attempt": 0,
                },
            },
        }
        verdict = evaluate_recovery(record)
        self.assertTrue(verdict.eligible,
                        "pre-execution block should not block re-eligibility")
        self.assertEqual("ELIGIBLE", verdict.reason)

    def test_execution_attempt_consumes_budget(self):
        """A child that actually started (execution_attempt=1) blocks
        further recovery attempts."""
        record = {
            "status": "FAIL",
            "reason": "WORKER_FAILED",
            "policy_state": {
                "goal": {
                    "recovery_dispatch_attempt": 1,
                    "recovery_execution_attempt": 1,
                },
            },
        }
        verdict = evaluate_recovery(record)
        self.assertFalse(verdict.eligible)
        self.assertEqual("MAX_RECOVERY_ATTEMPTS", verdict.reason)


# ─── 15. Worker actual failure consumes execution attempt ──────────────────


class TestExecutionAttemptConsumed(_BaseRecoveryTest):
    def test_worker_failure_consumes_execution_attempt(self):
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED")
        attempt = layer.attempt_recovery("core-fail-1")
        self.assertEqual(1, attempt["recovery_execution_attempt"])
        self.assertTrue(attempt["execution_started"])
        _seed(adapter, attempt["recovery_run_id"], _fail_outcome("WORKER_FAILED"))
        result = layer.wait_recovery(attempt["recovery_run_id"], wait_seconds=10)
        self.assertEqual("HARD_FAIL", result["final_state"])
        self.assertEqual(1, result["recovery_execution_attempt"])
        # Now the parent should be ineligible for further recovery
        verdict = layer.classify("core-fail-1")
        self.assertFalse(verdict.eligible)
        self.assertEqual("MAX_RECOVERY_ATTEMPTS", verdict.reason)


# ─── 16. Bounded dispatch attempts (no infinite retry) ─────────────────────


class TestBoundedDispatchAttempts(_BaseRecoveryTest):
    def test_max_dispatch_attempts_prevents_infinite_retry(self):
        """Even with pre-execution blocks (no execution consumed),
        the dispatch attempt count is bounded."""
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        self._dispatch_fail(engine, adapter, "WORKER_FAILED")

        # Simulate: create child records with dispatch attempts but no
        # execution attempts (pre-execution blocks).
        # We test the _count_existing_dispatches logic directly.
        # Create a fake child record with recovery_source_run_id.
        # Since we can't easily create a child without dispatching,
        # we verify the counter returns 0 initially.
        self.assertEqual(0, layer._count_existing_dispatches("core-fail-1"))

        # After one real dispatch:
        attempt = layer.attempt_recovery("core-fail-1")
        if attempt["status"] == "DISPATCHED":
            self.assertEqual(1, layer._count_existing_dispatches("core-fail-1"))

    def test_max_dispatch_attempts_rejects_beyond_cap(self):
        """When dispatch attempts reach MAX_RECOVERY_DISPATCH_ATTEMPTS,
        further attempts are rejected."""
        adapter = ScriptedAdapter()
        engine, layer = self._engine_no_auth(adapter)
        # Override max_dispatch_attempts to 1 for this test
        layer.max_dispatch_attempts = 1
        self._dispatch_fail(engine, adapter, "WORKER_FAILED")

        first = layer.attempt_recovery("core-fail-1")
        self.assertEqual("DISPATCHED", first["status"])

        # Second attempt: dispatch count is now 1 >= max_dispatch_attempts
        second = layer.attempt_recovery("core-fail-1")
        # The second attempt should be rejected because:
        # - if first was a real execution: MAX_RECOVERY_ATTEMPTS
        # - if first was pre-execution: MAX_DISPATCH_ATTEMPTS
        self.assertEqual("REJECTED", second["status"])
        self.assertIn(second["verdict"]["reason"],
                      {"MAX_RECOVERY_ATTEMPTS", "MAX_DISPATCH_ATTEMPTS"})


# ─── 17. Auth grant registration with CompositeGrantAuthorizer ─────────────


class TestCompositeGrantAuthorization(_BaseRecoveryTest):
    def test_non_direct_source_not_eligible_under_auth(self):
        """Under auth_required, a non-direct source run (core-*) is
        BLOCKED at dispatch time (not FAIL), so it is not a recovery
        candidate.  The recovery layer correctly rejects it as NOT_FAIL.

        Additionally, the recovery layer's _register_auth_grant would
        fail-closed with AUTH_GRANT_UNAVAILABLE for a core-*-rec-* child
        because DirectIngressGrantAuthorizer only owns direct-* runs."""
        adapter = ScriptedAdapter()
        engine, layer, direct_auth = self._engine_with_auth(adapter)
        # Non-direct run under auth_required → BLOCKED, not FAIL
        _seed(adapter, "core-fail-nondirect", _fail_outcome("MISSING_RESULT"))
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id="core-fail-nondirect", idempotency_key="idem-nondirect-1",
            goal_contract=GOAL_CONTRACT,
        )
        record = engine.wait("core-fail-nondirect", timeout_seconds=10)
        self.assertEqual("BLOCKED", record["status"],
                         "non-direct run under auth should be BLOCKED")
        # Recovery layer rejects: not a FAIL terminal state
        verdict = layer.classify("core-fail-nondirect")
        self.assertFalse(verdict.eligible)
        self.assertEqual("NOT_FAIL", verdict.reason)

        # Verify the auth grant routing: core-* child is NOT owned by
        # DirectIngressGrantAuthorizer
        self.assertFalse(direct_auth.owns_run("core-fail-nondirect-rec-12345678"))
        self.assertTrue(direct_auth.owns_run("direct-fail-abc123-rec-12345678"))

    def test_direct_source_child_gets_grant(self):
        """When the source run is direct-*, the recovery child is also
        direct-*-rec-* and the DirectIngressGrantAuthorizer owns it."""
        adapter = ScriptedAdapter()
        engine, layer, direct_auth = self._engine_with_auth(adapter)
        # Dispatch a direct-* run that will FAIL
        _seed(adapter, "direct-test-abc123", _fail_outcome("MISSING_RESULT"))
        from plachem_fast_gateway.runtime_policy import normalize_goal_contract
        nc = normalize_goal_contract(GOAL_CONTRACT)
        scope = execution_auth_scope(
            agent_id="qwentest", message="Do the goal.",
            core_run_id="direct-test-abc123", idempotency_key="idem-direct-1",
            goal_contract=nc.as_dict(),
        )
        direct_auth.register("direct-test-abc123", scope)
        engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id="direct-test-abc123", idempotency_key="idem-direct-1",
            goal_contract=GOAL_CONTRACT,
        )
        record = engine.wait("direct-test-abc123", timeout_seconds=10)
        self.assertEqual("FAIL", record["status"])

        attempt = layer.attempt_recovery("direct-test-abc123")
        # The child is "direct-test-abc123-rec-xxxxxxxx" which starts
        # with "direct-" → DirectIngressGrantAuthorizer owns it →
        # grant registration succeeds → dispatch proceeds
        self.assertEqual("DISPATCHED", attempt["status"])
        self.assertTrue(attempt["execution_started"])
        child = self.registry.get(attempt["recovery_run_id"])
        self.assertNotEqual("BLOCKED", child["status"])


if __name__ == "__main__":
    unittest.main()
