from __future__ import annotations

import json
import unittest

from plachem_fast_gateway.core_engine import production_result_validator
from plachem_fast_gateway.openclaw_adapter import (
    CoreRunStatus,
    MemoryRunBindingStore,
    OpenClawAdapter,
    recover_production_result_format,
)


class FakeSecretRef:
    def resolve(self):
        return "test-only-secret"


class FakeSocket:
    def __init__(self, handlers):
        self.handlers = handlers
        self.history_calls = 0
        self.responses = []
        self.methods = []

    def send(self, raw):
        frame = json.loads(raw)
        method = frame["method"]
        self.methods.append(method)
        if method == "connect":
            payload = {
                "type": "hello-ok",
                "auth": {"role": "operator", "scopes": ["operator.write"]},
                "features": {"methods": ["agent", "agent.wait", "chat.history"]},
            }
        else:
            if method == "chat.history":
                self.history_calls += 1
                handler = {"messages": []} if self.history_calls == 1 else self.handlers.get(method, {"messages": []})
            else:
                handler = self.handlers.get(method, None)
            payload = handler(frame["params"]) if callable(handler) else handler
        self.responses.append(json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": payload}))

    def recv(self, timeout=None):
        return self.responses.pop(0)

    def close(self):
        return None


def accepted(params):
    return {
        "status": "accepted",
        "runId": "local-result-recovery-run",
        "sessionKey": params.get("sessionKey", f"agent:{params['agentId']}:result-recovery-test"),
    }


def valid_result():
    return {
        "status": "completed",
        "summary": "bounded task completed",
        "evidence": [{
            "type": "runtime_observation",
            "detail": "Structured assistant result returned successfully",
        }],
        "artifacts": [],
        "scope": {"compliant": True, "violations": []},
    }


class ResultFormatRecoveryTests(unittest.TestCase):
    def adapter(self, handlers, recovery=recover_production_result_format, *, eligible=True):
        socket = FakeSocket(handlers)
        adapter = OpenClawAdapter(
            FakeSecretRef(),
            MemoryRunBindingStore(),
            result_validator=production_result_validator(),
            result_recovery=recovery,
            result_recovery_agent_ids={"qwentest"} if eligible else set(),
            socket_factory=lambda *_: socket,
        )
        adapter.submit("core-recovery-test", {
            "message": "bounded TEST_ONLY result recovery probe",
            "agentId": "qwentest",
            "idempotencyKey": "core-recovery-test",
            "timeout": 30,
        })
        return adapter, socket

    def test_non_local_agent_is_not_recovered(self):
        calls = []

        def recovery(*args):
            calls.append(args)
            return None

        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": "plain text completed"},
        }, recovery=recovery, eligible=False)

        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.FAIL, outcome.status)
        self.assertEqual("MISSING_POST_SUBMIT_RESULT", outcome.reason)
        self.assertEqual(0, outcome.format_recovery_attempts)
        self.assertEqual([], calls)

    def test_normal_result_bypasses_recovery(self):
        calls = []

        def recovery(*args):
            calls.append(args)
            return None

        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": valid_result()},
            "chat.history": {"messages": [{"role": "assistant", "content": valid_result()}]},
        }, recovery=recovery)

        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.PASS, outcome.status)
        self.assertEqual(0, outcome.format_recovery_attempts)
        self.assertEqual([], calls)

    def test_plain_text_recovers_once_to_valid_contract_without_rpc(self):
        adapter, socket = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok"},
            "chat.history": {
                "messages": [
                    {"role": "user", "content": "Return a bounded result."},
                    {"role": "assistant", "content": "Bounded LOCAL task completed."},
                ]
            },
        })

        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.PASS, outcome.status)
        self.assertEqual("RESULT_FORMAT_ERROR", outcome.format_error)
        self.assertEqual(1, outcome.format_recovery_attempts)
        self.assertEqual({"status", "summary", "evidence", "artifacts", "scope"}, set(outcome.result))
        self.assertEqual(1, socket.methods.count("agent"))
        self.assertEqual(1, socket.methods.count("agent.wait"))
        self.assertEqual(2, socket.methods.count("chat.history"))

    def test_missing_fields_are_recovered(self):
        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": {"status": "completed", "summary": "done"}},
            "chat.history": {"messages": [{"role": "assistant", "content": json.dumps({"status": "completed", "summary": "done"})}]},
        })

        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.PASS, outcome.status)
        self.assertEqual(1, outcome.format_recovery_attempts)
        self.assertEqual([], outcome.result["artifacts"])
        self.assertTrue(outcome.result["scope"]["compliant"])

    def test_markdown_fenced_json_is_normalized(self):
        fenced = "```json\n" + json.dumps(valid_result()) + "\n```"
        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok"},
            "chat.history": {"messages": [{"role": "assistant", "content": fenced}]},
        })

        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.PASS, outcome.status)
        self.assertEqual(1, outcome.format_recovery_attempts)

    def test_false_evidence_is_rejected_by_existing_validator(self):
        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": {
                "status": "completed",
                "summary": "claimed work",
                "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
                "artifacts": [],
            }},
            "chat.history": {"messages": [{"role": "assistant", "content": json.dumps({
                "status": "completed", "summary": "claimed work",
                "evidence": [{"type": "tool_execution", "detail": "Tool execution was completed"}],
                "artifacts": [],
            })}]},
        })

        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.FAIL, outcome.status)
        self.assertEqual("RESULT_SCHEMA_VALIDATION_FAILED:MISSING_REQUIRED_FIELD", outcome.reason)
        self.assertEqual("EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED", outcome.format_recovery_rejection)
        self.assertIsNone(outcome.result)

    def test_failed_recovery_is_not_retried(self):
        calls = []

        def unavailable(payload, observed):
            calls.append((payload, observed))
            return None

        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": "unstructured response"},
            "chat.history": {"messages": [{"role": "assistant", "content": "unstructured response"}]},
        }, recovery=unavailable)

        first = adapter.wait("core-recovery-test", timeout_seconds=1)
        second = adapter.wait("core-recovery-test", timeout_seconds=1)

        self.assertEqual(CoreRunStatus.FAIL, first.status)
        self.assertEqual("MISSING_RESULT", first.reason)
        self.assertEqual("RECOVERY_UNAVAILABLE", first.format_recovery_rejection)
        self.assertEqual(CoreRunStatus.FAIL, second.status)
        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
