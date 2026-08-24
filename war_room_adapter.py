from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import uuid
import select
import threading
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class DeliveryReceipt:
    delivery_id: str
    status: str
    session_id: str | None = None
    error_code: str | None = None
    run_id: str | None = None
    response_body: str | None = None


@dataclass
class GatewayRun:
    run_id: str
    status: str = "queued"
    error_code: str | None = None
    response_body: str | None = None


class FakeGatewayAdapter:
    """In-memory gateway for safe recovery/timeout integration tests."""

    def __init__(self) -> None:
        self.runs: dict[str, GatewayRun] = {}

    def send(self, *, agent_id: str, body: str, run_id: str) -> GatewayRun:
        return self.runs.setdefault(run_id, GatewayRun(run_id))

    def complete(self, *, run_id: str, status: str, response_body: str | None = None, error_code: str | None = None) -> GatewayRun:
        run = self.runs[run_id]
        run.status = status
        run.response_body = response_body
        run.error_code = error_code
        return run

    def mark_received(self, run_id: str) -> GatewayRun:
        return self.complete(run_id=run_id, status="received")

    def expire(self, run_id: str) -> GatewayRun:
        return self.complete(run_id=run_id, status="timed_out")

    def poll(self, run_id: str, agent_id: str | None = None) -> GatewayRun:
        return self.runs[run_id]


class SessionAdapter(Protocol):
    def deliver(self, *, delivery_id: str, agent_id: str, instruction_id: str, body: str) -> DeliveryReceipt: ...
    def stop(self, *, delivery_id: str, agent_id: str) -> DeliveryReceipt: ...
    def poll(self, *, run_id: str, agent_id: str) -> DeliveryReceipt: ...


