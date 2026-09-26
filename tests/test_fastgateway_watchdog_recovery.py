import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from direct_gateway_authorization import CompositeGrantAuthorizer, DirectIngressGrantAuthorizer
from plachem_fast_gateway.auth_broker import SQLiteAuthBroker, execution_auth_scope
from plachem_fast_gateway.core_engine import AgentRegistry, CoreEngine, ModelRegistry, RunRegistry
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from plachem_fast_gateway.recovery_layer import RecoveryLayer
from plachem_fast_gateway.runtime_policy import normalize_goal_contract

GOAL = {
    "primary_objective": "Ship the feature",
    "allowed_scope": ["CODE_CHANGES_IN_REPO"],
    "forbidden_scope": ["PRODUCTION_DEPLOY"],
    "expected_result": "VERIFIED_RESULT",
    "completion_conditions": ["CODE_WRITTEN", "TESTS_PASS"],
}

class SecretRef:
    def get(self, key_id):
        return b"test-pepper-bytes-32-bytes-long!!"

class Adapter:
    def __init__(self):
        self.submit_calls = []
        self.cancel_calls = []
    def submit(self, core_run_id, payload):
        self.submit_calls.append((core_run_id, dict(payload)))
        return RunBinding(core_run_id, f"oc-{core_run_id}", payload["agentId"],
                          f"agent:{payload['agentId']}:main", "session-1",
                          payload["idempotencyKey"], CoreRunStatus.RUNNING)
    def wait(self, core_run_id, *, timeout_seconds):
        return AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")
    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        return RunBinding(core_run_id, f"oc-{core_run_id}", "qwentest",
                          "agent:qwentest:main", "session-1", "idem-x",
                          CoreRunStatus.CANCELLED)
    def close(self):
        pass

def make_env():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    (root / "agents.json").write_text(json.dumps({"qwentest": {
        "enabled": True, "capabilities": ["safe-smoke"], "runtime_model_id": "local/test",
        "allowed_model_ids": ["local/test"], "allowed_policy_profiles": ["LOCAL_STANDARD"],
    }}))
    (root / "models.json").write_text(json.dumps({"models": {"local/test": {
        "runtime_class": "LOCAL", "policy_profile": "LOCAL_STANDARD",
        "max_runtime": 300, "max_retries": 1, "max_tool_calls": 20,
        "loop_guard": {"consecutive_threshold": 3},
        "context_policy": "FRESH_ON_LOOP", "fallback_policy": "MANUAL",
    }}}))
    registry = RunRegistry(root / "runs.jsonl")
    adapter = Adapter()
    direct = DirectIngressGrantAuthorizer()
    broker = SQLiteAuthBroker(root / "auth.sqlite3", SecretRef(), key_id="test-key")
    engine = CoreEngine(
        registry, AgentRegistry.load(root / "agents.json"), ModelRegistry.load(root / "models.json"),
        adapter, auth_broker=broker, auth_required=True,
        grant_authorizer=CompositeGrantAuthorizer(direct),
    )
    return temp, registry, adapter, direct, engine, RecoveryLayer(engine, registry)


def test_watchdog_cancelled_direct_run_reauths_child():
    temp, registry, adapter, direct, engine, layer = make_env()
    try:
        source_id = "direct-watchdog-source-abc123"
        idem = "direct:watchdog-source"
        contract = normalize_goal_contract(GOAL)
        scope = execution_auth_scope(
            agent_id="qwentest", message="Do the goal.",
            core_run_id=source_id, idempotency_key=idem,
            goal_contract=contract.as_dict(),
        )
        direct.register(source_id, scope)
        source = engine.dispatch(
            agent_id="qwentest", message="Do the goal.", timeout_seconds=10,
            core_run_id=source_id, idempotency_key=idem,
            goal_contract=GOAL, watchdog_managed=True,
            approved_paths=["/tmp/watchdog-result.json"],
        )
        assert source["status"] == "RUNNING"
        assert engine.cancel(source_id)["status"] == "CANCELLED"

        result = layer.recover_watchdog_cancelled(
            source_id,
            prior_evidence={"choice": "SALVAGE", "failed_method": "repeat-loop"},
        )
        assert result["status"] == "DISPATCHED"
        assert result["auth_rechecked"] is True
        assert result["goal_contract_preserved"] is True
        assert result["approved_paths_preserved"] is True

        child = registry.get(result["recovery_run_id"])
        assert child["status"] == "RUNNING"
        assert child["watchdog_managed"] is True
        assert child["approved_paths"] == ["/tmp/watchdog-result.json"]
        assert child["policy_state"]["goal"]["watchdog_recovery_source_run_id"] == source_id

        # A watchdog recovery child is generation 1 and must never create
        # a grandchild even if it is later cancelled by another watchdog pass.
        child_id = result["recovery_run_id"]
        assert engine.cancel(child_id)["status"] == "CANCELLED"
        chained = layer.recover_watchdog_cancelled(child_id)
        assert chained["status"] == "REJECTED"
        assert chained["reason"] == "MAX_WATCHDOG_RECOVERY_GENERATION"

        second = layer.recover_watchdog_cancelled(source_id)
        assert second["status"] == "REJECTED"
        assert second["reason"] == "MAX_WATCHDOG_RECOVERY_ATTEMPTS"
    finally:
        temp.cleanup()


def test_watchdog_cancelled_war_run_requires_requeue():
    temp, registry, adapter, direct, engine, layer = make_env()
    try:
        source_id = "war-delivery-123"
        registry.create(
            core_run_id=source_id,
            agent_id="qwentest",
            idempotency_key="delivery-123",
            request_hash="hash",
            policy={
                "runtime_class": "LOCAL",
                "task_class": "STANDARD",
                "model_profile": "test",
                "policy_profile": "LOCAL_STANDARD",
            },
            goal_contract=normalize_goal_contract(GOAL),
            watchdog_managed=True,
        )
        registry.transition(source_id, CoreRunStatus.RUNNING)
        registry.transition(source_id, CoreRunStatus.CANCELLED, reason="CORE_CANCELLED")

        result = layer.recover_watchdog_cancelled(
            source_id, prior_evidence={"choice": "SALVAGE"},
        )
        assert result["status"] == "REQUEUE_REQUIRED"
        latest = registry.get(source_id)
        assert latest["escalation_required"] is True
        assert latest["escalation_reason"] == "WATCHDOG_REQUEUE_REQUIRED"
        assert latest["escalation_package"]["required_action"] == "REQUEUE_THROUGH_CONTROLLED_LANE"
    finally:
        temp.cleanup()


def test_terminal_fail_watchdog_managed_never_enters_legacy_recovery():
    from plachem_fast_gateway.recovery_layer import evaluate_recovery

    verdict = evaluate_recovery({
        "status": "FAIL",
        "reason": "EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED",
        "watchdog_managed": True,
    })
    assert verdict.eligible is False
    assert verdict.reason == "WATCHDOG_MANAGED_REQUIRES_CONTROLLED_REQUEUE"
