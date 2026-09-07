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
    def test_recovery_preserves_explicit_status_and_promotes_nested_artifacts(self):
        payload = {"history": {"messages": [{
            "role": "assistant",
            "content": 'Result envelope: ' + json.dumps({
                "status": "completed",
                "summary": "systemd failed services: 0",
                "evidence": [{"type": "test", "detail": "report", "artifacts": [{"path": "/tmp/report.txt"}]}],
                "scope": {"compliant": True, "violations": []},
            }),
        }]}}
        recovered = recover_production_result_format(payload, {"core_run_id": "test"})
        self.assertIsNotNone(recovered)
        result = recovered["result"]
        self.assertEqual("completed", result["status"])
        self.assertEqual("systemd failed services: 0", result["summary"])
        self.assertEqual([{"path": "/tmp/report.txt"}], result["artifacts"])

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

    def test_current_run_tool_call_is_preserved_for_evidence_validation(self):
        value = valid_result()
        value["evidence"] = [
            {"type":"tool_execution", "detail":"Tool execution was completed"},
            {"type":"file_read", "detail":"Files were read by directory inspection"},
        ]
        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status":"ok"},
            "chat.history": {"messages": [
                {"role":"user", "content":"Read-only list the directory."},
                {"role":"assistant", "content":[
                    {"type":"toolCall", "name":"exec_command", "arguments":{"cmd":"ls -la"}},
                    {"type":"text", "text":json.dumps(value)},
                ]},
            ]},
        })
        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)
        self.assertEqual(CoreRunStatus.PASS, outcome.status)

    def test_rejected_result_keeps_raw_and_candidate_separate_from_success(self):
        invalid = {**valid_result(), "evidence":[{"type":"tool_execution", "detail":"Tool execution was completed"}]}
        raw = json.dumps(invalid)
        adapter, _ = self.adapter({
            "agent": accepted,
            "agent.wait": {"status":"ok"},
            "chat.history": {"messages":[{"role":"assistant", "content":raw}]},
        })
        outcome = adapter.wait("core-recovery-test", timeout_seconds=1)
        self.assertEqual(CoreRunStatus.FAIL, outcome.status)
        self.assertIsNone(outcome.result)
        self.assertEqual(invalid, outcome.rejected_result)
        self.assertEqual(raw, outcome.raw_response)

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



PRESERVED_TERMINAL_TEXT = '{"status":"completed","summary":"서버 상태를 점검했다. CPU 32코어 기준 load 1.12(경미), RAM 62GiB 중 사용 20GiB(32%), Swap 5.5GiB 사용, 디스크 1.8T 중 31% 사용(1.2T 여유). GPU 2장(RTX 3090) 모두 llama.cpp Qwen3.8 27B 모델이 로드되어 정상 동작 중(GPU0 메모리 23GB, GPU1 메모리 22.5GB, GPU1 utilization 47%). OpenClaw Gateway(port 18789) 응답 정상, 로컬 LLM health endpoint(127.0.0.1:8001) status ok, uvicorn app(port 8088) 응답 정상, Docker 실행 중, systemd 실패 서비스 0건. worktree /home/plachem-sever/codex-workspaces/cha-secretary/plachem-ai-server-monitor 에서 전체 테스트 스위트 실행 결과 433 passed, 5 warnings, 24 subtests passed (71.10s).","evidence":[{"type":"test","detail":"pytest -q 결과: 433 passed, 5 warnings, 24 subtests passed in 71.10s"},{"type":"test","detail":"curl http://127.0.0.1:8001/health -> {\\"status\\":\\"ok\\"}"},{"type":"test","detail":"curl http://127.0.0.1:18789/ -> OpenClaw Gateway HTML 응답 정상"},{"type":"test","detail":"curl http://127.0.0.1:8088/ -> HTML 응답 정상"},{"type":"test","detail":"nvidia-smi 확인: RTX 3090 x2, llama-server(pid 2721614) CUDA0 23034MiB + CUDA1 22292MiB 정상 로드, GPU1 47% utilization"},{"type":"test","detail":"uptime/loadavg: up 4 days 4:17, load 1.12/1.18/1.04, 32 core"},{"type":"test","detail":"free -h: RAM 62Gi total / 20Gi used / 41Gi available, Swap 8.0Gi / 5.5Gi used"},{"type":"test","detail":"df -h: /dev/nvme0n1p2 1.8T total / 532G used / 1.2T available (31%)"},{"type":"test","detail":"systemctl list-units --state=failed: 0건"},{"type":"test","detail":"ps aux: openclaw gateway (pid 122455), llama-server (pid 2721614), uvicorn (pid 159484) 모두 실행 중","artifacts":[{"path":"/tmp/server-report/df.txt"},{"path":"/tmp/server-report/failed_services.txt"},{"path":"/tmp/server-report/free.txt"},{"path":"/tmp/server-report/git_log.txt"},{"path":"/tmp/server-report/git_status.txt"},{"path":"/tmp/server-report/nvidia_smi.txt"},{"path":"/tmp/server-report/tests_summary.txt"},{"path":"/tmp/server-report/uptime.txt"}],"scope":{"compliant":true,"violations":[]}}'