class TestSessionAdapter:
    """Deterministic isolated adapter. It never touches OpenClaw or sends a message."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str, str, str]] = []
        self.stops: list[tuple[str, str]] = []

    def deliver(self, *, delivery_id: str, agent_id: str, instruction_id: str, body: str) -> DeliveryReceipt:
        self.deliveries.append((delivery_id, agent_id, instruction_id, body))
        if "[IMMUTABLE_GROUNDING_PACKET]" in body:
            packet_text = body.split("[IMMUTABLE_GROUNDING_PACKET]\n", 1)[1].split("\n[STRUCTURED_RESULT]", 1)[0]
            packet = json.loads(packet_text)
            response = json.dumps({
                "confirmed_worktree":packet["worktree"], "confirmed_revision":packet["revision"],
                "verdict":"PASS", "evidence":[f"/test-evidence/{agent_id}.json"],
                "summary":f"시연 응답 · {agent_id}", "representative_completion_claimed":False,
            }, ensure_ascii=False)
        else:
            response = (
                f"[시연 응답 · {agent_id}] 요청 ‘{body}’을(를) 정상 접수하고 처리했습니다. "
                "이 문장은 실제 업무 산출물이 아니라 UI 흐름 확인용 결과입니다."
            )
        return DeliveryReceipt(delivery_id, "responded", session_id=f"test-session-{agent_id}", response_body=response)

    def stop(self, *, delivery_id: str, agent_id: str) -> DeliveryReceipt:
        self.stops.append((delivery_id, agent_id))
        return DeliveryReceipt(delivery_id, "stopped", session_id=f"test-session-{agent_id}")


class PersistentGatewayBridge:
    """One long-lived official GatewayClient process for run ownership safety."""

    def __init__(self, process: Any | None = None) -> None:
        if process is None:
            node = os.environ.get("PLACHEM_WAR_ROOM_NODE_BIN", "node")
            script = Path(__file__).with_name("war_room_gateway_bridge.mjs")
            process = subprocess.Popen([node, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._process = process
        self._lock = threading.Lock()
        self._counter = 0
        hello = self._read_line(15)
        if not hello.get("ready") or not hello.get("connectionId"):
            raise RuntimeError("persistent_gateway_bridge_not_ready")
        self.connection_id = str(hello["connectionId"])

    def _read_line(self, timeout_seconds: int) -> dict[str, Any]:
        if not self._process.stdout:
            raise RuntimeError("persistent_gateway_bridge_stdout_missing")
        ready, _, _ = select.select([self._process.stdout], [], [], timeout_seconds)
        if not ready:
            raise TimeoutError("persistent_gateway_bridge_timeout")
        line = self._process.stdout.readline()
        if not line:
            raise RuntimeError("persistent_gateway_bridge_closed")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("persistent_gateway_bridge_invalid_response")
        return value

    def request(self, method: str, params: dict[str, Any], timeout_ms: int = 15000) -> tuple[dict[str, Any], str]:
        with self._lock:
            self._counter += 1
            request_id = f"war-room-{self._counter}"
            if not self._process.stdin:
                raise RuntimeError("persistent_gateway_bridge_stdin_missing")
            self._process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params, "timeoutMs": timeout_ms}) + "\n")
            self._process.stdin.flush()
            response = self._read_line(max(1, timeout_ms // 1000 + 5))
            response_connection = str(response.get("connectionId") or "")
            self.connection_id = response_connection
            if response.get("id") != request_id or response.get("ok") is not True or not isinstance(response.get("result"), dict):
                raise RuntimeError(str(response.get("error") or "persistent_gateway_bridge_rejected"))
            return response["result"], response_connection

    def close(self) -> None:
        if self._process.stdin:
            self._process.stdin.close()


class OpenClawSessionAdapter:
    """Opt-in official Gateway adapter for ``chat.send`` and ``chat.abort``.

    The boundary is disabled by default. It never uses a shell and discovers
    only the named agent's latest stored session. A custom executable remains
    available as a test/operations bridge, but the default enabled path calls
    the installed OpenClaw CLI Gateway RPC directly.
    """

    def __init__(self, runner: Callable[..., Any] = subprocess.run, bridge: Any | None = None) -> None:
        self._runner = runner
        self._bridge = bridge
        self._bindings: dict[str, tuple[str, str | None]] = {}
        self._run_bindings: dict[str, tuple[str, str | None]] = {}
        self._delivery_runs: dict[str, str] = {}
        self._run_started_at_ms: dict[str, int] = {}
        self._run_connections: dict[str, str] = {}

    def close(self) -> None:
        closer = getattr(self._bridge, "close", None)
        if closer:
            closer()

    @staticmethod
    def _test_session_prefix(agent_id: str) -> str:
        return f"agent:{agent_id.lower()}:war-room-test:"

    def create_disposable_session(self, *, agent_id: str, project_id: str) -> dict[str, Any]:
        """Create an empty, explicitly named session through the official Gateway.

        No task/message is supplied, so provisioning itself cannot run an agent.
        """
        nonce = str(uuid.uuid4())
        session_key = self._test_session_prefix(agent_id) + nonce
        data, error = self._gateway("sessions.create", {
            "key": session_key,
            "agentId": agent_id.lower(),
            "label": f"DISPOSABLE War Room test · {project_id} · {agent_id} · {nonce[:8]}",
            "emitCommandHooks": False,
        }, session_key)
        if error:
            raise RuntimeError(error.error_code or "openclaw_session_create_failed")
        returned_key = data.get("key") if data else None
        session_id = data.get("sessionId") if data else None
        if returned_key != session_key or not isinstance(session_id, str) or not session_id:
            raise RuntimeError("openclaw_session_create_unconfirmed")
        return {"session_key": session_key, "session_id": session_id, "purpose": "test", "disposable": True}

    def bind_delivery(self, delivery_id: str, *, session_key: str, session_id: str | None, disposable: bool, purpose: str, agent_id: str | None = None) -> None:
        if not disposable or purpose != "test" or ":war-room-test:" not in session_key or not session_key.startswith("agent:") or (agent_id and not session_key.startswith(self._test_session_prefix(agent_id))):
            raise ValueError("only explicit disposable test sessions are allowed")
        self._bindings[delivery_id] = (session_key, session_id)

    def bind_run(self, run_id: str, *, session_key: str, session_id: str | None, disposable: bool, purpose: str, delivery_id: str | None = None, started_at: int | None = None, agent_id: str | None = None) -> None:
        if not disposable or purpose != "test" or ":war-room-test:" not in session_key or not session_key.startswith("agent:") or (agent_id and not session_key.startswith(self._test_session_prefix(agent_id))):
            raise ValueError("only explicit disposable test sessions are allowed")
        self._run_bindings[run_id] = (session_key, session_id)
        if delivery_id:
            self._delivery_runs[delivery_id] = run_id
        if started_at is not None:
            self._run_started_at_ms[run_id] = int(started_at) * 1000

    @staticmethod
    def _result_payload(stdout: str) -> dict[str, Any]:
        value = json.loads(stdout or "{}")
        if not isinstance(value, dict):
            return {}
        result = value.get("result")
        return result if isinstance(result, dict) else value

    def _invoke_bridge(self, operation: str, payload: dict[str, str]) -> DeliveryReceipt:
        command = os.environ.get("PLACHEM_WAR_ROOM_ADAPTER_COMMAND")
        if not command:
            return DeliveryReceipt(payload["delivery_id"], "failed", error_code="real_adapter_command_missing")
        try:
            result = self._runner([command, operation], input=json.dumps(payload), text=True, capture_output=True, timeout=15, check=False)
            data = self._result_payload(result.stdout)
            status = data.get("status") if data.get("status") in {"received", "responded", "stopped", "failed", "timed_out"} else "failed"
            return DeliveryReceipt(payload["delivery_id"], status, data.get("session_id"), data.get("error_code"))
        except subprocess.TimeoutExpired:
            return DeliveryReceipt(payload["delivery_id"], "timed_out", error_code="real_adapter_timeout")
        except (OSError, json.JSONDecodeError):
            return DeliveryReceipt(payload["delivery_id"], "failed", error_code="real_adapter_failed")

    def _gateway(self, method: str, params: dict[str, Any], delivery_id: str) -> tuple[dict[str, Any] | None, DeliveryReceipt | None]:
        if self._bridge is None and self._runner is subprocess.run and os.environ.get("PLACHEM_WAR_ROOM_REAL_ADAPTER") == "1":
            try:
                self._bridge = PersistentGatewayBridge()
            except (OSError, RuntimeError, TimeoutError, json.JSONDecodeError):
                return None, DeliveryReceipt(delivery_id, "failed", error_code="persistent_gateway_bridge_failed")
        if self._bridge is not None:
            try:
                data, connection_id = self._bridge.request(method, params, timeout_ms=15000)
                if method == "chat.send" and isinstance(data.get("runId"), str):
                    self._run_connections[data["runId"]] = connection_id
                return data, None
            except TimeoutError:
                return None, DeliveryReceipt(delivery_id, "timed_out", error_code="openclaw_gateway_timeout")
            except (OSError, RuntimeError, json.JSONDecodeError):
                return None, DeliveryReceipt(delivery_id, "failed", error_code="openclaw_gateway_rejected")
        binary = os.environ.get("PLACHEM_WAR_ROOM_OPENCLAW_BIN", "openclaw")
        try:
            result = self._runner(
                [binary, "gateway", "call", method, "--params", json.dumps(params), "--json", "--timeout", "15000"],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                return None, DeliveryReceipt(delivery_id, "failed", error_code="openclaw_gateway_rejected")
            return self._result_payload(result.stdout), None
        except subprocess.TimeoutExpired:
            return None, DeliveryReceipt(delivery_id, "timed_out", error_code="openclaw_gateway_timeout")
        except (OSError, json.JSONDecodeError):
            return None, DeliveryReceipt(delivery_id, "failed", error_code="openclaw_gateway_failed")

    def deliver(self, *, delivery_id: str, agent_id: str, instruction_id: str, body: str) -> DeliveryReceipt:
        if os.environ.get("PLACHEM_WAR_ROOM_REAL_ADAPTER") != "1":
            return DeliveryReceipt(delivery_id, "failed", error_code="real_adapter_disabled")
        payload = {"delivery_id": delivery_id, "agent_id": agent_id, "instruction_id": instruction_id, "body": body}
        if os.environ.get("PLACHEM_WAR_ROOM_ADAPTER_COMMAND"):
            return self._invoke_bridge("deliver", payload)
        session = self._bindings.get(delivery_id)
        if session is None:
            return DeliveryReceipt(delivery_id, "failed", error_code="openclaw_session_missing")
        session_key, session_id = session
        history, history_error = self._gateway("chat.history", {"sessionKey": session_key, "agentId": agent_id, "limit": 1}, delivery_id)
        if history_error:
            return history_error
        session_info = history.get("sessionInfo", {}) if history else {}
        active_ids = session_info.get("activeRunIds", []) if isinstance(session_info, dict) else []
        if session_info.get("hasActiveRun") is True or (isinstance(active_ids, list) and active_ids):
            return DeliveryReceipt(delivery_id, "failed", session_id=session_id, error_code="openclaw_session_active_run_exists")
        data, error = self._gateway("chat.send", {
            "sessionKey": session_key,
            "agentId": agent_id,
            "sessionId": session_id,
            "message": body,
            "deliver": False,
            "idempotencyKey": delivery_id,
        }, delivery_id)
        if error:
            return error
        run_id = str(data.get("runId")) if data and data.get("runId") else None
        if run_id:
            self._run_bindings[run_id] = session
            self._delivery_runs[delivery_id] = run_id
        return DeliveryReceipt(delivery_id, "received", session_id=session_id, error_code=None if run_id else "openclaw_run_id_missing", run_id=run_id)

    @staticmethod
    def _latest_assistant_text(data: dict[str, Any] | None, *, not_before_ms: int | None = None) -> str | None:
        messages = data.get("messages", []) if data else []
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            timestamp = message.get("timestamp")
            if not_before_ms is not None and isinstance(timestamp, (int, float)) and int(timestamp) < not_before_ms:
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                chunks = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
                text = "\n".join(chunk for chunk in chunks if chunk).strip()
                if text:
                    return text
        return None

    def poll(self, *, run_id: str, agent_id: str) -> DeliveryReceipt:
        """Reconcile a non-blocking chat.send run without resending it."""
        data, error = self._gateway("agent.wait", {"runId": run_id, "timeoutMs": 1}, run_id)
        if error:
            return error
        status = str(data.get("status", "pending")) if data else "pending"
        if status in {"pending", "timeout"}:
            return DeliveryReceipt(run_id, "received", run_id=run_id)
        if status == "error":
            return DeliveryReceipt(run_id, "failed", error_code=str(data.get("error") or "openclaw_run_failed"), run_id=run_id)
        if status != "ok":
            return DeliveryReceipt(run_id, "failed", error_code="openclaw_run_status_invalid", run_id=run_id)
        session = self._run_bindings.get(run_id)
        response_body = None
        session_id = None
        if session is not None:
            session_key, session_id = session
            history, history_error = self._gateway("chat.history", {"sessionKey": session_key, "agentId": agent_id, "limit": 20}, run_id)
            if history_error is None:
                response_body = self._latest_assistant_text(history, not_before_ms=self._run_started_at_ms.get(run_id))
        return DeliveryReceipt(run_id, "responded", session_id=session_id, run_id=run_id, response_body=response_body)

    def stop(self, *, delivery_id: str, agent_id: str) -> DeliveryReceipt:
        if os.environ.get("PLACHEM_WAR_ROOM_REAL_ADAPTER") != "1":
            return DeliveryReceipt(delivery_id, "failed", error_code="real_adapter_disabled")
        payload = {"delivery_id": delivery_id, "agent_id": agent_id}
        if os.environ.get("PLACHEM_WAR_ROOM_ADAPTER_COMMAND"):
            return self._invoke_bridge("interrupt", payload)
        session = self._bindings.get(delivery_id)
        if session is None:
            return DeliveryReceipt(delivery_id, "failed", error_code="openclaw_session_missing")
        session_key, session_id = session
        params = {"sessionKey": session_key, "agentId": agent_id}
        run_id = self._delivery_runs.get(delivery_id)
        if run_id:
            owner_connection = self._run_connections.get(run_id)
            bridge_connection = getattr(self._bridge, "connection_id", None)
            if self._bridge is not None:
                try:
                    _, bridge_connection = self._bridge.request("bridge.status", {}, timeout_ms=2000)
                except (OSError, RuntimeError, TimeoutError, json.JSONDecodeError):
                    return DeliveryReceipt(delivery_id, "failed", session_id=session_id, error_code="openclaw_owner_connection_lost", run_id=run_id)
            if self._bridge is not None and (not owner_connection or bridge_connection != owner_connection):
                return DeliveryReceipt(delivery_id, "failed", session_id=session_id, error_code="openclaw_owner_connection_lost", run_id=run_id)
            params["runId"] = run_id
        data, error = self._gateway("chat.abort", params, delivery_id)
        if error:
            return error
        if not data or data.get("aborted") is not True:
            return DeliveryReceipt(delivery_id, "failed", session_id=session_id, error_code="openclaw_interrupt_unconfirmed")
        return DeliveryReceipt(delivery_id, "stopped", session_id=session_id)
