from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.openclaw_adapter import (
    CoreRunStatus,
    IdempotencyConflict,
    MemoryRunBindingStore,
    OpenClawAdapter,
    RunBinding,
    SessionBindingError,
    SQLiteRunBindingStore,
    ValidationDecision,
)
from plachem_fast_gateway.ubuntu_transport import create_ubuntu_worker_transport


class Secret:
    def resolve(self):
        return "test-token"


class Socket:
    def __init__(self, handlers):
        self.handlers = handlers
        self.responses = []
        self.calls = []

    def open(self):
        self.responses.append(json.dumps({
            "type": "event", "event": "connect.challenge", "payload": {"nonce": "n", "ts": 1},
        }))
        return self

    def send(self, raw):
        frame = json.loads(raw)
        self.calls.append(frame)
        if frame["method"] == "connect":
            payload = {
                "type": "hello-ok",
                "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
                "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
            }
        else:
            handler = self.handlers[frame["method"]]
            payload = handler(frame["params"]) if callable(handler) else handler
        self.responses.append(json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": payload}))

    def recv(self, timeout=None):
        return self.responses.pop(0)

    def close(self):
        pass


def accepted(params):
    return {
        "status": "accepted", "runId": "run-new",
        "sessionKey": params.get("sessionKey", "agent:qwentest:shared"),
    }


def accept_result(payload):
    result = payload.get("history", {}).get("messages", [])[-1]["content"]
    return ValidationDecision(CoreRunStatus.PASS, result=result)


class OpenClawWiringHardeningTests(unittest.TestCase):
    def make_adapter(self, history):
        socket = Socket({
            "agent": accepted,
            "agent.wait": {"status": "ok"},
            "chat.history": history,
            "sessions.abort": {"status": "aborted", "abortedRunId": "run-new"},
        })
        adapter = OpenClawAdapter(
            Secret(), MemoryRunBindingStore(), result_validator=accept_result,
            socket_factory=lambda *_: socket.open(),
        )
        return adapter, socket

    @staticmethod
    def payload(**changes):
        value = {"message": "work", "agentId": "qwentest", "idempotencyKey": "idem", "timeout": 1}
        value.update(changes)
        return value

    def test_retry_with_different_explicit_session_fails_closed(self):
        adapter, socket = self.make_adapter({"messages": []})
        adapter.submit("core", self.payload(sessionKey="agent:qwentest:shared"))
        with self.assertRaises(SessionBindingError):
            adapter.submit("core", self.payload(sessionKey="agent:qwentest:other"))
        self.assertEqual(1, sum(call["method"] == "agent" for call in socket.calls))

    def test_sqlite_restart_uniqueness_and_exact_cancel_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bindings.sqlite3"
            first_socket = Socket({
                "agent": accepted,
                "sessions.abort": {"status": "aborted", "abortedRunId": "run-new"},
            })
            first = OpenClawAdapter(Secret(), SQLiteRunBindingStore(path), socket_factory=lambda *_: first_socket.open())
            binding = first.submit("core", self.payload(sessionKey="agent:qwentest:shared"))
            restarted_store = SQLiteRunBindingStore(path)
            self.assertEqual(binding, restarted_store.get("core"))
            with self.assertRaises(IdempotencyConflict):
                restarted_store.put(RunBinding(
                    "other-core", "other-run", "qwentest", "agent:qwentest:other",
                    None, binding.idempotency_key, CoreRunStatus.RUNNING,
                ))
            first.cancel("core")
            abort = next(call for call in first_socket.calls if call["method"] == "sessions.abort")
            self.assertEqual({"key": binding.session_key, "runId": binding.openclaw_run_id, "agentId": binding.agent_id}, abort["params"])

    def test_clean_bootstrap_factory_is_importable_and_fixed(self):
        with tempfile.TemporaryDirectory() as temp:
            transport = create_ubuntu_worker_transport(Path(temp) / "bindings.sqlite3")
            self.addCleanup(transport.close)
            self.assertIsInstance(transport, OpenClawAdapter)


if __name__ == "__main__":
    unittest.main()
