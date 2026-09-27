from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import contextmanager
import gc
import hashlib
import hmac
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


@contextmanager
def _test_db(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class WarRoomControlledApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.old_db = os.environ.get("PLACHEM_WAR_ROOM_DB")
        self.old_home = os.environ.get("OPENCLAW_HOME")
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(root / "war-room.sqlite3")
        os.environ["OPENCLAW_HOME"] = str(root / "openclaw")
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = '{"main":"fixture-main-token","ERPcoder":"fixture-erpcoder-token","ERPmanager":"fixture-erpmanager-token","ERPqa":"fixture-erpqa-token"}'
        os.environ["PLACHEM_WAR_ROOM_TEST_ADAPTER"] = "1"
        os.environ["PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS"] = "main"
        os.environ["PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE"] = "1"
        os.environ["PLACHEM_WAR_ROOM_QA_SIGNING_SECRET"] = "fixture-qa-secret"
        import war_room
        war_room.provision_database()
        from app import app
        self.client = TestClient(app)
        self.client.__enter__()

    def task_body(self, scope: str, **values):
        body = {
            "scope": scope,
            "assignee_agent_id": "ERPcoder",
            "call_limit": 1,
            "turn_limit": 1,
            "deadline_at": int(time.time()) + 3600,
            "document_version": "baseline-2026-08-23",
        }
        body.update(values)
        return body

    def instruction_body(self, text: str, scope: str, **values):
        body = self.task_body(scope, **values)
        body["body"] = text
        return body

    def approval_body(self, decision: str = "approved"):
        body = {"decision": decision}
        if decision == "approved":
            body["expires_at"] = int(time.time()) + 1800
        return body

    def create_instruction(self, base: str, headers: dict[str, str], scope: str, body: str, key: str):
        task = self.client.post(
            base + "/tasks",
            json=self.task_body(scope),
            headers={**headers, "Idempotency-Key": key + "-task"},
        )
        self.assertEqual(201, task.status_code, task.text)
        instruction = self.client.post(
            base + "/instructions",
            json={"task_id": task.json()["task_id"], "body": body},
            headers={**headers, "Idempotency-Key": key + "-instruction"},
        )
        self.assertEqual(201, instruction.status_code, instruction.text)
        return task.json()["task_id"], instruction.json()["message_id"], instruction

    def tearDown(self) -> None:
        if self.old_db is None:
            os.environ.pop("PLACHEM_WAR_ROOM_DB", None)
        else:
            os.environ["PLACHEM_WAR_ROOM_DB"] = self.old_db
        if self.old_home is None:
            os.environ.pop("OPENCLAW_HOME", None)
        else:
            os.environ["OPENCLAW_HOME"] = self.old_home
        os.environ.pop("PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS", None)
        os.environ.pop("PLACHEM_WAR_ROOM_TEST_ADAPTER", None)
        os.environ.pop("PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS", None)
        os.environ.pop("PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE", None)
        os.environ.pop("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", None)
        os.environ.pop("PLACHEM_WAR_ROOM_SESSION_SECRET", None)
        os.environ.pop("PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET", None)
        os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        os.environ.pop("PLACHEM_WAR_ROOM_ADAPTER_COMMAND", None)
        os.environ.pop("PLACHEM_OPENCLAW_CONFIG", None)
        os.environ.pop("PLACHEM_FAST_GATEWAY_AGENTS", None)
        client = getattr(self, "client", None)
        if client is not None:
            client.__exit__(None, None, None)
        gc.collect()
        self.temp_dir.cleanup()

    def test_auth_rbac_idempotency_and_safe_transition(self) -> None:
        base = "/api/war-room/projects/plachem-agent-war-room"
        denied = self.client.post(base + "/tasks", json={"scope": "x"}, headers={"Idempotency-Key": "deny-1"})
        self.assertEqual(401, denied.status_code)
        headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token", "Idempotency-Key": "task-1"}
        task_payload = self.task_body("fixture task")
        created = self.client.post(base + "/tasks", json=task_payload, headers=headers)
        self.assertEqual(201, created.status_code, created.text)
        task_id = created.json()["task_id"]
        replay = self.client.post(base + "/tasks", json=task_payload, headers=headers)
        self.assertEqual(created.json(), replay.json())
        mismatch = self.client.post(base + "/tasks", json={"scope": "different"}, headers=headers)
        self.assertEqual(409, mismatch.status_code)
        transition = self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status": "awaiting_approval"}, headers={**headers, "Idempotency-Key": "transition-1"})
        self.assertEqual(200, transition.status_code, transition.text)
        approved = self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers, "Idempotency-Key": "approval-1"})
        self.assertEqual(200, approved.status_code, approved.text)
        running = self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status": "running"}, headers={**headers, "Idempotency-Key": "run-1"})
        self.assertEqual(200, running.status_code, running.text)
        qa = self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status": "qa"}, headers={**headers, "Idempotency-Key": "qa-1"})
        self.assertEqual(200, qa.status_code, qa.text)
        blocked = self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status": "completed", "qa_result": "PASS"}, headers={**headers, "Idempotency-Key": "complete-blocked"})
        self.assertEqual(403, blocked.status_code)
        evidence = self.client.post(f"/api/war-room/tasks/{task_id}/evidence", json={"uri": "/tmp/fixture.json", "summary": "isolated QA evidence", "evidence_type":"test"}, headers={**headers, "Idempotency-Key": "evidence-1"})
        self.assertEqual(201, evidence.status_code, evidence.text)
        evidence2 = self.client.post(f"/api/war-room/tasks/{task_id}/evidence", json={"uri": "/tmp/fixture-artifact.json", "summary": "isolated artifact", "evidence_type":"artifact"}, headers={**headers, "Idempotency-Key": "evidence-2"})
        self.assertEqual(201, evidence2.status_code, evidence2.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            row = con.execute("SELECT revision,scope,document_version,qa_cycle FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        payload = json.dumps({"task_id":task_id,"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","task_revision":row[0],"scope_hash":hashlib.sha256(row[1].encode()).hexdigest(),"document_version":row[2],"qa_cycle":row[3]}, sort_keys=True)
        signature = hmac.new(b"fixture-qa-secret", payload.encode(), hashlib.sha256).hexdigest()
        spoofed_qa = self.client.post(f"/api/war-room/tasks/{task_id}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","signature":signature}, headers={**headers, "Idempotency-Key":"qa-spoof-1"})
        self.assertEqual(403, spoofed_qa.status_code)
        qa_headers = {"X-War-Room-Actor": "ERPqa", "X-War-Room-Token": "fixture-erpqa-token", "Idempotency-Key":"qa-verdict-1"}
        agent_qa = self.client.post(f"/api/war-room/tasks/{task_id}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","source":"agent_result"}, headers={**qa_headers,"Idempotency-Key":"qa-agent-result"})
        self.assertEqual(200, agent_qa.status_code, agent_qa.text)
        qa = self.client.post(f"/api/war-room/tasks/{task_id}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","signature":signature}, headers=qa_headers)
        self.assertEqual(200, qa.status_code, qa.text)
        qa_replay = self.client.post(f"/api/war-room/tasks/{task_id}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","signature":signature}, headers=qa_headers)
        self.assertEqual(qa.json(), qa_replay.json())
        qa_without_idem = self.client.post(f"/api/war-room/tasks/{task_id}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","signature":signature}, headers={"X-War-Room-Actor":"ERPqa", "X-War-Room-Token":"fixture-erpqa-token"})
        self.assertEqual(400, qa_without_idem.status_code)
        complete = self.client.post(f"/api/war-room/tasks/{task_id}/representative-completion", json={"decision": "approved"}, headers={**headers, "Idempotency-Key": "complete-1"})
        self.assertEqual(200, complete.status_code, complete.text)
        audit = self.client.get(f"/api/war-room/tasks/{task_id}/audit", headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"})
        self.assertEqual(200, audit.status_code)
        self.assertGreaterEqual(len(audit.json()["items"]), 3)
        self.assertTrue(any(item["event_type"] == "qa_verdict_recorded" for item in audit.json()["items"]))

    def test_execution_endpoint_does_not_call_external_agent(self) -> None:
        response = self.client.post("/api/war-room/tasks/not-a-task/execute", headers={"X-War-Room-Actor": "main", "Idempotency-Key": "execute-1"})
        self.assertIn(response.status_code, {401, 403, 404})

    def test_integrated_prepare_is_atomic_and_idempotent(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token", "Idempotency-Key":"prepare-one"}
        payload = {
            "instruction":"원자 준비 테스트", "agent_ids":["ERPcoder","ERPmanager"],
            "deadline_at":int(time.time()) + 1800,
            "document_version":"baseline-2026-08-23",
        }
        url = "/api/war-room/projects/plachem-agent-war-room/prepare"
        first = self.client.post(url, json=payload, headers=headers)
        self.assertEqual(201, first.status_code, first.text)
        self.assertEqual("awaiting_approval", first.json()["status"])
        replay = self.client.post(url, json=payload, headers=headers)
        self.assertEqual(first.json(), replay.json())
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_tasks WHERE id=?", (first.json()["task_id"],)).fetchone()[0])
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_messages WHERE id=?", (first.json()["message_id"],)).fetchone()[0])
        bad = self.client.post(url, json={**payload,"agent_ids":["not-agent"]}, headers={**headers,"Idempotency-Key":"prepare-bad"})
        self.assertEqual(422, bad.status_code)

    def test_three_agent_prepare_auto_limits_and_manager_observer(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token", "Idempotency-Key":"prepare-three"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"세 에이전트 협업", "agent_ids":["ERPcoder","ERPmanager","main"], "deadline_at":int(time.time())+1800, "document_version":"baseline-2026-08-23"},
            headers=headers,
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            task = con.execute("SELECT call_limit,turn_limit FROM war_tasks WHERE id=?", (prepared.json()["task_id"],)).fetchone()
            manager = con.execute("SELECT role,can_read,can_comment,can_approve,can_execute FROM war_participants WHERE project_id=? AND principal_id='ERPmanager'", ("plachem-agent-war-room",)).fetchone()
        self.assertEqual((3, 3), task)
        self.assertEqual(("observer", 1, 1, 0, 0), manager)
        run = self.client.post(f"/api/war-room/tasks/{prepared.json()['task_id']}/approve-execute", json={"expires_at":int(time.time())+1200}, headers={**headers,"Idempotency-Key":"run-three"})
        self.assertEqual(200, run.status_code, run.text)
        self.assertEqual(["ERPcoder","ERPmanager","main"], [row["agent_id"] for row in run.json()["deliveries"]])

    def test_runtime_provisions_exact_disposable_sessions_for_three_agents(self) -> None:
        from war_room_runtime import provision_disposable_sessions
        class FixtureAdapter:
            def __init__(self): self.calls=[]
            def create_disposable_session(self, *, agent_id, project_id):
                self.calls.append((agent_id, project_id))
                return {"session_key":f"agent:{agent_id.lower()}:war-room-test:fixture", "session_id":f"session-{agent_id}", "purpose":"test", "disposable":True}
        adapter = FixtureAdapter()
        result = provision_disposable_sessions(db_path=Path(os.environ["PLACHEM_WAR_ROOM_DB"]), adapter=adapter, project_id="plachem-agent-war-room", agent_ids=["ERPcoder","ERPqa","ERPmanager"])
        self.assertEqual([("ERPcoder","plachem-agent-war-room"),("ERPqa","plachem-agent-war-room"),("ERPmanager","plachem-agent-war-room")], adapter.calls)
        self.assertEqual(3, len(result))
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            rows = con.execute("SELECT agent_id,purpose,disposable,enabled FROM war_project_sessions ORDER BY agent_id").fetchall()
        self.assertEqual([("ERPcoder","test",1,1),("ERPmanager","test",1,1),("ERPqa","test",1,1)], rows)

    def test_integrated_approve_execute_is_atomic_and_idempotent(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"통합 실행 테스트","agent_ids":["ERPcoder","ERPmanager"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
            headers={**headers,"Idempotency-Key":"integrated-prepare"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        url = f"/api/war-room/tasks/{prepared.json()['task_id']}/approve-execute"
        body = {"expires_at":int(time.time())+1200}
        first = self.client.post(url, json=body, headers={**headers,"Idempotency-Key":"integrated-run"})
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("running", first.json()["status"])
        self.assertEqual(2, len(first.json()["deliveries"]))
        replay = self.client.post(url, json=body, headers={**headers,"Idempotency-Key":"integrated-run"})
        self.assertEqual(first.json(), replay.json())
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual(2, con.execute("SELECT COUNT(*) FROM war_deliveries WHERE message_id=?", (prepared.json()["message_id"],)).fetchone()[0])

    def test_responded_without_body_is_failed_and_all_results_enter_qa(self) -> None:
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import process_due_deliveries
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"본문 필수 테스트","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
            headers={**headers,"Idempotency-Key":"body-prepare"},
        ).json()
        self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/approve-execute", json={"expires_at":int(time.time())+1200}, headers={**headers,"Idempotency-Key":"body-run"})
        class EmptyAdapter:
            def deliver(self, **kwargs): return DeliveryReceipt(kwargs["delivery_id"], "responded")
        result = process_due_deliveries(db_path=Path(os.environ["PLACHEM_WAR_ROOM_DB"]), adapter=EmptyAdapter())
        self.assertEqual("retry_scheduled", result[0]["status"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("running", con.execute("SELECT status FROM war_tasks WHERE id=?", (prepared["task_id"],)).fetchone()[0])

        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_deliveries SET status='queued',attempt_count=0,retry_count=0,next_attempt_at=NULL,error_code=NULL WHERE message_id=?", (prepared["message_id"],))
            con.commit()
        class BodyAdapter:
            def deliver(self, **kwargs): return DeliveryReceipt(kwargs["delivery_id"], "responded", response_body=json.dumps({
                "confirmed_worktree":str(Path.cwd()), "confirmed_revision":"baseline-2026-08-23",
                "verdict":"PASS", "evidence":["/evidence/result.json"], "summary":"실제 결과 본문",
                "representative_completion_claimed":False,
            }, ensure_ascii=False))
        result = process_due_deliveries(db_path=Path(os.environ["PLACHEM_WAR_ROOM_DB"]), adapter=BodyAdapter())
        self.assertEqual("responded", result[0]["status"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("qa", con.execute("SELECT status FROM war_tasks WHERE id=?", (prepared["task_id"],)).fetchone()[0])

    def test_terminal_structured_validation_failure_moves_task_to_rework_and_audits(self) -> None:
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import process_due_deliveries
        headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}
        cases = {
            "structured_response_fields_missing": json.dumps({"verdict":"PASS"}),
            "context_mismatch": json.dumps({"confirmed_worktree":"/wrong","confirmed_revision":"wrong","verdict":"PASS","evidence":["/e"],"summary":"x","representative_completion_claimed":False}),
            "representative_authority_exceeded": json.dumps({"confirmed_worktree":str(Path.cwd()),"confirmed_revision":"baseline-2026-08-23","verdict":"PASS","evidence":["/e"],"summary":"x","representative_completion_claimed":True}),
        }
        for index, (expected, body) in enumerate(cases.items()):
            with self.subTest(expected):
                prepared=self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare",json={"instruction":expected,"agent_ids":["ERPcoder","ERPmanager"],"execution_mode":"LEGACY","deadline_at":int(time.time())+900,"document_version":"baseline-2026-08-23"},headers={**headers,"Idempotency-Key":f"terminal-prepare-{index}"}).json()
                self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/approve-execute",json={"expires_at":int(time.time())+800},headers={**headers,"Idempotency-Key":f"terminal-run-{index}"})
                class InvalidAdapter:
                    def deliver(self, **kwargs): return DeliveryReceipt(kwargs["delivery_id"],"responded",response_body=body)
                process_due_deliveries(db_path=Path(os.environ["PLACHEM_WAR_ROOM_DB"]),adapter=InvalidAdapter())
                with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
                    self.assertEqual("rework_required",con.execute("SELECT status FROM war_tasks WHERE id=?",(prepared["task_id"],)).fetchone()[0])
                    self.assertGreaterEqual(con.execute("SELECT COUNT(*) FROM war_audit_events WHERE target_id IN (SELECT id FROM war_deliveries WHERE message_id=?) AND event_type='terminal_response_validation_rework'",(prepared["message_id"],)).fetchone()[0],1)

    def test_session_snapshot_uses_decimal_mtime_and_private_permissions(self) -> None:
        from war_room_session_integrity import compare, write_private_manifest
        payload={"schema":2,"mtime_encoding":"decimal_string","sessions":[{"agent":"ERPqa","session_key":"work","exists":True,"message_count":1,"size":2,"mtime_ns":"1787589999999999999","sha256":"a"*64}]}
        self.assertEqual(0,compare(payload,json.loads(json.dumps(payload)))["changed_count"])
        absent={"schema":2,"mtime_encoding":"decimal_string","sessions":[{"agent":"ERPqa","session_key":"missing","exists":False,"message_count":0,"size":0,"mtime_ns":None,"sha256":"b"*64}]}
        self.assertEqual(0,compare(absent,json.loads(json.dumps(absent)))["deleted_count"])
        target=Path(os.environ["PLACHEM_WAR_ROOM_DB"]).parent/"private"/"manifest.json"
        write_private_manifest(target,payload)
        self.assertTrue(target.parent.is_dir())
        self.assertTrue(target.is_file())
        if os.name == "nt":
            icacls = shutil.which("icacls")
            self.assertIsNotNone(icacls)
            acl = subprocess.run([icacls, str(target.parent)], capture_output=True, check=False)
            self.assertEqual(0, acl.returncode, acl.stderr)
            self.assertTrue(acl.stdout.strip())
        else:
            self.assertEqual(0o700,target.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600,target.stat().st_mode & 0o777)

    def test_session_integrity_required_before_qa_pass_but_not_rejection(self) -> None:
        headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}
        prepared=self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare",json={"instruction":"integrity gate","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+900,"document_version":"baseline-2026-08-23","grounding":{"worktree":str(Path.cwd()),"branch":"x","revision":"r","api_base":"/api","db_label":"isolated","forbidden":["existing work sessions"],"completion_conditions":["integrity"]}},headers={**headers,"Idempotency-Key":"integrity-prepare"}).json()
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET status='qa',qa_cycle=1 WHERE id=?",(prepared["task_id"],)); con.commit()
        for kind in ("test","artifact"):
            self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/evidence",json={"evidence_type":kind,"uri":f"/{kind}","summary":kind},headers={**headers,"Idempotency-Key":f"integrity-{kind}"})
        qa=self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/qa-verdict",json={"verdict":"PASS","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","source":"agent_result"},headers={"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-erpqa-token","Idempotency-Key":"integrity-qa"})
        self.assertEqual(409,qa.status_code,qa.text)
        rejected=self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/representative-completion",json={"decision":"rejected"},headers={**headers,"Idempotency-Key":"integrity-reject"})
        self.assertEqual(200,rejected.status_code,rejected.text)

    def test_representative_completion_is_separate_from_manage_transition(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"대표 완료 테스트","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
            headers={**headers,"Idempotency-Key":"rep-prepare"},
        ).json()
        task_id = prepared["task_id"]
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            scope = con.execute("SELECT scope,document_version,revision,qa_cycle FROM war_tasks WHERE id=?", (task_id,)).fetchone()
            con.execute("UPDATE war_tasks SET status='qa',qa_cycle=1 WHERE id=?", (task_id,))
            scope_hash = hashlib.sha256(scope[0].encode()).hexdigest()
            now = int(time.time())
            con.execute("INSERT INTO war_evidence (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("rep-evidence-test",task_id,"test","/tmp/rep-test","pass",scope[2],scope_hash,scope[1],1,now))
            con.execute("INSERT INTO war_evidence (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("rep-evidence-artifact",task_id,"artifact","/tmp/rep-artifact","pass",scope[2],scope_hash,scope[1],1,now))
            profile = "required:test,artifact"
            signed_payload = json.dumps({"task_id":task_id,"verdict":"PASS","evidence_profile":profile,"qa_principal":"ERPqa","task_revision":scope[2],"scope_hash":scope_hash,"document_version":scope[1],"qa_cycle":1}, sort_keys=True)
            signature = hmac.new(b"fixture-qa-secret", signed_payload.encode(), hashlib.sha256).hexdigest()
            con.execute("INSERT INTO war_qa_verdicts (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("rep-verdict",task_id,"ERPqa","PASS",profile,signature,signed_payload,scope[2],scope_hash,scope[1],1,now))
            con.commit()
        generic = self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"completed"}, headers={**headers,"Idempotency-Key":"rep-generic"})
        self.assertEqual(403, generic.status_code, generic.text)
        for actor, token in (("ERPcoder","fixture-erpcoder-token"),("ERPmanager","fixture-erpmanager-token"),("ERPqa","fixture-erpqa-token")):
            denied = self.client.post(f"/api/war-room/tasks/{task_id}/representative-completion", json={"decision":"approved"}, headers={"X-War-Room-Actor":actor,"X-War-Room-Token":token,"Idempotency-Key":f"rep-denied-{actor}"})
            self.assertEqual(403, denied.status_code, denied.text)
        approved = self.client.post(f"/api/war-room/tasks/{task_id}/representative-completion", json={"decision":"approved"}, headers={**headers,"Idempotency-Key":"rep-approved"})
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertEqual("completed", approved.json()["status"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_representative_approvals WHERE task_id=? AND representative_id='main'", (task_id,)).fetchone()[0])

    def test_qa_fail_moves_task_to_rework_required_with_audit(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"QA 실패 전이 테스트","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
            headers={**headers,"Idempotency-Key":"qa-fail-prepare"},
        ).json()
        task_id = prepared["task_id"]
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET status='qa',qa_cycle=1 WHERE id=?", (task_id,))
            con.commit()
        failed = self.client.post(
            f"/api/war-room/tasks/{task_id}/qa-verdict",
            json={"verdict":"FAIL","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","source":"agent_result"},
            headers={"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-erpqa-token","Idempotency-Key":"qa-fail-verdict"},
        )
        self.assertEqual(200, failed.status_code, failed.text)
        self.assertEqual("rework_required", failed.json()["status"])
        replay = self.client.post(
            f"/api/war-room/tasks/{task_id}/qa-verdict",
            json={"verdict":"FAIL","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","source":"agent_result"},
            headers={"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-erpqa-token","Idempotency-Key":"qa-fail-verdict"},
        )
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual(failed.json(), replay.json())
        conflict = self.client.post(
            f"/api/war-room/tasks/{task_id}/qa-verdict",
            json={"verdict":"REWORK","evidence_profile":"required:test,artifact","qa_principal":"ERPqa","source":"agent_result"},
            headers={"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-erpqa-token","Idempotency-Key":"qa-fail-verdict"},
        )
        self.assertEqual(409, conflict.status_code, conflict.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("rework_required", con.execute("SELECT status FROM war_tasks WHERE id=?", (task_id,)).fetchone()[0])
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_audit_events WHERE target_id=? AND event_type='qa_verdict_recorded'", (task_id,)).fetchone()[0])
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_audit_events WHERE target_id=? AND event_type='qa_verdict_rework_required'", (task_id,)).fetchone()[0])

    def test_representative_rejection_does_not_require_qa_pass_or_evidence(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"대표 반려 테스트","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
            headers={**headers,"Idempotency-Key":"rep-reject-prepare"},
        ).json()
        task_id = prepared["task_id"]
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET status='qa',qa_cycle=1 WHERE id=?", (task_id,))
            original_revision = con.execute("SELECT revision FROM war_tasks WHERE id=?", (task_id,)).fetchone()[0]
            con.commit()
        rejected = self.client.post(
            f"/api/war-room/tasks/{task_id}/representative-completion",
            json={"decision":"rejected"},
            headers={**headers,"Idempotency-Key":"rep-rejected-without-pass"},
        )
        self.assertEqual(200, rejected.status_code, rejected.text)
        self.assertEqual("rework_required", rejected.json()["status"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual(original_revision + 1, con.execute("SELECT revision FROM war_tasks WHERE id=?", (task_id,)).fetchone()[0])

    def test_quick_qa_opens_hidden_advanced_area_on_pc_and_mobile_contract(self) -> None:
        html = (Path(__file__).parents[1] / "static" / "war-room.html").read_text(encoding="utf-8")
        javascript = (Path(__file__).parents[1] / "static" / "war-room-ui.js").read_text(encoding="utf-8")
        self.assertIn('id="advanced-area"', html)
        self.assertIn("area.hidden = false", javascript)
        self.assertIn('document.getElementById("qa-task").focus()', javascript)
        # QA is intentionally integrated into the single task-detail screen.
        self.assertIn('document.getElementById("task-review").scrollIntoView', javascript)
        self.assertIn("@media(max-width:700px)", html)

    def test_quick_form_permissions_are_independent_for_main_pc_and_qa_mobile(self) -> None:
        html = (Path(__file__).parents[1] / "static" / "war-room.html").read_text(encoding="utf-8")
        javascript = (Path(__file__).parents[1] / "static" / "war-room-ui.js").read_text(encoding="utf-8")
        form_tag = html.split('id="quick-task-form"', 1)[1].split(">", 1)[0]
        self.assertNotIn("data-permission", form_tag)
        self.assertIn('id="quick-instruction" data-permission="manage"', html)
        self.assertIn('id="quick-agent-targets" data-permission="manage"', html)
        self.assertIn('id="quick-prepare"', html)
        self.assertIn('id="quick-open-qa"', html)
        self.assertNotIn('id="quick-open-qa" data-permission=', html)
        self.assertIn('id="quick-complete" class="primary" type="button" data-representative="true"', html)
        self.assertIn('document.querySelectorAll("[data-representative]")', javascript)
        main_access = self.client.get("/api/war-room/projects/plachem-agent-war-room/access", headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"})
        qa_access = self.client.get("/api/war-room/projects/plachem-agent-war-room/access", headers={"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-erpqa-token"})
        self.assertTrue(main_access.json()["is_representative"])
        self.assertFalse(qa_access.json()["is_representative"])
        self.assertIn("@media(max-width:700px)", html)

    def test_hardening_stop_qa_link_archive_auth_and_startup(self) -> None:
        import war_room
        from app import provision_war_room_on_startup
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        base = "/api/war-room/projects/plachem-agent-war-room"

        # Authentication is checked before any resource lookup.
        hidden = self.client.post("/api/war-room/tasks/private-missing/transition", json={"status":"running"}, headers={"Idempotency-Key":"oracle"})
        self.assertEqual(401, hidden.status_code)

        # Evidence and verdict lifecycle begins only after entering QA.
        task = self.client.post(base + "/tasks", json=self.task_body("hardening fixture"), headers={**headers,"Idempotency-Key":"hard-task"})
        task_id = task.json()["task_id"]
        early = self.client.post(f"/api/war-room/tasks/{task_id}/evidence", json={"uri":"/tmp/early","summary":"early","evidence_type":"test"}, headers={**headers,"Idempotency-Key":"early-evidence"})
        self.assertEqual(409, early.status_code)

        # One immutable instruction can belong to only one task.
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("hard-source", war_room.PROJECT_ID, "instruction", "agent", "main", "bound", None, None, int(time.time()), "hard-corr", "clean", None))
            con.commit()
        linked = self.client.post(base + "/tasks", json=self.task_body("linked", source_message_id="hard-source"), headers={**headers,"Idempotency-Key":"linked-1"})
        self.assertEqual(201, linked.status_code, linked.text)
        duplicate = self.client.post(base + "/tasks", json=self.task_body("duplicate", source_message_id="hard-source"), headers={**headers,"Idempotency-Key":"linked-2"})
        self.assertEqual(409, duplicate.status_code)

        # ACK cannot manufacture a stop; it must bind to a stopped delivery.
        no_stop = self.client.post(base + "/stop-ack", json={"delivery_id":"missing"}, headers={**headers,"Idempotency-Key":"ack-no-stop"})
        self.assertEqual(409, no_stop.status_code)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            stop_cycle=int(time.time())
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,stop_cycle_at,created_at) VALUES (?,?,?,?,?,?,?)", ("hard-stop-delivery", "hard-source", "ERPcoder", "stopped", 1, stop_cycle, stop_cycle))
            con.execute("UPDATE war_project_control SET stop_requested_at=?,stop_state='stop_requested' WHERE project_id=?", (stop_cycle, war_room.PROJECT_ID))
            con.commit()
        wrong_ack = self.client.post(base + "/stop-ack", json={"delivery_id":"missing"}, headers={**headers,"Idempotency-Key":"ack-wrong"})
        self.assertEqual(409, wrong_ack.status_code)
        ack = self.client.post(base + "/stop-ack", json={"delivery_id":"hard-stop-delivery"}, headers={**headers,"Idempotency-Key":"ack-bound"})
        self.assertEqual(200, ack.status_code, ack.text)
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"awaiting_approval"},headers={**headers,"Idempotency-Key":"stopped-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals",json=self.approval_body(),headers={**headers,"Idempotency-Key":"stopped-approve"})
        blocked_run=self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"running"},headers={**headers,"Idempotency-Key":"stopped-run"})
        self.assertEqual(409,blocked_run.status_code)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET status='qa' WHERE id=?",(task_id,)); con.commit()
        blocked_evidence=self.client.post(f"/api/war-room/tasks/{task_id}/evidence",json={"uri":"/tmp/stopped","summary":"blocked"},headers={**headers,"Idempotency-Key":"stopped-evidence"})
        self.assertEqual(409,blocked_evidence.status_code)

        # Archived projects reject later mutations.
        made = self.client.post("/api/war-room/projects", json={"name":"Archived immutable fixture"}, headers={**headers,"Idempotency-Key":"archive-made"})
        pid = made.json()["project_id"]
        self.client.post(f"/api/war-room/projects/{pid}/archive", json={}, headers={**headers,"Idempotency-Key":"archive-it"})
        blocked = self.client.post(f"/api/war-room/projects/{pid}/tasks", json=self.task_body("blocked", document_version="unknown"), headers={**headers,"Idempotency-Key":"archive-block"})
        self.assertEqual(409, blocked.status_code)

        # Startup provisioning is idempotent and restores a fresh missing DB.
        fresh = Path(self.temp_dir.name) / "fresh-startup.sqlite3"
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(fresh)
        provision_war_room_on_startup(); provision_war_room_on_startup()
        self.assertTrue(fresh.is_file())
        with _test_db(fresh) as con:
            self.assertIsNotNone(con.execute("SELECT 1 FROM war_projects LIMIT 1").fetchone())

    def test_R_WXGTDY_authenticated_gets_and_principal_mismatch_are_blocked(self) -> None:
        """R-WXGTDY: all HTTP War Room GETs require membership and trusted identity."""
        self.assertEqual(401, self.client.get("/api/war-room/projects").status_code)
        self.assertEqual(401, self.client.get("/api/war-room/projects", headers={"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-main-token"}).status_code)
        self.assertEqual(200, self.client.get("/api/war-room/projects", headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}).status_code)

    def test_R_WXGTDY_every_read_route_requires_membership(self) -> None:
        """R-WXGTDY: project/task/timeline/session/audit/evidence reads are all gated."""
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token", "Idempotency-Key":"read-route-task"}
        created = self.client.post("/api/war-room/projects/plachem-agent-war-room/tasks", json=self.task_body("read route fixture"), headers=headers)
        self.assertEqual(201, created.status_code, created.text)
        task_id = created.json()["task_id"]
        paths = [
            "/api/war-room/projects",
            "/api/war-room/projects/plachem-agent-war-room",
            "/api/war-room/projects/plachem-agent-war-room/participants",
            "/api/war-room/projects/plachem-agent-war-room/timeline",
            "/api/war-room/projects/plachem-agent-war-room/operations",
            "/api/war-room/projects/plachem-agent-war-room/manyfast-baseline",
            "/api/war-room/projects/plachem-agent-war-room/tasks",
            f"/api/war-room/tasks/{task_id}",
            f"/api/war-room/tasks/{task_id}/audit",
            f"/api/war-room/tasks/{task_id}/evidence",
            "/api/war-room/projects/plachem-agent-war-room/manyfast-reference",
        ]
        for path in paths:
            self.assertEqual(401, self.client.get(path).status_code, path)
            self.assertIn(self.client.get(path, headers=headers).status_code, {200, 404}, path)

    def test_R_OUKGFB_immutable_instruction_and_test_delivery_adapter(self) -> None:
        headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token", "Idempotency-Key": "message-1"}
        task_id, message_id, created = self.create_instruction("/api/war-room/projects/plachem-agent-war-room", headers, "fixture delivery task", "fixture instruction", "delivery")
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**headers,"Idempotency-Key":"delivery-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers,"Idempotency-Key":"delivery-approve"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"running"}, headers={**headers,"Idempotency-Key":"delivery-run"})
        delivery = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id": "ERPcoder", "task_id": task_id}, headers={**headers, "Idempotency-Key": "delivery-1"})
        self.assertEqual(201, delivery.status_code, delivery.text)
        self.assertEqual("queued", delivery.json()["status"])
        self.assertNotIn("fixture instruction", json.dumps(delivery.json()))

    def test_R_WXGTDY_DB_integrity_and_append_only_audit(self) -> None:
        import war_room
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        with _test_db(db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO war_tasks (id,project_id,scope,status,manyfast_version,created_at,updated_at) VALUES ('bad','missing','x','invalid','v',1,1)")
            con.execute("INSERT INTO war_audit_events VALUES ('audit-1',?,?,?,?,?,?,?,?)", (war_room.PROJECT_ID, "main", "fixture", "project", "x", "{}", "corr", 1))
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM war_audit_events WHERE id='audit-1'")

    def test_R_OUKGFB_delivery_retry_reuses_id_and_no_restart_rerun(self) -> None:
        """R-OUKGFB: retry is explicit, idempotent, and not a gateway restart rerun."""
        import war_room
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import process_due_deliveries, request_delivery_retry

        class ScriptedAdapter:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def deliver(self, *, delivery_id: str, agent_id: str, instruction_id: str, body: str) -> DeliveryReceipt:
                self.calls.append(delivery_id)
                if len(self.calls) == 1:
                    return DeliveryReceipt(delivery_id, "failed", error_code="fixture_gateway_timeout")
                return DeliveryReceipt(delivery_id, "responded", session_id="fixture-session", response_body="retry result")

            def stop(self, *, delivery_id: str, agent_id: str) -> DeliveryReceipt:
                return DeliveryReceipt(delivery_id, "stopped")

        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = 1_700_000_000
        with _test_db(db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute(
                "INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("fixture-retry-message", war_room.PROJECT_ID, "instruction", "agent", "main", "same bytes", None, None, now, "corr-retry", "clean", None),
            )
            con.execute(
                "INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,max_attempts,next_attempt_at,deadline_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("fixture-retry-delivery", "fixture-retry-message", "ERPcoder", "queued", 0, 2, now, now + 30, now),
            )
            con.commit()
        adapter = ScriptedAdapter()
        first = process_due_deliveries(db_path=db, adapter=adapter, now=now)
        self.assertEqual("retry_scheduled", first[0]["status"])
        self.assertEqual(["fixture-retry-delivery"], adapter.calls)
        with _test_db(db) as con:
            row = con.execute("SELECT status,attempt_count FROM war_deliveries WHERE id='fixture-retry-delivery'").fetchone()
            self.assertEqual(("queued", 1), row)
        self.assertFalse(request_delivery_retry(db_path=db, delivery_id="fixture-retry-delivery", now=now + 1))
        second = process_due_deliveries(db_path=db, adapter=adapter, now=now + 3)
        self.assertEqual("responded", second[0]["status"])
        self.assertEqual(["fixture-retry-delivery", "fixture-retry-delivery"], adapter.calls)
        # A fresh worker instance must not replay a terminal delivery.
        self.assertEqual([], process_due_deliveries(db_path=db, adapter=adapter, now=now + 4))
        self.assertEqual(2, len(adapter.calls))

    def test_manual_retry_is_available_after_automatic_cycle_is_exhausted(self) -> None:
        """A human-authorized retry starts a new bounded cycle on the same delivery."""
        import war_room
        base = "/api/war-room/projects/plachem-agent-war-room"
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        task = self.client.post(base + "/tasks", json=self.task_body("manual retry after exhaustion"), headers={**headers, "Idempotency-Key":"manual-cycle-task"}).json()
        instruction = self.client.post(base + "/instructions", json={"task_id":task["task_id"],"body":"manual retry body"}, headers={**headers, "Idempotency-Key":"manual-cycle-instruction"}).json()
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"]); now = int(time.time())
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,max_attempts,retry_count,error_class,error_code,deadline_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("manual-cycle-delivery",instruction["message_id"],"ERPcoder","failed",3,3,3,"system_error","transport_error",now+3600,now))
            con.commit()
        result = self.client.post("/api/war-room/deliveries/manual-cycle-delivery/retry", json={}, headers={**headers, "Idempotency-Key":"manual-cycle-retry"})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("manual-cycle-delivery", result.json()["delivery_id"])
        with sqlite3.connect(db) as con:
            self.assertEqual(("queued", 0, None), con.execute("SELECT status,retry_count,error_class FROM war_deliveries WHERE id='manual-cycle-delivery'").fetchone())

    def test_R_OTWMNJ_stop_ack_timeout_and_adapter_failure_timers(self) -> None:
        """R-OTWMNJ: stop ack wins; expiry is unconfirmed; adapter failure is failed."""
        import war_room
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import process_stop_timers, request_project_stop

        class StopAdapter:
            def __init__(self, receipt: DeliveryReceipt) -> None:
                self.receipt = receipt
                self.calls: list[str] = []

            def deliver(self, **kwargs: str) -> DeliveryReceipt:
                return DeliveryReceipt(kwargs["delivery_id"], "responded")

            def stop(self, *, delivery_id: str, agent_id: str) -> DeliveryReceipt:
                self.calls.append(delivery_id)
                return self.receipt

        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = 1_700_000_100
        with _test_db(db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("fixture-stop-message", war_room.PROJECT_ID, "instruction", "agent", "main", "stop bytes", None, None, now, "corr-stop", "clean", None))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,created_at) VALUES (?,?,?,?,?,?)", ("fixture-ack-delivery", "fixture-stop-message", "ERPcoder", "sent", 1, now))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,created_at) VALUES (?,?,?,?,?,?)", ("fixture-timeout-delivery", "fixture-stop-message", "ERPqa", "sent", 1, now))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,created_at) VALUES (?,?,?,?,?,?)", ("fixture-failure-delivery", "fixture-stop-message", "main", "sent", 1, now))
            con.commit()
        # The worker records per-delivery stop state before the project timer.
        ack_adapter = StopAdapter(DeliveryReceipt("fixture-ack-delivery", "stopped"))
        failure_adapter = StopAdapter(DeliveryReceipt("fixture-failure-delivery", "failed", error_code="fixture_stop_unavailable"))
        request_project_stop(db_path=db, project_id=war_room.PROJECT_ID, actor_id="main", adapter=ack_adapter, now=now, deadline=now + 5, delivery_ids=["fixture-ack-delivery"])
        request_project_stop(db_path=db, project_id=war_room.PROJECT_ID, actor_id="main", adapter=failure_adapter, now=now, deadline=now + 5, delivery_ids=["fixture-failure-delivery"])
        with _test_db(db) as con:
            self.assertEqual("stopped", con.execute("SELECT status FROM war_deliveries WHERE id='fixture-ack-delivery'").fetchone()[0])
            self.assertEqual("failed", con.execute("SELECT status FROM war_deliveries WHERE id='fixture-failure-delivery'").fetchone()[0])
            con.execute("UPDATE war_project_control SET stop_state='stop_requested',stop_deadline=? WHERE project_id=?", (now + 5, war_room.PROJECT_ID))
            con.commit()
        process_stop_timers(db_path=db, now=now + 6)
        with _test_db(db) as con:
            control = con.execute("SELECT stop_state FROM war_project_control WHERE project_id=?", (war_room.PROJECT_ID,)).fetchone()[0]
            self.assertEqual("stop_unconfirmed", control)

    def test_R_NCAFXY_manyfast_reference_change_preserves_existing_approval(self) -> None:
        """Manyfast is optional reference data and cannot rewrite existing work."""
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        created = self.client.post("/api/war-room/projects/plachem-agent-war-room/tasks", json=self.task_body("drift task"), headers={**headers,"Idempotency-Key":"drift-task"})
        task_id = created.json()["task_id"]
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**headers,"Idempotency-Key":"drift-await"})
        approved = self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers,"Idempotency-Key":"drift-approve"})
        self.assertEqual(200, approved.status_code, approved.text)
        drift = self.client.put("/api/war-room/projects/plachem-agent-war-room/manyfast-reference", json={"document_version":"baseline-v2"}, headers={**headers,"Idempotency-Key":"drift-ref"})
        self.assertEqual(200, drift.status_code, drift.text)
        self.assertTrue(drift.json()["drift"])
        self.assertEqual(0, drift.json()["invalidated_tasks"])
        self.assertTrue(drift.json()["existing_tasks_preserved"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            task = con.execute("SELECT status,manyfast_version,document_version,revision FROM war_tasks WHERE id=?", (task_id,)).fetchone()
            self.assertEqual(("approved", "baseline-2026-08-23", "baseline-2026-08-23", 1), task)
            self.assertIsNone(con.execute("SELECT revoked_at FROM war_approvals WHERE task_id=?", (task_id,)).fetchone()[0])

    def test_R_GOAQPQ_project_update_observer_and_deactivate_isolation(self) -> None:
        """R-GOAQPQ/F-XCFFIW: lifecycle, observer read-only, deactivation isolation."""
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        created = self.client.post("/api/war-room/projects", json={"name":"Fixture Observer Project"}, headers={**headers,"Idempotency-Key":"project-create"})
        self.assertEqual(201, created.status_code, created.text)
        project_id = created.json()["project_id"]
        duplicate = self.client.post("/api/war-room/projects", json={"name":"fixture observer project"}, headers={**headers,"Idempotency-Key":"project-duplicate"})
        self.assertEqual(409, duplicate.status_code)
        updated = self.client.patch(f"/api/war-room/projects/{project_id}", json={"name":"Fixture Observer Project Updated","status":"active"}, headers={**headers,"Idempotency-Key":"project-update"})
        self.assertEqual(200, updated.status_code, updated.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertGreaterEqual(con.execute("SELECT COUNT(*) FROM war_audit_events WHERE project_id=?", (project_id,)).fetchone()[0], 2)
        add = self.client.post(f"/api/war-room/projects/{project_id}/participants", json={"principal_id":"ERPqa","role":"developer"}, headers={**headers,"Idempotency-Key":"participant-add"})
        self.assertEqual(201, add.status_code, add.text)
        observer = self.client.patch(f"/api/war-room/projects/{project_id}/participants/ERPqa", json={"role":"observer"}, headers={**headers,"Idempotency-Key":"participant-observer"})
        self.assertEqual(200, observer.status_code, observer.text)
        observer_headers = {"X-War-Room-Actor":"ERPqa","X-War-Room-Token":"fixture-erpqa-token"}
        self.assertEqual(200, self.client.get(f"/api/war-room/projects/{project_id}", headers=observer_headers).status_code)
        access = self.client.get(f"/api/war-room/projects/{project_id}/access", headers=observer_headers)
        self.assertEqual(200, access.status_code, access.text)
        self.assertEqual("observer", access.json()["role"])
        self.assertEqual(["read"], access.json()["permissions"])
        denied_write = self.client.post(f"/api/war-room/projects/{project_id}/tasks", json={"scope":"must deny"}, headers={**observer_headers,"Idempotency-Key":"observer-write"})
        self.assertEqual(403, denied_write.status_code)
        self.assertEqual(403, self.client.post(f"/api/war-room/projects/{project_id}/archive", json={}, headers={**observer_headers,"Idempotency-Key":"observer-archive"}).status_code)
        deactivated = self.client.patch(f"/api/war-room/projects/{project_id}/participants/ERPqa", json={"active":False}, headers={**headers,"Idempotency-Key":"participant-deactivate"})
        self.assertEqual(200, deactivated.status_code, deactivated.text)
        self.assertEqual(403, self.client.get(f"/api/war-room/projects/{project_id}/access", headers=observer_headers).status_code)
        self.assertEqual(403, self.client.get(f"/api/war-room/projects/{project_id}", headers=observer_headers).status_code)
        archived = self.client.post(f"/api/war-room/projects/{project_id}/archive", json={}, headers={**headers,"Idempotency-Key":"project-archive"})
        self.assertEqual(200, archived.status_code, archived.text)
        retained = self.client.get(f"/api/war-room/projects/{project_id}", headers=headers)
        self.assertEqual(200, retained.status_code, retained.text)
        self.assertEqual("archived", retained.json()["project"]["status"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertGreaterEqual(con.execute("SELECT COUNT(*) FROM war_audit_events WHERE project_id=? AND event_type IN ('project_created','project_updated','project_archived')", (project_id,)).fetchone()[0], 3)

    def test_project_create_archive_and_stop_are_idempotent(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        create_headers = {**headers, "Idempotency-Key":"project-idem"}
        first = self.client.post("/api/war-room/projects", json={"name":"Idempotent Project"}, headers=create_headers)
        self.assertEqual(201, first.status_code, first.text)
        replay = self.client.post("/api/war-room/projects", json={"name":"Idempotent Project"}, headers=create_headers)
        self.assertEqual(first.json(), replay.json())
        mismatch = self.client.post("/api/war-room/projects", json={"name":"Different Project"}, headers=create_headers)
        self.assertEqual(409, mismatch.status_code)
        project_id = first.json()["project_id"]
        archive_headers = {**headers, "Idempotency-Key":"archive-idem"}
        archived = self.client.post(f"/api/war-room/projects/{project_id}/archive", json={}, headers=archive_headers)
        self.assertEqual(200, archived.status_code, archived.text)
        self.assertEqual(archived.json(), self.client.post(f"/api/war-room/projects/{project_id}/archive", json={}, headers=archive_headers).json())

    def test_reverse_proxy_session_cookie_drives_headerless_ui_api(self) -> None:
        os.environ["PLACHEM_WAR_ROOM_SESSION_SECRET"] = "fixture-session-secret"
        os.environ["PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET"] = "fixture-proxy-secret"
        page = self.client.get("/war-room", headers={"X-Authenticated-Principal":"main", "X-War-Room-Proxy-Secret":"fixture-proxy-secret"})
        self.assertEqual(200, page.status_code)
        self.assertEqual(200, self.client.get("/api/war-room/projects").status_code)
        created = self.client.post(
            "/api/war-room/projects",
            json={"name":"Headerless Session Project"},
            headers={"Idempotency-Key":"headerless-project"},
        )
        self.assertEqual(201, created.status_code, created.text)
        self.assertNotIn("fixture-main-token", page.text)
        page = self.client.get("/war-room", headers={"X-Authenticated-Principal":"main", "X-War-Room-Proxy-Secret":"fixture-proxy-secret"})
        self.assertEqual(200, page.status_code)
        javascript = (Path(__file__).parents[1] / "static" / "war-room-ui.js").read_text(encoding="utf-8")
        self.assertIn('credentials: "same-origin"', javascript)
        self.assertNotIn("X-War-Room-Token", page.text + javascript)
        self.assertIn("data-permission=\"manage\"", page.text)
        self.assertIn(".shell > * { min-width:0; }", page.text)
        self.assertIn("/access", javascript)
        self.assertEqual(200, self.client.get("/api/war-room/projects").status_code)
        main_access = self.client.get("/api/war-room/projects/plachem-agent-war-room/access")
        self.assertEqual(200, main_access.status_code, main_access.text)
        self.assertIn("execute", main_access.json()["permissions"])

    def test_R_UWPCOI_mutations_require_idempotency_key_and_call_policy(self) -> None:
        """R-UWPCOI: mutation replay safety and bounded call/turn/deadline policy."""
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        no_key = self.client.post("/api/war-room/projects", json={"name":"No Key Project"}, headers=headers)
        self.assertEqual(400, no_key.status_code)
        task_id, message_id, message = self.create_instruction("/api/war-room/projects/plachem-agent-war-room", headers, "bounded execution", "bounded call", "bounded")
        self.assertEqual(201, message.status_code, message.text)
        self.assertEqual(200, self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**headers,"Idempotency-Key":"bounded-await"}).status_code)
        self.assertEqual(200, self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers,"Idempotency-Key":"bounded-approve"}).status_code)
        self.assertEqual(200, self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"running"}, headers={**headers,"Idempotency-Key":"bounded-run"}).status_code)
        first = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"bounded-delivery-1"})
        self.assertEqual(201, first.status_code, first.text)
        second = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"bounded-delivery-2"})
        self.assertEqual(409, second.status_code, second.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET deadline_at=? WHERE id=?", (1, task_id))
            con.commit()
        deadline = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"bounded-delivery-deadline"})
        self.assertEqual(409, deadline.status_code, deadline.text)

    def test_R_ADVFLT_timeline_filters_are_project_scoped(self) -> None:
        """R-ADVFLT: timeline type/author/time filters do not cross project scope."""
        main_headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        qa_headers = {"X-War-Room-Actor":"ERPqa", "X-War-Room-Token":"fixture-erpqa-token"}
        filter_task_id, filter_message_id, instruction = self.create_instruction("/api/war-room/projects/plachem-agent-war-room", main_headers, "filter instruction task", "filter instruction", "filter-scoped")
        opinion = self.client.post("/api/war-room/projects/plachem-agent-war-room/messages", json={"body":"filter opinion","message_type":"opinion"}, headers={**qa_headers,"Idempotency-Key":"filter-opinion"})
        self.assertEqual(201, instruction.status_code, instruction.text)
        self.assertEqual(201, opinion.status_code, opinion.text)
        filtered = self.client.get("/api/war-room/projects/plachem-agent-war-room/timeline?message_type=instruction&author_id=main", headers=main_headers)
        self.assertEqual(200, filtered.status_code, filtered.text)
        self.assertTrue(filtered.json()["items"])
        self.assertTrue(all(item["message_type"] == "instruction" and item["author_id"] == "main" for item in filtered.json()["items"]))

    def test_R_MRBDLT_advanced_timeline_filters(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        base = "/api/war-room/projects/plachem-agent-war-room"
        filter_task_id, filter_message_id, instruction = self.create_instruction(base, headers, "filter delivery task", "filter instruction", "filter-delivery")
        self.assertEqual(201, instruction.status_code, instruction.text)
        opinion = self.client.post(base + "/messages", json={"body":"filter opinion","message_type":"opinion"}, headers={**headers,"Idempotency-Key":"filter-opinion"})
        self.assertEqual(201, opinion.status_code, opinion.text)
        task_id = filter_task_id
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**headers,"Idempotency-Key":"filter-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers,"Idempotency-Key":"filter-approve"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"running"}, headers={**headers,"Idempotency-Key":"filter-run"})
        delivered = self.client.post(f"/api/war-room/messages/{filter_message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"filter-delivery"})
        self.assertEqual(201, delivered.status_code, delivered.text)
        typed = self.client.get(base + "/timeline?message_type=instruction&author_id=main&delivery_status=queued&from_ts=1", headers=headers)
        self.assertEqual(200, typed.status_code, typed.text)
        self.assertEqual([filter_message_id], [row["id"] for row in typed.json()["items"]])
        self.assertEqual(422, self.client.get(base + "/timeline?message_type=invalid", headers=headers).status_code)

    def test_R_UWPCOI_call_count_turn_deadline_target_and_dedupe_policy(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        base = "/api/war-room/projects/plachem-agent-war-room"
        task_id, message_id, message = self.create_instruction(base, headers, "policy task", "policy instruction", "policy")
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**headers,"Idempotency-Key":"policy-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers,"Idempotency-Key":"policy-approve"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"running"}, headers={**headers,"Idempotency-Key":"policy-run"})
        wrong_target = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPqa", "task_id":task_id}, headers={**headers,"Idempotency-Key":"policy-wrong"})
        self.assertEqual(409, wrong_target.status_code)
        first = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"policy-first"})
        self.assertEqual(201, first.status_code, first.text)
        limited = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"policy-second"})
        self.assertEqual(409, limited.status_code)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET deadline_at=? WHERE id=?", (int(time.time()) - 1, task_id))
            con.execute("UPDATE war_task_calls SET call_count=0,turn_count=0 WHERE task_id=?", (task_id,))
            con.commit()
        expired = self.client.post(f"/api/war-room/messages/{message_id}/deliveries", json={"agent_id":"ERPcoder", "task_id":task_id}, headers={**headers,"Idempotency-Key":"policy-expired"})
        self.assertEqual(409, expired.status_code)

    def test_R_NCAFXY_last_good_manyfast_snapshot(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        base = "/api/war-room/projects/plachem-agent-war-room/manyfast-snapshot"
        saved = self.client.post(base, json={"document_version":"v-good","snapshot":{"requirements":9,"token":"must-redact"}}, headers={**headers,"Idempotency-Key":"snapshot-good"})
        self.assertEqual(201, saved.status_code, saved.text)
        current = self.client.get(base, headers=headers)
        self.assertEqual(200, current.status_code, current.text)
        self.assertTrue(current.json()["snapshot"]["is_last_good"])

    def test_real_openclaw_adapter_uses_official_send_and_interrupt_rpc(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter
        class Bridge:
            connection_id = "conn-1"
            def __init__(self): self.calls = []
            def request(self, method, params, timeout_ms=15000):
                self.calls.append((method, params))
                if method == "agent":
                    return {"runId":"run-1", "status":"accepted", "sessionKey":params["sessionKey"]}, self.connection_id
                return {"aborted":True,"runIds":["run-1"]}, self.connection_id
        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"
        try:
            bridge = Bridge()
            adapter = OpenClawSessionAdapter(bridge=bridge)
            adapter.bind_delivery("delivery-real", session_key="agent:erpcoder:war-room-test:fixture", session_id="session-fixture", disposable=True, purpose="test", agent_id="ERPcoder")
            self.assertEqual("received", adapter.deliver(delivery_id="delivery-real", agent_id="ERPcoder", instruction_id="instruction-real", body="same bytes").status)
            self.assertEqual("stopped", adapter.stop(delivery_id="delivery-real", agent_id="ERPcoder").status)
        finally:
            os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        self.assertEqual(["agent","bridge.status","chat.abort"], [call[0] for call in bridge.calls])
        submit = bridge.calls[0][1]
        self.assertEqual("erpcoder", submit["agentId"])
        self.assertEqual(3600, submit["timeout"])
        self.assertEqual({"sessionKey","agentId","message","idempotencyKey","timeout"}, set(submit))

    def test_real_adapter_creates_only_explicit_agent_owned_disposable_session(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        class Bridge:
            connection_id = "conn-1"
            def __init__(self): self.calls = []
            def request(self, method, params, timeout_ms=15000):
                self.calls.append((method, params))
                return {"ok": True, "key": params["key"], "sessionId": "session-disposable"}, self.connection_id

        bridge = Bridge()
        adapter = OpenClawSessionAdapter(bridge=bridge)
        binding = adapter.create_disposable_session(agent_id="ERPcoder", project_id="fixture-project")
        self.assertEqual("session-disposable", binding["session_id"])
        self.assertEqual("test", binding["purpose"])
        self.assertTrue(binding["disposable"])
        self.assertEqual(1, len(bridge.calls))
        self.assertEqual("sessions.create", bridge.calls[0][0])
        self.assertEqual("erpcoder", bridge.calls[0][1]["agentId"])
        self.assertTrue(bridge.calls[0][1]["key"].startswith("agent:erpcoder:war-room-test:"))

    def test_real_adapter_refuses_second_active_run_in_disposable_session(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter
        methods: list[str] = []
        class Bridge:
            connection_id = "conn-1"
            def request(self, method, params, timeout_ms=15000):
                methods.append(method)
                return {"runId":"run-1", "status":"accepted"}, self.connection_id
        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"
        try:
            adapter = OpenClawSessionAdapter(bridge=Bridge())
            adapter.bind_delivery("delivery-active", session_key="agent:erpcoder:war-room-test:active", session_id="session-active", disposable=True, purpose="test")
            receipt = adapter.deliver(delivery_id="delivery-active", agent_id="ERPcoder", instruction_id="instruction", body="must not send")
        finally:
            os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        self.assertIsNone(receipt.error_code)
        self.assertEqual(["agent"], methods)

    def test_poll_recovers_pending_run_from_durable_new_assistant_history(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        class Bridge:
            connection_id = "recovery-connection"
            def request(self, method, params, timeout_ms=15000):
                if method == "agent.wait":
                    return {"status":"timeout"}, self.connection_id
                return {"sessionInfo":{"hasActiveRun":False,"activeRunIds":[]},"messages":[{"role":"assistant","timestamp":100001,"content":[{"type":"text","text":"durable result"}]}]}, self.connection_id

        adapter = OpenClawSessionAdapter(bridge=Bridge())
        adapter.bind_run("durable-run", session_key="agent:erpcoder:war-room-test:durable", session_id="durable-session", disposable=True, purpose="test", started_at=100, agent_id="ERPcoder")
        receipt = adapter.poll(run_id="durable-run", agent_id="ERPcoder")
        self.assertEqual("responded", receipt.status)
        self.assertEqual("durable result", receipt.response_body)

    def test_poll_does_not_recover_history_while_session_has_active_run(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        class Bridge:
            connection_id = "active-connection"
            def request(self, method, params, timeout_ms=15000):
                if method == "agent.wait":
                    return {"status":"pending"}, self.connection_id
                return {"sessionInfo":{"hasActiveRun":True,"activeRunIds":["active-run"]},"messages":[{"role":"assistant","timestamp":100001,"content":"partial"}]}, self.connection_id

        adapter = OpenClawSessionAdapter(bridge=Bridge())
        adapter.bind_run("active-run", session_key="agent:erpcoder:war-room-test:active-poll", session_id="active-session", disposable=True, purpose="test", started_at=100, agent_id="ERPcoder")
        self.assertEqual("received", adapter.poll(run_id="active-run", agent_id="ERPcoder").status)

    def test_poll_rejects_assistant_history_older_than_bound_run(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        class Bridge:
            connection_id = "old-connection"
            def request(self, method, params, timeout_ms=15000):
                if method == "agent.wait":
                    return {"status":"timeout"}, self.connection_id
                return {"sessionInfo":{"hasActiveRun":False,"activeRunIds":[]},"messages":[{"role":"assistant","timestamp":99999,"content":"old result"}]}, self.connection_id

        adapter = OpenClawSessionAdapter(bridge=Bridge())
        adapter.bind_run("new-run", session_key="agent:erpcoder:war-room-test:old-history", session_id="old-session", disposable=True, purpose="test", started_at=100, agent_id="ERPcoder")
        self.assertEqual("received", adapter.poll(run_id="new-run", agent_id="ERPcoder").status)

    def test_poll_history_error_preserves_received_state(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        class Bridge:
            connection_id = "error-connection"
            def request(self, method, params, timeout_ms=15000):
                if method == "agent.wait":
                    return {"status":"pending"}, self.connection_id
                raise RuntimeError("history unavailable")

        adapter = OpenClawSessionAdapter(bridge=Bridge())
        adapter.bind_run("error-run", session_key="agent:erpcoder:war-room-test:history-error", session_id="error-session", disposable=True, purpose="test", started_at=100, agent_id="ERPcoder")
        receipt = adapter.poll(run_id="error-run", agent_id="ERPcoder")
        self.assertEqual("received", receipt.status)
        self.assertIsNone(receipt.error_code)

    def test_abort_requires_same_persistent_gateway_connection_as_send(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        class Bridge:
            def __init__(self) -> None:
                self.connection_id = "conn-1"
                self.calls: list[str] = []
            def request(self, method: str, params: dict, timeout_ms: int = 15000):
                self.calls.append(method)
                if method == "agent":
                    return {"runId":"persistent-run","status":"started"}, self.connection_id
                if method == "bridge.status":
                    return {"connected":True}, self.connection_id
                return {"aborted":True,"runIds":["persistent-run"]}, self.connection_id

        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"
        try:
            bridge = Bridge()
            adapter = OpenClawSessionAdapter(bridge=bridge)
            adapter.bind_delivery("persistent-delivery", session_key="agent:erpcoder:war-room-test:persistent", session_id="persistent-session", disposable=True, purpose="test", agent_id="ERPcoder")
            sent = adapter.deliver(delivery_id="persistent-delivery", agent_id="ERPcoder", instruction_id="instruction", body="safe")
            bridge.connection_id = "conn-2"
            stopped = adapter.stop(delivery_id="persistent-delivery", agent_id="ERPcoder")
        finally:
            os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        self.assertEqual("received", sent.status)
        self.assertEqual("openclaw_owner_connection_lost", stopped.error_code)
        self.assertEqual(["agent","bridge.status"], bridge.calls)

    def test_abort_uses_same_persistent_gateway_connection_and_exact_run_id(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter
        class Bridge:
            connection_id = "same-connection"
            def __init__(self): self.calls = []
            def request(self, method, params, timeout_ms=15000):
                self.calls.append((method, params))
                results = {
                    "agent":{"runId":"same-run","status":"accepted"},
                    "bridge.status":{"connected":True},
                    "chat.abort":{"aborted":True,"runIds":["same-run"]},
                }
                return results[method], self.connection_id
        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"
        try:
            bridge = Bridge(); adapter = OpenClawSessionAdapter(bridge=bridge)
            adapter.bind_delivery("same-delivery", session_key="agent:erpcoder:war-room-test:same", session_id="same-session", disposable=True, purpose="test", agent_id="ERPcoder")
            self.assertEqual("received", adapter.deliver(delivery_id="same-delivery", agent_id="ERPcoder", instruction_id="instruction", body="safe").status)
            self.assertEqual("stopped", adapter.stop(delivery_id="same-delivery", agent_id="ERPcoder").status)
        finally:
            os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        abort = [params for method,params in bridge.calls if method == "chat.abort"]
        self.assertEqual("same-run", abort[0]["runId"])

    def test_service_runtime_keeps_send_and_stop_owner_and_restart_fails_closed(self) -> None:
        import war_room
        from war_room_adapter import DeliveryReceipt
        from war_room_runtime import WarRoomRuntime

        class OwnedAdapter:
            def __init__(self): self.owned = set()
            def bind_delivery(self, delivery_id, **kwargs): pass
            def bind_run(self, run_id, **kwargs): pass
            def deliver(self, *, delivery_id, agent_id, instruction_id, body):
                self.owned.add(delivery_id)
                return DeliveryReceipt(delivery_id,"received",run_id="owned-run")
            def stop(self, *, delivery_id, agent_id):
                if delivery_id not in self.owned:
                    return DeliveryReceipt(delivery_id,"failed",error_code="openclaw_owner_connection_lost")
                return DeliveryReceipt(delivery_id,"stopped",run_id="owned-run")
            def poll(self, **kwargs): return DeliveryReceipt(kwargs["run_id"],"received",run_id=kwargs["run_id"])

        db=Path(os.environ["PLACHEM_WAR_ROOM_DB"]); now=1_700_000_000
        with _test_db(db) as con:
            con.execute("INSERT INTO war_project_sessions(project_id,agent_id,session_key,session_id,enabled,purpose,disposable) VALUES (?,?,?,?,1,'test',1)",(war_room.PROJECT_ID,"ERPcoder","agent:erpcoder:war-room-test:runtime","runtime-session"))
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",("runtime-message",war_room.PROJECT_ID,"instruction","agent","main","safe",None,None,now,"runtime-corr","clean",None))
            con.execute("INSERT INTO war_deliveries(id,message_id,agent_id,status,attempt_count,max_attempts,next_attempt_at,deadline_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",("runtime-delivery","runtime-message","ERPcoder","queued",0,3,now,now+60,now))
            con.commit()
        runtime=WarRoomRuntime(adapter=OwnedAdapter())
        self.assertEqual("received",runtime.tick(db_path=db,now=now)[0]["status"])
        self.assertEqual("stopped",runtime.stop_project(db_path=db,project_id=war_room.PROJECT_ID,actor_id="main",now=now+1)["deliveries"][0]["status"])
        with _test_db(db) as con:
            con.execute("UPDATE war_deliveries SET status='received',run_id='owned-run' WHERE id='runtime-delivery'"); con.commit()
        restarted=WarRoomRuntime(adapter=OwnedAdapter())
        self.assertEqual("failed",restarted.stop_project(db_path=db,project_id=war_room.PROJECT_ID,actor_id="main",now=now+2)["deliveries"][0]["status"])

    def test_fresh_real_adapter_recovery_requires_delivery_scoped_binding_before_poll(self) -> None:
        """Restart recovery must use the delivery's original session, never the current project session."""
        import war_room
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import recover_received_deliveries

        class FreshAdapter:
            def __init__(self) -> None:
                self.bound: dict[str, tuple[str, str | None]] = {}
            def bind_run(self, run_id: str, *, session_key: str, session_id: str | None, disposable: bool, purpose: str) -> None:
                if not disposable or purpose != "test":
                    raise ValueError("unsafe")
                self.bound[run_id] = (session_key, session_id)
            def poll(self, *, run_id: str, agent_id: str) -> DeliveryReceipt:
                if run_id not in self.bound:
                    return DeliveryReceipt(run_id, "failed", error_code="openclaw_run_session_missing", run_id=run_id)
                return DeliveryReceipt(run_id, "responded", session_id=self.bound[run_id][1], run_id=run_id, response_body="fresh worker result")

        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = 1_700_000_000
        with _test_db(db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("INSERT INTO war_project_sessions(project_id,agent_id,session_key,session_id,enabled,purpose,disposable) VALUES (?,?,?,?,1,'test',1)", (war_room.PROJECT_ID,"ERPcoder","agent:erpcoder:war-room-test:fixture","session-disposable"))
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("restart-message",war_room.PROJECT_ID,"instruction","agent","main","safe test",None,None,now,"restart-corr","clean",None))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,max_attempts,deadline_at,created_at,run_id) VALUES (?,?,?,?,?,?,?,?,?)", ("restart-delivery","restart-message","ERPcoder","received",1,3,now+30,now,"run-restart"))
            con.commit()
        adapter = FreshAdapter()

        # Project-level binding alone is not sufficient for restart recovery.
        result = recover_received_deliveries(db_path=db, gateway=adapter, now=now+1)
        self.assertEqual([{"delivery_id":"restart-delivery","run_id":"run-restart","status":"failed"}], result)
        self.assertNotIn("run-restart", adapter.bound)

        # Once the original delivery-scoped binding is present, recovery may
        # safely rebind the run and collect its terminal response.
        with sqlite3.connect(db) as con:
            con.execute(
                """UPDATE war_deliveries
                   SET status='received',error_code=NULL,error_class=NULL,
                       session_key=?,session_id=?
                   WHERE id='restart-delivery'""",
                ("agent:erpcoder:war-room-test:original","session-original"),
            )
            con.commit()
        result = recover_received_deliveries(db_path=db, gateway=adapter, now=now+2)
        self.assertEqual([{"delivery_id":"restart-delivery","run_id":"run-restart","status":"responded"}], result)
        self.assertEqual(("agent:erpcoder:war-room-test:original","session-original"), adapter.bound["run-restart"])

    def test_grounding_accepts_short_and_full_git_revision_of_same_commit(self) -> None:
        from war_room_worker import _structured_result

        headers = {"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token","Idempotency-Key":"prefix-prepare"}
        prepared = self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare", json={
            "instruction":"revision prefix", "agent_ids":["ERPcoder"],
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
            "grounding":{"worktree":"/safe/worktree","branch":"fix/runtime","revision":"27c5015","api_base":"/api","db_label":"isolated","forbidden":["production DB"],"completion_conditions":["tests pass"]},
        }, headers=headers).json()
        response = json.dumps({
            "confirmed_worktree":"/safe/worktree",
            "confirmed_revision":"27c5015aabbccddeeff001122334455667788990",
            "verdict":"PASS", "evidence":["/tmp/test.log"], "summary":"ok",
            "representative_completion_claimed":False,
        })
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.row_factory = sqlite3.Row
            result, error = _structured_result(con, prepared["message_id"], "ERPcoder", response)
        self.assertIsNone(error)
        self.assertEqual("PASS", result["verdict"])

    def test_grounding_revision_normalization_is_bidirectional_and_rejects_unsafe_prefixes(self) -> None:
        from war_room_worker import _structured_result

        def prepare(revision: str, key: str) -> dict:
            return self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare", json={
                "instruction":f"revision {key}", "agent_ids":["ERPcoder"],
                "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
                "grounding":{"worktree":"/safe/worktree","branch":"fix/runtime","revision":revision,"api_base":"/api","db_label":"isolated","forbidden":["production DB"],"completion_conditions":["tests pass"]},
            }, headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token","Idempotency-Key":key}).json()

        def validate(prepared: dict, revision: str) -> str | None:
            response = json.dumps({
                "confirmed_worktree":"/safe/worktree", "confirmed_revision":revision,
                "verdict":"PASS", "evidence":["/tmp/test.log"], "summary":"ok",
                "representative_completion_claimed":False,
            })
            with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
                con.row_factory = sqlite3.Row
                _, error = _structured_result(con, prepared["message_id"], "ERPcoder", response)
            return error

        full = "27c5015aabbccddeeff001122334455667788990"
        self.assertIsNone(validate(prepare(full, "full-to-short"), "27c5015"))
        self.assertIsNone(validate(prepare(" 27C5015 ", "normalized-short"), f" {full.upper()} "))
        self.assertEqual("context_mismatch", validate(prepare("27c5015", "unrelated-sha"), "37c5015aabbccddeeff001122334455667788990"))
        for length in range(1, 7):
            short = "abcdef"[:length]
            self.assertEqual("context_mismatch", validate(prepare(short, f"too-short-{length}"), short + "1234567890"))

    def test_recovery_validation_failure_does_not_mutate_frozen_receipt(self) -> None:
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import recover_received_deliveries

        headers = {"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare", json={
            "instruction":"immutable recovery", "agent_ids":["ERPcoder"], "execution_mode":"LEGACY",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
            "grounding":{"worktree":"/safe/worktree","branch":"fix/runtime","revision":"27c5015","api_base":"/api","db_label":"isolated","forbidden":["production DB"],"completion_conditions":["tests pass"]},
        }, headers={**headers,"Idempotency-Key":"frozen-prepare"}).json()
        self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/approve-execute", json={"expires_at":int(time.time())+500}, headers={**headers,"Idempotency-Key":"frozen-run"})
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        with _test_db(db) as con:
            delivery_id = con.execute("SELECT id FROM war_deliveries WHERE message_id=?", (prepared["message_id"],)).fetchone()[0]
            con.execute("UPDATE war_deliveries SET status='received',run_id='frozen-run' WHERE id=?", (delivery_id,))
            con.commit()

        invalid = json.dumps({
            "confirmed_worktree":"/wrong/worktree", "confirmed_revision":"27c5015",
            "verdict":"PASS", "evidence":["/tmp/test.log"], "summary":"wrong",
            "representative_completion_claimed":False,
        })
        class FrozenGateway:
            def poll(self, *, run_id: str, agent_id: str) -> DeliveryReceipt:
                return DeliveryReceipt(run_id, "responded", run_id=run_id, response_body=invalid)

        recovered = recover_received_deliveries(db_path=db, gateway=FrozenGateway(), now=int(time.time()))
        self.assertEqual("failed", recovered[0]["status"])
        with _test_db(db) as con:
            self.assertEqual(("failed","context_mismatch"), con.execute("SELECT status,error_code FROM war_deliveries WHERE id=?", (delivery_id,)).fetchone())

    def test_explicit_binding_and_byte_equivalent_selected_fanout(self) -> None:
        headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}; base="/api/war-room/projects/plachem-agent-war-room"
        task=self.client.post(base+"/tasks",json=self.task_body("fanout",agent_ids=["ERPcoder","ERPmanager"],call_limit=2),headers={**headers,"Idempotency-Key":"fan-task"})
        task_id=task.json()["task_id"]
        instruction=self.client.post(base+"/instructions",json={"task_id":task_id,"body":"identical bytes"},headers={**headers,"Idempotency-Key":"fan-inst"})
        mid=instruction.json()["message_id"]
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"awaiting_approval"},headers={**headers,"Idempotency-Key":"fan-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals",json=self.approval_body(),headers={**headers,"Idempotency-Key":"fan-approve"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"running"},headers={**headers,"Idempotency-Key":"fan-run"})
        fan=self.client.post(f"/api/war-room/messages/{mid}/deliveries",json={"task_id":task_id,"agent_ids":["ERPcoder","ERPmanager"]},headers={**headers,"Idempotency-Key":"fan-send"})
        self.assertEqual(201,fan.status_code,fan.text); self.assertEqual(["ERPcoder","ERPmanager"],[x["agent_id"] for x in fan.json()["deliveries"]])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            rows=con.execute("SELECT d.agent_id,m.body FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id WHERE d.message_id=? ORDER BY d.agent_id",(mid,)).fetchall()
        self.assertEqual([("ERPcoder","identical bytes"),("ERPmanager","identical bytes")],rows)
        self.assertNotIn("main",[row[0] for row in rows])

    def test_demo_endpoints_are_environment_gated(self) -> None:
        headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}
        self.assertEqual(200,self.client.get("/api/war-room/demo-mode",headers=headers).status_code)
        os.environ.pop("PLACHEM_WAR_ROOM_TEST_ADAPTER",None)
        try:
            self.assertEqual(404,self.client.get("/api/war-room/demo-mode",headers=headers).status_code)
            self.assertEqual(404,self.client.post("/api/war-room/demo/process",json={},headers={**headers,"Idempotency-Key":"prod-demo"}).status_code)
        finally:
            os.environ["PLACHEM_WAR_ROOM_TEST_ADAPTER"]="1"

    def test_operating_fix_human_representative_and_legacy_reviewer_assignment(self) -> None:
        import war_room
        project_id = "plachem-agent-war-room"
        human = "human-representative"
        os.environ["PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS"] = human
        os.environ.pop("PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE", None)
        os.environ["PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET"] = "fixture-proxy-secret"
        os.environ["PLACHEM_WAR_ROOM_SESSION_SECRET"] = "fixture-session-secret"
        war_room.provision_database()
        human_headers = {
            "X-Authenticated-Principal": human,
            "X-War-Room-Proxy-Secret": "fixture-proxy-secret",
        }
        main_headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = int(time.time())
        task_id, message_id, approval_id, evidence_id = "legacy-task", "legacy-message", "legacy-approval", "legacy-evidence"
        original = "legacy instruction must remain byte-identical"
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO war_messages(id,project_id,message_type,author_type,author_id,body,created_at,redaction_state,original_body) VALUES (?,?,?,?,?,?,?,?,?)", (message_id,project_id,"instruction","agent","main",original,now,"clean",original))
            con.execute("""INSERT INTO war_tasks(id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,manyfast_version,document_version,call_limit,turn_limit,execution_mode,deadline_at,revision,qa_cycle,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'baseline-2026-08-23','v1',1,1,'LEGACY',?,1,1,?,?)""", (task_id,project_id,message_id,"ERPcoder",None,"legacy scope","qa",now+3600,now,now))
            con.execute("INSERT INTO war_task_agents VALUES (?,?)", (task_id,"ERPcoder"))
            con.execute("INSERT INTO war_approvals(id,task_id,approver_id,decision,scope_hash,document_version,assignee_agent_id,target_set_hash,expires_at,revoked_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (approval_id,task_id,"main","approved",hashlib.sha256(b"legacy scope").hexdigest(),"v1","ERPcoder",hashlib.sha256(json.dumps(["ERPcoder"]).encode()).hexdigest(),now+1800,None,now))
            con.execute("INSERT INTO war_evidence(id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (evidence_id,task_id,"test","/tmp/legacy","preserve",1,hashlib.sha256(b"legacy scope").hexdigest(),"v1",1,now))
            con.commit()

        forged = self.client.post(f"/api/war-room/tasks/{task_id}/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"legacy review","task_revision":1}, headers={"X-Authenticated-Principal":human,"Idempotency-Key":"forged-reviewer"})
        self.assertEqual(401, forged.status_code)
        main_denied = self.client.post(f"/api/war-room/tasks/{task_id}/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"legacy review","task_revision":1}, headers={**main_headers,"Idempotency-Key":"main-reviewer"})
        self.assertEqual(403, main_denied.status_code)
        legacy_cookie = hmac.new(b"fixture-session-secret", b"main", hashlib.sha256).hexdigest()
        cookie_denied = self.client.post(f"/api/war-room/tasks/{task_id}/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"legacy review","task_revision":1}, headers={"Idempotency-Key":"cookie-reviewer"}, cookies={"war_room_session":f"main.{legacy_cookie}"})
        self.assertEqual(403, cookie_denied.status_code)

        assigned = self.client.post(f"/api/war-room/tasks/{task_id}/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"legacy independent QA","task_revision":1}, headers={**human_headers,"Idempotency-Key":"human-reviewer"})
        self.assertEqual(200, assigned.status_code, assigned.text)
        self.assertEqual([approval_id], assigned.json()["revoked_approval_ids"])
        self.assertTrue(assigned.json()["new_approval_required"])
        replay = self.client.post(f"/api/war-room/tasks/{task_id}/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"legacy independent QA","task_revision":1}, headers={**human_headers,"Idempotency-Key":"human-reviewer"})
        self.assertEqual(assigned.json(), replay.json())
        duplicate = self.client.post(f"/api/war-room/tasks/{task_id}/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"again","task_revision":2}, headers={**human_headers,"Idempotency-Key":"human-reviewer-duplicate"})
        self.assertEqual(409, duplicate.status_code)
        with sqlite3.connect(db) as con:
            task = con.execute("SELECT reviewer_agent_id,status,revision FROM war_tasks WHERE id=?", (task_id,)).fetchone()
            message = con.execute("SELECT body,original_body FROM war_messages WHERE id=?", (message_id,)).fetchone()
            approval = con.execute("SELECT decision,revoked_at FROM war_approvals WHERE id=?", (approval_id,)).fetchone()
            evidence = con.execute("SELECT summary FROM war_evidence WHERE id=?", (evidence_id,)).fetchone()
            audit = con.execute("SELECT payload_redacted FROM war_audit_events WHERE target_id=? AND event_type='legacy_task_reviewer_assigned'", (task_id,)).fetchone()
        self.assertEqual(("ERPqa","awaiting_approval",2), task)
        self.assertEqual((original,original), message)
        self.assertEqual("approved", approval[0]); self.assertIsNotNone(approval[1])
        self.assertEqual(("preserve",), evidence)
        self.assertIn(approval_id, audit[0])
        newly_approved = self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**human_headers,"Idempotency-Key":"human-new-approval"})
        self.assertEqual(200, newly_approved.status_code, newly_approved.text)
        self.assertEqual("approved", newly_approved.json()["status"])

        prepared = self.client.post(f"/api/war-room/projects/{project_id}/prepare", json={"instruction":"main prepare remains allowed","reviewer_agent_id":"ERPqa","agent_ids":["ERPcoder"],"deadline_at":now+1800,"document_version":"baseline-2026-08-23"}, headers={**main_headers,"Idempotency-Key":"main-still-prepares"})
        self.assertEqual(201, prepared.status_code, prepared.text)
        main_approval = self.client.post(f"/api/war-room/tasks/{prepared.json()['task_id']}/approvals", json=self.approval_body(), headers={**main_headers,"Idempotency-Key":"main-cannot-approve"})
        self.assertEqual(403, main_approval.status_code)

        with sqlite3.connect(db) as con:
            for suffix, status in (("active","running"),("completed","completed"),("self","draft")):
                mid, tid = f"legacy-{suffix}-message", f"legacy-{suffix}-task"
                con.execute("INSERT INTO war_messages(id,project_id,message_type,author_type,author_id,body,created_at,redaction_state,original_body) VALUES (?,?,?,?,?,?,?,?,?)", (mid,project_id,"instruction","agent","main",suffix,now,"clean",suffix))
                con.execute("""INSERT INTO war_tasks(id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,manyfast_version,document_version,call_limit,turn_limit,execution_mode,deadline_at,revision,qa_cycle,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,'baseline-2026-08-23','v1',1,1,'LEGACY',?,1,0,?,?)""", (tid,project_id,mid,"ERPcoder",None,suffix,status,now+3600,now,now))
                con.execute("INSERT INTO war_task_agents VALUES (?,?)", (tid,"ERPcoder"))
            con.execute("INSERT INTO war_deliveries(id,message_id,agent_id,status,created_at) VALUES ('active-delivery','legacy-active-message','ERPcoder','queued',?)", (now,))
            con.execute("UPDATE war_participants SET role='qa',can_comment=1 WHERE project_id=? AND principal_id='ERPcoder'", (project_id,))
            con.commit()
        active_blocked = self.client.post("/api/war-room/tasks/legacy-active-task/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"must wait","task_revision":1}, headers={**human_headers,"Idempotency-Key":"active-blocked"})
        self.assertEqual(409, active_blocked.status_code)
        completed_blocked = self.client.post("/api/war-room/tasks/legacy-completed-task/reviewer", json={"reviewer_agent_id":"ERPqa","reason":"immutable","task_revision":1}, headers={**human_headers,"Idempotency-Key":"completed-blocked"})
        self.assertEqual(409, completed_blocked.status_code)
        self_review_blocked = self.client.post("/api/war-room/tasks/legacy-self-task/reviewer", json={"reviewer_agent_id":"ERPcoder","reason":"not independent","task_revision":1}, headers={**human_headers,"Idempotency-Key":"self-blocked"})
        self.assertEqual(422, self_review_blocked.status_code)

    def test_demo_worker_persists_visible_response_body(self) -> None:
        headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}
        base="/api/war-room/projects/plachem-agent-war-room"
        task=self.client.post(base+"/tasks",json=self.task_body("visible demo response"),headers={**headers,"Idempotency-Key":"visible-task"})
        task_id=task.json()["task_id"]
        instruction=self.client.post(base+"/instructions",json={"task_id":task_id,"body":"make demo result"},headers={**headers,"Idempotency-Key":"visible-instruction"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"awaiting_approval"},headers={**headers,"Idempotency-Key":"visible-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals",json=self.approval_body(),headers={**headers,"Idempotency-Key":"visible-approval"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"running"},headers={**headers,"Idempotency-Key":"visible-run"})
        self.client.post(f"/api/war-room/messages/{instruction.json()['message_id']}/deliveries",json={"task_id":task_id,"agent_id":"ERPcoder"},headers={**headers,"Idempotency-Key":"visible-delivery"})
        processed=self.client.post("/api/war-room/demo/process",json={},headers={**headers,"Idempotency-Key":"visible-process"})
        self.assertEqual("responded",processed.json()["items"][0]["status"])
        deliveries=self.client.get(base+"/deliveries",headers=headers).json()["items"]
        visible=next(row for row in deliveries if row["message_id"]==instruction.json()["message_id"])
        self.assertIn("시연 응답 · ERPcoder",visible["response_body"])
        self.assertTrue(visible["response_message_id"])

    def test_three_screen_ui_prepares_without_delivery_and_uses_explicit_approval(self) -> None:
        html = (Path(__file__).parents[1] / "static" / "war-room.html").read_text(encoding="utf-8")
        javascript = (Path(__file__).parents[1] / "static" / "war-room-ui.js").read_text(encoding="utf-8")
        self.assertNotIn("qa-signature", html + javascript)
        self.assertIn('source:"agent_result"', javascript)
        for stable_id in ("task-agent-targets","demo-controls","demo-session-key","stop-ack-delivery","delivery-cards"):
            self.assertIn(f'id="{stable_id}"', html)
        self.assertIn("retryDemoDelivery", javascript)
        self.assertIn("processDemoQueue", javascript)
        self.assertEqual(5, html.count('data-screen='))
        self.assertIn('data-screen="process-board"', html)
        self.assertIn('execution_mode: "FAST_GATEWAY"', javascript)
        self.assertIn("reviewer_agent_id", javascript)
        self.assertIn("Agent 호출 0건", javascript)
        for stable_id in ("quick-task-form","quick-instruction","quick-agent-targets","quick-approve-run","quick-delivery-cards","advanced-area"):
            self.assertIn(f'id="{stable_id}"', html)
        self.assertIn("`${base}/prepare`", javascript)
        self.assertIn("/approve-execute", javascript)
        self.assertIn('"qa"].includes(task.status)', javascript)
        self.assertIn('task.status === "completed"', javascript)
        self.assertIn("quickApproveAndRun", javascript)
        self.assertIn('currentTasks.find(task => ["awaiting_approval","approved","running","qa"].includes(task.status)', javascript)
        self.assertIn("작업 시작", html)
        self.assertIn("결과 검토", html)
        self.assertNotIn("전달 다시 시도", javascript)
        self.assertIn('data-advanced-section hidden', html)
        self.assertIn('id="advanced-area" class="advanced-area" hidden', html)
        self.assertIn("다른 작업 종료 후 순서대로 실행", javascript)

    def test_prepare_persists_immutable_grounding_packet_and_prompt_contract(self) -> None:
        headers = {"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token","Idempotency-Key":"grounding-prepare"}
        response = self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare", json={
            "instruction":"현재 기준만 검증", "agent_ids":["ERPcoder","ERPmanager"],
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
            "grounding":{"worktree":"/safe/worktree","branch":"fix/collab","revision":"abc123","api_base":"http://127.0.0.1:8114/api/war-room","db_label":"isolated-demo","forbidden":["production DB"],"completion_conditions":["tests pass"]},
        }, headers=headers)
        self.assertEqual(201, response.status_code, response.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            packet = json.loads(con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (response.json()["task_id"],)).fetchone()[0])
            body = con.execute("SELECT body FROM war_messages WHERE id=?", (response.json()["message_id"],)).fetchone()[0]
        self.assertEqual("abc123", packet["revision"])
        self.assertEqual("현재 기준만 검증", body)
        from war_room_actions import _grounded_instruction
        submitted = _grounded_instruction(body, packet, response.json()["execution_mode"])
        self.assertIn("FAST_GATEWAY_RESULT", submitted)
        self.assertIn("/safe/worktree", submitted)

    def test_running_approve_execute_returns_existing_calls_instead_of_409(self) -> None:
        headers={"X-War-Room-Actor":"main","X-War-Room-Token":"fixture-main-token"}
        prepared=self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare",json={"instruction":"중복 실행","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+600,"document_version":"baseline-2026-08-23"},headers={**headers,"Idempotency-Key":"existing-prepare"}).json()
        first=self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/approve-execute",json={"expires_at":int(time.time())+500},headers={**headers,"Idempotency-Key":"existing-run-1"})
        second=self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/approve-execute",json={"expires_at":int(time.time())+500},headers={**headers,"Idempotency-Key":"existing-run-2"})
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual("already_running", second.json()["execution_state"])
        self.assertEqual(first.json()["deliveries"][0]["delivery_id"], second.json()["deliveries"][0]["delivery_id"])

    def test_ui_project_selection_and_no_hardcoded_project_id(self) -> None:
        html = (Path(__file__).parents[1] / "static" / "war-room.html").read_text(encoding="utf-8")
        javascript = (Path(__file__).parents[1] / "static" / "war-room-ui.js").read_text(encoding="utf-8")
        self.assertIn("selectedProjectId", javascript)
        self.assertIn("selectProject", javascript)
        self.assertNotIn('const projectId = "plachem-agent-war-room"', html + javascript)

    def test_delivery_requires_approved_task_and_assignee(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        base = "/api/war-room/projects/plachem-agent-war-room"
        message = self.client.post(base + "/messages", json={"body":"direct delivery"}, headers={**headers,"Idempotency-Key":"message-bypass"})
        self.assertEqual(201, message.status_code)
        delivery = self.client.post(f"/api/war-room/messages/{message.json()['id']}/deliveries", json={"agent_id":"ERPcoder", "task_id":"missing-task"}, headers={**headers,"Idempotency-Key":"delivery-bypass"})
        self.assertEqual(409, delivery.status_code)

    def test_R_TASKLINK_instruction_without_task_is_rejected(self) -> None:
        """R-TASKLINK: an instruction requires an existing same-project task."""
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        base = "/api/war-room/projects/plachem-agent-war-room"
        instruction = self.client.post(
            base + "/messages",
            json={"body":"must be linked", "message_type":"instruction"},
            headers={**headers, "Idempotency-Key":"tasklink-message"},
        )
        self.assertEqual(422, instruction.status_code, instruction.text)
        task = self.client.post(base + "/tasks", json=self.task_body("linked draft task"), headers={**headers, "Idempotency-Key":"tasklink-task"})
        self.assertEqual(201, task.status_code, task.text)
        task_id = task.json()["task_id"]
        linked = self.client.post(base + "/instructions", json={"task_id":task_id,"body":"linked instruction"}, headers={**headers, "Idempotency-Key":"tasklink-instruction"})
        self.assertEqual(201, linked.status_code, linked.text)
        replay = self.client.post(base + "/instructions", json={"task_id":task_id,"body":"linked instruction"}, headers={**headers, "Idempotency-Key":"tasklink-instruction"})
        self.assertEqual(linked.json(), replay.json())
        duplicate = self.client.post(base + "/instructions", json={"task_id":task_id,"body":"second instruction"}, headers={**headers, "Idempotency-Key":"tasklink-duplicate"})
        self.assertEqual(409, duplicate.status_code, duplicate.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            row = con.execute(
                "SELECT m.id,t.id,t.status FROM war_messages m JOIN war_tasks t ON t.source_message_id=m.id WHERE m.id=?",
                (linked.json()["message_id"],),
            ).fetchone()
            self.assertEqual((linked.json()["message_id"], task_id, "draft"), row)
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_messages WHERE body='linked instruction'").fetchone()[0])
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("UPDATE war_messages SET body='mutated' WHERE id=?", (linked.json()["message_id"],))

        second = self.client.post("/api/war-room/projects", json={"name":"Task link isolation", "manyfast_version":"baseline-2026-08-23"}, headers={**headers, "Idempotency-Key":"tasklink-project"})
        self.assertEqual(201, second.status_code, second.text)
        other_project = second.json()["project_id"]
        other_task = self.client.post(f"/api/war-room/projects/{other_project}/tasks", json=self.task_body("other project task"), headers={**headers, "Idempotency-Key":"tasklink-other-task"})
        self.assertEqual(201, other_task.status_code, other_task.text)
        cross = self.client.post(base + "/instructions", json={"task_id":other_task.json()["task_id"],"body":"cross project"}, headers={**headers, "Idempotency-Key":"tasklink-cross"})
        self.assertEqual(403, cross.status_code, cross.text)

    def test_participant_upsert_preserves_same_agent_across_projects(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        created = self.client.post("/api/war-room/projects", json={"name":"Second participant project"}, headers={**headers,"Idempotency-Key":"second-project"})
        self.assertEqual(201, created.status_code)
        project_id = created.json()["project_id"]
        add = self.client.post(f"/api/war-room/projects/{project_id}/participants", json={"principal_id":"ERPcoder","role":"developer"}, headers={**headers,"Idempotency-Key":"second-participant"})
        self.assertEqual(201, add.status_code, add.text)
        participants = self.client.get(f"/api/war-room/projects/{project_id}/participants", headers=headers).json()["items"]
        self.assertEqual(["ERPcoder"], [row["principal_id"] for row in participants if row["principal_id"] == "ERPcoder"])
        original = self.client.get("/api/war-room/projects/plachem-agent-war-room/participants", headers=headers).json()["items"]
        self.assertIn("ERPcoder", [row["principal_id"] for row in original])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            rows = con.execute("SELECT project_id, principal_id FROM war_participants WHERE principal_id='ERPcoder' ORDER BY project_id").fetchall()
        self.assertIn(("plachem-agent-war-room", "ERPcoder"), rows)
        self.assertIn((project_id, "ERPcoder"), rows)
        self.assertEqual(2, len(rows))
        self.assertEqual(200, self.client.get(f"/api/war-room/projects/{project_id}/access", headers=headers).status_code)

    def test_chat_send_runid_event_recovery(self) -> None:
        from war_room_adapter import FakeGatewayAdapter
        gateway = FakeGatewayAdapter()
        run = gateway.send(agent_id="ERPcoder", body="fixture", run_id="run-1")
        self.assertEqual("run-1", run.run_id)
        gateway.complete(run_id="run-1", status="responded")
        self.assertEqual("responded", gateway.poll(run_id="run-1").status)

    def test_capability_flags_enforced_on_reads_and_writes(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token", "Idempotency-Key":"cap-participant"}
        self.client.patch("/api/war-room/projects/plachem-agent-war-room/participants/ERPqa", json={"role":"observer"}, headers=headers)
        observer = {"X-War-Room-Actor":"ERPqa", "X-War-Room-Token":"fixture-erpqa-token"}
        self.assertEqual(200, self.client.get("/api/war-room/projects/plachem-agent-war-room/access", headers=observer).status_code)
        denied = self.client.post("/api/war-room/projects/plachem-agent-war-room/messages", json={"body":"no comment"}, headers={**observer,"Idempotency-Key":"no-comment"})
        self.assertEqual(403, denied.status_code)

    def test_fake_gateway_received_recovery_and_timeout(self) -> None:
        from war_room_adapter import FakeGatewayAdapter
        gateway = FakeGatewayAdapter()
        gateway.send(agent_id="ERPcoder", body="fixture", run_id="run-recover")
        gateway.mark_received("run-recover")
        self.assertEqual("received", gateway.poll("run-recover").status)
        gateway.expire("run-recover")
        self.assertEqual("timed_out", gateway.poll("run-recover").status)

    def test_received_recovery_worker_polls_runid_without_resend(self) -> None:
        from war_room_adapter import FakeGatewayAdapter
        from war_room_worker import recover_received_deliveries
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = int(time.time())
        with _test_db(db) as con:
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("recovery-message", "plachem-agent-war-room", "instruction", "agent", "main", "recovery body", None, None, now, "recovery-corr", "clean", None))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,run_id,created_at) VALUES (?,?,?,?,?,?,?)", ("recovery-delivery", "recovery-message", "ERPcoder", "received", 1, "run-recover", now))
            con.commit()
        gateway = FakeGatewayAdapter()
        gateway.send(agent_id="ERPcoder", body="recovery body", run_id="run-recover")
        gateway.complete(run_id="run-recover", status="responded", response_body="recovered result")
        recovered = recover_received_deliveries(db_path=db, gateway=gateway, now=now + 1)
        self.assertEqual([{"delivery_id":"recovery-delivery","run_id":"run-recover","status":"responded"}], recovered)
        with _test_db(db) as con:
            self.assertEqual("responded", con.execute("SELECT status FROM war_deliveries WHERE id='recovery-delivery'").fetchone()[0])

    def test_received_recovery_does_not_overwrite_stop_won_after_poll(self) -> None:
        from types import SimpleNamespace
        from war_room_worker import recover_received_deliveries

        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = int(time.time())
        task_id, message_id, _ = self.create_instruction(
            "/api/war-room/projects/plachem-agent-war-room",
            {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"},
            "stale poll race", "recovery body", "stale-poll",
        )
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,run_id,created_at) VALUES (?,?,?,?,?,?,?)", ("stale-delivery", message_id, "ERPcoder", "received", 1, "stale-run", now))
            con.execute("INSERT INTO war_execution_runs (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,run_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", ("stale-core", "plachem-agent-war-room", task_id, "ERPcoder", "stale-run", "RUNNING", now, now))
            con.commit()

        class PollThatLosesToStop:
            def poll(self, *, run_id, agent_id):
                with sqlite3.connect(db) as update_con:
                    update_con.execute("UPDATE war_deliveries SET status='stopped' WHERE id='stale-delivery'")
                    update_con.execute("UPDATE war_execution_runs SET run_status='CANCELLED',cancel_reason='USER_CANCEL' WHERE core_run_id='stale-core'")
                    update_con.commit()
                return SimpleNamespace(status="failed", error_code="OPENCLAW_ERROR", run_id=run_id)

            def execution_snapshot(self, delivery_id):
                raise AssertionError("stale recovery must not write a snapshot")

        self.assertEqual([], recover_received_deliveries(db_path=db, gateway=PollThatLosesToStop(), now=now + 1))
        with sqlite3.connect(db) as con:
            self.assertEqual("stopped", con.execute("SELECT status FROM war_deliveries WHERE id='stale-delivery'").fetchone()[0])
            self.assertEqual(("CANCELLED", "USER_CANCEL"), con.execute("SELECT run_status,cancel_reason FROM war_execution_runs WHERE core_run_id='stale-core'").fetchone())

    def test_process_delivery_does_not_overwrite_stop_won_during_deliver(self) -> None:
        from types import SimpleNamespace
        from war_room_worker import process_due_deliveries

        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        now = int(time.time())
        task_id, message_id, _ = self.create_instruction(
            "/api/war-room/projects/plachem-agent-war-room",
            {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"},
            "stale deliver race", "delivery body", "stale-deliver",
        )
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,run_id,created_at) VALUES (?,?,?,?,?,?,?)", ("deliver-race", message_id, "ERPcoder", "queued", 0, None, now))
            con.execute("INSERT INTO war_execution_runs (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,run_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", ("deliver-core", "plachem-agent-war-room", task_id, "ERPcoder", "deliver-run", "RUNNING", now, now))
            con.commit()

        class DeliverThatLosesToStop:
            def deliver(self, *, delivery_id, agent_id, instruction_id, body):
                with sqlite3.connect(db) as update_con:
                    update_con.execute("UPDATE war_deliveries SET status='stopped' WHERE id=?", (delivery_id,))
                    update_con.execute("UPDATE war_execution_runs SET run_status='CANCELLED',cancel_reason='USER_CANCEL' WHERE core_run_id='deliver-core'")
                    update_con.commit()
                return SimpleNamespace(status="failed", error_code="OPENCLAW_ERROR", run_id="deliver-run", response_body=None, session_id=None)

            def execution_snapshot(self, delivery_id):
                raise AssertionError("stale delivery must not write a snapshot")

        self.assertEqual([], process_due_deliveries(db_path=db, adapter=DeliverThatLosesToStop(), now=now + 1))
        with sqlite3.connect(db) as con:
            self.assertEqual("stopped", con.execute("SELECT status FROM war_deliveries WHERE id='deliver-race'").fetchone()[0])
            self.assertEqual(("CANCELLED", "USER_CANCEL"), con.execute("SELECT run_status,cancel_reason FROM war_execution_runs WHERE core_run_id='deliver-core'").fetchone())

    def test_agent_inputs_casefold_to_canonical_and_reject_casefold_duplicates(self) -> None:
        base = "/api/war-room/projects/plachem-agent-war-room"
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        mixed = self.client.post(base + "/prepare", json={
            "instruction":"canonical disposable echo",
            "agent_ids":[" ErPcOdEr ", "eRpMaNaGeR"],
            "assignee_agent_id":" erpcoder ",
            "deadline_at":int(time.time()) + 600,
            "document_version":"baseline-2026-08-23",
        }, headers={**headers, "Idempotency-Key":"canonical-mixed"})
        self.assertEqual(201, mixed.status_code, mixed.text)
        self.assertEqual(["ERPcoder", "ERPmanager"], mixed.json()["agent_ids"])
        duplicate = self.client.post(base + "/prepare", json={
            "instruction":"duplicate canonical ids",
            "agent_ids":["ErPcOdEr", " erpcoder "],
            "deadline_at":int(time.time()) + 600,
            "document_version":"baseline-2026-08-23",
        }, headers={**headers, "Idempotency-Key":"canonical-duplicate"})
        self.assertEqual(422, duplicate.status_code, duplicate.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            rows = con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=? ORDER BY agent_id", (mixed.json()["task_id"],)).fetchall()
        self.assertEqual([("ERPcoder",), ("ERPmanager",)], rows)

    def test_structured_result_contract_precedes_instruction_and_response_is_immutable(self) -> None:
        from war_room_adapter import TestSessionAdapter
        from war_room_worker import process_due_deliveries
        base = "/api/war-room/projects/plachem-agent-war-room"
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(base + "/prepare", json={
            "instruction":"echo this phrase but return the required JSON",
            "agent_ids":["ERPcoder"], "execution_mode":"LEGACY",
            "deadline_at":int(time.time()) + 600,
            "document_version":"baseline-2026-08-23",
            "grounding":{"worktree":"/safe/echo","branch":"p1","revision":"rev-echo","api_base":"/api","db_label":"temp","forbidden":["production DB"],"completion_conditions":["echo"]},
        }, headers={**headers, "Idempotency-Key":"contract-priority"}).json()
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            prompt = con.execute("SELECT body FROM war_messages WHERE id=?", (prepared["message_id"],)).fetchone()[0]
        self.assertEqual("echo this phrase but return the required JSON", prompt)
        run = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/approve-execute", json={"expires_at":int(time.time())+500}, headers={**headers, "Idempotency-Key":"contract-run"})
        self.assertEqual(200, run.status_code, run.text)
        submitted: list[str] = []
        class CapturingAdapter(TestSessionAdapter):
            def deliver(self, **values):
                submitted.append(values["body"])
                return super().deliver(**values)
        processed = process_due_deliveries(db_path=Path(os.environ["PLACHEM_WAR_ROOM_DB"]), adapter=CapturingAdapter())
        self.assertEqual("responded", processed[0]["status"])
        self.assertTrue(submitted[0].startswith("[STRUCTURED_RESULT]"))
        self.assertLess(submitted[0].index("[STRUCTURED_RESULT]"), submitted[0].index("[ORIGINAL_INSTRUCTION_CONTEXT]"))
        self.assertIn("[ORIGINAL_INSTRUCTION_CONTEXT]\necho this phrase but return the required JSON", submitted[0])
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            row = con.execute("SELECT d.status,d.response_message_id,m.body,m.original_body FROM war_deliveries d JOIN war_messages m ON m.id=d.response_message_id WHERE d.message_id=?", (prepared["message_id"],)).fetchone()
        self.assertEqual("responded", row[0])
        self.assertTrue(row[1])
        result = json.loads(row[2])
        self.assertEqual(result, json.loads(row[3]))
        self.assertEqual("PASS", result["verdict"])

    def test_skeleton_dynamic_agent_catalog_participation_and_permissions(self) -> None:
        root = Path(self.temp_dir.name)
        openclaw = root / "openclaw.json"
        gateway = root / "gateway-agents.json"
        agents = ["main", "ERPcoder", "ERPmanager", "ERPqa", "FlexDev", "DisabledDev"]
        openclaw.write_text(json.dumps({"agents":{"list":[{"id": item} for item in agents]}}), encoding="utf-8")
        gateway.write_text(json.dumps({item:{"allowed":True,"enabled":item != "DisabledDev","capabilities":["chat"]} for item in agents}), encoding="utf-8")
        os.environ["PLACHEM_OPENCLAW_CONFIG"] = str(openclaw)
        os.environ["PLACHEM_FAST_GATEWAY_AGENTS"] = str(gateway)
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = json.dumps({
            "main":"fixture-main-token", "ERPcoder":"fixture-erpcoder-token",
            "ERPmanager":"fixture-erpmanager-token", "ERPqa":"fixture-erpqa-token",
            "FlexDev":"fixture-flexdev-token",
        })
        base = "/api/war-room/projects/plachem-agent-war-room"
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        candidates = self.client.get(base + "/agent-candidates", headers=main)
        self.assertEqual(200, candidates.status_code, candidates.text)
        by_id = {item["agent_id"]:item for item in candidates.json()["items"]}
        self.assertTrue(by_id["FlexDev"]["execution_eligible"])
        self.assertFalse(by_id["DisabledDev"]["execution_eligible"])
        not_participating = self.client.post(base + "/prepare", json={
            "instruction":"dynamic candidate", "agent_ids":["FlexDev"],
            "assignee_agent_id":"FlexDev", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"dynamic-not-participant"})
        self.assertEqual(409, not_participating.status_code, not_participating.text)
        disabled = self.client.post(base + "/participants", json={"principal_id":"DisabledDev","role":"developer"}, headers={**main,"Idempotency-Key":"dynamic-disabled"})
        self.assertEqual(409, disabled.status_code, disabled.text)
        added = self.client.post(base + "/participants", json={"principal_id":"FlexDev","role":"developer"}, headers={**main,"Idempotency-Key":"dynamic-add"})
        self.assertEqual(201, added.status_code, added.text)
        duplicate = self.client.post(base + "/participants", json={"principal_id":"FlexDev","role":"developer"}, headers={**main,"Idempotency-Key":"dynamic-duplicate"})
        self.assertEqual(409, duplicate.status_code, duplicate.text)
        prepared = self.client.post(base + "/prepare", json={
            "instruction":"dynamic candidate", "agent_ids":["FlexDev"],
            "assignee_agent_id":"FlexDev", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"dynamic-prepared"})
        self.assertEqual(201, prepared.status_code, prepared.text)
        denied = self.client.post(f"/api/war-room/tasks/{prepared.json()['task_id']}/approve-execute", json={"expires_at":int(time.time())+500}, headers={"X-War-Room-Actor":"FlexDev","X-War-Room-Token":"fixture-flexdev-token","Idempotency-Key":"dynamic-denied"})
        self.assertEqual(403, denied.status_code, denied.text)

    def test_skeleton_max_instruction_prepare_then_single_explicit_submission(self) -> None:
        base = "/api/war-room/projects/plachem-agent-war-room"
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        instruction = "가" * 4095 + "끝"
        self.assertEqual(4096, len(instruction))
        payload = {
            "instruction":instruction, "agent_ids":["ERPcoder"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "execution_mode":"FAST_GATEWAY",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }
        prepared = self.client.post(base + "/prepare", json=payload, headers={**headers,"Idempotency-Key":"max-prepare"})
        self.assertEqual(201, prepared.status_code, prepared.text)
        self.assertEqual("FAST_GATEWAY", prepared.json()["execution_mode"])
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            stored = con.execute("SELECT body,original_body FROM war_messages WHERE id=?", (prepared.json()["message_id"],)).fetchone()
            self.assertEqual((instruction, instruction), stored)
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM war_deliveries WHERE message_id=?", (prepared.json()["message_id"],)).fetchone()[0])
        approval_body = {"expires_at":int(time.time())+500}
        first = self.client.post(f"/api/war-room/tasks/{prepared.json()['task_id']}/approve-execute", json=approval_body, headers={**headers,"Idempotency-Key":"max-approve"})
        replay = self.client.post(f"/api/war-room/tasks/{prepared.json()['task_id']}/approve-execute", json=approval_body, headers={**headers,"Idempotency-Key":"max-approve"})
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), replay.json())
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM war_deliveries WHERE message_id=?", (prepared.json()["message_id"],)).fetchone()[0])

    def test_skeleton_independent_qa_evidence_completion_and_stop_barrier(self) -> None:
        base = "/api/war-room/projects/plachem-agent-war-room"
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        qa = {"X-War-Room-Actor":"ERPqa", "X-War-Room-Token":"fixture-erpqa-token"}
        prepared = self.client.post(base + "/prepare", json={
            "instruction":"independent QA", "agent_ids":["ERPcoder"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"qa-boundary-prepare"}).json()
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET status='qa' WHERE id=?", (prepared["task_id"],))
            con.commit()
        self_qa = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:","qa_principal":"main","source":"agent_result"}, headers={**main,"Idempotency-Key":"qa-self"})
        self.assertEqual(403, self_qa.status_code, self_qa.text)
        test_evidence = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/evidence", json={"uri":"/isolated/test.json","summary":"test","evidence_type":"test"}, headers={**main,"Idempotency-Key":"qa-test-evidence"})
        self.assertEqual(201, test_evidence.status_code, test_evidence.text)
        relaxed = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:test","qa_principal":"ERPqa","source":"agent_result"}, headers={**qa,"Idempotency-Key":"qa-relaxed"})
        self.assertEqual(409, relaxed.status_code, relaxed.text)
        artifact = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/evidence", json={"uri":"/isolated/artifact.json","summary":"artifact","evidence_type":"artifact"}, headers={**main,"Idempotency-Key":"qa-artifact-evidence"})
        self.assertEqual(201, artifact.status_code, artifact.text)
        passed = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/qa-verdict", json={"verdict":"PASS","evidence_profile":"required:","qa_principal":"ERPqa","source":"agent_result"}, headers={**qa,"Idempotency-Key":"qa-pass"})
        self.assertEqual(200, passed.status_code, passed.text)
        completed = self.client.post(f"/api/war-room/tasks/{prepared['task_id']}/representative-completion", json={"decision":"approved"}, headers={**main,"Idempotency-Key":"qa-complete"})
        self.assertEqual(200, completed.status_code, completed.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("required:test,artifact", con.execute("SELECT evidence_profile FROM war_qa_verdicts WHERE task_id=?", (prepared["task_id"],)).fetchone()[0])

        stopped_task = self.client.post(base + "/prepare", json={
            "instruction":"stop boundary", "agent_ids":["ERPcoder"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"stop-boundary-prepare"}).json()
        run = self.client.post(f"/api/war-room/tasks/{stopped_task['task_id']}/approve-execute", json={"expires_at":int(time.time())+500}, headers={**main,"Idempotency-Key":"stop-boundary-run"})
        self.assertEqual(200, run.status_code, run.text)
        stopped = self.client.post(base + "/stop", json={}, headers={**main,"Idempotency-Key":"stop-boundary-stop"})
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual("stopped", stopped.json()["status"])
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("stopped", con.execute("SELECT status FROM war_tasks WHERE id=?", (stopped_task["task_id"],)).fetchone()[0])
        blocked = self.client.post(base + "/prepare", json={
            "instruction":"must not start", "agent_ids":["ERPcoder"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"stop-boundary-blocked"})
        self.assertEqual(409, blocked.status_code, blocked.text)

    def test_review_fix_reviewer_is_separate_from_every_execution_agent(self) -> None:
        base = "/api/war-room/projects/plachem-agent-war-room"
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        qa = {"X-War-Room-Actor":"ERPqa", "X-War-Room-Token":"fixture-erpqa-token"}
        invalid = {
            "instruction":"reviewer overlap", "scope":"reviewer overlap",
            "agent_ids":["ERPcoder", "ERPqa"], "assignee_agent_id":"ERPcoder",
            "reviewer_agent_id":"ERPqa", "deadline_at":int(time.time())+600,
            "document_version":"baseline-2026-08-23",
        }
        prepared = self.client.post(base + "/prepare", json=invalid, headers={**main,"Idempotency-Key":"review-overlap-prepare"})
        self.assertEqual(422, prepared.status_code, prepared.text)
        created = self.client.post(base + "/tasks", json=invalid, headers={**main,"Idempotency-Key":"review-overlap-create"})
        self.assertEqual(422, created.status_code, created.text)

        valid = self.client.post(base + "/prepare", json={
            "instruction":"legacy malformed assignment", "agent_ids":["ERPcoder"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"review-valid-prepare"})
        self.assertEqual(201, valid.status_code, valid.text)
        task_id = valid.json()["task_id"]
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("INSERT INTO war_task_agents(task_id,agent_id) VALUES (?,?)", (task_id,"ERPqa"))
            con.commit()
        approval = self.client.post(f"/api/war-room/tasks/{task_id}/approve-execute", json={"expires_at":int(time.time())+500}, headers={**main,"Idempotency-Key":"review-overlap-approve"})
        self.assertEqual(422, approval.status_code, approval.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET status='qa' WHERE id=?", (task_id,))
            con.commit()
        verdict = self.client.post(f"/api/war-room/tasks/{task_id}/qa-verdict", json={"verdict":"PASS","qa_principal":"ERPqa","source":"agent_result"}, headers={**qa,"Idempotency-Key":"review-overlap-qa"})
        self.assertEqual(403, verdict.status_code, verdict.text)

    def test_review_fix_partial_stop_requires_full_confirmation_and_fresh_approval(self) -> None:
        from war_room_adapter import DeliveryReceipt

        base = "/api/war-room/projects/plachem-agent-war-room"
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(base + "/prepare", json={
            "instruction":"partial stop", "agent_ids":["ERPcoder","ERPmanager"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"partial-stop-prepare"})
        self.assertEqual(201, prepared.status_code, prepared.text)
        task_id = prepared.json()["task_id"]
        run = self.client.post(f"/api/war-room/tasks/{task_id}/approve-execute", json={"expires_at":int(time.time())+500}, headers={**main,"Idempotency-Key":"partial-stop-run"})
        self.assertEqual(200, run.status_code, run.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_deliveries SET status='received' WHERE message_id=?", (prepared.json()["message_id"],))
            con.commit()

        class MixedStop:
            def stop(self, *, delivery_id, agent_id):
                return DeliveryReceipt(delivery_id, "stopped" if agent_id == "ERPcoder" else "failed", error_code=None if agent_id == "ERPcoder" else "stop_not_confirmed")

        with mock.patch("war_room_actions._adapter_for_mode", return_value=MixedStop()):
            stopped = self.client.post(base + "/stop", json={}, headers={**main,"Idempotency-Key":"partial-stop"})
        self.assertEqual("stop_failed", stopped.json()["status"])
        deliveries = {item["agent_id"]:item["delivery_id"] for item in run.json()["deliveries"]}
        ack = self.client.post(base + "/stop-ack", json={"delivery_id":deliveries["ERPcoder"]}, headers={**main,"Idempotency-Key":"partial-stop-ack-one"})
        self.assertEqual("stop_failed", ack.json()["status"])
        blocked_resume = self.client.post(base + "/resume", json={}, headers={**main,"Idempotency-Key":"partial-stop-resume-blocked"})
        self.assertEqual(409, blocked_resume.status_code, blocked_resume.text)
        blocked_prepare = self.client.post(base + "/prepare", json={
            "instruction":"blocked after partial stop", "agent_ids":["ERPcoder"],
            "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
        }, headers={**main,"Idempotency-Key":"partial-stop-new-blocked"})
        self.assertEqual(409, blocked_prepare.status_code, blocked_prepare.text)

        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_deliveries SET status='stopped',error_code=NULL WHERE id=?", (deliveries["ERPmanager"],))
            con.commit()
        confirmed = self.client.post(base + "/stop-ack", json={"delivery_id":deliveries["ERPmanager"]}, headers={**main,"Idempotency-Key":"partial-stop-ack-all"})
        self.assertEqual("stopped", confirmed.json()["status"])
        still_needs_approval = self.client.post(base + "/resume", json={}, headers={**main,"Idempotency-Key":"partial-stop-resume-no-approval"})
        self.assertEqual(409, still_needs_approval.status_code, still_needs_approval.text)
        awaiting = self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**main,"Idempotency-Key":"partial-stop-awaiting"})
        self.assertEqual(200, awaiting.status_code, awaiting.text)
        approval = self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json={"decision":"approved","expires_at":int(time.time())+500}, headers={**main,"Idempotency-Key":"partial-stop-fresh-approval"})
        self.assertEqual(200, approval.status_code, approval.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            cycle_at = con.execute("SELECT stop_requested_at FROM war_project_control WHERE project_id='plachem-agent-war-room'").fetchone()[0]
            con.execute("UPDATE war_approvals SET created_at=? WHERE id=?", (cycle_at + 1, approval.json()["approval_id"]))
            con.commit()
        resumed = self.client.post(base + "/resume", json={}, headers={**main,"Idempotency-Key":"partial-stop-resume-approved"})
        self.assertEqual(200, resumed.status_code, resumed.text)
        self.assertEqual("running", resumed.json()["status"])

    def test_review_fix_stop_cycle_tasks_remain_approval_subjects_after_status_changes(self) -> None:
        base = "/api/war-room/projects/plachem-agent-war-room"
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}

        def prepare(label: str) -> dict:
            response = self.client.post(base + "/prepare", json={
                "instruction":label, "agent_ids":["ERPcoder"],
                "assignee_agent_id":"ERPcoder", "reviewer_agent_id":"ERPqa",
                "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
            }, headers={**main,"Idempotency-Key":f"cycle-prepare-{label}"})
            self.assertEqual(201, response.status_code, response.text)
            return response.json()

        unrelated = prepare("unrelated-awaiting-task")
        affected = [prepare("affected-one"), prepare("affected-two")]
        deliveries = []
        for index, item in enumerate(affected):
            run = self.client.post(
                f"/api/war-room/tasks/{item['task_id']}/approve-execute",
                json={"expires_at":int(time.time())+500},
                headers={**main,"Idempotency-Key":f"cycle-initial-run-{index}"},
            )
            self.assertEqual(200, run.status_code, run.text)
            deliveries.append(run.json()["deliveries"][0]["delivery_id"])

        stopped = self.client.post(base + "/stop", json={}, headers={**main,"Idempotency-Key":"cycle-stop"})
        self.assertEqual(200, stopped.status_code, stopped.text)
        for index, delivery_id in enumerate(deliveries):
            ack = self.client.post(
                base + "/stop-ack", json={"delivery_id":delivery_id},
                headers={**main,"Idempotency-Key":f"cycle-stop-ack-{index}"},
            )
            self.assertEqual(200, ack.status_code, ack.text)
        self.assertEqual("stopped", ack.json()["status"])

        for index, item in enumerate(affected):
            awaiting = self.client.post(
                f"/api/war-room/tasks/{item['task_id']}/transition",
                json={"status":"awaiting_approval"},
                headers={**main,"Idempotency-Key":f"cycle-awaiting-{index}"},
            )
            self.assertEqual(200, awaiting.status_code, awaiting.text)

        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            cycle_at = con.execute(
                "SELECT stop_requested_at FROM war_project_control WHERE project_id=?",
                ("plachem-agent-war-room",),
            ).fetchone()[0]

        with mock.patch("war_room_actions._now", return_value=cycle_at + 1):
            approved = self.client.post(
                f"/api/war-room/tasks/{affected[1]['task_id']}/approvals",
                json={"decision":"approved", "expires_at":cycle_at+500},
                headers={**main,"Idempotency-Key":"cycle-fresh-approval-two"},
            )
            self.assertEqual(200, approved.status_code, approved.text)
            missing_blocked = self.client.post(
                base + "/resume", json={},
                headers={**main,"Idempotency-Key":"cycle-resume-missing"},
            )
        self.assertEqual(409, missing_blocked.status_code, missing_blocked.text)

        rejected = self.client.post(
            f"/api/war-room/tasks/{affected[0]['task_id']}/approvals",
            json={"decision":"rejected"},
            headers={**main,"Idempotency-Key":"cycle-rejected-one"},
        )
        self.assertEqual(200, rejected.status_code, rejected.text)
        self.assertEqual("draft", rejected.json()["status"])
        rejected_blocked = self.client.post(
            base + "/resume", json={},
            headers={**main,"Idempotency-Key":"cycle-resume-rejected"},
        )
        self.assertEqual(409, rejected_blocked.status_code, rejected_blocked.text)

        awaiting_again = self.client.post(
            f"/api/war-room/tasks/{affected[0]['task_id']}/transition",
            json={"status":"awaiting_approval"},
            headers={**main,"Idempotency-Key":"cycle-awaiting-one-again"},
        )
        self.assertEqual(200, awaiting_again.status_code, awaiting_again.text)
        with mock.patch("war_room_actions._now", return_value=cycle_at + 2):
            final_approval = self.client.post(
                f"/api/war-room/tasks/{affected[0]['task_id']}/approvals",
                json={"decision":"approved", "expires_at":cycle_at+500},
                headers={**main,"Idempotency-Key":"cycle-fresh-approval-one"},
            )
            self.assertEqual(200, final_approval.status_code, final_approval.text)
            resumed = self.client.post(
                base + "/resume", json={},
                headers={**main,"Idempotency-Key":"cycle-resume-all-approved"},
            )
        self.assertEqual(200, resumed.status_code, resumed.text)
        self.assertEqual("running", resumed.json()["status"])

        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            unrelated_status = con.execute(
                "SELECT status FROM war_tasks WHERE id=?", (unrelated["task_id"],)
            ).fetchone()[0]
        self.assertEqual("awaiting_approval", unrelated_status)
        prepared_after_resume = prepare("new-task-after-approved-resume")
        self.assertEqual("awaiting_approval", prepared_after_resume["status"])

    def test_integrated_fix_qa_participant_does_not_require_execution_admission(self) -> None:
        from war_room_agents import WarRoomAgent, load_agent_catalog
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        project = self.client.post(
            "/api/war-room/projects", json={"name":"QA role-only admission"},
            headers={**main,"Idempotency-Key":"qa-role-project"},
        ).json()["project_id"]
        catalog = load_agent_catalog()
        catalog["ERPqa"] = WarRoomAgent("ERPqa", True, False, True, ())
        with mock.patch("war_room_actions.load_agent_catalog", return_value=catalog):
            response = self.client.post(
                f"/api/war-room/projects/{project}/participants",
                json={"principal_id":"ERPqa","role":"qa"},
                headers={**main,"Idempotency-Key":"qa-role-add"},
            )
        self.assertEqual(201, response.status_code, response.text)

    def test_integrated_fix_task_stop_is_scoped_and_empty_is_confirmed(self) -> None:
        from war_room_adapter import DeliveryReceipt
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"scoped stop","agent_ids":["ERPcoder"],"deadline_at":int(time.time())+600,"document_version":"baseline-2026-08-23"},
            headers={**main,"Idempotency-Key":"scoped-stop-prepare"},
        ).json()
        run = self.client.post(
            f"/api/war-room/tasks/{prepared['task_id']}/approve-execute",
            json={"expires_at":int(time.time())+500},
            headers={**main,"Idempotency-Key":"scoped-stop-run"},
        ).json()
        delivery_id = run["deliveries"][0]["delivery_id"]
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_deliveries SET status='received',run_id='scoped-run' WHERE id=?", (delivery_id,))
            con.commit()
        class StopAdapter:
            def stop(self, *, delivery_id, agent_id):
                return DeliveryReceipt(delivery_id, "stopped", run_id="scoped-run")
        with mock.patch("war_room_actions._adapter_for_mode", return_value=StopAdapter()):
            stopped = self.client.post(
                f"/api/war-room/tasks/{prepared['task_id']}/stop", json={},
                headers={**main,"Idempotency-Key":"scoped-stop"},
            )
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertTrue(stopped.json()["confirmed"])
        self.assertEqual([delivery_id], stopped.json()["delivery_ids"])
        self.assertEqual("stopped", stopped.json()["status"])

    def test_integrated_fix_qa_guard_and_rework_delivery_generation(self) -> None:
        from war_room_adapter import DeliveryReceipt
        from war_room_worker import process_due_deliveries
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={"instruction":"generation rework","agent_ids":["ERPcoder"],"execution_mode":"LEGACY","deadline_at":int(time.time())+600,"document_version":"baseline-2026-08-23"},
            headers={**main,"Idempotency-Key":"generation-prepare"},
        ).json()
        first = self.client.post(
            f"/api/war-room/tasks/{prepared['task_id']}/approve-execute",
            json={"expires_at":int(time.time())+500},
            headers={**main,"Idempotency-Key":"generation-run-1"},
        )
        blocked_qa = self.client.post(
            f"/api/war-room/tasks/{prepared['task_id']}/transition", json={"status":"qa"},
            headers={**main,"Idempotency-Key":"generation-qa-blocked"},
        )
        self.assertEqual(409, blocked_qa.status_code, blocked_qa.text)
        class InvalidAdapter:
            def deliver(self, **kwargs):
                return DeliveryReceipt(kwargs["delivery_id"], "responded", response_body='{"verdict":"PASS"}')
        result = process_due_deliveries(
            db_path=Path(os.environ["PLACHEM_WAR_ROOM_DB"]), adapter=InvalidAdapter(),
        )
        self.assertEqual("failed", result[0]["status"])
        awaiting = self.client.post(
            f"/api/war-room/tasks/{prepared['task_id']}/transition", json={"status":"awaiting_approval"},
            headers={**main,"Idempotency-Key":"generation-awaiting"},
        )
        self.assertEqual(200, awaiting.status_code, awaiting.text)
        second = self.client.post(
            f"/api/war-room/tasks/{prepared['task_id']}/approve-execute",
            json={"expires_at":int(time.time())+500},
            headers={**main,"Idempotency-Key":"generation-run-2"},
        )
        self.assertEqual(200, second.status_code, second.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            rows = con.execute(
                "SELECT task_revision,status FROM war_deliveries WHERE message_id=? ORDER BY task_revision",
                (prepared["message_id"],),
            ).fetchall()
        self.assertEqual([(1,"failed"),(2,"queued")], rows)

    def test_reapproval_renews_expired_deadline_revokes_old_approval_and_preserves_history(self) -> None:
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={
                "instruction":"expired deadline rework",
                "agent_ids":["ERPcoder"],
                "deadline_at":int(time.time())+600,
                "document_version":"baseline-2026-08-23",
            },
            headers={**main,"Idempotency-Key":"deadline-rework-prepare"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        task_id = prepared.json()["task_id"]
        old_approval = self.client.post(
            f"/api/war-room/tasks/{task_id}/approvals",
            json={"decision":"approved","expires_at":int(time.time())+500},
            headers={**main,"Idempotency-Key":"deadline-old-approval"},
        )
        self.assertEqual(200, old_approval.status_code, old_approval.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute(
                "UPDATE war_tasks SET status='rework_required',revision=2,deadline_at=? WHERE id=?",
                (int(time.time())-1, task_id),
            )
            con.commit()

        renewed = self.client.post(
            f"/api/war-room/tasks/{task_id}/transition",
            json={"status":"awaiting_approval"},
            headers={**main,"Idempotency-Key":"deadline-renew"},
        )
        self.assertEqual(200, renewed.status_code, renewed.text)
        self.assertGreater(renewed.json()["deadline_at"], int(time.time()))
        self.assertEqual([old_approval.json()["approval_id"]], renewed.json()["revoked_approval_ids"])
        fresh_expires_at = int(time.time())+500
        fresh_approval = self.client.post(
            f"/api/war-room/tasks/{task_id}/approvals",
            json={"decision":"approved","expires_at":fresh_expires_at},
            headers={**main,"Idempotency-Key":"deadline-fresh-approval"},
        )
        self.assertEqual(200, fresh_approval.status_code, fresh_approval.text)
        # Also cover a task left in the exact broken live state before this
        # fix: approved revision > 1 with a valid fresh approval but an old
        # task deadline.
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_tasks SET deadline_at=? WHERE id=?", (int(time.time())-1, task_id))
            con.commit()
        running = self.client.post(
            f"/api/war-room/tasks/{task_id}/transition",
            json={"status":"running"},
            headers={**main,"Idempotency-Key":"deadline-running"},
        )
        self.assertEqual(200, running.status_code, running.text)
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            task = con.execute(
                "SELECT status,revision,source_message_id,deadline_at FROM war_tasks WHERE id=?", (task_id,)
            ).fetchone()
            approvals = con.execute(
                "SELECT id,revoked_at FROM war_approvals WHERE task_id=? ORDER BY created_at,id", (task_id,)
            ).fetchall()
        self.assertEqual("running", task[0])
        self.assertEqual(2, task[1])
        self.assertEqual(prepared.json()["message_id"], task[2])
        self.assertEqual(fresh_expires_at, task[3])
        self.assertEqual(2, len(approvals))
        self.assertIsNotNone(dict(approvals)[old_approval.json()["approval_id"]])
        self.assertIsNone(dict(approvals)[fresh_approval.json()["approval_id"]])

    def test_exact_task_lookup_returns_owner_project_and_enriched_task(self) -> None:
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={
                "instruction":"exact global lookup",
                "agent_ids":["ERPcoder"],
                "deadline_at":int(time.time())+600,
                "document_version":"baseline-2026-08-23",
            },
            headers={**main,"Idempotency-Key":"exact-lookup-prepare"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        response = self.client.get(
            f"/api/war-room/tasks/{prepared.json()['task_id']}", headers=main,
        )
        self.assertEqual(200, response.status_code, response.text)
        task = response.json()["task"]
        self.assertEqual(prepared.json()["task_id"], task["id"])
        self.assertEqual("plachem-agent-war-room", task["project_id"])
        self.assertEqual(["ERPcoder"], task["agent_ids"])
        self.assertIn("evidence_count", task)
        missing = self.client.get("/api/war-room/tasks/not-found", headers=main)
        self.assertIn(missing.status_code, {403, 404})

    def test_rework_and_stopped_generations_use_fresh_call_budget_and_delivery_revision(self) -> None:
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        for flow in ("rework_required", "stopped"):
            with self.subTest(flow=flow):
                prepared = self.client.post(
                    "/api/war-room/projects/plachem-agent-war-room/prepare",
                    json={
                        "instruction":f"{flow} generation budget",
                        "agent_ids":["ERPcoder"],
                        "deadline_at":int(time.time())+600,
                        "document_version":"baseline-2026-08-23",
                    },
                    headers={**main,"Idempotency-Key":f"{flow}-generation-prepare"},
                )
                self.assertEqual(201, prepared.status_code, prepared.text)
                task_id = prepared.json()["task_id"]
                first = self.client.post(
                    f"/api/war-room/tasks/{task_id}/approve-execute",
                    json={"expires_at":int(time.time())+500},
                    headers={**main,"Idempotency-Key":f"{flow}-generation-first"},
                )
                self.assertEqual(200, first.status_code, first.text)
                old_delivery_id = first.json()["deliveries"][0]["delivery_id"]
                old_terminal = "stopped" if flow == "stopped" else "responded"
                with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
                    con.execute(
                        "UPDATE war_deliveries SET status=? WHERE id=?", (old_terminal, old_delivery_id)
                    )
                    con.execute(
                        """INSERT INTO war_task_calls(task_id,task_revision,call_count,turn_count,updated_at)
                           VALUES (?,1,1,1,?)""",
                        (task_id, int(time.time())),
                    )
                    con.execute(
                        "UPDATE war_tasks SET status=?,revision=2,deadline_at=? WHERE id=?",
                        (flow, int(time.time())-1, task_id),
                    )
                    con.commit()

                awaiting = self.client.post(
                    f"/api/war-room/tasks/{task_id}/transition",
                    json={"status":"awaiting_approval","deadline_at":int(time.time())+600},
                    headers={**main,"Idempotency-Key":f"{flow}-generation-awaiting"},
                )
                self.assertEqual(200, awaiting.status_code, awaiting.text)
                current_revision = 3 if flow == "stopped" else 2
                approved = self.client.post(
                    f"/api/war-room/tasks/{task_id}/approvals",
                    json={"decision":"approved","expires_at":int(time.time())+500},
                    headers={**main,"Idempotency-Key":f"{flow}-generation-approval"},
                )
                self.assertEqual(200, approved.status_code, approved.text)
                running = self.client.post(
                    f"/api/war-room/tasks/{task_id}/transition",
                    json={"status":"running"},
                    headers={**main,"Idempotency-Key":f"{flow}-generation-running"},
                )
                self.assertEqual(200, running.status_code, running.text)
                delivery = self.client.post(
                    f"/api/war-room/messages/{prepared.json()['message_id']}/deliveries",
                    json={"task_id":task_id,"agent_ids":["ERPcoder"]},
                    headers={**main,"Idempotency-Key":f"{flow}-generation-delivery"},
                )
                self.assertEqual(201, delivery.status_code, delivery.text)
                with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
                    revisions = con.execute(
                        "SELECT task_revision,status FROM war_deliveries WHERE message_id=? ORDER BY task_revision",
                        (prepared.json()["message_id"],),
                    ).fetchall()
                    calls = con.execute(
                        "SELECT task_revision,call_count,turn_count FROM war_task_calls WHERE task_id=? ORDER BY task_revision",
                        (task_id,),
                    ).fetchall()
                    pk = {row[1]:row[5] for row in con.execute("PRAGMA table_info(war_task_calls)")}
                self.assertEqual([(1,old_terminal),(current_revision,"queued")], revisions)
                self.assertEqual([(1,1,1)], calls)
                self.assertEqual(1, pk["task_id"])
                self.assertEqual(2, pk["task_revision"])
                with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
                    con.execute(
                        "UPDATE war_deliveries SET status='responded' WHERE id=?",
                        (delivery.json()["delivery_id"],),
                    )
                    con.commit()

    def test_call_counter_migration_preserves_legacy_totals_and_is_idempotent(self) -> None:
        from war_room_actions import provision_action_schema

        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={
                "instruction":"legacy counter migration",
                "agent_ids":["ERPcoder"],
                "deadline_at":int(time.time())+600,
                "document_version":"baseline-2026-08-23",
            },
            headers={**main,"Idempotency-Key":"legacy-counter-prepare"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        task_id = prepared.json()["task_id"]
        db_path = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        with sqlite3.connect(db_path) as con:
            con.execute("DROP TABLE war_task_calls")
            con.execute("""CREATE TABLE war_task_calls (
                task_id TEXT PRIMARY KEY REFERENCES war_tasks(id),
                call_count INTEGER NOT NULL DEFAULT 0,
                turn_count INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )""")
            con.execute(
                "INSERT INTO war_task_calls(task_id,call_count,turn_count,updated_at) VALUES (?,3,2,?)",
                (task_id, int(time.time())),
            )
            con.commit()

        provision_action_schema(str(db_path))
        provision_action_schema(str(db_path))
        with sqlite3.connect(db_path) as con:
            rows = con.execute(
                "SELECT task_revision,call_count,turn_count FROM war_task_calls WHERE task_id=?",
                (task_id,),
            ).fetchall()
            totals = con.execute(
                "SELECT SUM(call_count),SUM(turn_count) FROM war_task_calls WHERE task_id=?",
                (task_id,),
            ).fetchone()
            pk = {row[1]:row[5] for row in con.execute("PRAGMA table_info(war_task_calls)")}
        self.assertEqual([(1,3,2)], rows)
        self.assertEqual((3,2), totals)
        self.assertEqual({"task_id":1,"task_revision":2}, {key:pk[key] for key in ("task_id","task_revision")})

    def test_delivery_list_maps_each_revision_to_its_exact_run_once(self) -> None:
        main = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={
                "instruction":"exact delivery run mapping",
                "agent_ids":["ERPcoder"],
                "deadline_at":int(time.time())+600,
                "document_version":"baseline-2026-08-23",
            },
            headers={**main,"Idempotency-Key":"exact-run-map-prepare"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        task_id, message_id = prepared.json()["task_id"], prepared.json()["message_id"]
        now = int(time.time())
        deliveries = [
            ("map-delivery-old","openclaw-old",1),
            ("map-delivery-new","openclaw-new",2),
            ("map-delivery-legacy",None,3),
        ]
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            for delivery_id, run_id, revision in deliveries:
                con.execute(
                    """INSERT INTO war_deliveries
                       (id,message_id,agent_id,task_revision,status,attempt_count,run_id,created_at)
                       VALUES (?,?,?,?,'responded',1,?,?)""",
                    (delivery_id,message_id,"ERPcoder",revision,run_id,now+revision),
                )
            for suffix, summary in (("old","old-result"),("new","new-result")):
                con.execute(
                    """INSERT INTO war_execution_runs
                       (core_run_id,war_project_id,war_task_id,agent_id,openclaw_run_id,run_status,
                        result_summary,created_at,updated_at)
                       VALUES (?,?,?,?,?,'PASS',?,?,?)""",
                    (f"war-openclaw-{suffix}","plachem-agent-war-room",task_id,"ERPcoder",f"openclaw-{suffix}",summary,now,now),
                )
            con.commit()
        response = self.client.get(
            "/api/war-room/projects/plachem-agent-war-room/deliveries", headers=main,
        )
        self.assertEqual(200, response.status_code, response.text)
        by_id: dict[str,list[dict]] = {}
        for item in response.json()["items"]:
            if item["id"].startswith("map-delivery-"):
                by_id.setdefault(item["id"], []).append(item)
        self.assertEqual({key:1 for key,_,_ in deliveries}, {key:len(value) for key,value in by_id.items()})
        self.assertEqual("war-openclaw-old", by_id["map-delivery-old"][0]["core_run_id"])
        self.assertEqual("old-result", by_id["map-delivery-old"][0]["result_summary"])
        self.assertEqual("war-openclaw-new", by_id["map-delivery-new"][0]["core_run_id"])
        self.assertEqual("new-result", by_id["map-delivery-new"][0]["result_summary"])
        self.assertIsNone(by_id["map-delivery-legacy"][0]["core_run_id"])
        self.assertIsNone(by_id["map-delivery-legacy"][0]["result_summary"])

if __name__ == "__main__":
    unittest.main()
