import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from war_room_fast_gateway import FastGatewayWarRoomAdapter
from war_room_worker import _persist_execution
from war_room_actions import _grounded_instruction


class FakeCore:
    def __init__(self):
        self.records = {}
        self.cancelled = []
        self.status_calls = []
        self.dispatch_calls = []

    def dispatch(self, **kwargs):
        self.dispatch_calls.append(kwargs.copy())
        cid = kwargs["core_run_id"]
        self.records[cid] = {"core_run_id": cid, "agent_id": kwargs["agent_id"], "status": "RUNNING",
                             "openclaw_binding": {"openclaw_run_id": "oc-1", "session_key": "agent:erpcoder:main", "session_id": None},
                             "runtime_seconds": 2.0, "policy_status": "NORMAL", "cancel_reason": "",
                             "escalation_required": False, "result": None}
        return self.records[cid]

    def status(self, cid):
        self.status_calls.append(cid)
        return self.records[cid]

    def wait(self, cid, *, timeout_seconds):
        raise AssertionError("poll must not collect or reconcile terminal results")

    def cancel(self, cid):
        self.cancelled.append(cid)
        self.records[cid]["status"] = "CANCELLED"
        self.records[cid]["cancel_reason"] = "OPENCLAW_ABORTED"
        return self.records[cid]

    def mark_user_cancelled(self, cid):
        self.records[cid]["status"] = "CANCELLED"
        self.records[cid]["cancel_reason"] = "USER_CANCEL"
        return self.records[cid]


class FakeControl:
    def abort(self, *, session_key):
        return "aborted"


