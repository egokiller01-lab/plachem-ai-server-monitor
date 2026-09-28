import ast
import json
import sqlite3
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import war_room_runtime
from test_production_authorization_wiring import production
from test_war_room_task_contract_v2 import INSTRUCTION
from war_room_adapter import DeliveryReceipt
import war_room_actions as actions


ROOT = Path(__file__).resolve().parents[1]


def test_result_only_endpoint_is_authorized_idempotent_and_never_dispatches():
    source = (ROOT / "war_room_actions.py").read_text()
    tree = ast.parse(source)
    endpoint = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "revalidate_task_result")
    body = ast.get_source_segment(source, endpoint)
    assert "_require_representative" in body
    assert "_validate_mutation_contract" in body
    assert "_require_fresh_context" in body
    assert "_idem" in body and "_save_idem" in body
    assert "war_result_revalidation_history" in body
    assert "war_execution_runs SET" not in body
    assert "war_task_calls SET" not in body
    assert "dispatch_execution" not in body


def test_result_revalidation_history_is_append_only():
    migration = (ROOT / "migrations/20260928_result_revalidation_history.sql").read_text()
    assert "validator_version" in migration
    assert "original_response" in migration
    assert "dispatch_count" in migration
    with sqlite3.connect(":memory:") as con:
        con.execute("CREATE TABLE war_tasks(id TEXT PRIMARY KEY)")
        con.executescript(migration)
        con.execute("INSERT INTO war_tasks VALUES ('task-1')")
        con.execute("""INSERT INTO war_result_revalidation_history
            VALUES ('h','task-1',1,'core','openclaw','session','v1','reason','FAIL','FAIL','original',1,1)""")
        try:
            con.execute("UPDATE war_result_revalidation_history SET reason='tampered'")
            raise AssertionError("history update was allowed")
        except sqlite3.DatabaseError as exc:
            assert "append-only" in str(exc)


def test_simple_ui_wires_eligible_result_only_button():
    source = (ROOT / "static/war-room-simple.js").read_text()
    html = (ROOT / "static/war-room-simple.html").read_text()
    assert "결과만 재검증" in source
    assert "revalidate-result" in source
    assert "task_revalidate_result" in source
    assert "task.revalidation?.eligible" in source
    assert "worker_redispatched: False" not in source
    assert 'WAR_ROOM_SIMPLE_ASSET_VERSION = "20260928-result-revalidation-v1"' in source
    assert 'war-room-simple.js?v=20260928-result-revalidation-v1' in html


class RevalidationQAAdapter:
    def __init__(self, evidence_path: Path):
        self.evidence_path = evidence_path
        self.revalidate_calls = []
        self.qa_deliveries = []

    def revalidate_result(self, core_run_id):
        self.revalidate_calls.append(core_run_id)
        return SimpleNamespace(status="PASS", reason="saved RESULT snapshot is valid")

    def create_disposable_session(self, *, agent_id, project_id):
        return {"session_key": f"agent:{agent_id.lower()}:war-room-test:revalidation",
                "session_id": "qa-revalidation-session", "purpose": "test", "disposable": True}

    def deliver(self, *, delivery_id, agent_id, instruction_id, body):
        self.qa_deliveries.append(delivery_id)
        packet = json.loads(body.split("[IMMUTABLE_GROUNDING_PACKET]\n", 1)[1].split(
            "\n[ORIGINAL_INSTRUCTION_CONTEXT]", 1)[0])
        result = {
            "confirmed_worktree": packet["worktree"],
            "confirmed_revision": packet["revision"],
            "verdict": "PASS",
            "summary": "Independent RESULT revalidation QA PASS",
            "evidence": [str(self.evidence_path)],
            "representative_completion_claimed": False,
        }
        return DeliveryReceipt(delivery_id, "responded", run_id="qa-revalidation-run",
                               response_body=json.dumps(result))

    def close(self):
        return None


