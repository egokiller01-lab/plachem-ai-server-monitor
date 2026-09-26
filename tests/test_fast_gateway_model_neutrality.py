"""Regression coverage for model metadata crossing the execution boundary.

Legacy records below are local test fixtures, not evidence of a public record
injection endpoint. The reserved-name case needs no record modification.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from fast_gateway_service import create_core_engine
from plachem_fast_gateway.core_engine import (
    AgentRegistration, AgentRegistry, CoreEngine, RunRegistry,
)
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from plachem_fast_gateway.production_runtime import create_ubuntu_core_engine
from plachem_fast_gateway.runtime_policy import ModelRegistry, RuntimeClass, RuntimeModelProfile


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Adapter:
    def __init__(self):
        self.submitted = {}
        self.waits = []
        self.cancelled = []

    def submit(self, run_id, payload):
        self.submitted[run_id] = dict(payload)
        return RunBinding(
            run_id, "oc-" + run_id, payload["agentId"],
            payload.get("sessionKey", "agent:worker:main"), "session-" + run_id,
            payload["idempotencyKey"], CoreRunStatus.RUNNING,
        )

    def wait(self, run_id, *, timeout_seconds):
        self.waits.append((run_id, timeout_seconds))
        return AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        payload = self.submitted[run_id]
        return RunBinding(
            run_id, "oc-" + run_id, payload["agentId"],
            payload.get("sessionKey", "agent:worker:main"), "session-" + run_id,
            payload["idempotencyKey"], CoreRunStatus.CANCELLED,
        )


@pytest.fixture
def timers(monkeypatch):
    scheduled = []

    class Timer:
        def __init__(self, seconds, callback, args=()):
            self.seconds, self.callback, self.args = seconds, callback, args
            self.cancelled = False

        def start(self):
            scheduled.append(self)

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr("plachem_fast_gateway.core_engine.threading.Timer", Timer)
    return scheduled


def legacy_profile(model_id, *, local=False):
    return RuntimeModelProfile(
        model_id, RuntimeClass.LOCAL if local else RuntimeClass.CLOUD,
        "RETIRED", 5.0 if local else 1800.0, 0 if local else 10,
        None, {"consecutive_threshold": 2 if local else 8},
        "MANUAL" if local else "REUSE", "NONE", 0,
        execution_budget=4.0 if local else 1800.0,
        finalization_recovery_budget=1.0 if local else 0.0,
    )


@pytest.fixture
def running(tmp_path, timers):
    def build(case):
        clock, adapter = Clock(), Adapter()
        models = ModelRegistry({
            "legacy-local": legacy_profile("legacy-local", local=True),
            "legacy-cloud": legacy_profile("legacy-cloud"),
            # Collides with the record produced by an ordinary dispatch.
            "__neutral__": legacy_profile("__neutral__"),
        })
        agents = AgentRegistry({"worker": AgentRegistration(
            "worker", True, ("review",), "stale-model", (), (),
        )})
        registry = RunRegistry(tmp_path / "runs.jsonl", clock=clock)
        engine = CoreEngine(registry, agents, models, adapter, clock=clock)
        record = engine.dispatch(
            agent_id="worker", message="bounded work", timeout_seconds=1,
            core_run_id="run-1", idempotency_key="idem-1",
        )
        if case != "__neutral__":
            # Simulate the most recent snapshot from an older run store.
            record["model_profile"] = case
            with registry.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        return engine, adapter, clock

    return build


@pytest.mark.parametrize("case", ["legacy-local", "legacy-cloud", "__neutral__", "unknown", None])
def test_retry_limit_is_independent_of_record_model(running, timers, case):
    engine, adapter, _ = running(case)
    first = engine.observe_runtime_event("run-1", {"kind": "retry"})
    assert first["status"] == "RUNNING"
    second = engine.observe_runtime_event("run-1", {"kind": "retry"})
    assert second["status"] == "CANCELLED"
    assert second["cancel_reason"] == "RETRY_LIMIT"
    assert adapter.cancelled == ["run-1"]
    assert len(timers) == 1  # Events must not reset the original deadline.
    assert timers[0].seconds == 240.0
    assert adapter.submitted["run-1"]["timeout"] == 300.0
    assert set(adapter.submitted["run-1"]) == {
        "message", "agentId", "idempotencyKey", "timeout", "_trustedValidationContext"
    }


@pytest.mark.parametrize("case,elapsed,expected", [
    ("legacy-local", 6, "RUNNING"),
    ("legacy-cloud", 301, "CANCELLED"),
    ("__neutral__", 301, "CANCELLED"),
])
def test_poll_uses_original_server_deadline(running, timers, case, elapsed, expected):
    engine, adapter, clock = running(case)
    started = engine.status("run-1")["started_at"]
    clock.now += timedelta(seconds=elapsed)
    result = engine.wait("run-1", timeout_seconds=1)
    assert result["status"] == expected
    assert result["started_at"] == started
    assert len(timers) == 1
    if expected == "RUNNING":
        assert adapter.cancelled == []
    else:
        assert result["cancel_reason"] == "EXECUTION_RUNTIME_LIMIT"


@pytest.mark.parametrize("case", ["legacy-local", "legacy-cloud", "__neutral__"])
def test_deadline_callback_preserves_reserved_result_collection(running, case):
    engine, adapter, clock = running(case)
    clock.now += timedelta(seconds=240)
    engine._enforce_runtime_deadline("run-1")
    result = engine.status("run-1")
    exhausted = next(e for e in result["policy_events"] if e["code"] == "EXECUTION_BUDGET_EXHAUSTED")
    assert exhausted["details"] == {"execution_budget": 240.0, "finalization_recovery_budget": 60.0}
    assert adapter.waits == [("run-1", 10.0)]
    assert result["cancel_reason"] == "EXECUTION_RUNTIME_LIMIT"
    assert adapter.cancelled == ["run-1"]


@pytest.mark.parametrize("case", ["legacy-local", "legacy-cloud", "__neutral__"])
def test_trusted_no_progress_guard_uses_server_threshold(running, case):
    engine, adapter, _ = running(case)
    event = {"kind": "error", "error_code": "EAGAIN", "state_changed": False}
    for _ in range(2):
        assert engine.observe_runtime_event("run-1", event)["status"] == "RUNNING"
    result = engine.observe_runtime_event("run-1", event)
    assert result["status"] == "CANCELLED"
    assert result["cancel_reason"] == "LOOP_DETECTED"
    assert adapter.cancelled == ["run-1"]


@pytest.mark.parametrize("case", ["legacy-local", "legacy-cloud", "__neutral__"])
def test_existing_fresh_context_contract_is_not_model_selected(running, case):
    engine, adapter, _ = running(case)
    engine.registry.update_goal_state("run-1", reinjection_ready=True)
    child = engine.fresh_context("run-1")
    assert child["status"] == "RUNNING"
    assert child["parent_core_run_id"] == "run-1"
    assert child["context_reset_count"] == 1
    assert child["openclaw_binding"]["session_key"] != "agent:worker:main"
    assert adapter.submitted[child["core_run_id"]]["agentId"] == "worker"
    assert adapter.submitted[child["core_run_id"]]["timeout"] == 300.0
    assert adapter.cancelled == ["run-1"]


@pytest.mark.parametrize("factory", ["service", "ubuntu"])
@pytest.mark.parametrize("model_file", ["missing", "malformed", "invalid-registry", "reserved-name"])
def test_composition_does_not_depend_on_model_configuration(tmp_path, timers, monkeypatch, factory, model_file):
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"worker": {
        "enabled": True, "capabilities": ["review"],
        "runtime_model_id": "stale", "allowed_model_ids": [], "allowed_policy_profiles": [],
    }}), encoding="utf-8")
    models = tmp_path / "models.json"
    if model_file != "missing":
        content = {"malformed": "{broken", "invalid-registry": "{}", "reserved-name": json.dumps({
            "models": {"__neutral__": {
                "runtime_class": "CLOUD", "policy_profile": "RETIRED", "max_runtime": 1800,
                "max_retries": 10, "max_tool_calls": None, "loop_guard": {"consecutive_threshold": 8},
                "context_policy": "REUSE", "fallback_policy": "NONE", "max_context_resets": 0,
            }},
        })}[model_file]
        models.write_text(content, encoding="utf-8")
    adapter = Adapter()
    # Production composition now requires an actual task grant. Keep model
    # independence under that boundary instead of disabling authorization.
    monkeypatch.setenv("PLACHEM_AUTH_BROKER_DB", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("PLACHEM_AUTH_BROKER_KEY_ID", "model-test")
    monkeypatch.setenv("PLACHEM_AUTH_BROKER_PEPPER_MODEL_TEST", "isolated-model-test-key")
    if factory == "service":
        engine = create_core_engine(
            run_path=tmp_path / "runs.jsonl", bindings_path=tmp_path / "bindings.sqlite3",
            agents_path=agents, models_path=models, adapter=adapter,
        )
    else:
        monkeypatch.setattr("plachem_fast_gateway.production_runtime.create_ubuntu_worker_transport", lambda _: adapter)
        engine = create_ubuntu_core_engine(
            runs_path=tmp_path / "runs.jsonl", agents_path=agents, models_path=models,
            bindings_path=tmp_path / "bindings.sqlite3",
        )
    from plachem_fast_gateway.auth_broker import execution_auth_scope
    from plachem_fast_gateway.runtime_policy import normalize_goal_contract
    grant = engine.auth_broker.issue(execution_auth_scope(
        agent_id="worker", message="bounded work", core_run_id="run-1", idempotency_key="run-1",
        goal_contract=normalize_goal_contract(None).as_dict()), ttl_seconds=30, created_by="fixture")
    assert engine.dispatch(agent_id="worker", message="bounded work", timeout_seconds=1,
                           core_run_id="run-1", auth_token=grant.token)["status"] == "RUNNING"
    assert engine.observe_runtime_event("run-1", {"kind": "retry"})["status"] == "RUNNING"
    assert engine.observe_runtime_event("run-1", {"kind": "retry"})["status"] == "CANCELLED"


@pytest.mark.parametrize("metadata", [
    {},
    {"runtime_model_id": None, "allowed_model_ids": None, "allowed_policy_profiles": None},
    {"runtime_model_id": 42, "allowed_model_ids": "obsolete", "allowed_policy_profiles": {}},
])
def test_agent_admission_needs_identity_and_capabilities_only(tmp_path, timers, metadata):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"worker": {"enabled": True, "capabilities": ["review"], **metadata}}))
    registry = AgentRegistry.load(path)
    assert registry.candidate_agent_ids(["review"]) == ["worker"]
    adapter = Adapter()
    engine = CoreEngine(RunRegistry(tmp_path / "runs.jsonl"), registry, ModelRegistry({}), adapter)
    assert engine.dispatch(agent_id="worker", message="work", timeout_seconds=1,
                           core_run_id="run-1")["status"] == "RUNNING"


@pytest.mark.parametrize("field,value,reason", [
    ("enabled", "true", "INVALID_AGENT_ENABLED"),
    ("capabilities", "review", "INVALID_AGENT_CAPABILITIES"),
])
def test_agent_authority_fields_still_fail_closed(tmp_path, field, value, reason):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"worker": {"enabled": True, "capabilities": [], field: value}}))
    with pytest.raises(ValueError, match=reason):
        AgentRegistry.load(path)
