"""Recovery v0.2 auto-wiring tests: FAIL -> RecoveryLayer hook, PASS -> no recovery."""

from __future__ import annotations

import threading
from typing import Any, Mapping

import pytest

from plachem_fast_gateway.core_engine import CoreRunStatus
from plachem_fast_gateway.recovery_layer import RecoveryLayer, RecoveryVerdict


class _FakeAdapter:
    """Minimal adapter satisfying CoreEngine's dispatch/wait surface."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.closed = False

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> Any:
        from plachem_fast_gateway.core_engine import AdapterOutcome
        outcome = self._outcomes.pop(0) if self._outcomes else AdapterOutcome(CoreRunStatus.PASS, "")
        return outcome

    def cancel(self, core_run_id: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeLayer:
    """Records every recover() call; returns a PASS_RECOVERED-shaped result."""

    def __init__(self, *, outcome: str = "DISPATCHED", final_state: str = "PASS_RECOVERED") -> None:
        self.calls: list[str] = []
        self.outcome = outcome
        self.final_state = final_state

    def recover(self, core_run_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(core_run_id)
        return {
            "status": self.outcome,
            "source_run_id": core_run_id,
            "recovery_run_id": f"{core_run_id}-rec-test" if self.outcome == "DISPATCHED" else None,
            "final_state": self.final_state if self.outcome == "DISPATCHED" else None,
        }


_DEFAULT_GOAL = {
    "primary_objective": "test",
    "allowed_scope": ["A"],
    "forbidden_scope": ["B"],
    "expected_result": "R",
    "completion_conditions": ["C1"],
}


def _make_harness(monkeypatch, outcomes: list, *, layer: _FakeLayer | None = None):
    """Build a PersistentExecutionHarness with a fake engine/adapter pair."""
    import fast_gateway_service as svc

    class _FakeEngine:
        def __init__(self) -> None:
            self.adapter = _FakeAdapter(outcomes)
            self.registry = _FakeRegistry()
            self.auth_broker = None
            self.grant_authorizer = None

        def set_terminal_transition_guard(self, guard) -> None:
            self._guard = guard

        def dispatch(self, **kwargs: Any) -> dict[str, Any]:
            cid = kwargs.get("core_run_id") or f"direct-test-{len(self.registry._records)}"
            rec = self.registry._new(cid, kwargs.get("agent_id", "erpmanager"))
            rec["idempotency_key"] = kwargs.get("idempotency_key") or f"{cid}"
            rec["goal_contract"] = dict(kwargs.get("goal_contract") or _DEFAULT_GOAL)
            rec["verified_progress"] = {"completed_conditions": []}
            self.registry._records[cid] = rec
            # Resolve the scripted terminal outcome synchronously so the
            # harness observes the same terminal state the real service
            # would after adapter.wait.
            outcome = self.adapter.wait(cid, timeout_seconds=1.0)
            rec["status"] = outcome.status.value
            rec["reason"] = outcome.reason
            self.registry._records[cid] = rec
            return rec

        def wait(self, core_run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
            rec = self.registry.get(core_run_id)
            if rec is None:
                raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
            status = str(rec.get("status") or "")
            if status not in _TERMINAL:
                from plachem_fast_gateway.core_engine import AdapterOutcome
                outcome = self.adapter.wait(core_run_id, timeout_seconds=timeout_seconds)
                rec["status"] = outcome.status.value
                rec["reason"] = outcome.reason
                self.registry._records[core_run_id] = rec
            return rec

        def cancel(self, core_run_id: str) -> dict[str, Any]:
            rec = self.registry.get(core_run_id) or {}
            rec["status"] = CoreRunStatus.CANCELLED.value
            self.registry._records[core_run_id] = rec
            return rec

        def request_user_cancel(self, core_run_id: str) -> None:
            pass

        def is_cancel_requested(self, core_run_id: str) -> bool:
            return False

    class _FakeRegistry:
        def __init__(self) -> None:
            self._records: dict[str, dict[str, Any]] = {}

        def _new(self, core_run_id: str, agent_id: str) -> dict[str, Any]:
            return {
                "core_run_id": core_run_id,
                "agent_id": agent_id,
                "status": CoreRunStatus.QUEUED.value,
                "reason": "",
                "goal_contract": dict(_DEFAULT_GOAL),
                "verified_progress": {"completed_conditions": []},
                "policy_state": {"goal": {}},
            }

        def get(self, core_run_id: str) -> dict[str, Any] | None:
            rec = self._records.get(core_run_id)
            return dict(rec) if rec is not None else None

        def recent(self, limit: int = 50) -> list[dict[str, Any]]:
            return [dict(r) for r in list(self._records.values())[-limit:]]

        def update_goal_state(self, core_run_id: str, *, goal_state: dict[str, Any],
                              event_code: str = "", event_details: Mapping[str, Any] | None = None) -> dict[str, Any]:
            rec = self._records[core_run_id]
            goal = dict((rec.get("policy_state") or {}).get("goal") or {})
            goal.update(goal_state)
            rec["policy_state"] = {"goal": goal}
            return dict(rec)

    engine = _FakeEngine()
    harness = svc.PersistentExecutionHarness(engine)
    if layer is not None:
        harness.set_recovery_layer(layer)
    return harness


_TERMINAL = {
    CoreRunStatus.PASS.value, CoreRunStatus.FAIL.value, CoreRunStatus.BLOCKED.value,
    CoreRunStatus.TIMEOUT.value, CoreRunStatus.CANCELLED.value,
}


class TestRecoveryWiring:
    def test_pass_run_never_triggers_recovery(self):
        from plachem_fast_gateway.core_engine import AdapterOutcome
        layer = _FakeLayer()
        harness = _make_harness(None, [AdapterOutcome(CoreRunStatus.PASS, "")], layer=layer)
        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-pass-00000000000000000000000000000001")
        assert rec["status"] == CoreRunStatus.PASS.value
        assert layer.calls == []

    def test_fail_run_triggers_recovery_once(self):
        from plachem_fast_gateway.core_engine import AdapterOutcome
        layer = _FakeLayer()
        harness = _make_harness(None, [AdapterOutcome(CoreRunStatus.FAIL, "WORKER_FAILED")], layer=layer)
        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-fail-00000000000000000000000000000001")
        assert rec["status"] == CoreRunStatus.FAIL.value
        assert rec["reason"] == "WORKER_FAILED"
        assert layer.calls == ["direct-fail-00000000000000000000000000000001"]

    def test_recovery_child_never_re_recovers(self):
        """A record that IS a recovery child (goal_state carries
        recovery_source_run_id) must not trigger another recovery, even
        though its run id ends with -rec-."""
        from plachem_fast_gateway.core_engine import AdapterOutcome
        layer = _FakeLayer()
        harness = _make_harness(None, [], layer=layer)
        cid = "direct-parent-000000000000000000000000000001-rec-aabbccdd"
        rec = {
            "core_run_id": cid,
            "status": CoreRunStatus.FAIL.value,
            "reason": "WORKER_FAILED",
            "policy_state": {"goal": {"recovery_source_run_id": "direct-parent-000000000000000000000000000001"}},
        }
        harness._handle_recovery(rec)
        assert layer.calls == []  # child FAIL is the chain end

    def test_nonchild_run_with_rec_in_name_still_recovers(self):
        """A primary run whose id merely contains '-rec-' but has no
        recovery_source_run_id in goal_state is NOT a recovery child and
        is still eligible for recovery dispatch."""
        from plachem_fast_gateway.core_engine import AdapterOutcome
        layer = _FakeLayer()
        harness = _make_harness(None, [], layer=layer)
        cid = "direct-weird-rec-name-0000000000000000000001"
        rec = {
            "core_run_id": cid,
            "status": CoreRunStatus.FAIL.value,
            "reason": "WORKER_FAILED",
            "policy_state": {"goal": {}},
        }
        harness._handle_recovery(rec)
        assert layer.calls == [cid]  # goal_state is the authoritative marker

    def test_no_layer_means_no_recovery(self):
        from plachem_fast_gateway.core_engine import AdapterOutcome
        harness = _make_harness(None, [AdapterOutcome(CoreRunStatus.FAIL, "WORKER_FAILED")])
        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-fail-00000000000000000000000000000002")
        assert rec["status"] == CoreRunStatus.FAIL.value
        # No layer installed: _handle_recovery is a no-op, no exception.

    def test_layer_exception_does_not_break_terminal_record(self):
        from plachem_fast_gateway.core_engine import AdapterOutcome

        class _BoomLayer:
            def recover(self, core_run_id: str, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("boom")

        harness = _make_harness(None, [AdapterOutcome(CoreRunStatus.FAIL, "WORKER_FAILED")], layer=_BoomLayer())
        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-fail-00000000000000000000000000000003")
        assert rec["status"] == CoreRunStatus.FAIL.value  # terminal state preserved

    def test_blocked_run_never_triggers_recovery(self):
        from plachem_fast_gateway.core_engine import AdapterOutcome
        layer = _FakeLayer()
        harness = _make_harness(None, [AdapterOutcome(CoreRunStatus.BLOCKED, "AUTH_AUTH_REQUIRED")], layer=layer)
        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-block-00000000000000000000000000000001")
        assert rec["status"] == CoreRunStatus.BLOCKED.value
        assert layer.calls == []

    def test_policy_blocked_fail_not_recovered_by_layer(self):
        """A FAIL with a POLICY_BLOCKED reason is classified non-eligible by
        the real RecoveryLayer, so recover() returns REJECTED without
        dispatching a child."""
        from plachem_fast_gateway.core_engine import AdapterOutcome

        layer = _FakeLayer(outcome="REJECTED", final_state="POLICY_BLOCKED")
        harness = _make_harness(None, [AdapterOutcome(CoreRunStatus.FAIL, "POLICY_ABORT_FAILED")], layer=layer)
        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-policy-00000000000000000000000000000001")
        assert rec["status"] == CoreRunStatus.FAIL.value
        assert rec["reason"] == "POLICY_ABORT_FAILED"
        # The hook was invoked (FAIL), the layer returned REJECTED, and the
        # original record is unchanged.
        assert layer.calls == ["direct-policy-00000000000000000000000000000001"]


class TestRecoverableFailEndToEnd:
    """Recoverable FAIL -> real RecoveryLayer -> child PASS -> PASS_RECOVERED,
    using the real RecoveryLayer and a scripted dispatcher (no OpenClaw)."""

    def test_recoverable_fail_child_pass_gives_pass_recovered(self):
        from plachem_fast_gateway.core_engine import AdapterOutcome
        from plachem_fast_gateway.recovery_layer import RecoveryLayer

        from plachem_fast_gateway.core_engine import AdapterOutcome

        class _ScriptedDispatcher:
            def __init__(self, harness: Any) -> None:
                self._harness = harness

            def dispatch(self, *, agent_id: str, message: str, timeout_seconds: float,
                         core_run_id: str | None = None, idempotency_key: str | None = None,
                         goal_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
                # Child runs through the harness so its terminal state is
                # observed the same way as primary runs.
                return self._harness.core_engine.dispatch(
                    agent_id=agent_id, message=message, timeout_seconds=timeout_seconds,
                    core_run_id=core_run_id, idempotency_key=idempotency_key,
                    goal_contract=goal_contract,
                )

        class _OutcomeEngine(_FakeEngineForE2E):
            pass

        outcomes = [
            AdapterOutcome(CoreRunStatus.FAIL, "WORKER_FAILED"),   # primary
            AdapterOutcome(CoreRunStatus.PASS, ""),                 # recovery child
        ]
        harness = _make_harness_e2e(outcomes)
        layer = RecoveryLayer(harness.core_engine, harness.core_engine.registry, dispatcher=_ScriptedDispatcher(harness))
        harness.set_recovery_layer(layer)

        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-e2e-00000000000000000000000000000001")
        assert rec["status"] == CoreRunStatus.FAIL.value

        # Find the child created by the recovery layer
        children = [r for r in harness.core_engine.registry.recent(50)
                    if (r.get("policy_state") or {}).get("goal", {}).get("recovery_source_run_id")
                    == "direct-e2e-00000000000000000000000000000001"]
        assert len(children) == 1
        child = children[0]
        # Child reached terminal PASS via the scripted outcome
        assert child["status"] == CoreRunStatus.PASS.value
        verdict = layer._final_state_of(child)
        assert verdict == "PASS_RECOVERED"

        # Max 1 execution attempt on the child
        goal = child["policy_state"]["goal"]
        assert goal["recovery_execution_attempt"] == 1

    def test_recovery_failure_no_second_attempt(self):
        """Child FAIL (HARD_FAIL) -> no further recovery dispatches."""
        from plachem_fast_gateway.core_engine import AdapterOutcome

        class _ScriptedDispatcher:
            def __init__(self, harness: Any, count: list[int]) -> None:
                self._harness = harness
                self._count = count

            def dispatch(self, *, agent_id: str, message: str, timeout_seconds: float,
                         core_run_id: str | None = None, idempotency_key: str | None = None,
                         goal_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
                self._count.append(1)
                return self._harness.core_engine.dispatch(
                    agent_id=agent_id, message=message, timeout_seconds=timeout_seconds,
                    core_run_id=core_run_id, idempotency_key=idempotency_key,
                    goal_contract=goal_contract,
                )

        outcomes = [
            AdapterOutcome(CoreRunStatus.FAIL, "WORKER_FAILED"),  # primary
            AdapterOutcome(CoreRunStatus.FAIL, "WORKER_FAILED"),  # child fails
        ]
        harness = _make_harness_e2e(outcomes)
        count: list[int] = []
        layer = RecoveryLayer(harness.core_engine, harness.core_engine.registry, dispatcher=_ScriptedDispatcher(harness, count))
        harness.set_recovery_layer(layer)

        rec = harness.dispatch(agent_id="erpmanager", message="m", timeout_seconds=1,
                               core_run_id="direct-e2e-00000000000000000000000000000002")
        assert rec["status"] == CoreRunStatus.FAIL.value

        children = [r for r in harness.core_engine.registry.recent(50)
                    if (r.get("policy_state") or {}).get("goal", {}).get("recovery_source_run_id")
                    == "direct-e2e-00000000000000000000000000000002"]
        assert len(children) == 1
        assert children[0]["status"] == CoreRunStatus.FAIL.value
        # Only one recovery dispatch happened; the child's FAIL did not
        # trigger a second recovery (child chain-end rule).
        assert len(count) == 1


class _FakeEngineForE2E:
    def __init__(self, outcomes: list) -> None:
        self.adapter = _FakeAdapter(outcomes)
        self.registry = _FakeRegistryE2E()
        self.auth_broker = None
        self.grant_authorizer = None
        self._guard = None

    def set_terminal_transition_guard(self, guard) -> None:
        self._guard = guard

    def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        cid = kwargs.get("core_run_id") or f"direct-e2e-{len(self.registry._records)}"
        rec = self.registry._new(cid, kwargs.get("agent_id", "erpmanager"))
        rec["idempotency_key"] = kwargs.get("idempotency_key") or f"{cid}"
        rec["goal_contract"] = dict(kwargs.get("goal_contract") or rec.get("goal_contract") or {})
        self.registry._records[cid] = rec
        # Synchronous scripted terminal resolution (mirrors adapter.wait).
        outcome = self.adapter.wait(cid, timeout_seconds=1.0)
        rec["status"] = outcome.status.value
        rec["reason"] = outcome.reason
        self.registry._records[cid] = rec
        return rec

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        rec = self.registry.get(core_run_id)
        if rec is None:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        if str(rec.get("status") or "") not in _TERMINAL:
            from plachem_fast_gateway.core_engine import AdapterOutcome
            outcome = self.adapter.wait(core_run_id, timeout_seconds=timeout_seconds)
            rec["status"] = outcome.status.value
            rec["reason"] = outcome.reason
        return rec

    def cancel(self, core_run_id: str) -> dict[str, Any]:
        rec = self.registry.get(core_run_id) or {}
        rec["status"] = CoreRunStatus.CANCELLED.value
        self.registry._records[core_run_id] = rec
        return rec


class _FakeRegistryE2E:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def _new(self, core_run_id: str, agent_id: str) -> dict[str, Any]:
        return {
            "core_run_id": core_run_id,
            "agent_id": agent_id,
            "status": CoreRunStatus.QUEUED.value,
            "reason": "",
            "idempotency_key": f"{core_run_id}",
            "goal_contract": {
                "primary_objective": "test",
                "allowed_scope": ["A"],
                "forbidden_scope": ["B"],
                "expected_result": "R",
                "completion_conditions": ["C1"],
            },
            "verified_progress": {"completed_conditions": []},
            "policy_state": {"goal": {}},
        }

    def get(self, core_run_id: str) -> dict[str, Any] | None:
        rec = self._records.get(core_run_id)
        return dict(rec) if rec is not None else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in list(self._records.values())[-limit:]]

    def update_goal_state(self, core_run_id: str, *, goal_state: dict[str, Any],
                          event_code: str = "", event_details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rec = self._records[core_run_id]
        goal = dict((rec.get("policy_state") or {}).get("goal") or {})
        goal.update(goal_state)
        rec["policy_state"] = {"goal": goal}
        return dict(rec)


def _make_harness_e2e(outcomes: list):
    import fast_gateway_service as svc
    engine = _FakeEngineForE2E(outcomes)
    return svc.PersistentExecutionHarness(engine)
