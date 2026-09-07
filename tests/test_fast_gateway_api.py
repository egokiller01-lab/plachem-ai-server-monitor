from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

import app
from fast_gateway_api import DispatchRequest, _present


class FastGatewayCommandCenterTests(unittest.TestCase):
    def test_command_center_projection_preserves_cancelled_and_hides_binding(self):
        projected = _present(
            {
                "core_run_id": "core-1",
                "agent_id": "qwentest",
                "status": "CANCELLED",
                "reason": "OPENCLAW_ABORTED",
                "openclaw_binding": {"session_key": "agent:qwentest:private"},
            }
        )
        self.assertEqual("CANCELLED", projected["status"])
        self.assertNotIn("openclaw_binding", projected)

    def test_command_center_routes_are_mounted(self):
        paths = {route.path for route in app.app.routes}
        self.assertIn("/api/fast-gateway/runs", paths)
        self.assertIn("/api/fast-gateway/runs/{core_run_id}/cancel", paths)
        self.assertIn("/api/fast-gateway/runs/{core_run_id}/fresh-context", paths)
        self.assertIn("/fast-gateway", paths)

    def test_command_center_projection_exposes_policy_without_private_state(self):
        projected = _present(
            {
                "core_run_id": "core-policy",
                "agent_id": "renamed-worker",
                "status": "CANCELLED",
                "runtime_class": "LOCAL",
                "model_profile": "local/x",
                "policy_profile": "LOCAL_STANDARD",
                "runtime_seconds": 300.1,
                "retry_count": 1,
                "tool_call_count": None,
                "tool_call_metric": "UNSUPPORTED",
                "policy_status": "CANCELLED",
                "policy_events": [{"code": "RUNTIME_LIMIT"}],
                "cancel_reason": "LOCAL_LLM_RUNTIME_LIMIT",
                "context_policy": "FRESH_ON_LOOP",
                "fallback_policy": "MANUAL",
                "policy_state": {"last_action_signature": "private"},
            }
        )
        self.assertEqual("LOCAL", projected["runtime_class"])
        self.assertEqual("LOCAL_LLM_RUNTIME_LIMIT", projected["cancel_reason"])
        self.assertEqual("UNSUPPORTED", projected["tool_call_metric"])
        self.assertNotIn("policy_state", projected)

    def test_command_center_projection_preserves_escalation_after_cancel(self):
        projected = _present(
            {
                "core_run_id": "drift-child",
                "agent_id": "erpmanager",
                "status": "CANCELLED",
                "cancel_reason": "LOCAL_LLM_GOAL_DRIFT",
                "escalation_required": True,
                "escalation_reason": "LOCAL_LLM_GOAL_DRIFT",
            }
        )
        self.assertTrue(projected["escalation_required"])
        self.assertEqual("LOCAL_LLM_GOAL_DRIFT", projected["escalation_reason"])

    def test_dispatch_api_rejects_caller_model_and_provider_override(self):
        base = {"agent_id": "qwentest", "message": "safe", "timeout_seconds": 10}
        with self.assertRaises(ValidationError):
            DispatchRequest(**base, model="local/x")
        with self.assertRaises(ValidationError):
            DispatchRequest(**base, provider="vllm")
        for forbidden in ("session_key", "session_id", "context_policy"):
            with self.assertRaises(ValidationError):
                DispatchRequest(**base, **{forbidden: "caller-value"})

    def test_dispatch_api_accepts_structured_goal_contract_only(self):
        request = DispatchRequest(
            agent_id="secretary", message="safe", timeout_seconds=10,
            goal_contract={
                "primary_objective": "Return validated result",
                "allowed_scope": ["SAFE_RESPONSE"],
                "forbidden_scope": ["PRODUCTION_FILES"],
                "expected_result": "Validated response",
                "completion_conditions": ["RESPONSE_VALIDATED"],
            },
        )
        self.assertEqual("SAFE_RESPONSE", request.goal_contract.allowed_scope[0])
        with self.assertRaises(ValidationError):
            DispatchRequest(
                agent_id="secretary", message="safe", timeout_seconds=10,
                goal_contract={**request.goal_contract.model_dump(), "goal_id": "caller-owned"},
            )

    def test_command_center_page_has_required_policy_columns(self):
        page = (Path(__file__).resolve().parents[1] / "static" / "fast-gateway.html").read_text(encoding="utf-8")
        for label in ("Runtime Class", "Model/Profile", "Runtime", "Retry", "Policy", "Final Status", "Cancel Reason"):
            self.assertIn(label, page)
        for label in ("Goal Status", "Context Reset", "Escalation Required", "Escalation Reason"):
            self.assertIn(label, page)


if __name__ == "__main__":
    unittest.main()