class ActualResponseRegressionTests(unittest.TestCase):
    def recover(self, text):
        return recover_production_result_format(
            {"history": {"messages": [{"role": "assistant", "content": text}]}}, {})

    def test_preserved_response_keeps_eight_artifacts(self):
        with self.assertRaises(json.JSONDecodeError):
            json.loads(PRESERVED_TERMINAL_TEXT)
        result = self.recover(PRESERVED_TERMINAL_TEXT)["result"]
        self.assertEqual("completed", result["status"])
        self.assertEqual(8, len(result["artifacts"]))
        self.assertEqual(10, len(result["evidence"]))
        self.assertEqual({"compliant": True, "violations": []}, result["scope"])
        self.assertFalse(result["summary"].startswith("{"))

    def test_truncated_string_and_value_are_not_fabricated(self):
        for text in ['{"status":"completed","summary":"cut',
                     '{"status":"completed","scope":',
                     '{"status":"completed",']:
            with self.subTest(text=text):
                self.assertIsNone(self.recover(text))

    def test_uppercase_direct_status_beats_summary(self):
        result = recover_production_result_format({"result": {
            **valid_result(), "status": " COMPLETED ", "summary": "failed services: 0"}}, {})
        self.assertEqual("completed", result["result"]["status"])

    def test_invalid_artifacts_not_erased(self):
        result = self.recover(json.dumps({**valid_result(), "artifacts": "invalid"}))
        self.assertEqual("invalid", result["result"]["artifacts"])
        self.assertEqual(CoreRunStatus.FAIL, production_result_validator()(result).status)

    def test_null_evidence_does_not_crash(self):
        result = self.recover(json.dumps({**valid_result(), "evidence": None}))
        self.assertIsNone(result["result"]["evidence"])
        self.assertEqual(CoreRunStatus.FAIL, production_result_validator()(result).status)

    def test_nested_scope_violation_is_preserved(self):
        value = valid_result()
        del value["scope"]
        value["evidence"][0]["scope"] = {"compliant": False, "violations": ["outside task"]}
        result = self.recover(json.dumps(value))
        self.assertFalse(result["result"]["scope"]["compliant"])
        self.assertEqual(CoreRunStatus.FAIL, production_result_validator()(result).status)

    def test_fake_transport_preserved_response_no_extra_worker_call(self):
        import tempfile
        with tempfile.TemporaryDirectory() as missing_dir:
            adapter, socket = ResultFormatRecoveryTests().adapter({
                "agent": accepted, "agent.wait": {"status": "ok"},
                "chat.history": {"messages": [{"role": "assistant", "content": PRESERVED_TERMINAL_TEXT.replace("/tmp/server-report/", missing_dir + "/")}]},
            })
            outcome = adapter.wait("core-recovery-test", timeout_seconds=1)
            self.assertEqual(CoreRunStatus.FAIL, outcome.status)
            self.assertEqual("ARTIFACT_VALIDATION_FAILED:ARTIFACT_NOT_FOUND", outcome.format_recovery_rejection)
            self.assertEqual(1, socket.methods.count("agent"))
            self.assertEqual(1, outcome.format_recovery_attempts)

if __name__ == "__main__":
    unittest.main()
