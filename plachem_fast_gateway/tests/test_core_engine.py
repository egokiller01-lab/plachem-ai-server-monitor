from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plachem_fast_gateway.core_engine import (
    AgentRegistry,
    CoreEngine,
    ModelRegistry,
    RunRegistry,
    production_result_validator,
)
from plachem_fast_gateway.openclaw_adapter import (
    AdapterOutcome,
    CoreRunStatus,
    RunBinding,
    TransportError,
)


TEST_OUTPUT_ROOT = Path("/home/plachem-sever/.openclaw/agents/ERPcoder/01_ACTIVE/war-room-p0-readonly-artifact-reuse/test-output")
TEST_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


class FakeAdapter:
    def __init__(self) -> None:
        self.submit_calls = []
        self.wait_calls = []
        self.cancel_calls = []
        self.submit_error = None
        self.wait_error = None
        self.wait_outcome = AdapterOutcome(
            CoreRunStatus.PASS,
            result={
                "status": "completed",
                "summary": "done",
                "evidence": [{"type": "response"}],
                "artifacts": [],
                "scope": {"compliant": True, "violations": []},
            },
        )

    def submit(self, core_run_id, payload):
        self.submit_calls.append((core_run_id, dict(payload)))
        if self.submit_error:
            raise self.submit_error
        return RunBinding(
            core_run_id,
            "oc-1",
            payload["agentId"],
            f"agent:{payload['agentId']}:main",
            "session-1",
            payload["idempotencyKey"],
            CoreRunStatus.RUNNING,
        )

    def wait(self, core_run_id, *, timeout_seconds):
        self.wait_calls.append((core_run_id, timeout_seconds))
        if self.wait_error:
            raise self.wait_error
        return self.wait_outcome

    def cancel(self, core_run_id):
        self.cancel_calls.append(core_run_id)
        return RunBinding(
            core_run_id,
            "oc-1",
            "qwentest",
            "agent:qwentest:main",
            "session-1",
            core_run_id,
            CoreRunStatus.CANCELLED,
        )


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class CoreEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        agents_path = root / "agents.json"
        agents_path.write_text(
            json.dumps(
                {
                    "qwentest": {
                        "enabled": True,
                        "capabilities": ["safe-smoke"],
                        "runtime_model_id": "local/test",
                        "allowed_model_ids": ["local/test"],
                        "allowed_policy_profiles": ["LOCAL_STANDARD"],
                    },
                    "disabled": {
                        "enabled": False,
                        "capabilities": [],
                        "runtime_model_id": "local/test",
                        "allowed_model_ids": ["local/test"],
                        "allowed_policy_profiles": ["LOCAL_STANDARD"],
                    },
                    "unknown-model": {
                        "enabled": True,
                        "capabilities": [],
                        "runtime_model_id": "missing/model",
                        "allowed_model_ids": ["missing/model"],
                        "allowed_policy_profiles": ["LOCAL_STANDARD"],
                    },
                    "cloudtest": {
                        "enabled": True,
                        "capabilities": ["safe-smoke"],
                        "runtime_model_id": "cloud/test",
                        "allowed_model_ids": ["cloud/test"],
                        "allowed_policy_profiles": ["CLOUD_STANDARD"],
                    },
                }
            ),
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
                "cloud/test": {
                    "runtime_class": "CLOUD", "policy_profile": "CLOUD_STANDARD",
                    "max_runtime": 1800, "max_retries": 2, "max_tool_calls": 100,
                    "loop_guard": {"consecutive_threshold": 5},
                    "context_policy": "REUSE", "fallback_policy": "ELIGIBLE",
                },
            }}),
            encoding="utf-8",
        )
        self.clock = FakeClock()
        self.registry = RunRegistry(root / "runs.jsonl", clock=self.clock)
        self.adapter = FakeAdapter()
        self.engine = CoreEngine(
            self.registry,
            AgentRegistry.load(agents_path),
            ModelRegistry.load(models_path),
            self.adapter,
            clock=self.clock,
        )

    def dispatch(self, **updates):
        values = {
            "core_run_id": "core-1",
            "agent_id": "qwentest",
            "message": "Return a safe response.",
            "timeout_seconds": 10,
            "idempotency_key": "idem-1",
        }
        values.update(updates)
        return self.engine.dispatch(**values)

    def test_dispatch_success_stores_full_binding_and_running(self):
        record = self.dispatch()
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual("oc-1", record["openclaw_binding"]["openclaw_run_id"])
        self.assertEqual("session-1", record["openclaw_binding"]["session_id"])
        sent = self.adapter.submit_calls[0][1]
        self.assertEqual(
            {"message", "agentId", "idempotencyKey", "timeout", "_trustedValidationContext"},
            set(sent),
        )
        self.assertNotIn("runtime_model_id", sent)
        self.assertEqual("LOCAL", record["runtime_class"])
        self.assertEqual("NEUTRAL", record["policy_profile"])

    def test_wait_success_transitions_to_pass(self):
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=2)
        self.assertEqual("PASS", record["status"])
        self.assertEqual("done", record["result"]["summary"])

    def test_result_validation_success_and_failure(self):
        validator = production_result_validator()
        good = {
            "status": "ok",
            "result": {
                "status": "completed",
                "summary": "safe response",
                "evidence": [{"type": "response"}],
                "artifacts": [],
                "scope": {"compliant": True, "violations": []},
            },
        }
        self.assertEqual(CoreRunStatus.PASS, validator(good).status)
        bad = {"status": "ok", "result": {"status": "completed", "summary": "unsafe"}}
        decision = validator(bad)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertIn("RESULT_SCHEMA_VALIDATION_FAILED", decision.reason)

    def test_false_tool_evidence_is_rejected_without_observed_tool_call(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_false_file_evidence_is_rejected_without_observed_file_operation(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "file_write", "detail": "A file was modified"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_text_only_runtime_observation_is_valid(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "runtime_observation",
                          "detail": "Structured assistant result returned successfully"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        self.assertEqual(CoreRunStatus.PASS, production_result_validator()(payload).status)

    def test_negative_deploy_evidence_is_not_misread_as_positive_claim(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "SIMPLE_UI_TEST_PASS",
            "evidence": [{"type": "instruction_receipt",
                          "detail": "forbidden 항목(merge/push/deploy) 중 아무것도 실행하지 않았다."}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        self.assertEqual(CoreRunStatus.PASS, production_result_validator()(payload).status)

    def test_negative_deploy_evidence_contradicts_observed_deploy(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "verification", "detail": "deploy 실행하지 않았다"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [
            {"role": "user", "content": "deploy"},
            {"role": "assistant", "content": [
                {"type": "toolCall", "name": "exec", "arguments": {"command": "wrangler deploy"}},
            ]},
        ]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_CONTRADICTION", decision.reason)

    def test_observed_tool_action_allows_matching_evidence(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [
            {"role": "user", "content": "run a tool"},
            {"role": "assistant", "content": [
                {"type": "toolCall", "name": "exec", "arguments": {"command": "date"}},
            ]},
        ]}}
        self.assertEqual(CoreRunStatus.PASS, production_result_validator()(payload).status)

    def test_read_only_existing_path_is_rejected_when_claimed_as_new_artifact(self):
        existing = Path(tempfile.mkstemp()[1])
        try:
            payload = {"status": "ok", "result": {
                "status": "completed", "summary": "inspected",
                "evidence": [{"type": "file_read", "detail": f"ls -l {existing}"}],
                "artifacts": [{"path": str(existing)}],
                "scope": {"compliant": True, "violations": []},
            }, "history": {"messages": [
                {"role": "user", "content": f"read-only inspect {existing}"},
                {"role": "assistant", "content": [
                    {"type": "toolCall", "name": "exec", "arguments": {"command": f"ls -l {existing}"}},
                ]},
            ]}}
            decision = production_result_validator()(payload)
            self.assertEqual(CoreRunStatus.FAIL, decision.status)
            self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason)
        finally:
            existing.unlink(missing_ok=True)

    def _read_only_artifact_payload(self, artifact, *, designation="__default__", tool=None):
        if designation == "__default__":
            designation = str(artifact)
        tool = tool if tool is not None else {"type": "toolCall", "name": "write", "arguments": {"path": str(artifact)}}
        payload = {
            "status": "ok",
            "result": {
                "status": "completed", "summary": "report",
                "evidence": [{"type": "file_write", "detail": "artifact created"}],
                "artifacts": [{"path": str(artifact)}],
                "scope": {"compliant": True, "violations": []},
            },
            "history": {"messages": [
                {"role": "user", "content": f"read-only inspect and create result {artifact}"},
                {"role": "assistant", "content": [tool]},
            ]},
        }
        if designation is not None:
            payload["approved_paths"] = [designation]
        return payload

    def test_read_only_designated_new_result_artifact_is_accepted(self):
        artifact = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        try:
            decision = production_result_validator()(self._read_only_artifact_payload(artifact))
            self.assertEqual(CoreRunStatus.PASS, decision.status)
        finally:
            artifact.unlink(missing_ok=True)

    def test_read_only_preexisting_artifact_reuse_without_creation_is_rejected(self):
        artifact = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        try:
            payload = self._read_only_artifact_payload(
                artifact,
                tool={"type": "toolCall", "name": "exec", "arguments": {"command": f"ls -l {artifact}"}},
            )
            decision = production_result_validator()(payload)
            self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason)
        finally:
            artifact.unlink(missing_ok=True)

    def test_read_only_source_config_database_and_service_writes_remain_rejected(self):
        artifact = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        try:
            for protected in ("/src/app.py", "/etc/service.conf", "/var/lib/app.sqlite", "/etc/systemd/service.service"):
                payload = self._read_only_artifact_payload(
                    artifact,
                    tool={"type": "toolCall", "name": "exec", "arguments": {"command": f"touch {protected}"}},
                )
                decision = production_result_validator()(payload)
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT", decision.reason, protected)
        finally:
            artifact.unlink(missing_ok=True)

    def test_read_only_undesignated_artifact_is_rejected(self):
        artifact = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        try:
            payload = self._read_only_artifact_payload(artifact, designation="/tmp/other-result.json")
            decision = production_result_validator()(payload)
            self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason)
        finally:
            artifact.unlink(missing_ok=True)

    def test_read_only_artifact_path_traversal_and_sibling_prefix_are_rejected(self):
        parent = Path(tempfile.mkdtemp())
        artifact = parent / "result.json"
        artifact.write_text("ok", encoding="utf-8")
        sibling = Path(str(parent) + "-sibling")
        sibling.write_text("ok", encoding="utf-8")
        try:
            for designation, artifact_path in (
                (str(parent / ".." / parent.name / "result.json"), artifact),
                (str(parent), sibling),
            ):
                payload = self._read_only_artifact_payload(artifact_path, designation=designation)
                decision = production_result_validator()(payload)
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason)
        finally:
            artifact.unlink(missing_ok=True)
            sibling.unlink(missing_ok=True)
            parent.rmdir()

    def test_read_only_missing_or_empty_artifact_designation_fails_closed(self):
        artifact = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        try:
            for designation in (None, ""):
                payload = self._read_only_artifact_payload(artifact, designation=designation)
                decision = production_result_validator()(payload)
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason)
        finally:
            artifact.unlink(missing_ok=True)

    def test_read_only_rejects_symlink_designation_and_artifact(self):
        target = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        alias = target.with_name(target.name + "-alias")
        try:
            alias.symlink_to(target)
            for artifact, approved in ((target, alias), (alias, target)):
                payload = self._read_only_artifact_payload(artifact, designation=str(approved))
                decision = production_result_validator()(payload)
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason)
        finally:
            alias.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    def test_read_only_protected_paths_are_rejected_even_when_designated(self):
        for protected in ("/etc/hosts", "/var/lib", "/etc/systemd"):
            payload = self._read_only_artifact_payload(
                Path(protected), designation=protected,
                tool={"type": "toolCall", "name": "exec", "arguments": {"command": f"touch {protected}"}},
            )
            decision = production_result_validator()(payload)
            self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE", decision.reason, protected)


    def _new_write_target_artifact(self, name="RESULT.md"):
        temp = tempfile.TemporaryDirectory(prefix="write-target-", dir=TEST_OUTPUT_ROOT)
        self.addCleanup(temp.cleanup)
        artifact = Path(temp.name) / name
        artifact.write_text("report", encoding="utf-8")
        return artifact

    def test_read_only_write_body_paths_are_data(self):
        artifact = self._new_write_target_artifact()
        body = (
            "Inspected /home/plachem-sever/codex-workspaces/cha-secretary/"
            "plachem-ai-server-monitor. References: /etc/hosts, /var/lib/app.sqlite, "
            "/etc/systemd/service.service and /src/app.py."
        )
        for field in ("arguments", "input"):
            for target_key in ("path", "file_path"):
                with self.subTest(field=field, target_key=target_key):
                    tool = {"type": "toolCall", "name": "write",
                            field: {target_key: str(artifact), "content": body}}
                    decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
                    self.assertEqual(CoreRunStatus.PASS, decision.status, decision.reason)

    def test_read_only_write_body_command_names_are_data(self):
        artifact = self._new_write_target_artifact()
        for body in ("pytest was not run", "unittest results", "npm test",
                     "node test.js", "Examples: touch file; tee file; cp a b; mv a b; a > b"):
            with self.subTest(body=body):
                tool = {"type": "tool_use", "toolName": "WRITE",
                        "input": {"path": str(artifact), "content": body}}
                decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
                self.assertEqual(CoreRunStatus.PASS, decision.status, decision.reason)

    def test_read_only_write_target_with_spaces_and_quotes_is_exact(self):
        artifact = self._new_write_target_artifact("RESULT with 'quotes' and spaces.md")
        tool = {"type": "toolCall", "name": "write",
                "arguments": {"path": str(artifact), "content": "report"}}
        decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
        self.assertEqual(CoreRunStatus.PASS, decision.status, decision.reason)

    def test_read_only_write_unapproved_target_is_rejected_despite_body(self):
        artifact = self._new_write_target_artifact()
        protected = ("/etc/hosts", "/var/lib/app.sqlite", "/etc/systemd/service.service",
                     "/src/app.py", str(artifact) + "-sibling", str(artifact).lower())
        for target in protected:
            with self.subTest(target=target):
                tool = {"type": "toolCall", "name": "write",
                        "arguments": {"path": target, "content": str(artifact)}}
                decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT", decision.reason)

    def test_read_only_write_missing_or_malformed_target_fails_closed(self):
        artifact = self._new_write_target_artifact()
        body = str(artifact)
        bad_args = [{"content": body}]
        for target in (None, "", " ", 42, True, [body], {"path": body},
                       "RESULT.md", "../RESULT.md", "/tmp/invalid\0name"):
            bad_args.append({"path": target, "content": body})
        bad_args.extend([body, [body], {"content": {"path": body}}])
        for arguments in bad_args:
            with self.subTest(arguments=arguments):
                tool = {"type": "toolCall", "name": "write", "arguments": arguments}
                decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT", decision.reason)

    def test_read_only_write_conflicting_targets_fail_closed(self):
        artifact = self._new_write_target_artifact()
        for aliases in (
            {"path": str(artifact), "file_path": "/etc/hosts"},
            {"path": "/etc/hosts", "file_path": str(artifact)},
            {"path": str(artifact), "file_path": None},
        ):
            with self.subTest(aliases=aliases):
                tool = {"type": "toolCall", "name": "write", "arguments": aliases}
                decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT", decision.reason)
        tool = {"type": "toolCall", "name": "write",
                "arguments": {"path": str(artifact)}, "input": {"path": "/etc/hosts"}}
        decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
        self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT", decision.reason)

    def test_read_only_write_target_symlink_and_traversal_are_rejected(self):
        artifact = self._new_write_target_artifact()
        alias = artifact.with_name("alias.md")
        alias.symlink_to(artifact)
        parent_alias = artifact.parent / "parent-alias"
        parent_alias.symlink_to(artifact.parent, target_is_directory=True)
        targets = (str(alias), str(parent_alias / artifact.name),
                   f"{artifact.parent}/../{artifact.parent.name}/{artifact.name}")
        for target in targets:
            with self.subTest(target=target):
                tool = {"type": "toolCall", "name": "write",
                        "arguments": {"path": target, "content": str(artifact)}}
                decision = production_result_validator()(self._read_only_artifact_payload(artifact, tool=tool))
                self.assertEqual("SCOPE_VALIDATION_FAILED:READ_ONLY_WRITE_ATTEMPT", decision.reason)

    def test_read_only_write_does_not_mask_following_forbidden_tool(self):
        artifact = self._new_write_target_artifact()
        for tool, reason in (
            ({"type": "toolCall", "name": "write", "arguments": {"path": "/etc/hosts"}},
             "READ_ONLY_WRITE_ATTEMPT"),
            ({"type": "toolCall", "name": "exec", "arguments": {"command": "touch /etc/hosts"}},
             "READ_ONLY_WRITE_ATTEMPT"),
            ({"type": "toolCall", "name": "exec", "arguments": {"command": "pytest --version"}},
             "READ_ONLY_SCOPE_EXPANSION"),
        ):
            with self.subTest(tool=tool):
                payload = self._read_only_artifact_payload(artifact)
                payload["history"]["messages"][-1]["content"].append(tool)
                decision = production_result_validator()(payload)
                self.assertEqual("SCOPE_VALIDATION_FAILED:" + reason, decision.reason)

    def test_stale_bounded_history_without_user_boundary_is_not_current_run_evidence(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [
            {"role": "assistant", "content": [
                {"type": "toolCall", "name": "exec", "arguments": {"command": "date"}},
            ]},
            {"role": "toolResult", "content": "stale"},
        ]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_unobserved_process_invocation_claim_is_rejected(self):
        payload = {"status": "ok", "result": {
            "status": "completed", "summary": "done",
            "evidence": [{"type": "runtime_observation",
                          "detail": "Parent process was invoked via a CLI and exited with code 0"}],
            "artifacts": [], "scope": {"compliant": True, "violations": []},
        }, "history": {"messages": [{"role": "user", "content": "text only"}]}}
        decision = production_result_validator()(payload)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", decision.reason)

    def test_cancel_calls_adapter_and_command_center_state_is_cancelled(self):
        self.dispatch()
        record = self.engine.cancel("core-1")
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)
        self.assertEqual("CANCELLED", self.engine.status("core-1")["status"])

    def test_local_wait_timeout_continues_within_absolute_runtime_budget(self):
        outcomes = iter([
            AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT"),
            self.adapter.wait_outcome,
        ])

        def wait(core_run_id, *, timeout_seconds):
            self.adapter.wait_calls.append((core_run_id, timeout_seconds))
            self.clock.advance(timeout_seconds if len(self.adapter.wait_calls) == 1 else 60)
            return next(outcomes)

        self.adapter.wait = wait
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=180)
        self.assertEqual("PASS", record["status"])
        self.assertEqual([180.0, 60.0], [call[1] for call in self.adapter.wait_calls])
        self.assertEqual([], self.adapter.cancel_calls)

    def test_watchdog_managed_local_wait_yields_running_after_one_poll(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")

        def wait(core_run_id, *, timeout_seconds):
            self.adapter.wait_calls.append((core_run_id, timeout_seconds))
            self.clock.advance(timeout_seconds)
            return self.adapter.wait_outcome

        self.adapter.wait = wait
        self.dispatch(watchdog_managed=True)
        record = self.engine.wait("core-1", timeout_seconds=1)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([1.0], [call[1] for call in self.adapter.wait_calls])
        self.assertEqual([], self.adapter.cancel_calls)

    def test_local_wait_timeout_at_policy_deadline_aborts(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")

        def wait(core_run_id, *, timeout_seconds):
            self.adapter.wait_calls.append((core_run_id, timeout_seconds))
            self.clock.advance(timeout_seconds)
            return self.adapter.wait_outcome

        self.adapter.wait = wait
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=180)
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual("EXECUTION_RUNTIME_LIMIT", record["cancel_reason"])
        self.assertEqual([180.0, 60.0, 10.0], [call[1] for call in self.adapter.wait_calls])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)

    def test_cloud_bounded_wait_timeout_remains_running(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.TIMEOUT, "OPENCLAW_TIMEOUT")
        self.dispatch(agent_id="cloudtest")
        record = self.engine.wait("core-1", timeout_seconds=180)
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual([], self.adapter.cancel_calls)

    def test_runtime_policy_limit_aborts_and_records_distinct_cancel_reason(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.RUNNING, "RUN_STILL_ACTIVE")
        self.dispatch()
        self.clock.advance(301)
        record = self.engine.wait("core-1", timeout_seconds=10)
        self.assertEqual("CANCELLED", record["status"])
        self.assertEqual("EXECUTION_RUNTIME_LIMIT", record["cancel_reason"])
        self.assertEqual(["core-1"], self.adapter.cancel_calls)
        self.assertEqual("RUNTIME_LIMIT", record["policy_events"][-1]["code"])

    def test_missing_runtime_model_uses_running_neutral_contract(self):
        record = self.dispatch(agent_id="unknown-model")
        self.assertEqual("RUNNING", record["status"])
        self.assertEqual("LOCAL", record["runtime_class"])
        self.assertEqual("NEUTRAL", record["policy_profile"])
        self.assertEqual([], record["policy_events"])
        self.assertEqual(1, len(self.adapter.submit_calls))

    def test_invalid_agent_is_rejected_before_transport(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT"):
            self.dispatch(agent_id="missing")
        with self.assertRaisesRegex(ValueError, "UNKNOWN_AGENT"):
            self.dispatch(agent_id="disabled")
        self.assertEqual([], self.adapter.submit_calls)

    def test_transport_failure_maps_to_fail(self):
        self.adapter.submit_error = TransportError("closed")
        record = self.dispatch()
        self.assertEqual("FAIL", record["status"])
        self.assertEqual("TRANSPORT_FAILURE", record["reason"])

    def test_idempotent_retry_does_not_dispatch_twice(self):
        first = self.dispatch()
        second = self.dispatch()
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.adapter.submit_calls))

    def test_duplicate_dispatch_prevention_across_core_runs(self):
        self.dispatch()
        with self.assertRaisesRegex(ValueError, "DUPLICATE_DISPATCH"):
            self.dispatch(core_run_id="core-2")
        self.assertEqual(1, len(self.adapter.submit_calls))

    def test_same_core_run_changed_payload_is_idempotency_conflict(self):
        self.dispatch()
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            self.dispatch(message="changed")

    def test_run_registry_rejects_invalid_terminal_transition(self):
        self.dispatch()
        self.engine.wait("core-1", timeout_seconds=1)
        with self.assertRaisesRegex(ValueError, "RUN_ALREADY_TERMINAL"):
            self.engine.cancel("core-1")

    def test_error_plus_aborted_outcome_becomes_cancelled(self):
        self.adapter.wait_outcome = AdapterOutcome(CoreRunStatus.CANCELLED, "OPENCLAW_ABORTED")
        self.dispatch()
        record = self.engine.wait("core-1", timeout_seconds=1)
        self.assertEqual("CANCELLED", record["status"])

    # ------------------------------------------------------------------
    # Server-supplied approved_paths designation (War Room task contract)
    # ------------------------------------------------------------------

    def test_dispatch_persists_server_approved_paths_on_record(self):
        record = self.dispatch(approved_paths=["/home/plachem-sever/.openclaw/agents/ERPmanager/01_ACTIVE/task/05_QA_EVIDENCE"])
        self.assertEqual("RUNNING", record["status"])
        persisted = self.registry.get("core-1")
        self.assertEqual(
            ["/home/plachem-sever/.openclaw/agents/ERPmanager/01_ACTIVE/task/05_QA_EVIDENCE"],
            persisted["approved_paths"],
        )

    def test_dispatch_without_approved_paths_defaults_to_empty(self):
        record = self.dispatch()
        persisted = self.registry.get("core-1")
        self.assertEqual([], persisted["approved_paths"])

    def test_dispatch_rejects_relative_or_malformed_approved_paths(self):
        for bad in (
            ["relative/path"],
            ["relative/path", "/abs"],
            ["/abs", ""],
            ["/abs", "not-a-string"],
            ["/abs", 123],
        ):
            with self.assertRaisesRegex(ValueError, "INVALID_APPROVED_PATHS"):
                self.dispatch(core_run_id="core-bad", idempotency_key="idem-bad", approved_paths=bad)
        self.assertIsNone(self.registry.get("core-bad"))

    def test_dispatch_approved_paths_non_list_is_rejected(self):
        for bad in ("/abs/path", "not-a-list", None.__class__ and 42):
            with self.assertRaisesRegex(ValueError, "INVALID_APPROVED_PATHS"):
                self.dispatch(core_run_id="core-bad", idempotency_key="idem-bad", approved_paths=bad)
        self.assertIsNone(self.registry.get("core-bad"))

    def test_duplicate_dispatch_replay_keeps_approved_paths(self):
        paths = ["/home/plachem-sever/.openclaw/agents/ERPmanager/01_ACTIVE/task/05_QA_EVIDENCE"]
        first = self.dispatch(approved_paths=paths)
        second = self.dispatch(approved_paths=paths)
        self.assertTrue(second["replayed"] if "replayed" in second else True)
        self.assertEqual(1, len(self.adapter.submit_calls))
        persisted = self.registry.get("core-1")
        self.assertEqual(paths, persisted["approved_paths"])

    def test_read_only_designated_path_from_record_flows_into_validator_envelope(self):
        """The run record's server-side approved_paths reach the validation
        envelope; worker-claimed approved_paths never do."""
        artifact = Path(tempfile.mkstemp(dir=TEST_OUTPUT_ROOT)[1])
        try:
            designation = str(artifact)
            record = self.dispatch(
                message=f"read-only inspect and create result {designation}",
                approved_paths=[designation],
            )
            self.assertEqual("RUNNING", record["status"])
            # The persisted record carries the server designation and the
            # adapter receives it only as internal trusted validation context.
            self.assertEqual([designation], self.registry.get("core-1")["approved_paths"])
            submitted = self.adapter.submit_calls[-1][1]
            self.assertEqual(
                {"approved_paths": [designation]},
                submitted["_trustedValidationContext"],
            )
        finally:
            artifact.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