class WarRoomFastGatewayCorrectionTests(unittest.TestCase):
    def test_poll_is_read_only_and_never_calls_wait(self):
        path = Path(tempfile.mktemp())
        with sqlite3.connect(path) as con:
            con.execute(
                "CREATE TABLE war_execution_runs "
                "(core_run_id TEXT PRIMARY KEY, openclaw_run_id TEXT)"
            )
            con.execute("INSERT INTO war_execution_runs VALUES ('core-1','openclaw-1')")
        core = FakeCore()
        core.records["core-1"] = {"core_run_id": "core-1", "status": "RUNNING"}

        receipt = FastGatewayWarRoomAdapter(core, path).poll(
            run_id="openclaw-1", agent_id="ERPcoder",
        )

        self.assertEqual("received", receipt.status)
        self.assertEqual(["core-1"], core.status_calls)

    def test_legacy_and_fast_instruction_contracts_are_explicitly_separate(self):
        packet = {"worktree": "/safe", "branch": "main", "revision": "abc1234", "api_base": "/api", "db_label": "isolated", "forbidden": ["production DB"], "completion_conditions": ["tests pass"]}
        legacy = _grounded_instruction("legacy intent", packet)
        fast = _grounded_instruction("fast intent", packet, "FAST_GATEWAY")
        self.assertIn("confirmed_worktree", legacy)
        self.assertIn("confirmed_revision", legacy)
        self.assertIn("representative_completion_claimed", legacy)
        self.assertIn('"status":"completed|blocked|failed"', fast)
        self.assertIn('"evidence":[{"type":"...","detail":"..."}]', fast)
        self.assertIn('"artifacts":[{"path":"..."}]', fast)
        self.assertIn('"scope":{"compliant":true,"violations":[]}', fast)
        self.assertNotIn("confirmed_worktree", fast)
        self.assertNotIn("confirmed_revision", fast)
        self.assertNotIn("representative_completion_claimed", fast)
        self.assertIn("first character must be {", fast)
        self.assertIn("last character must be }", fast)
        self.assertIn("code fences, backticks", fast)
        self.assertIn("prose before or after", fast)

    def test_recent_runs_endpoint_executes_against_run_registry(self):
        from fast_gateway_api import recent_runs
        path = Path(tempfile.mktemp())
        path.write_text(json.dumps({"core_run_id": "c-endpoint", "agent_id": "erpcoder", "status": "RUNNING"}) + "\n")
        with patch("fast_gateway_api._run_path", return_value=path):
            result = recent_runs(1)
        self.assertEqual("c-endpoint", result["runs"][0]["core_run_id"])

    def test_projection_conflict_updates_evidence_and_artifacts(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE war_execution_runs (core_run_id TEXT PRIMARY KEY, war_project_id TEXT, war_task_id TEXT, agent_id TEXT, openclaw_run_id TEXT, session_key TEXT, run_status TEXT, runtime_seconds REAL, result_summary TEXT, result_json TEXT, evidence_json TEXT, artifacts_json TEXT, policy_status TEXT, cancel_reason TEXT, escalation_required INTEGER, created_at INTEGER, updated_at INTEGER)")
        base = {"core_run_id":"c1","openclaw_run_id":"o1","session_key":"agent:x:main","run_status":"RUNNING","result_summary":"old","result_json":"{}","evidence_json":"[\"old-e\"]","artifacts_json":"[\"old-a\"]","policy_status":"NORMAL","cancel_reason":"","escalation_required":0}
        _persist_execution(con, snapshot=base, project_id="p", task_id="t", agent_id="ERPcoder", now=1)
        base.update(result_summary="new", evidence_json="[\"new-e\"]", artifacts_json="[\"new-a\"]", run_status="PASS")
        _persist_execution(con, snapshot=base, project_id="p", task_id="t", agent_id="ERPcoder", now=2)
        self.assertEqual(("[\"new-e\"]", "[\"new-a\"]"), con.execute("SELECT evidence_json,artifacts_json FROM war_execution_runs").fetchone())

    def test_rejected_projection_preserves_raw_candidate_and_reason(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE war_execution_runs (core_run_id TEXT PRIMARY KEY, war_project_id TEXT, war_task_id TEXT, agent_id TEXT, openclaw_run_id TEXT, session_key TEXT, run_status TEXT, runtime_seconds REAL, result_summary TEXT, result_json TEXT, evidence_json TEXT, artifacts_json TEXT, raw_response TEXT, rejected_result_json TEXT, validation_error TEXT, policy_status TEXT, cancel_reason TEXT, escalation_required INTEGER, created_at INTEGER, updated_at INTEGER)")
        snapshot = {
            "core_run_id":"c-rejected", "openclaw_run_id":"o-rejected", "session_key":"agent:x:main",
            "run_status":"FAIL", "result_summary":"inspected", "result_json":None,
            "evidence_json":None, "artifacts_json":None,
            "raw_response":'{"status":"completed","artifacts":[{"path":"AGENTS.md"}]}',
            "rejected_result_json":'{"status":"completed","artifacts":[{"path":"AGENTS.md"}]}',
            "validation_error":"SCOPE_VALIDATION_FAILED:READ_ONLY_ARTIFACT_REUSE",
            "policy_status":"NORMAL", "cancel_reason":"READ_ONLY_ARTIFACT_REUSE", "escalation_required":0,
        }
        _persist_execution(con, snapshot=snapshot, project_id="p", task_id="t", agent_id="ERPmanager", now=1)
        row = con.execute("SELECT raw_response,rejected_result_json,validation_error FROM war_execution_runs").fetchone()
        self.assertEqual((snapshot["raw_response"], snapshot["rejected_result_json"], snapshot["validation_error"]), row)

    def test_fast_instruction_distinguishes_read_only_evidence_from_new_artifacts(self):
        packet = {"worktree": "/safe", "branch": "main", "revision": "abc1234", "api_base": "/api", "db_label": "isolated", "forbidden": ["writes"], "completion_conditions": ["inspect"]}
        message = _grounded_instruction("read-only inspect AGENTS.md", packet, "FAST_GATEWAY")
        self.assertIn("Artifacts are outputs newly produced by this run", message)
        self.assertIn("return an empty artifacts array", message)

    def test_fast_cancel_is_core_cancel_and_snapshot_is_cancelled(self):
        path = Path(tempfile.mktemp())
        with sqlite3.connect(path) as con:
            con.executescript("""
                CREATE TABLE war_deliveries (id TEXT PRIMARY KEY, message_id TEXT, agent_id TEXT);
                CREATE TABLE war_messages (id TEXT PRIMARY KEY, project_id TEXT, body TEXT);
                CREATE TABLE war_tasks (id TEXT PRIMARY KEY, source_message_id TEXT, execution_mode TEXT);
                CREATE TABLE war_grounding_packets (task_id TEXT PRIMARY KEY, packet_json TEXT);
                CREATE TABLE war_execution_runs (
                    core_run_id TEXT PRIMARY KEY, war_project_id TEXT, war_task_id TEXT,
                    agent_id TEXT, openclaw_run_id TEXT, session_key TEXT, run_status TEXT, updated_at INTEGER
                );
            """)
            con.execute("INSERT INTO war_deliveries VALUES ('d1','m1','ERPcoder')")
            con.execute("INSERT INTO war_messages VALUES ('m1','p1','intent')")
            con.execute("INSERT INTO war_tasks VALUES ('t1','m1','FAST_GATEWAY')")
            packet = {"worktree":"/tmp/test","branch":"main","revision":"abcdef0","api_base":"/api","db_label":"test","forbidden":["live"],"completion_conditions":["done"],"required_evidence":["test"],"session_integrity_required":False}
            con.execute("INSERT INTO war_grounding_packets VALUES ('t1',?)", (json.dumps(packet),))
            con.execute("INSERT INTO war_execution_runs VALUES ('war-d1','p1','t1','erpcoder','oc-1','agent:erpcoder:war-room-test','RUNNING',1)")
        core = FakeCore(); adapter = FastGatewayWarRoomAdapter(core, path, control_rpc=FakeControl())
        adapter.deliver(delivery_id="d1", agent_id="ERPcoder", instruction_id="m1", body="intent")
        receipt = adapter.stop(delivery_id="d1", agent_id="ERPcoder")
        self.assertEqual("stopped", receipt.status)
        self.assertEqual([], core.cancelled)
        self.assertEqual("CANCELLED", adapter.execution_snapshot("d1")["run_status"])

    def test_ui_projection_has_summary_evidence_artifacts_without_session_key(self):
        ui_source = Path("static/war-room-ui.js").read_text()
        api_source = Path("war_room_actions.py").read_text()
        self.assertIn("result_summary", ui_source)
        self.assertIn("evidence_json", ui_source)
        self.assertIn("artifacts_json", ui_source)
        self.assertIn('value.pop("session_key", None)', api_source)
        self.assertIn('value.pop("session_id", None)', api_source)

    def test_deliver_prefers_exact_result_artifact_paths_over_broad_scope(self):
        path = Path(tempfile.mktemp())
        exact = "/home/plachem-sever/.openclaw/agents/cliper/01_ACTIVE/test/result.md"
        broad = "/home/plachem-sever/.openclaw/agents/cliper/01_ACTIVE/test"
        with sqlite3.connect(path) as con:
            con.executescript("""
                CREATE TABLE war_deliveries (id TEXT PRIMARY KEY, message_id TEXT, agent_id TEXT);
                CREATE TABLE war_messages (id TEXT PRIMARY KEY, project_id TEXT, body TEXT);
                CREATE TABLE war_tasks (id TEXT PRIMARY KEY, source_message_id TEXT, execution_mode TEXT);
                CREATE TABLE war_grounding_packets (task_id TEXT PRIMARY KEY, packet_json TEXT);
            """)
            con.execute("INSERT INTO war_deliveries VALUES ('d2','m2','cliper')")
            con.execute("INSERT INTO war_messages VALUES ('m2','p2','read-only result')")
            con.execute("INSERT INTO war_tasks VALUES ('t2','m2','FAST_GATEWAY')")
            packet = {
                "worktree": broad, "branch": "test", "revision": "v1",
                "api_base": "/api", "db_label": "test",
                "forbidden": ["production DB"], "completion_conditions": ["done"],
                "approved_paths": [broad], "result_artifact_paths": [exact],
            }
            con.execute("INSERT INTO war_grounding_packets VALUES ('t2',?)", (json.dumps(packet),))
        core = FakeCore()
        receipt = FastGatewayWarRoomAdapter(core, path).deliver(
            delivery_id="d2", agent_id="cliper", instruction_id="m2", body="ignored"
        )
        self.assertEqual("received", receipt.status)
        self.assertEqual([exact], core.dispatch_calls[-1]["approved_paths"])


if __name__ == "__main__":
    unittest.main()
