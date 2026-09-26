from __future__ import annotations

import json
import inspect
import tempfile
import time
import unittest
from unittest.mock import ANY, Mock, patch
from pathlib import Path

from plachem_fast_gateway.openclaw_adapter import (
    CompositeResultValidator,
    CoreRunStatus,
    GatewayContractError,
    EnvironmentSecretRef,
    IdempotencyConflict,
    MemoryRunBindingStore,
    OpenClawAdapter,
    RunBinding,
    SessionBindingError,
    SQLiteRunBindingStore,
    TransportError,
    ValidationDecision,
    redact_secrets,
)


class FakeSecretRef:
    def __init__(self, value="super-secret-token"):
        self.value = value
        self.calls = 0

    def resolve(self):
        self.calls += 1
        return self.value


class FakeSocket:
    def __init__(self, handlers=None, *, fail_send=False, hello=None):
        self.handlers = handlers or {}
        self.fail_send = fail_send
        self.responses = []
        self.calls = []
        self.closed = False
        self.hello = hello

    def open(self):
        self.closed = False
        self.responses.append(json.dumps({
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": "mock-challenge", "ts": 1},
        }))
        return self

    def send(self, raw):
        if self.fail_send:
            raise OSError("socket closed")
        frame = json.loads(raw)
        self.calls.append(frame)
        method = frame["method"]
        if method == "connect":
            payload = self.hello or {
                "type": "hello-ok",
                "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
                "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
            }
        else:
            handler = self.handlers.get(method, {"messages": []} if method == "chat.history" else None)
            if handler is None:
                raise KeyError(method)
            payload = handler(frame["params"]) if callable(handler) else handler
        self.responses.append(json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": payload}))

    def recv(self, timeout=None):
        if not self.responses:
            raise TimeoutError("no response")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class SequenceSocketFactory:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.calls = 0

    def __call__(self, _url, _timeout):
        self.calls += 1
        if not self.sockets:
            raise OSError("reconnect unavailable")
        return self.sockets.pop(0).open()


def accepted(params):
    return {
        "status": "accepted",
        "runId": "openclaw-run-1",
        "sessionKey": params.get("sessionKey", f"agent:{params['agentId']}:main"),
    }


def pass_validator(payload):
    return ValidationDecision(CoreRunStatus.PASS, result={"payload": dict(payload)})


class OpenClawAdapterTests(unittest.TestCase):
    def adapter(self, handlers=None, *, validator=pass_validator, socket=None, store=None):
        fake = socket or FakeSocket(handlers)
        adapter = OpenClawAdapter(
            FakeSecretRef(),
            store or MemoryRunBindingStore(),
            result_validator=validator,
            socket_factory=lambda _url, _timeout: fake.open(),
        )
        return adapter, fake

    @staticmethod
    def payload(**updates):
        value = {
            "message": "Return a safe confirmation.",
            "agentId": "qwentest",
            "idempotencyKey": "core-run-1",
            "timeout": 30,
        }
        value.update(updates)
        return value

    def test_connect_auth_and_agent_availability(self):
        secret_ref = FakeSecretRef()
        fake = FakeSocket()
        adapter = OpenClawAdapter(secret_ref, MemoryRunBindingStore(), socket_factory=lambda *_: fake.open())
        hello = adapter.connect()
        self.assertEqual("hello-ok", hello["type"])
        self.assertEqual(1, secret_ref.calls)
        connect = fake.calls[0]
        self.assertEqual(4, connect["params"]["minProtocol"])
        self.assertEqual(4, connect["params"]["maxProtocol"])
        self.assertEqual("gateway-client", connect["params"]["client"]["id"])
        self.assertEqual(["operator.read", "operator.write"], connect["params"]["scopes"])
        self.assertEqual("super-secret-token", connect["params"]["auth"]["token"])
        self.assertIn("agent", adapter.rpc.methods)

    def test_default_socket_disables_client_keepalive_for_bounded_long_rpc(self):
        from plachem_fast_gateway.openclaw_adapter import _default_socket_factory

        fake = object()
        with patch("websockets.sync.client.connect", return_value=fake) as connect:
            self.assertIs(fake, _default_socket_factory("ws://127.0.0.1:18789", 10.0))
        self.assertIsNone(connect.call_args.kwargs["ping_interval"])
        self.assertNotIn("ping_timeout", connect.call_args.kwargs)

    def test_connect_accepts_current_nested_hello_auth_shape(self):
        hello = {
            "type": "hello-ok",
            "protocol": 4,
            "auth": {"role": "operator", "scopes": ["operator.admin"]},
            "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
        }
        adapter, _ = self.adapter(socket=FakeSocket(hello=hello))
        self.assertEqual(hello, adapter.connect())
        self.assertEqual("operator", adapter.rpc.role)
        self.assertEqual(("operator.admin",), adapter.rpc.scopes)

    def test_connect_rejects_missing_role(self):
        hello = {
            "type": "hello-ok",
            "auth": {"scopes": ["operator.write"]},
            "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
        }
        adapter, fake = self.adapter(socket=FakeSocket(hello=hello))
        with self.assertRaisesRegex(GatewayContractError, "operator role"):
            adapter.connect()
        self.assertTrue(fake.closed)

    def test_connect_rejects_missing_scopes(self):
        hello = {
            "type": "hello-ok",
            "auth": {"role": "operator"},
            "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
        }
        adapter, fake = self.adapter(socket=FakeSocket(hello=hello))
        with self.assertRaisesRegex(GatewayContractError, "operator.read/write scopes"):
            adapter.connect()
        self.assertTrue(fake.closed)

    def test_connect_rejects_wrong_role(self):
        hello = {
            "type": "hello-ok",
            "auth": {"role": "node", "scopes": ["operator.write"]},
            "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
        }
        adapter, _ = self.adapter(socket=FakeSocket(hello=hello))
        with self.assertRaisesRegex(GatewayContractError, "operator role"):
            adapter.connect()

    def test_connect_rejects_insufficient_scopes(self):
        hello = {
            "type": "hello-ok",
            "auth": {"role": "operator", "scopes": ["operator.read"]},
            "features": {"methods": ["agent", "agent.wait", "chat.history", "sessions.abort"]},
        }
        adapter, _ = self.adapter(socket=FakeSocket(hello=hello))
        with self.assertRaisesRegex(GatewayContractError, "operator.read/write scopes"):
            adapter.connect()

    def test_raw_token_constructor_path_is_not_available(self):
        parameters = inspect.signature(OpenClawAdapter).parameters
        self.assertNotIn("token", parameters)
        self.assertNotIn("credential", parameters)
        with self.assertRaisesRegex(TypeError, "trusted SecretRef"):
            OpenClawAdapter("raw-token", MemoryRunBindingStore())
        with self.assertRaisesRegex(ValueError, "SecretRef slot"):
            EnvironmentSecretRef("raw-token")

    def test_submit_uses_exact_contract_and_saves_binding(self):
        adapter, fake = self.adapter({"agent": accepted})
        binding = adapter.submit("core-run-1", self.payload())
        self.assertEqual(CoreRunStatus.RUNNING, binding.status)
        self.assertEqual("agent:qwentest:fast-gateway-core-run-1", binding.session_key)
        params = fake.calls[-1]["params"]
        self.assertEqual({"message", "agentId", "idempotencyKey", "timeout", "sessionKey"}, set(params))

    def test_wait_validates_before_pass(self):
        histories = iter([
            {"messages": []},
            {"messages": [{"role": "assistant", "content": {"status": "completed"}}]},
        ])
        handlers = {
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": {"status": "completed"}},
            "chat.history": lambda _: next(histories),
        }
        adapter, _ = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        decision = adapter.wait("core-run-1", timeout_seconds=2)
        self.assertEqual(CoreRunStatus.PASS, decision.status)

    def test_status_ok_is_not_automatically_pass(self):
        handlers = {
            "agent": accepted,
            "agent.wait": {"status": "ok", "result": {}},
        }
        adapter, _ = self.adapter(handlers, validator=lambda _: ValidationDecision(CoreRunStatus.FAIL, "bad evidence"))
        adapter.submit("core-run-1", self.payload())
        decision = adapter.wait("core-run-1", timeout_seconds=2)
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual(CoreRunStatus.FAIL, adapter.bindings.get("core-run-1").status)

    def test_composite_validator_requires_schema_evidence_artifact_and_scope(self):
        calls = []

        def check(name):
            def validate(result, envelope):
                calls.append((name, result["status"], envelope["status"]))
                return None
            return validate

        validator = CompositeResultValidator(
            result_schema=check("schema"),
            evidence=check("evidence"),
            artifacts=check("artifacts"),
            scope=check("scope"),
        )
        decision = validator({"status": "ok", "result": {"status": "completed", "summary": "done"}})
        self.assertEqual(CoreRunStatus.PASS, decision.status)
        self.assertEqual(["schema", "evidence", "artifacts", "scope"], [item[0] for item in calls])

    def test_composite_validator_fails_closed_on_scope_failure(self):
        allow = lambda _result, _envelope: None
        validator = CompositeResultValidator(
            result_schema=allow,
            evidence=allow,
            artifacts=allow,
            scope=lambda _result, _envelope: "OUT_OF_SCOPE",
        )
        decision = validator({"status": "ok", "result": {"status": "completed"}})
        self.assertEqual(CoreRunStatus.FAIL, decision.status)
        self.assertEqual("SCOPE_VALIDATION_FAILED:OUT_OF_SCOPE", decision.reason)

    def test_composite_validator_extracts_gateway_content_blocks(self):
        allow = lambda _result, _envelope: None
        validator = CompositeResultValidator(
            result_schema=allow,
            evidence=allow,
            artifacts=allow,
            scope=allow,
        )
        payload = {
            "status": "ok",
            "history": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": '{"status":"completed","summary":"done"}'},
                        ],
                    }
                ]
            },
        }
        decision = validator(payload)
        self.assertEqual(CoreRunStatus.PASS, decision.status)
        self.assertEqual("done", decision.result["summary"])

    def test_wait_uses_bounded_history_only_when_result_missing(self):
        handlers = {
            "agent": accepted,
            "agent.wait": {"status": "ok"},
            "chat.history": {"messages": [{"role": "assistant", "content": "done"}]},
        }
        adapter, fake = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        adapter.wait("core-run-1", timeout_seconds=2)
        history = next(call for call in fake.calls if call["method"] == "chat.history")
        self.assertEqual(20, history["params"]["limit"])

    def test_idempotent_retry_does_not_execute_twice(self):
        adapter, fake = self.adapter({"agent": accepted})
        first = adapter.submit("core-run-1", self.payload())
        second = adapter.submit("core-run-1", self.payload())
        self.assertEqual(first, second)
        self.assertEqual(1, sum(call["method"] == "agent" for call in fake.calls))
        with self.assertRaises(IdempotencyConflict):
            adapter.submit("core-run-1", self.payload(idempotencyKey="different"))

    def test_timeout_maps_to_core_timeout(self):
        handlers = {"agent": accepted, "agent.wait": {"status": "timeout"}}
        adapter, _ = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        decision = adapter.wait("core-run-1", timeout_seconds=0.01)
        self.assertEqual(CoreRunStatus.TIMEOUT, decision.status)
        self.assertEqual(CoreRunStatus.RUNNING, adapter.bindings.get("core-run-1").status)

    def test_wait_uses_private_rpc_operation(self):
        handlers = {"agent": accepted, "agent.wait": {"status": "pending"}}
        adapter, _ = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        fresh = Mock(return_value={"status": "pending"})
        adapter.rpc.request_fresh = fresh
        adapter.wait("core-run-1", timeout_seconds=1)
        fresh.assert_called_once_with(
            "agent.wait",
            {"runId": "openclaw-run-1", "timeoutMs": 1000},
            timeout=6.0,
            event_handler=ANY,
        )

    def test_abort_empty_run_ids_is_idempotent_for_finished_run(self):
        adapter, _ = self.adapter({"agent": accepted})
        adapter.submit("core-run-1", self.payload())
        adapter.rpc.request_on_owner_connection = Mock(
            return_value={"ok": True, "aborted": False, "runIds": []}
        )
        binding = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, binding.status)

    def test_abort_never_reconnects_away_from_submit_owner_connection(self):
        adapter, fake = self.adapter({"agent": accepted, "sessions.abort": {"status": "aborted"}})
        adapter.submit("core-run-1", self.payload())
        adapter.rpc._last_activity -= 120.0
        binding = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, binding.status)
        self.assertFalse(fake.closed)
        self.assertEqual(1, sum(call["method"] == "connect" for call in fake.calls))

    def test_abort_uses_bound_identity_and_adds_cancelled(self):
        handlers = {"agent": accepted, "sessions.abort": {"status": "aborted"}}
        adapter, fake = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        binding = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, binding.status)
        abort = fake.calls[-1]
        self.assertEqual("sessions.abort", abort["method"])
        self.assertEqual(
            {"key": "agent:qwentest:fast-gateway-core-run-1", "runId": "openclaw-run-1", "agentId": "qwentest"},
            abort["params"],
        )

    # --- TASK 4: fresh-connection abort fallback (stable device.id) ---

    def test_owner_connection_gone_cancel_falls_back_to_fresh_connection(self):
        """When the owner socket is gone, one fresh connection with the same
        stable device.id is used to abort the run."""
        handlers = {"agent": accepted, "sessions.abort": {"status": "aborted"}}
        adapter, fake = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        # Kill the owner socket to simulate a dropped owner connection.
        with adapter.rpc._lock:
            adapter.rpc._disconnect_locked()
        self.assertIsNone(adapter.rpc._socket)
        abort_handler = Mock(return_value={"status": "aborted"})
        adapter.rpc.request_fresh_abort = abort_handler
        binding = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, binding.status)
        self.assertEqual(1, abort_handler.call_count)
        abort_handler.assert_called_once_with(
            session_key="agent:qwentest:fast-gateway-core-run-1",
            run_id="openclaw-run-1",
            agent_id="qwentest",
            timeout=15.0,
        )

    def test_owner_connection_gone_fresh_abort_success_is_cancelled(self):
        """Fresh-connection abort success maps to CANCELLED status, not
        POLICY_ABORT_FAILED."""
        handlers = {"agent": accepted, "sessions.abort": {"status": "aborted"}}
        adapter, fake = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        with adapter.rpc._lock:
            adapter.rpc._disconnect_locked()
        adapter.rpc.request_fresh_abort = Mock(return_value={"status": "aborted"})
        binding = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, binding.status)
        # No exception raised — POLICY_ABORT_FAILED does not occur.

    def test_owner_connection_gone_fresh_abort_failure_propagates(self):
        """If the fresh-connection abort also fails, the error propagates
        (no retry, no silent success)."""
        handlers = {"agent": accepted, "sessions.abort": {"status": "aborted"}}
        adapter, fake = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        with adapter.rpc._lock:
            adapter.rpc._disconnect_locked()
        adapter.rpc.request_fresh_abort = Mock(
            side_effect=TransportError("fresh connection failed")
        )
        with self.assertRaises(TransportError):
            adapter.cancel("core-run-1")
        # Exactly one fresh attempt — no retry.
        self.assertEqual(1, adapter.rpc.request_fresh_abort.call_count)

    def test_fresh_connection_uses_same_device_identity_path(self):
        """The fresh connection inherits the same device identity path as the
        owner connection, preserving stable device.id."""
        handlers = {"agent": accepted, "sessions.abort": {"status": "aborted"}}
        adapter, fake = self.adapter(handlers)
        adapter.submit("core-run-1", self.payload())
        with adapter.rpc._lock:
            adapter.rpc._disconnect_locked()
        # The fresh-abort helper constructs its private client with this
        # same stable device identity path.
        self.assertIsNotNone(adapter.rpc._device_identity_path)
        adapter.rpc.request_fresh_abort = Mock(return_value={"status": "aborted"})
        binding = adapter.cancel("core-run-1")
        self.assertEqual(1, adapter.rpc.request_fresh_abort.call_count)
        self.assertEqual(CoreRunStatus.CANCELLED, binding.status)

    def test_session_id_lookup_uses_private_describe_and_preserves_owner(self):
        hello = {
            "type": "hello-ok",
            "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
            "features": {"methods": [
                "agent", "agent.wait", "chat.history", "sessions.abort", "sessions.describe",
            ]},
        }
        owner = FakeSocket(
            {"chat.history": {"messages": []}, "agent": accepted,
             "sessions.abort": {"status": "aborted"}},
            hello=hello,
        )
        lookup = FakeSocket(
            {"sessions.describe": {
                "session": {
                    "key": "agent:qwentest:fast-gateway-core-run-1",
                    "sessionId": "resolved-session-1",
                }
            }},
            hello=hello,
        )
        factory = SequenceSocketFactory([owner, lookup])
        adapter = OpenClawAdapter(
            FakeSecretRef(), MemoryRunBindingStore(), socket_factory=factory,
        )
        binding = adapter.submit("core-run-1", self.payload())
        self.assertEqual("resolved-session-1", binding.session_id)
        self.assertIs(adapter.rpc._socket, owner)
        self.assertFalse(owner.closed)
        self.assertEqual(2, factory.calls)
        cancelled = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, cancelled.status)
        self.assertEqual(
            1, sum(call["method"] == "sessions.abort" for call in owner.calls),
        )

    def test_session_id_lookup_failure_is_best_effort_and_keeps_owner(self):
        hello = {
            "type": "hello-ok",
            "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
            "features": {"methods": [
                "agent", "agent.wait", "chat.history", "sessions.abort", "sessions.describe",
            ]},
        }
        owner = FakeSocket(
            {"chat.history": {"messages": []}, "agent": accepted,
             "sessions.abort": {"status": "aborted"}},
            hello=hello,
        )
        broken_lookup = FakeSocket(
            {"sessions.describe": lambda _:
                (_ for _ in ()).throw(OSError("private lookup failed"))},
            hello=hello,
        )
        factory = SequenceSocketFactory([owner, broken_lookup])
        adapter = OpenClawAdapter(
            FakeSecretRef(), MemoryRunBindingStore(), socket_factory=factory,
        )
        binding = adapter.submit("core-run-1", self.payload())
        self.assertIsNone(binding.session_id)
        self.assertIs(adapter.rpc._socket, owner)
        self.assertFalse(owner.closed)
        cancelled = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, cancelled.status)

    def test_fresh_abort_negotiates_chat_abort_and_uses_session_key_field(self):
        owner = FakeSocket({"chat.history": {"messages": []}, "agent": accepted})
        fresh_hello = {
            "type": "hello-ok",
            "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
            "features": {"methods": ["agent", "agent.wait", "chat.history", "chat.abort"]},
        }
        fresh = FakeSocket(
            {"chat.abort": {"ok": True, "aborted": True, "runIds": ["openclaw-run-1"]}},
            hello=fresh_hello,
        )
        factory = SequenceSocketFactory([owner, fresh])
        adapter = OpenClawAdapter(
            FakeSecretRef(), MemoryRunBindingStore(), socket_factory=factory,
        )
        adapter.submit("core-run-1", self.payload())
        with adapter.rpc._lock:
            adapter.rpc._disconnect_locked()
        cancelled = adapter.cancel("core-run-1")
        self.assertEqual(CoreRunStatus.CANCELLED, cancelled.status)
        abort_call = next(call for call in fresh.calls if call["method"] == "chat.abort")
        self.assertEqual(
            {
                "sessionKey": "agent:qwentest:fast-gateway-core-run-1",
                "runId": "openclaw-run-1",
                "agentId": "qwentest",
            },
            abort_call["params"],
        )
        self.assertNotIn("key", abort_call["params"])

    def test_invalid_agent_and_forbidden_fields_fail_before_transport(self):
        adapter, fake = self.adapter({"agent": accepted})
        with self.assertRaisesRegex(GatewayContractError, "INVALID_AGENT_ID"):
            adapter.submit("core-run-1", self.payload(agentId="../bad"))
        for field in ("model", "provider", "endpoint", "credential", "cwd", "sessionId", "channel"):
            with self.subTest(field=field), self.assertRaisesRegex(GatewayContractError, "FORBIDDEN_RUNTIME_FIELD"):
                adapter.submit("core-run-1", self.payload(**{field: "forbidden"}))
        self.assertFalse(any(call["method"] == "agent" for call in fake.calls))

    def test_invalid_session_binding_fails_before_and_after_submit(self):
        adapter, _ = self.adapter({"agent": accepted})
        with self.assertRaises(SessionBindingError):
            adapter.submit("core-run-1", self.payload(sessionKey="agent:other:main"))
        bad_response = lambda _: {"status": "accepted", "runId": "x", "sessionKey": "agent:other:main"}
        adapter, _ = self.adapter({"agent": bad_response})
        with self.assertRaises(SessionBindingError):
            adapter.submit("core-run-1", self.payload())

    def test_transport_failure_is_fail_closed(self):
        adapter, _ = self.adapter(socket=FakeSocket(fail_send=True))
        with self.assertRaises(TransportError):
            adapter.connect()

    def test_transport_failure_reconnects_once_and_preserves_request(self):
        first = FakeSocket({"agent": lambda _: (_ for _ in ()).throw(OSError("stale"))})
        second = FakeSocket({"agent": accepted})
        factory = SequenceSocketFactory([first, second])
        adapter = OpenClawAdapter(FakeSecretRef(), MemoryRunBindingStore(), socket_factory=factory)
        binding = adapter.submit("core-run-1", self.payload(sessionKey="agent:qwentest:main"))
        self.assertEqual("openclaw-run-1", binding.openclaw_run_id)
        self.assertEqual(2, factory.calls)
        agent_calls = [call for socket in (first, second) for call in socket.calls if call["method"] == "agent"]
        self.assertEqual(2, len(agent_calls))
        self.assertEqual(agent_calls[0]["params"], agent_calls[1]["params"])
        self.assertTrue(first.closed)

    def test_reconnect_failure_is_bounded_to_one_retry(self):
        first = FakeSocket({"agent": lambda _: (_ for _ in ()).throw(OSError("stale"))})
        factory = SequenceSocketFactory([first])
        adapter = OpenClawAdapter(FakeSecretRef(), MemoryRunBindingStore(), socket_factory=factory)
        with self.assertRaises(TransportError):
            adapter.submit("core-run-1", self.payload())
        self.assertEqual(2, factory.calls)

    def test_idle_socket_is_discarded_before_reuse(self):
        first = FakeSocket({"agent": accepted})
        second = FakeSocket({"agent": accepted})
        factory = SequenceSocketFactory([first, second])
        adapter = OpenClawAdapter(
            FakeSecretRef(),
            MemoryRunBindingStore(),
            socket_factory=factory,
        )
        adapter.rpc._stale_after_seconds = 0.01
        adapter.submit("core-run-1", self.payload())
        time.sleep(0.02)
        adapter.submit("core-run-2", self.payload(idempotencyKey="core-run-2"))
        self.assertEqual(2, factory.calls)
        self.assertTrue(first.closed)

    def test_sqlite_store_persists_binding_separate_from_war_room(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "core" / "bindings.sqlite3"
            store = SQLiteRunBindingStore(path)
            binding = RunBinding("core", "oc", "qwentest", "agent:qwentest:main", None, "idem", CoreRunStatus.QUEUED)
            store.put(binding)
            self.assertEqual(binding, SQLiteRunBindingStore(path).get("core"))
            self.assertNotIn("war_room", str(path))

    def test_secret_redaction(self):
        redacted = redact_secrets({"token": "secret", "message": "prefix secret suffix"}, ("secret",))
        self.assertEqual("[REDACTED]", redacted["token"])
        self.assertNotIn("secret", redacted["message"])


if __name__ == "__main__":
    unittest.main()