def test_http_result_revalidation_queues_one_qa_and_preserves_original_run(production, monkeypatch, tmp_path):
    root, client, _, _ = production
    monkeypatch.setenv("PLACHEM_WAR_ROOM_AUTO_QA", "1")
    monkeypatch.setenv("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", "result-revalidation-test")
    main = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main",
            "Idempotency-Key": "result-revalidation-prepare"}
    rep = {"X-Authenticated-Principal": "human-representative",
           "X-War-Room-Proxy-Secret": "fixture-proxy-secret"}
    prepared = client.post("/api/war-room/projects/plachem-agent-war-room/prepare",
                           headers=main, json={
                               "instruction": INSTRUCTION,
                               "agent_ids": ["ERPmanager"],
                               "reviewer_agent_id": "ERPqa",
                               "execution_mode": "LEGACY",
                               "document_version": "baseline-2026-08-23",
                               "deadline_at": int(time.time()) + 9000,
                               "grounding": {"worktree": str(root),
                                             "forbidden": ["production DB", "merge/push/deploy"]},
                           })
    assert prepared.status_code == 201, prepared.text
    item = prepared.json()
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context",
                         headers=rep, params={"action": "task_approve_execute",
                                              "target_id": item["task_id"]})
    approved = client.post(f"/api/war-room/tasks/{item['task_id']}/approve-execute",
                           headers={**rep, "Idempotency-Key": "result-revalidation-approve"},
                           json={"expires_at": int(time.time()) + 3600,
                                 "context_token": context.json()["context_token"]})
    assert approved.status_code == 200, approved.text
    approval = approved.json()
    database = root / "war-room.sqlite3"
    evidence_path = tmp_path / "revalidation-evidence.txt"
    evidence_path.write_text("RESULT_REVALIDATION_PASS", encoding="utf-8")
    adapter = RevalidationQAAdapter(evidence_path)
    runtime = war_room_runtime.WarRoomRuntime(adapter=adapter)
    monkeypatch.setattr(war_room_runtime, "_RUNTIME", runtime)
    core_run_id = "core-result-revalidation"
    original_raw = "ORIGINAL_FAIL_RAW_RESPONSE"
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        con.execute("UPDATE war_tasks SET status='running' WHERE id=?",
                    (item["task_id"],))
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (item["task_id"],)).fetchone()
        con.execute("""INSERT INTO war_processing_issues
            (task_id,task_revision,delivery_id,stage,code,state,created_at,updated_at)
            VALUES (?,?,?,'RESULT','RESULT_VALIDATION_REQUIRED','OPEN',?,?)""",
                    (item["task_id"], task["revision"], approval["deliveries"][0]["delivery_id"], int(time.time()), int(time.time())))
        con.execute("""INSERT INTO war_execution_runs
            (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,session_key,run_status,
             result_summary,raw_response,policy_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (core_run_id, "plachem-agent-war-room", item["task_id"], "ERPmanager",
                     "openclaw-result-revalidation", "agent:erpmanager:result-revalidation",
                     "FAIL", "original summary", original_raw, "accepted", int(time.time()), int(time.time())))
        con.execute("UPDATE war_deliveries SET run_id=? WHERE id=?",
                    (core_run_id, approval["deliveries"][0]["delivery_id"]))
        packet = json.loads(con.execute(
            "SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (item["task_id"],)
        ).fetchone()[0])
        required = actions._normalize_required_evidence(packet.get("required_evidence", ["test", "artifact"]))
        for required_item in required:
            con.execute("""INSERT INTO war_evidence
                (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,
                 qa_cycle,immutable,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"original-{required_item['id']}", item["task_id"],
                         required_item.get("evidence_type") or required_item["id"], str(evidence_path),
                         "original immutable RESULT evidence", task["revision"],
                         __import__("hashlib").sha256(task["scope"].encode()).hexdigest(),
                         task["document_version"], task["qa_cycle"], 1, int(time.time())))
        worker_runs_before = con.execute("SELECT COUNT(*) FROM war_execution_runs").fetchone()[0]

    rep = {"X-Authenticated-Principal": "human-representative",
           "X-War-Room-Proxy-Secret": "fixture-proxy-secret"}
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context",
                         headers=rep, params={"action": "task_revalidate_result", "target_id": item["task_id"]})
    assert context.status_code == 200, context.text
    body = {"context_token": context.json()["context_token"], "contract_version": 1,
            "project_id": "plachem-agent-war-room", "task_id": item["task_id"], "task_revision": 1,
            "core_run_id": core_run_id}

    denied = client.post(f"/api/war-room/tasks/{item['task_id']}/revalidate-result",
                         headers={"Idempotency-Key": "result-revalidation-denied"}, json=body)
    assert denied.status_code == 401
    tampered = client.post(f"/api/war-room/tasks/{item['task_id']}/revalidate-result",
                           headers={**rep, "Idempotency-Key": "result-revalidation-tampered"},
                           json={**body, "task_revision": 99})
    assert tampered.status_code == 409
    foreign = client.post(f"/api/war-room/tasks/{item['task_id']}/revalidate-result",
                          headers={**rep, "Idempotency-Key": "result-revalidation-foreign"},
                          json={**body, "core_run_id": "foreign-core-run"})
    assert foreign.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_execution_runs SET cancel_reason='cancelled-by-stop' WHERE core_run_id=?",
                    (core_run_id,))
    cancelled = client.post(f"/api/war-room/tasks/{item['task_id']}/revalidate-result",
                            headers={**rep, "Idempotency-Key": "result-revalidation-cancelled"},
                            json=body)
    assert cancelled.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_execution_runs SET cancel_reason=NULL WHERE core_run_id=?",
                    (core_run_id,))

    def submit(key):
        return client.post(f"/api/war-room/tasks/{item['task_id']}/revalidate-result",
                           headers={**rep, "Idempotency-Key": key}, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["result-revalidation-a", "result-revalidation-b"]))
    successful = [response for response in responses if response.status_code == 200]
    successful_key = ["result-revalidation-a", "result-revalidation-b"][
        [response.status_code for response in responses].index(200)
    ]
    assert len(successful) == 1, [(response.status_code, response.text) for response in responses]
    assert any(response.status_code == 409 for response in responses)
    assert successful[0].json()["qa_delivery_queued"] is True
    assert successful[0].json()["worker_redispatched"] is False
    replay = client.post(f"/api/war-room/tasks/{item['task_id']}/revalidate-result",
                         headers={**rep, "Idempotency-Key": successful_key}, json=body)
    assert replay.status_code == 200 and replay.json() == successful[0].json()

    for _ in range(20):
        runtime.tick(db_path=database)
        with sqlite3.connect(database) as con:
            verdict = con.execute("SELECT verdict FROM war_qa_verdicts WHERE task_id=?",
                                  (item["task_id"],)).fetchone()
        if verdict:
            break
        time.sleep(0.01)
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (item["task_id"],)).fetchone()
        run = con.execute("SELECT * FROM war_execution_runs WHERE core_run_id=?", (core_run_id,)).fetchone()
        qa_count = con.execute(
            "SELECT COUNT(*) FROM war_deliveries WHERE task_revision=? AND agent_id='ERPqa'",
            (task["revision"],),
        ).fetchone()[0]
        history_count = con.execute(
            "SELECT COUNT(*) FROM war_result_revalidation_history WHERE task_id=?",
            (item["task_id"],),
        ).fetchone()[0]
        worker_runs_after = con.execute("SELECT COUNT(*) FROM war_execution_runs").fetchone()[0]
    if verdict != ("PASS",):
        with sqlite3.connect(database) as debug_con:
            debug_rows = debug_con.execute(
                "SELECT agent_id,status,error_code FROM war_deliveries WHERE message_id=?",
                (item["message_id"],),
            ).fetchall()
        pytest.fail(f"QA did not PASS; calls={adapter.qa_deliveries}; deliveries={debug_rows}")
    assert task["status"] == "qa" and task["revision"] == 1
    assert run["run_status"] == "FAIL" and run["raw_response"] == original_raw
    assert history_count == 1 and qa_count == 1
    assert len(adapter.revalidate_calls) == 1 and len(adapter.qa_deliveries) == 1
    assert worker_runs_after == worker_runs_before
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        final_task = con.execute("SELECT * FROM war_tasks WHERE id=?", (item["task_id"],)).fetchone()
        assert actions._representative_completion_checks(con, final_task)["missing"] == []
    runtime.close()
