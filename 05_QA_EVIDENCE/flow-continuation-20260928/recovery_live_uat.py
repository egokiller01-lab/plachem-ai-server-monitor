"""Isolated API UAT with actual configured agents; never production task approval."""
import json
import os
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
STATE = Path(__file__).parent / ("live-state-" + str(int(time.time())))
STATE.mkdir()
assert STATE.is_relative_to(ROOT / "05_QA_EVIDENCE")

scenario = "read"
fixture = STATE / "business-fixture"
fixture.mkdir()
if scenario == "read":
    (fixture / "receivables.json").write_text(json.dumps({"opening":1250000,"invoices":[320000,180000],"receipts":[250000,100000]}))
    instruction = f"Read {fixture / 'receivables.json'} only. Calculate closing receivables as opening plus invoices minus receipts, show the calculation and source path. No file changes or new output files."
    task_profile = "READ_ONLY"
else:
    (fixture / "ledger.py").write_text("def closing(opening, invoices, receipts):\n    return opening + sum(invoices) + sum(receipts)\n")
    (fixture / "test_ledger.py").write_text("from ledger import closing\ndef test_balance():\n    assert closing(1250000,[320000,180000],[250000,100000]) == 1400000\ndef test_empty():\n    assert closing(10,[],[]) == 10\n")
    instruction = f"In isolated fixture {fixture}, fix only ledger.py so closing receivables equals opening plus invoices minus receipts. Do not change test_ledger.py. Run python3 -B -m pytest -p no:cacheprovider {fixture / 'test_ledger.py'} and report exact results. No production files, services, git or databases may be changed."
    task_profile = "CODE_CHANGE"

main_token, proxy_secret = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
os.environ.update({
    "PLACHEM_WAR_ROOM_DB": str(STATE / "war-room.sqlite3"),
    "PLACHEM_FAST_GATEWAY_RUNS": str(STATE / "runs.jsonl"),
    "PLACHEM_FAST_GATEWAY_BINDINGS": str(STATE / "bindings.sqlite3"),
    "PLACHEM_AUTH_BROKER_DB": str(STATE / "broker.sqlite3"),
    "PLACHEM_AUTH_BROKER_KEY_ID": "uat", "PLACHEM_AUTH_BROKER_PEPPER_UAT": secrets.token_urlsafe(32),
    "PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS": json.dumps({"main": main_token}),
    "PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS": "isolated-uat-approver",
    "PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET": proxy_secret,
    "PLACHEM_WAR_ROOM_SESSION_SECRET": secrets.token_urlsafe(32),
    "PLACHEM_WAR_ROOM_QA_SIGNING_SECRET": secrets.token_urlsafe(32),
    "PLACHEM_WAR_ROOM_REAL_ADAPTER": "1", "PLACHEM_WAR_ROOM_AUTO_QA": "1",
    "PLACHEM_FAST_GATEWAY_ENABLED": "1", "PLACHEM_WAR_ROOM_WORKTREE": str(ROOT),
})
os.environ.pop("PLACHEM_FAST_GATEWAY_CORE_DB", None)
os.environ.pop("PLACHEM_WAR_ROOM_TEST_ADAPTER", None)
assert os.environ.get("OPENCLAW_GATEWAY_TOKEN"), "service credential unavailable"
import plachem_fast_gateway.openclaw_adapter as transport
transport.DEFAULT_DEVICE_IDENTITY_PATH = ROOT.parent / "plachem-ai-server-monitor/runtime/fast-gateway-device-identity.json"
assert transport.DEFAULT_DEVICE_IDENTITY_PATH.is_file(), "registered service identity unavailable"
from fastapi.testclient import TestClient
from app import app
import war_room
assert war_room._db_path().resolve() == (STATE / "war-room.sqlite3").resolve()
main = {"X-War-Room-Actor": "main", "X-War-Room-Token": main_token}
rep = {"X-Authenticated-Principal": "isolated-uat-approver", "X-War-Room-Proxy-Secret": proxy_secret}
report = {"scenario": scenario, "fixture":str(fixture), "production_modified": False, "representative_completion_performed": False,
          "real_agents": True, "isolated_database": str(STATE / "war-room.sqlite3")}
