import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from war_room_adapter import DeliveryReceipt
import war_room_runtime
from test_production_authorization_wiring import production


class RecoveryAdapter:
    def __init__(self, artifact: Path):
        self.artifact = artifact
        self.deliveries = []

    def create_disposable_session(self, *, agent_id, project_id):
        return {"session_key": f"agent:{agent_id.lower()}:war-room-test:qa-recovery",
                "session_id": "qa-recovery-session", "purpose": "test", "disposable": True}

    def deliver(self, *, delivery_id, agent_id, instruction_id, body):
        self.deliveries.append((delivery_id, agent_id))
        packet = json.loads(body.split("[IMMUTABLE_GROUNDING_PACKET]\n", 1)[1].split(
            "\n[ORIGINAL_INSTRUCTION_CONTEXT]", 1)[0])
        result = {"confirmed_worktree": packet["worktree"], "confirmed_revision": packet["revision"],
                  "verdict": "PASS", "summary": "Recovered QA PASS", "evidence": [str(self.artifact)],
                  "representative_completion_claimed": False}
        return DeliveryReceipt(delivery_id, "responded", run_id="qa-recovery-run",
                               response_body=json.dumps(result))

    def revalidate_result(self, core_run_id):
        raise AssertionError("result revalidation is not part of QA recovery")

    def close(self):
        return None


