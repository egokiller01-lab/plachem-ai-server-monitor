from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plachem_fast_gateway.openclaw_adapter import (
    CoreRunStatus,
    MemoryRunBindingStore,
    OpenClawAdapter,
    SQLiteRunBindingStore,
    ValidationDecision,
)
from plachem_fast_gateway.production_runtime import create_ubuntu_core_engine

try:
    from .test_openclaw_adapter import FakeSecretRef, FakeSocket
except ImportError:  # unittest discovery may import this file as a top-level module
    from test_openclaw_adapter import FakeSecretRef, FakeSocket


def accepted(params):
    return {"status": "accepted", "runId": params["idempotencyKey"], "sessionKey": params["sessionKey"]}


def validator(payload):
    messages = payload.get("history", {}).get("messages", [])
    if not messages:
        return ValidationDecision(CoreRunStatus.FAIL, "MISSING_RESULT")
    return ValidationDecision(CoreRunStatus.PASS, result={"content": messages[-1]["content"]})


class WatermarkTests(unittest.TestCase):
    def adapter(self, history):
        calls = {"history": 0}

        def next_history(_params):
            index = calls["history"]
            calls["history"] += 1
            return history[min(index, len(history) - 1)]

        socket = FakeSocket({
            "chat.history": next_history,
            "agent": accepted,
            "agent.wait": {"status": "ok"},
            "sessions.abort": {"status": "aborted"},
        })
        adapter = OpenClawAdapter(
            FakeSecretRef(), MemoryRunBindingStore(), result_validator=validator,
            socket_factory=lambda *_: socket.open(),
        )
        return adapter, socket, calls

    @staticmethod
    def payload(**updates):
        payload = {"message": "work", "agentId": "erpcoder", "idempotencyKey": "idem-1", "timeout": 2}
        payload.update(updates)
        return payload

    def test_stale_only_is_rejected(self):
        old = {"role": "assistant", "content": "old", "__openclaw": {"seq": 4, "id": "old"}}
        adapter, _, _ = self.adapter([{"messages": [old]}, {"messages": [old]}])
        adapter.submit("core-1", self.payload())
        outcome = adapter.wait("core-1", timeout_seconds=1)
        self.assertEqual(CoreRunStatus.FAIL, outcome.status)
        self.assertEqual("MISSING_POST_SUBMIT_RESULT", outcome.reason)

    def test_only_new_assistant_is_validated(self):
        old = {"role": "assistant", "content": "old", "__openclaw": {"seq": 4, "id": "old"}}
        new = {"role": "assistant", "content": "new", "__openclaw": {"seq": 5, "id": "new"}}
        adapter, _, _ = self.adapter([{"messages": [old]}, {"messages": [old, new]}])
        adapter.submit("core-1", self.payload())
        outcome = adapter.wait("core-1", timeout_seconds=1)
        self.assertEqual(CoreRunStatus.PASS, outcome.status)
        self.assertEqual("new", outcome.result["content"])

    def test_same_session_cross_run_uses_each_runs_own_boundary(self):
        session = "agent:erpcoder:shared"
        first = {"role": "assistant", "content": "first", "__openclaw": {"seq": 1, "id": "first"}}
        second = {"role": "assistant", "content": "second", "__openclaw": {"seq": 2, "id": "second"}}
        adapter, socket, _ = self.adapter([
            {"messages": []}, {"messages": [first]},
            {"messages": [first]}, {"messages": [first, second]},
        ])
        adapter.submit("core-1", self.payload(sessionKey=session))
        self.assertEqual("first", adapter.wait("core-1", timeout_seconds=1).result["content"])
        socket.handlers["agent.wait"] = {"status": "ok"}
        adapter.submit("core-2", self.payload(sessionKey=session, idempotencyKey="idem-2"))
        self.assertEqual("second", adapter.wait("core-2", timeout_seconds=1).result["content"])

    def test_id_boundary_fallback_and_missing_boundary_fail_closed(self):
        old = {"role": "assistant", "id": "old", "content": "old"}
        new = {"role": "assistant", "id": "new", "content": "new"}
        adapter, _, _ = self.adapter([{"messages": [old]}, {"messages": [old, new]}])
        adapter.submit("core-1", self.payload())
        self.assertEqual(CoreRunStatus.PASS, adapter.wait("core-1", timeout_seconds=1).status)

        adapter, _, _ = self.adapter([{"messages": [old]}, {"messages": [new]}])
        adapter.submit("core-2", self.payload(idempotencyKey="idem-2"))
        outcome = adapter.wait("core-2", timeout_seconds=1)
        self.assertEqual("HISTORY_WATERMARK_UNVERIFIABLE", outcome.reason)

    def test_malformed_prior_cursor_fails_closed(self):
        old = {"role": "assistant", "content": "old", "__openclaw": {"seq": "bad"}}
        new = {"role": "assistant", "content": "new", "__openclaw": {"seq": 2}}
        adapter, _, _ = self.adapter([{"messages": [old]}, {"messages": [old, new]}])
        adapter.submit("core-1", self.payload())
        self.assertEqual("HISTORY_WATERMARK_UNVERIFIABLE", adapter.wait("core-1", timeout_seconds=1).reason)

    def test_pending_timeout_cancel_leave_watermark_unchanged(self):
        for observed in ("pending", "timeout"):
            adapter, socket, _ = self.adapter([{"messages": []}])
            socket.handlers["agent.wait"] = {"status": observed}
            adapter.submit("core-1", self.payload())
            before = adapter.bindings.get_preparation("core-1")
            adapter.wait("core-1", timeout_seconds=1)
            self.assertEqual(before, adapter.bindings.get_preparation("core-1"))
        adapter, _, _ = self.adapter([{"messages": []}])
        adapter.submit("core-1", self.payload())
        before = adapter.bindings.get_preparation("core-1")
        adapter.cancel("core-1")
        self.assertEqual(before, adapter.bindings.get_preparation("core-1"))

    def test_sqlite_migrates_old_binding_and_restores_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("""CREATE TABLE openclaw_run_bindings (
                    core_run_id TEXT PRIMARY KEY, openclaw_run_id TEXT NOT NULL UNIQUE,
                    agent_id TEXT NOT NULL, session_key TEXT NOT NULL, session_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL)""")
            store = SQLiteRunBindingStore(path)
            adapter, socket, _ = self.adapter([{"messages": []}])
            adapter.bindings = store
            adapter.submit("core-1", self.payload())
            restored = SQLiteRunBindingStore(path).get_preparation("core-1")
            self.assertEqual("agent:erpcoder:fast-gateway-core-1", restored.session_key)
            self.assertTrue(restored.history_was_empty)

    def test_binding_schema_never_touches_war_room_database(self):
        with tempfile.TemporaryDirectory() as directory:
            war_path = Path(directory) / "war_room.sqlite3"
            binding_path = Path(directory) / "bindings.sqlite3"
            with sqlite3.connect(war_path) as connection:
                connection.execute("CREATE TABLE sentinel(value TEXT)")
                connection.execute("INSERT INTO sentinel VALUES ('untouched')")
            before = war_path.read_bytes()
            SQLiteRunBindingStore(binding_path)
            self.assertEqual(before, war_path.read_bytes())
            with sqlite3.connect(binding_path) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("openclaw_run_preparations", tables)

    def test_submit_retry_reuses_watermark_and_forbids_runtime_fields(self):
        adapter, socket, calls = self.adapter([{"messages": []}])
        socket.handlers["agent"] = lambda _params: (_ for _ in ()).throw(OSError("submit failed"))
        with self.assertRaises(Exception):
            adapter.submit("core-1", self.payload())
        socket.handlers["agent"] = accepted
        adapter.submit("core-1", self.payload())
        self.assertEqual(1, calls["history"])
        sent = next(call["params"] for call in socket.calls if call["method"] == "agent")
        self.assertFalse({"model", "provider", "endpoint"} & set(sent))


class ProductionCompositionTests(unittest.TestCase):
    def test_clean_entry_composes_ubuntu_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = Path(__file__).resolve().parents[1]
            agents = package / "agents.json"
            models = package / "models.json"
            sentinel = object()
            with patch("plachem_fast_gateway.production_runtime.create_ubuntu_worker_transport", return_value=sentinel) as factory:
                engine = create_ubuntu_core_engine(
                    runs_path=root / "runs.jsonl", agents_path=agents, models_path=models,
                    bindings_path=root / "bindings.sqlite3",
                )
            factory.assert_called_once_with(root / "bindings.sqlite3")
            self.assertIs(sentinel, engine.adapter)


if __name__ == "__main__":
    unittest.main()