with TestClient(app) as client:
    from war_room_runtime import get_runtime
    owner = get_runtime().adapter
    original_create = owner.create_disposable_session
    injection = {"count": 0}
    def fail_qa_creation_once(**kwargs):
        injection["count"] += 1
        if injection["count"] == 1:
            raise RuntimeError("ISOLATED_UAT_QA_PROVISION_FAULT")
        return original_create(**kwargs)
    owner.create_disposable_session = fail_qa_creation_once
    resumed = False
    main["Idempotency-Key"] = "uat-prepare-" + STATE.name
    response = client.post("/api/war-room/projects/plachem-agent-war-room/prepare", headers=main, json={
        "instruction": instruction,
        "agent_ids": ["ERPmanager"], "reviewer_agent_id": "ERPqa", "task_profile": task_profile,
        "execution_mode": "FAST_GATEWAY", "document_version": "baseline-2026-08-23",
        "deadline_at": int(time.time()) + 9000,
        "grounding": {"worktree": str(fixture), "forbidden": ["production DB", "existing work sessions", "merge/push/deploy"]},
    })
    report["prepare_http"] = response.status_code
    assert response.status_code == 201, response.text
    task_id = response.json()["task_id"]
    report["task_id"] = task_id
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                         params={"action": "task_approve_execute", "target_id": task_id})
    assert context.status_code == 200, context.text
    rep["Idempotency-Key"] = "uat-approve-" + STATE.name
    approved = client.post(f"/api/war-room/tasks/{task_id}/approve-execute", headers=rep,
                           json={"expires_at": int(time.time()) + 3600,
                                 "context_token": context.json()["context_token"]})
    report["approve_http"] = approved.status_code
    assert approved.status_code == 200, approved.text
    print(json.dumps({"event": "isolated_test_started", "task_id": task_id}), flush=True)
    start = time.monotonic()
    last = None
    while time.monotonic() - start < 240:
        state = client.get(f"/api/war-room/tasks/{task_id}", headers=rep).json().get("task", {})
        summary = {"status": state.get("status"), "state": state.get("state"), "qa": state.get("latest_qa_verdict")}
        if summary != last:
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            last = summary
        if not resumed and state.get("status") == "qa" and any(i.get("stage") == "QA" for i in state.get("processing_issues", [])):
            ctx = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                             params={"action":"task_resume_qa", "target_id":task_id})
            assert ctx.status_code == 200, ctx.text
            response = client.post(f"/api/war-room/tasks/{task_id}/resume-qa", headers={**rep,"Idempotency-Key":"resume-"+STATE.name},
                json={"context_token":ctx.json()["context_token"],"contract_version":1,
                      "project_id":"plachem-agent-war-room","task_id":task_id,"task_revision":state["revision"]})
            report["resume_reply"] = {"http":response.status_code,"body":response.json()}
            print(json.dumps({"event":"real_qa_resume", **report["resume_reply"]}), flush=True)
            resumed = True
        if state.get("latest_qa_verdict") or state.get("status") in {"rework_required", "stopped"}:
            break
        time.sleep(2)
    report["task"] = state
    import sqlite3
    import war_room_actions as actions
    with sqlite3.connect(STATE / "war-room.sqlite3") as db:
        db.row_factory = sqlite3.Row
        current_task = db.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        report["completion_checks"] = actions._representative_completion_checks(db, current_task)
    print(json.dumps({"final_approval_missing": report["completion_checks"]["missing"]}), flush=True)
    report["deliveries"] = client.get("/api/war-room/projects/plachem-agent-war-room/deliveries", headers=rep).json()
    if state.get("status") in {"running", "qa"} and not state.get("latest_qa_verdict"):
        context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                             params={"action": "task_stop", "target_id": task_id}).json()
        report["stop_http"] = client.post(f"/api/war-room/tasks/{task_id}/stop", headers=rep,
                                         json={"context_token": context.get("context_token")}).status_code
    report["injected_qa_faults"] = 1 if injection["count"] else 0
    report["qa_provision_calls"] = injection["count"]
    report["elapsed_seconds"] = round(time.monotonic() - start, 2)
(STATE / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({"event": "isolated_test_finished", "result_path": str(STATE / "result.json"),
                  "task_status": state.get("status"), "qa": state.get("latest_qa_verdict")}, ensure_ascii=False), flush=True)