def test_http_false_qa_rework_recovery_preserves_verdict_and_queues_only_qa(
    production, monkeypatch, tmp_path
):
    root, client, _, _ = production
    monkeypatch.setenv("PLACHEM_WAR_ROOM_AUTO_QA", "1")
    monkeypatch.setenv("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", "qa-recovery-secret")
    from test_war_room_task_contract_v2 import INSTRUCTION

    main = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main",
            "Idempotency-Key": "qa-recovery-prepare"}
    prepared = client.post("/api/war-room/projects/plachem-agent-war-room/prepare", headers=main, json={
        "instruction": INSTRUCTION, "agent_ids": ["ERPmanager"], "reviewer_agent_id": "ERPqa",
        "execution_mode": "LEGACY", "document_version": "baseline-2026-08-23",
        "deadline_at": int(time.time()) + 9000,
        "grounding": {"worktree": str(root), "forbidden": ["production DB"],
                       "required_evidence": [{"id": "artifact", "evidence_type": "artifact",
                                              "source_command": "cat result.txt", "expected_contains": "PASS"}]},
    })
    assert prepared.status_code == 201, prepared.text
    item = prepared.json()
    rep = {"X-Authenticated-Principal": "human-representative",
           "X-War-Room-Proxy-Secret": "fixture-proxy-secret"}
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                         params={"action": "task_approve_execute", "target_id": item["task_id"]})
    approved = client.post(f"/api/war-room/tasks/{item['task_id']}/approve-execute", headers={**rep,
        "Idempotency-Key": "qa-recovery-approve"}, json={"expires_at": int(time.time()) + 3600,
        "context_token": context.json()["context_token"]})
    assert approved.status_code == 200, approved.text
    worker_delivery = approved.json()["deliveries"][0]["delivery_id"]
    artifact = tmp_path / "result.txt"
    artifact.write_text("PASS\n", encoding="utf-8")
    database = root / "war-room.sqlite3"
    now = int(time.time())
    core_run_id = "war-original-worker-pass"
    openclaw_run_id = "original-openclaw"
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (item["task_id"],)).fetchone()
        con.execute("UPDATE war_tasks SET status='rework_required',revision=2,qa_cycle=1 WHERE id=?",
                    (item["task_id"],))
        con.execute("""UPDATE war_deliveries SET status='responded',run_id=?,session_key=?,session_id=?,
                    response_message_id=? WHERE id=?""",
                    (openclaw_run_id, None, "original-session", item["message_id"], worker_delivery))
        con.execute("""INSERT INTO war_execution_runs
            (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,session_key,run_status,
             result_summary,raw_response,policy_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (core_run_id, "plachem-agent-war-room", item["task_id"], "ERPmanager", openclaw_run_id,
                     "agent:erpmanager:original", "PASS", "Worker PASS", "ORIGINAL_WORKER_RESPONSE", "accepted", now, now))
        scope_hash = hashlib.sha256(task["scope"].encode()).hexdigest()
        evidence_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        con.execute("""INSERT INTO war_evidence
            (id,task_id,evidence_type,uri,summary,sha256,task_revision,scope_hash,document_version,qa_cycle,
             run_id,source_command,expected_contains,immutable,contract_evidence_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("original-evidence", item["task_id"], "artifact", str(artifact), "original", evidence_sha,
                     1, scope_hash, task["document_version"], 1, core_run_id, "cat result.txt", "PASS", 1,
                     "artifact", now))
        con.execute("""INSERT INTO war_qa_verdicts
            (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,task_revision,
             scope_hash,document_version,qa_cycle,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("false-qa-verdict", item["task_id"], "ERPqa", "REWORK", "required:artifact", "sig",
                     "signed-original", 1, scope_hash, task["document_version"], 1, now))
        con.execute("""INSERT INTO war_processing_issues
            (task_id,task_revision,delivery_id,stage,code,state,created_at,updated_at)
            VALUES (?,?,?,'QA','QA_RECEIPT_PATH_FALSE_REWORK','OPEN',?,?)""",
                    (item["task_id"], 2, worker_delivery, now, now))
        qa_response_id = "qa-response-original"
        qa_delivery_id = "qa-delivery-original"
        packet = json.loads(con.execute(
            "SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (item["task_id"],)
        ).fetchone()[0])
        con.execute("""INSERT INTO war_messages
            (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,
             correlation_id,redaction_state,original_body)
            VALUES (?,?, 'result','agent','ERPqa',?,?,?,'qa-correlation','clean',?)""",
                    (qa_response_id, "plachem-agent-war-room", json.dumps({
                        "confirmed_worktree": packet["worktree"], "confirmed_revision": packet["revision"],
                        "verdict": "REWORK", "summary": "The listed execution receipt path does not exist.",
                        "evidence": [str(artifact)], "representative_completion_claimed": False,
                    }), item["message_id"], now, json.dumps({
                        "confirmed_worktree": packet["worktree"], "confirmed_revision": packet["revision"],
                        "verdict": "REWORK", "summary": "The listed execution receipt path does not exist.",
                        "evidence": [str(artifact)], "representative_completion_claimed": False,
                    })))
        con.execute("""INSERT INTO war_deliveries
            (id,message_id,agent_id,task_revision,status,attempt_count,max_attempts,response_message_id,
             run_id,session_key,session_id,created_at,deadline_at)
            VALUES (?,?,?,1,'responded',1,3,?,?,?,?,?,?)""",
                    (qa_delivery_id, item["message_id"], "ERPqa", qa_response_id, "qa-run",
                     "agent:erpqa:original", "qa-session", now, now + 3600))
        con.commit()

    adapter = RecoveryAdapter(artifact)
    monkeypatch.setattr(war_room_runtime, "_RUNTIME", war_room_runtime.WarRoomRuntime(adapter=adapter))
    candidate = client.get(f"/api/war-room/tasks/{item['task_id']}", headers=rep)
    assert candidate.status_code == 200, candidate.text
    assert candidate.json()["task"]["qa_recovery"] == {
        "eligible": True, "core_run_id": core_run_id, "qa_verdict_id": "false-qa-verdict",
        "recovery_code": "QA_FALSE_REWORK_RECEIPT_PATH",
    }
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                         params={"action": "task_resume_qa", "target_id": item["task_id"]})
    assert context.status_code == 200, context.text
    body = {"context_token": context.json()["context_token"], "contract_version": 1,
            "project_id": "plachem-agent-war-room", "task_id": item["task_id"], "task_revision": 2,
            "core_run_id": core_run_id, "qa_verdict_id": "false-qa-verdict",
            "recovery_code": "QA_FALSE_REWORK_RECEIPT_PATH"}

    tampered = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                           headers={**rep, "Idempotency-Key": "qa-recovery-tampered"},
                           json={**body, "task_revision": 1})
    assert tampered.status_code == 409
    foreign = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                          headers={**rep, "Idempotency-Key": "qa-recovery-foreign"},
                          json={**body, "core_run_id": "foreign-run"})
    assert foreign.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_deliveries SET session_key='agent:foreign:mismatch' WHERE id=?", (worker_delivery,))
    session_mismatch = client.post(
        f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
        headers={**rep, "Idempotency-Key": "qa-recovery-session-mismatch"}, json=body,
    )
    assert session_mismatch.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_deliveries SET session_key=NULL WHERE id=?", (worker_delivery,))
        con.execute("UPDATE war_deliveries SET run_id='foreign-openclaw' WHERE id=?", (worker_delivery,))
    run_mismatch = client.post(
        f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
        headers={**rep, "Idempotency-Key": "qa-recovery-run-mismatch"}, json=body,
    )
    assert run_mismatch.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_deliveries SET run_id=? WHERE id=?", (openclaw_run_id, worker_delivery))
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_execution_runs SET cancel_reason='stop-cancelled' WHERE core_run_id=?", (core_run_id,))
    cancelled = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                            headers={**rep, "Idempotency-Key": "qa-recovery-cancelled"}, json=body)
    assert cancelled.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_execution_runs SET cancel_reason=NULL WHERE core_run_id=?", (core_run_id,))

    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_processing_issues SET code='QA_SUBSTANTIVE_REWORK' WHERE task_id=?", (item["task_id"],))
    substantive = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                              headers={**rep, "Idempotency-Key": "qa-recovery-substantive"}, json=body)
    assert substantive.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_processing_issues SET code='QA_RECEIPT_PATH_FALSE_REWORK' WHERE task_id=?", (item["task_id"],))
        con.execute("UPDATE war_approvals SET expires_at=? WHERE task_id=?", (int(time.time()) - 1, item["task_id"]))
    expired = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                          headers={**rep, "Idempotency-Key": "qa-recovery-expired"}, json=body)
    assert expired.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_approvals SET expires_at=?", (int(time.time()) + 3600,))
        con.execute("UPDATE war_approvals SET target_set_hash='mismatch' WHERE task_id=?", (item["task_id"],))
    target_mismatch = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                                  headers={**rep, "Idempotency-Key": "qa-recovery-target-mismatch"}, json=body)
    assert target_mismatch.status_code == 409
    with sqlite3.connect(database) as con:
        con.execute("UPDATE war_approvals SET target_set_hash='' WHERE task_id=?", (item["task_id"],))
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                         params={"action": "task_resume_qa", "target_id": item["task_id"]})
    assert context.status_code == 200, context.text
    body["context_token"] = context.json()["context_token"]

    def submit(key):
        return client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                           headers={**rep, "Idempotency-Key": key}, json=body)

    concurrent_keys = ["qa-recovery-a", "qa-recovery-b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, concurrent_keys))
    successful = [response for response in responses if response.status_code == 200]
    assert len(successful) == 1, [(response.status_code, response.text) for response in responses]
    assert any(response.status_code == 409 for response in responses)
    result = successful[0].json()
    assert result["worker_redispatched"] is False and result["qa_delivery_queued"] is True

    successful_key = next(
        key for key, response in zip(concurrent_keys, responses)
        if response.status_code == 200
    )
    replay = client.post(f"/api/war-room/tasks/{item['task_id']}/recover-qa-rework",
                         headers={**rep, "Idempotency-Key": successful_key}, json=body)
    assert replay.status_code == 200
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        task = con.execute("SELECT * FROM war_tasks WHERE id=?", (item["task_id"],)).fetchone()
        old_verdict = con.execute("SELECT verdict,task_revision,qa_cycle FROM war_qa_verdicts WHERE id='false-qa-verdict'").fetchone()
        qa_count = con.execute("SELECT COUNT(*) FROM war_deliveries WHERE task_revision=2 AND agent_id='ERPqa'",
                               ).fetchone()[0]
        worker_count = con.execute("SELECT COUNT(*) FROM war_deliveries WHERE task_revision=2 AND agent_id='ERPmanager'").fetchone()[0]
        copied = con.execute("SELECT sha256,run_id,immutable FROM war_evidence WHERE task_id=? AND task_revision=2 AND qa_cycle=2",
                             (item["task_id"],)).fetchall()
    assert task["status"] == "qa" and task["qa_cycle"] == 2 and task["revision"] == 2
    assert tuple(old_verdict) == ("REWORK", 1, 1)
    assert qa_count == 1 and worker_count == 0 and len(copied) == 1
    assert copied[0]["sha256"] == evidence_sha and copied[0]["run_id"] == core_run_id and copied[0]["immutable"] == 1

    for _ in range(30):
        war_room_runtime._RUNTIME.tick(db_path=database)
        with sqlite3.connect(database) as con:
            verdict = con.execute("SELECT verdict FROM war_qa_verdicts WHERE task_id=? AND qa_cycle=2",
                                  (item["task_id"],)).fetchone()
        if verdict:
            break
    assert verdict == ("PASS",)
    assert len(adapter.deliveries) == 1 and adapter.deliveries[0][1] == "ERPqa"
    war_room_runtime._RUNTIME.close()
