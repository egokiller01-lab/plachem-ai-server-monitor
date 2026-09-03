from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway import AgentRegistry, CoreEngine, ModelRegistry, RunRegistry
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from plachem_fast_gateway.runtime_policy import normalize_goal_contract


GOAL = {
    "primary_objective": "Return a safe validated response",
    "allowed_scope": ["SAFE_RESPONSE"],
    "forbidden_scope": ["PRODUCTION_FILES"],
    "expected_result": "Validated result",
    "completion_conditions": ["RESPONSE_VALIDATED"],
}


class Adapter:
    def __init__(self):
        self.submits = []
        self.cancels = []
        self.outcome = AdapterOutcome(CoreRunStatus.PASS, result={
            "status": "completed", "summary": "done", "evidence": [{"type": "response"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        })

    def submit(self, core_run_id, payload):
        self.submits.append((core_run_id, dict(payload)))
        key = payload.get("sessionKey", f"agent:{payload['agentId']}:main")
        return RunBinding(core_run_id, f"oc-{core_run_id}", payload["agentId"], key,
                          f"sid-{core_run_id}", payload["idempotencyKey"], CoreRunStatus.RUNNING)

    def wait(self, core_run_id, *, timeout_seconds):
        return self.outcome

    def cancel(self, core_run_id):
        self.cancels.append(core_run_id)
        run_id, payload = next(item for item in self.submits if item[0] == core_run_id)
        return RunBinding(run_id, f"oc-{run_id}", payload["agentId"],
                          payload.get("sessionKey", f"agent:{payload['agentId']}:main"),
                          f"sid-{run_id}", payload["idempotencyKey"], CoreRunStatus.CANCELLED)


def model(runtime="LOCAL", context="FRESH_ON_LOOP"):
    return {
        "runtime_class": runtime, "policy_profile": f"{runtime}_STANDARD", "max_runtime": 300,
        "max_retries": 1, "max_tool_calls": 20, "loop_guard": {"consecutive_threshold": 3},
        "context_policy": context, "fallback_policy": "MANUAL", "max_context_resets": 1,
    }


def agent(model_id, runtime="LOCAL"):
    return {"enabled": True, "capabilities": [], "runtime_model_id": model_id,
            "allowed_model_ids": [model_id], "allowed_policy_profiles": [f"{runtime}_STANDARD"]}


class RuntimePolicyV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "agents.json").write_text(json.dumps({
            "secretary": agent("local/x"), "researcher": agent("local/x"),
        }), encoding="utf-8")
        (root / "models.json").write_text(json.dumps({"models": {"local/x": model()}}), encoding="utf-8")
        self.adapter = Adapter()
        self.registry = RunRegistry(root / "runs.jsonl")
        self.engine = CoreEngine(self.registry, AgentRegistry.load(root / "agents.json"),
                                 ModelRegistry.load(root / "models.json"), self.adapter)

    def dispatch(self, run="run-1", agent_id="secretary"):
        return self.engine.dispatch(agent_id=agent_id, message="safe", timeout_seconds=5,
                                    core_run_id=run, idempotency_key=run, goal_contract=GOAL)

    @staticmethod
    def checkpoint(goal_id, **updates):
        value = {"goal_id": goal_id, "current_step": "VALIDATE", "completed_conditions": [],
                 "remaining_conditions": ["RESPONSE_VALIDATED"], "blocking_reason": "NONE",
                 "artifact_delta": [], "scope_delta": [], "evidence_delta": []}
        value.update(updates)
        return value

    def test_normal_local_completion(self):
        record = self.dispatch()
        self.assertEqual("NORMAL", record["goal_status"])
        self.assertEqual("PASS", self.engine.wait("run-1", timeout_seconds=1)["status"])

    def test_goal_contract_normalized_and_immutable(self):
        record = self.dispatch()
        before = record["goal_contract"]
        self.engine.observe_progress_checkpoint("run-1", self.checkpoint(before["goal_id"],
            completed_conditions=["RESPONSE_VALIDATED"], remaining_conditions=[]))
        self.assertEqual(before, self.registry.get("run-1")["goal_contract"])

    def test_objective_replacement_first_guarded_then_cancelled(self):
        goal_id = self.dispatch()["goal_contract"]["goal_id"]
        first = self.engine.observe_progress_checkpoint("run-1", self.checkpoint("goal-other"))
        self.assertEqual(("GUARDED", "OBJECTIVE_REPLACEMENT"),
                         (first["goal_status"], first["policy_events"][-1]["code"]))
        final = self.engine.observe_progress_checkpoint("run-1", self.checkpoint("goal-other"))
        self.assertEqual(("CANCELLED", "LOCAL_LLM_GOAL_DRIFT"),
                         (final["status"], final["cancel_reason"]))
        self.assertNotEqual(goal_id, "goal-other")

    def test_scope_expansion_detection(self):
        goal_id = self.dispatch()["goal_contract"]["goal_id"]
        record = self.engine.observe_progress_checkpoint("run-1", self.checkpoint(
            goal_id, scope_delta=["PRODUCTION_FILES"]))
        self.assertEqual("SCOPE_EXPANSION", record["policy_events"][-1]["code"])

    def test_no_progress_detection(self):
        goal_id = self.dispatch()["goal_contract"]["goal_id"]
        cp = self.checkpoint(goal_id)
        self.engine.observe_progress_checkpoint("run-1", cp)
        record = self.engine.observe_progress_checkpoint("run-1", cp)
        self.assertEqual("NO_PROGRESS", record["policy_events"][-1]["code"])

    def test_fresh_context_uses_unique_session_and_minimal_payload(self):
        record = self.dispatch()
        self.engine.observe_progress_checkpoint("run-1", self.checkpoint("goal-other"))
        child = self.engine.fresh_context("run-1")
        self.assertEqual(1, child["context_reset_count"])
        source_key = self.adapter.submits[0][1].get("sessionKey", "agent:secretary:main")
        child_payload = self.adapter.submits[1][1]
        self.assertNotEqual(source_key, child_payload["sessionKey"])
        self.assertTrue(child_payload["sessionKey"].startswith("agent:secretary:fast-gateway-"))
        self.assertNotIn("transcript", child_payload["message"].lower())
        self.assertNotIn("reasoning", child_payload["message"].lower())
        self.assertNotIn("secret", child_payload["message"].lower())

    def test_fresh_context_does_not_fabricate_success_instructions(self):
        record = self.dispatch()
        self.engine.observe_progress_checkpoint("run-1", self.checkpoint("goal-other"))
        self.engine.fresh_context("run-1")
        message = self.adapter.submits[1][1]["message"]
        compact = message.replace(" ", "").lower()
        self.assertIn(record["goal_contract"]["goal_id"], message)
        self.assertIn('"completion_conditions":["RESPONSE_VALIDATED"]', message)
        self.assertIn('"completed_conditions":[]', message)
        self.assertIn('"needed_evidence":{"completion_conditions":["RESPONSE_VALIDATED"]', message)
        self.assertIn("exactly one raw JSON object", message)
        self.assertIn("Do not use Markdown code fences", message)
        self.assertIn("do not add prose before or after", message)
        self.assertIn("status, summary, evidence, artifacts, and scope are mandatory", message)
        self.assertIn("required non-empty array", message)
        self.assertIn("string fields type and detail", message)
        self.assertIn("runtime_observation", message)
        self.assertIn("Never claim a tool call, file read or write", message)
        self.assertNotIn('"status":"completed"', compact)
        self.assertNotIn('"current_step":"COMPLETE"'.lower(), message.lower())
        self.assertNotIn('"completed_conditions":["RESPONSE_VALIDATED"]', message)
        self.assertNotIn('[{"type":"response"}]', compact)
        self.assertNotIn('"evidence_delta":["RESPONSE"]'.lower(), message.lower())

    def test_context_reset_limit_creates_safe_escalation_package(self):
        self.dispatch()
        self.engine.observe_progress_checkpoint("run-1", self.checkpoint("goal-other"))
        child = self.engine.fresh_context("run-1")
        child_id = child["core_run_id"]
        child_goal_id = child["goal_contract"]["goal_id"]
        self.engine.observe_progress_checkpoint(child_id, self.checkpoint(
            child_goal_id, completed_conditions=["RESPONSE_VALIDATED"], remaining_conditions=[]))
        self.engine.observe_progress_checkpoint(child_id, self.checkpoint("goal-other"))
        blocked = self.engine.fresh_context(child_id)
        self.assertEqual("BLOCKED", blocked["status"])
        self.assertTrue(blocked["escalation_required"])
        package = blocked["escalation_package"]
        self.assertEqual("CONTEXT_RESET_FAILED", blocked["escalation_reason"])
        def keys(value):
            if isinstance(value, dict):
                return {str(key).lower() for key in value} | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()
        self.assertTrue({"transcript", "reasoning", "search_log", "secret"}.isdisjoint(keys(package)))

    def test_secret_rejected_from_contract_and_registry(self):
        bad = {**GOAL, "primary_objective": "Use token=DO_NOT_STORE"}
        with self.assertRaisesRegex(ValueError, "INVALID_GOAL_CONTRACT"):
            self.engine.dispatch(agent_id="secretary", message="safe", timeout_seconds=5,
                                 core_run_id="secret", idempotency_key="secret", goal_contract=bad)
        self.assertNotIn("DO_NOT_STORE", self.registry.path.read_text(encoding="utf-8") if self.registry.path.exists() else "")

    def test_researcher_runtime_switches_by_registry_not_name(self):
        local = self.dispatch(run="local", agent_id="researcher")
        root = Path(self.temp.name)
        (root / "cloud-agents.json").write_text(json.dumps({"researcher": agent("cloud/x", "CLOUD")}), encoding="utf-8")
        (root / "cloud-models.json").write_text(json.dumps({"models": {"cloud/x": model("CLOUD", "REUSE")}}), encoding="utf-8")
        cloud_engine = CoreEngine(RunRegistry(root / "cloud.jsonl"), AgentRegistry.load(root / "cloud-agents.json"),
                                  ModelRegistry.load(root / "cloud-models.json"), Adapter())
        cloud = cloud_engine.dispatch(agent_id="researcher", message="safe", timeout_seconds=5,
                                      core_run_id="cloud", idempotency_key="cloud", goal_contract=GOAL)
        self.assertEqual(("LOCAL", "CLOUD"), (local["runtime_class"], cloud["runtime_class"]))

    def test_goal_id_is_deterministic_and_caller_cannot_supply_it(self):
        self.assertEqual(normalize_goal_contract(GOAL), normalize_goal_contract(GOAL))
        with self.assertRaisesRegex(ValueError, "FIELDS"):
            normalize_goal_contract({**GOAL, "goal_id": "caller-controlled"})

    def test_validated_adapter_result_checkpoint_is_automatically_guarded(self):
        self.dispatch()
        self.adapter.outcome = AdapterOutcome(CoreRunStatus.PASS, result={
            "status": "completed", "summary": "done", "evidence": [{"type": "response"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
            "progress_checkpoint": self.checkpoint("goal-replaced"),
        })
        guarded = self.engine.wait("run-1", timeout_seconds=1)
        self.assertEqual(("RUNNING", "GUARDED", True),
                         (guarded["status"], guarded["goal_status"], guarded["goal_reinjection_ready"]))
        self.assertEqual("OBJECTIVE_REPLACEMENT", guarded["policy_events"][-1]["code"])

    def test_fresh_child_inherits_drift_and_repeated_result_drift_cancels(self):
        self.dispatch()
        first = self.engine.observe_progress_checkpoint("run-1", self.checkpoint("goal-replaced"))
        self.assertFalse(first["escalation_required"])
        child = self.engine.fresh_context("run-1")
        self.assertFalse(child["escalation_required"])
        child_id = child["core_run_id"]
        self.adapter.outcome = AdapterOutcome(CoreRunStatus.PASS, result={
            "status": "completed", "summary": "done", "evidence": [{"type": "response"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
            "progress_checkpoint": self.checkpoint("goal-replaced"),
        })
        final = self.engine.wait(child_id, timeout_seconds=1)
        self.assertEqual(("CANCELLED", "LOCAL_LLM_GOAL_DRIFT"),
                         (final["status"], final["cancel_reason"]))
        self.assertTrue(final["escalation_required"])
        self.assertEqual("LOCAL_LLM_GOAL_DRIFT", final["escalation_reason"])
        self.assertEqual("ESCALATION_REQUIRED", final["policy_events"][-1]["code"])
        persisted = self.registry.get(child_id)
        self.assertTrue(persisted["escalation_required"])
        self.assertEqual("CANCELLED", persisted["status"])


if __name__ == "__main__":
    unittest.main()
