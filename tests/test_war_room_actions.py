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
        os.environ.pop("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", None)
        os.environ.pop("PLACHEM_WAR_ROOM_SESSION_SECRET", None)
        os.environ.pop("PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET", None)
        os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        os.environ.pop("PLACHEM_WAR_ROOM_ADAPTER_COMMAND", None)
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

    def test_execution_endpoint_does_not_call_external_agent(self) -> None:
        response = self.client.post("/api/war-room/tasks/not-a-task/execute", headers={"X-War-Room-Actor": "main", "Idempotency-Key": "execute-1"})
        self.assertIn(response.status_code, {401, 403, 404})

    def test_integrated_prepare_is_atomic_and_idempotent(self) -> None:
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token", "Idempotency-Key":"prepare-one"}
        payload = {
            "instruction":"원자 준비 테스트", "agent_ids":["ERPcoder","ERPqa"],
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
            json={"instruction":"세 에이전트 협업", "agent_ids":["ERPcoder","ERPqa","ERPmanager"], "deadline_at":int(time.time())+1800, "document_version":"baseline-2026-08-23"},
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
        self.assertEqual(["ERPcoder","ERPmanager","ERPqa"], [row["agent_id"] for row in run.json()["deliveries"]])

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
            json={"instruction":"통합 실행 테스트","agent_ids":["ERPcoder","ERPqa"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
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
        self.assertEqual("failed", result[0]["status"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("running", con.execute("SELECT status FROM war_tasks WHERE id=?", (prepared["task_id"],)).fetchone()[0])

        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            con.execute("UPDATE war_deliveries SET status='queued',attempt_count=0,error_code=NULL WHERE message_id=?", (prepared["message_id"],)); con.commit()
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
                prepared=self.client.post("/api/war-room/projects/plachem-agent-war-room/prepare",json={"instruction":expected,"agent_ids":["ERPcoder","ERPqa"],"deadline_at":int(time.time())+900,"document_version":"baseline-2026-08-23"},headers={**headers,"Idempotency-Key":f"terminal-prepare-{index}"}).json()
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
            con.execute("INSERT INTO war_evidence (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("rep-evidence",task_id,"test","/tmp/rep","pass",scope[2],scope_hash,scope[1],1,int(time.time())))
            con.execute("INSERT INTO war_qa_verdicts (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,task_revision,scope_hash,document_version,qa_cycle,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("rep-verdict",task_id,"ERPqa","PASS","required:test","sig","payload",scope[2],scope_hash,scope[1],1,int(time.time())))
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
            json={"instruction":"QA 실패 전이 테스트","agent_ids":["ERPcoder","ERPqa"],"deadline_at":int(time.time())+1800,"document_version":"baseline-2026-08-23"},
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
        self.assertIn('document.getElementById("results-qa").scrollIntoView', javascript)
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
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("hard-source", war_room.PROJECT_ID, "instruction", "agent", "main", "bound", None, None, int(time.time()), "hard-corr", "clean"))
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
                "INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("fixture-retry-message", war_room.PROJECT_ID, "instruction", "agent", "main", "same bytes", None, None, now, "corr-retry", "clean"),
            )
            con.execute(
                "INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,max_attempts,next_attempt_at,deadline_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("fixture-retry-delivery", "fixture-retry-message", "ERPcoder", "queued", 0, 2, now, now + 30, now),
            )
            con.commit()
        adapter = ScriptedAdapter()
        first = process_due_deliveries(db_path=db, adapter=adapter, now=now)
        self.assertEqual("failed", first[0]["status"])
        self.assertEqual(["fixture-retry-delivery"], adapter.calls)
        with _test_db(db) as con:
            row = con.execute("SELECT status,attempt_count FROM war_deliveries WHERE id='fixture-retry-delivery'").fetchone()
            self.assertEqual(("failed", 1), row)
        self.assertTrue(request_delivery_retry(db_path=db, delivery_id="fixture-retry-delivery", now=now + 1))
        second = process_due_deliveries(db_path=db, adapter=adapter, now=now + 1)
        self.assertEqual("responded", second[0]["status"])
        self.assertEqual(["fixture-retry-delivery", "fixture-retry-delivery"], adapter.calls)
        # A fresh worker instance must not replay a terminal delivery.
        self.assertEqual([], process_due_deliveries(db_path=db, adapter=adapter, now=now + 2))
        self.assertEqual(2, len(adapter.calls))

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
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("fixture-stop-message", war_room.PROJECT_ID, "instruction", "agent", "main", "stop bytes", None, None, now, "corr-stop", "clean"))
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

    def test_R_NCAFXY_manyfast_version_drift_persists_and_revokes_approval(self) -> None:
        """R-NCAFXY/F-NCAFXY: drift persists reference and invalidates approval."""
        headers = {"X-War-Room-Actor":"main", "X-War-Room-Token":"fixture-main-token"}
        created = self.client.post("/api/war-room/projects/plachem-agent-war-room/tasks", json=self.task_body("drift task"), headers={**headers,"Idempotency-Key":"drift-task"})
        task_id = created.json()["task_id"]
        self.client.post(f"/api/war-room/tasks/{task_id}/transition", json={"status":"awaiting_approval"}, headers={**headers,"Idempotency-Key":"drift-await"})
        approved = self.client.post(f"/api/war-room/tasks/{task_id}/approvals", json=self.approval_body(), headers={**headers,"Idempotency-Key":"drift-approve"})
        self.assertEqual(200, approved.status_code, approved.text)
        drift = self.client.put("/api/war-room/projects/plachem-agent-war-room/manyfast-reference", json={"document_version":"baseline-v2"}, headers={**headers,"Idempotency-Key":"drift-ref"})
        self.assertEqual(200, drift.status_code, drift.text)
        self.assertTrue(drift.json()["drift"])
        self.assertEqual(1, drift.json()["invalidated_tasks"])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            self.assertEqual("awaiting_approval", con.execute("SELECT status FROM war_tasks WHERE id=?", (task_id,)).fetchone()[0])
            self.assertIsNotNone(con.execute("SELECT revoked_at FROM war_approvals WHERE task_id=?", (task_id,)).fetchone()[0])

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
        sessions_dir = Path(os.environ["OPENCLAW_HOME"]) / "agents" / "ERPcoder" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "sessions.json").write_text(json.dumps({"agent:ERPcoder:fixture":{"sessionId":"session-fixture","updatedAt":2}}), encoding="utf-8")
        calls: list[list[str]] = []
        def runner(args, **kwargs):
            calls.append(args)
            method = args[args.index("call") + 1]
            if method == "chat.history":
                payload = {"result":{"sessionInfo":{"hasActiveRun":False,"activeRunIds":[]},"messages":[]}}
            elif method == "chat.send":
                payload = {"result":{"runId":"run-1"}}
            else:
                payload = {"result":{"aborted":True,"runIds":["run-1"]}}
                params = json.loads(args[args.index("--params") + 1])
                self.assertEqual("run-1", params.get("runId"))
            stdout = json.dumps(payload)
            return mock.Mock(returncode=0, stdout=stdout)
        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"
        try:
            adapter = OpenClawSessionAdapter(runner=runner)
            adapter.bind_delivery("delivery-real", session_key="agent:erpcoder:war-room-test:fixture", session_id="session-fixture", disposable=True, purpose="test")
            self.assertEqual("received", adapter.deliver(delivery_id="delivery-real", agent_id="ERPcoder", instruction_id="instruction-real", body="same bytes").status)
            self.assertEqual("stopped", adapter.stop(delivery_id="delivery-real", agent_id="ERPcoder").status)
        finally:
            os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        self.assertEqual(["chat.history","chat.send","chat.abort"], [call[call.index("call") + 1] for call in calls])

    def test_real_adapter_creates_only_explicit_agent_owned_disposable_session(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter

        calls: list[list[str]] = []
        def runner(command, **kwargs):
            calls.append(command)
            method = command[command.index("call") + 1]
            params = json.loads(command[command.index("--params") + 1])
            self.assertEqual("sessions.create", method)
            self.assertEqual("erpcoder", params["agentId"])
            self.assertTrue(params["key"].startswith("agent:erpcoder:war-room-test:"))
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({"result": {"ok": True, "key": params["key"], "sessionId": "session-disposable"}}), "stderr": ""})()

        adapter = OpenClawSessionAdapter(runner=runner)
        binding = adapter.create_disposable_session(agent_id="ERPcoder", project_id="fixture-project")
        self.assertEqual("session-disposable", binding["session_id"])
        self.assertEqual("test", binding["purpose"])
        self.assertTrue(binding["disposable"])
        self.assertEqual(1, len(calls))

    def test_real_adapter_refuses_second_active_run_in_disposable_session(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter
        methods: list[str] = []
        def runner(command, **kwargs):
            method = command[command.index("call") + 1]
            methods.append(method)
            return mock.Mock(returncode=0, stdout=json.dumps({"result":{"sessionInfo":{"hasActiveRun":True,"activeRunIds":["existing-run"]},"messages":[]}}))
        os.environ["PLACHEM_WAR_ROOM_REAL_ADAPTER"] = "1"
        try:
            adapter = OpenClawSessionAdapter(runner=runner)
            adapter.bind_delivery("delivery-active", session_key="agent:erpcoder:war-room-test:active", session_id="session-active", disposable=True, purpose="test")
            receipt = adapter.deliver(delivery_id="delivery-active", agent_id="ERPcoder", instruction_id="instruction", body="must not send")
        finally:
            os.environ.pop("PLACHEM_WAR_ROOM_REAL_ADAPTER", None)
        self.assertEqual("openclaw_session_active_run_exists", receipt.error_code)
        self.assertEqual(["chat.history"], methods)

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
                if method == "chat.history":
                    return {"sessionInfo":{"hasActiveRun":False,"activeRunIds":[]},"messages":[]}, self.connection_id
                if method == "chat.send":
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
        self.assertEqual(["chat.history","chat.send","bridge.status"], bridge.calls)

    def test_abort_uses_same_persistent_gateway_connection_and_exact_run_id(self) -> None:
        from war_room_adapter import OpenClawSessionAdapter
        class Bridge:
            connection_id = "same-connection"
            def __init__(self): self.calls = []
            def request(self, method, params, timeout_ms=15000):
                self.calls.append((method, params))
                results = {
                    "chat.history":{"sessionInfo":{"hasActiveRun":False,"activeRunIds":[]},"messages":[]},
                    "chat.send":{"runId":"same-run","status":"started"},
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
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",("runtime-message",war_room.PROJECT_ID,"instruction","agent","main","safe",None,None,now,"runtime-corr","clean"))
            con.execute("INSERT INTO war_deliveries(id,message_id,agent_id,status,attempt_count,max_attempts,next_attempt_at,deadline_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",("runtime-delivery","runtime-message","ERPcoder","queued",0,3,now,now+60,now))
            con.commit()
        runtime=WarRoomRuntime(adapter=OwnedAdapter())
        self.assertEqual("received",runtime.tick(db_path=db,now=now)[0]["status"])
        self.assertEqual("stopped",runtime.stop_project(db_path=db,project_id=war_room.PROJECT_ID,actor_id="main",now=now+1)["deliveries"][0]["status"])
        with _test_db(db) as con:
            con.execute("UPDATE war_deliveries SET status='received',run_id='owned-run' WHERE id='runtime-delivery'"); con.commit()
        restarted=WarRoomRuntime(adapter=OwnedAdapter())
        self.assertEqual("failed",restarted.stop_project(db_path=db,project_id=war_room.PROJECT_ID,actor_id="main",now=now+2)["deliveries"][0]["status"])

    def test_fresh_real_adapter_rebinds_received_run_from_database_before_poll(self) -> None:
        """Worker restart must not lose the session needed to collect chat.history."""
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
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("restart-message",war_room.PROJECT_ID,"instruction","agent","main","safe test",None,None,now,"restart-corr","clean"))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,max_attempts,deadline_at,created_at,run_id) VALUES (?,?,?,?,?,?,?,?,?)", ("restart-delivery","restart-message","ERPcoder","received",1,3,now+30,now,"run-restart"))
            con.commit()
        adapter = FreshAdapter()
        result = recover_received_deliveries(db_path=db, gateway=adapter, now=now+1)
        self.assertEqual([{"delivery_id":"restart-delivery","run_id":"run-restart","status":"responded"}], result)
        self.assertEqual(("agent:erpcoder:war-room-test:fixture","session-disposable"), adapter.bound["run-restart"])

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
            "instruction":"immutable recovery", "agent_ids":["ERPcoder"],
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
        task=self.client.post(base+"/tasks",json=self.task_body("fanout",agent_ids=["ERPcoder","ERPqa"],call_limit=2),headers={**headers,"Idempotency-Key":"fan-task"})
        task_id=task.json()["task_id"]
        instruction=self.client.post(base+"/instructions",json={"task_id":task_id,"body":"identical bytes"},headers={**headers,"Idempotency-Key":"fan-inst"})
        mid=instruction.json()["message_id"]
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"awaiting_approval"},headers={**headers,"Idempotency-Key":"fan-await"})
        self.client.post(f"/api/war-room/tasks/{task_id}/approvals",json=self.approval_body(),headers={**headers,"Idempotency-Key":"fan-approve"})
        self.client.post(f"/api/war-room/tasks/{task_id}/transition",json={"status":"running"},headers={**headers,"Idempotency-Key":"fan-run"})
        fan=self.client.post(f"/api/war-room/messages/{mid}/deliveries",json={"task_id":task_id,"agent_ids":["ERPcoder","ERPqa"]},headers={**headers,"Idempotency-Key":"fan-send"})
        self.assertEqual(201,fan.status_code,fan.text); self.assertEqual(["ERPcoder","ERPqa"],[x["agent_id"] for x in fan.json()["deliveries"]])
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            rows=con.execute("SELECT d.agent_id,m.body FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id WHERE d.message_id=? ORDER BY d.agent_id",(mid,)).fetchall()
        self.assertEqual([("ERPcoder","identical bytes"),("ERPqa","identical bytes")],rows)
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

    def test_instruction_ui_calls_delivery_and_never_labels_message_only_as_queued(self) -> None:
        html = (Path(__file__).parents[1] / "static" / "war-room.html").read_text(encoding="utf-8")
        javascript = (Path(__file__).parents[1] / "static" / "war-room-ui.js").read_text(encoding="utf-8")
        self.assertNotIn("qa-signature", html + javascript)
        self.assertIn('source:"agent_result"', javascript)
        for stable_id in ("task-agent-targets","demo-controls","demo-session-key","stop-ack-delivery","delivery-cards"):
            self.assertIn(f'id="{stable_id}"', html)
        self.assertIn("agent_ids: task.agent_ids", javascript)
        self.assertIn("retryDemoDelivery", javascript)
        self.assertIn("processDemoQueue", javascript)
        self.assertIn("`${base}/instructions`", javascript)
        self.assertIn("message-task", html)
        self.assertIn("/api/war-room/messages/${task.source_message_id}/deliveries", javascript)
        self.assertIn("승인 후 실행하세요", javascript)
        self.assertNotIn("`queued ${result.id.slice(0,8)}`", html + javascript)
        for stable_id in ("quick-task-form","quick-instruction","quick-agent-targets","quick-approve-run","quick-delivery-cards","advanced-area"):
            self.assertIn(f'id="{stable_id}"', html)
        self.assertIn("`${base}/prepare`", javascript)
        self.assertIn("/approve-execute", javascript)
        self.assertIn('"qa"].includes(task.status)', javascript)
        self.assertIn('task.status === "completed"', javascript)
        self.assertIn("quickApproveAndRun", javascript)
        self.assertIn("충돌과 다음 행동을 확인하세요", javascript)
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
            "instruction":"현재 기준만 검증", "agent_ids":["ERPcoder","ERPqa","ERPmanager"],
            "deadline_at":int(time.time())+600, "document_version":"baseline-2026-08-23",
            "grounding":{"worktree":"/safe/worktree","branch":"fix/collab","revision":"abc123","api_base":"http://127.0.0.1:8114/api/war-room","db_label":"isolated-demo","forbidden":["production DB"],"completion_conditions":["tests pass"]},
        }, headers=headers)
        self.assertEqual(201, response.status_code, response.text)
        with _test_db(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as con:
            packet = json.loads(con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (response.json()["task_id"],)).fetchone()[0])
            body = con.execute("SELECT body FROM war_messages WHERE id=?", (response.json()["message_id"],)).fetchone()[0]
        self.assertEqual("abc123", packet["revision"])
        self.assertIn("STRUCTURED_RESULT", body)
        self.assertIn("/safe/worktree", body)

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
            con.execute("INSERT INTO war_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("recovery-message", "plachem-agent-war-room", "instruction", "agent", "main", "recovery body", None, None, now, "recovery-corr", "clean"))
            con.execute("INSERT INTO war_deliveries (id,message_id,agent_id,status,attempt_count,run_id,created_at) VALUES (?,?,?,?,?,?,?)", ("recovery-delivery", "recovery-message", "ERPcoder", "received", 1, "run-recover", now))
            con.commit()
        gateway = FakeGatewayAdapter()
        gateway.send(agent_id="ERPcoder", body="recovery body", run_id="run-recover")
        gateway.complete(run_id="run-recover", status="responded", response_body="recovered result")
        recovered = recover_received_deliveries(db_path=db, gateway=gateway, now=now + 1)
        self.assertEqual([{"delivery_id":"recovery-delivery","run_id":"run-recover","status":"responded"}], recovered)
        with _test_db(db) as con:
            self.assertEqual("responded", con.execute("SELECT status FROM war_deliveries WHERE id='recovery-delivery'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
