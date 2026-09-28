"""Full server flow with a simulated OpenClaw boundary, never production approval."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
import fast_gateway_service as service
import war_room_actions as actions
from plachem_fast_gateway.core_engine import production_result_validator
from plachem_fast_gateway.openclaw_adapter import OpenClawAdapter, CoreRunStatus
from war_room_adapter import DeliveryReceipt
from war_room_runtime import WarRoomRuntime
from war_room_task_contract import profile_grounding, verify_receipt
from test_production_authorization_wiring import production

INSTRUCTION = "변경이나 도구 사용 없이 지시 수신 사실과 SIMPLE_UI_TEST_PASS를 보고하라."


def result_body(marker="SIMPLE_UI_TEST_PASS"):
    return {"status": "completed", "summary": marker, "evidence": [
        {"type": "no_change", "detail": "파일 쓰기/edit/배포/DB 접근을 호출하지 않았다."}],
        "artifacts": [], "scope": {"compliant": True, "violations": []}}


def envelope(profile, result, calls=None):
    messages = [{"role": "user", "content": INSTRUCTION}]
    if calls:
        messages.append({"role": "assistant", "content": calls})
    messages.append({"role": "assistant", "content": [{"type": "text", "text": json.dumps(result)}]})
    return {"history": {"messages": messages}, "war_room_task_profile": profile,
            "approved_paths": []}


@pytest.mark.parametrize("profile", ["ACKNOWLEDGEMENT", "READ_ONLY", "CODE_CHANGE"])
def test_profile_has_task_conditions_not_blanket_full_regression(profile):
    packet = profile_grounding({"task_profile": profile}, {})
    assert packet["worker_timeout_seconds"] == packet["qa_timeout_seconds"] == 3600
    assert "full regression passes" not in packet["completion_conditions"]
    assert packet["required_evidence"][0]["id"] == "execution_receipt"


@pytest.mark.parametrize("profile", ["ACKNOWLEDGEMENT", "READ_ONLY"])
def test_negated_report_is_not_a_positive_mutation_claim(profile):
    decision = production_result_validator()(envelope(profile, result_body()))
    assert decision.status == CoreRunStatus.PASS, decision.reason


def test_receipt_only_response_does_not_need_worker_authored_evidence():
    result = result_body()
    result["evidence"] = []
    decision = production_result_validator()(envelope("ACKNOWLEDGEMENT", result))
    assert decision.status == CoreRunStatus.PASS, decision.reason


@pytest.mark.parametrize("profile", ["ACKNOWLEDGEMENT", "READ_ONLY"])
def test_missing_bound_transcript_is_not_accepted(profile):
    decision = production_result_validator()({"result": result_body(), "war_room_task_profile": profile})
    assert decision.status != CoreRunStatus.PASS


@pytest.mark.parametrize("profile", ["ACKNOWLEDGEMENT", "READ_ONLY"])
def test_actual_write_is_rejected_even_if_report_denies_it(profile):
    calls = [{"type": "toolCall", "name": "write", "arguments": {"path": "/tmp/not-approved", "content": "x"}}]
    decision = production_result_validator()(envelope(profile, result_body(), calls))
    assert decision.status != CoreRunStatus.PASS


def test_reading_and_not_modifying_a_file_is_not_contradiction():
    result = result_body()
    result["evidence"] = [{"type": "file", "detail": "Read the file; no files were modified."}]
    calls = [{"type": "toolCall", "name": "read", "arguments": {"path": "/tmp/input.txt"}}]
    decision = production_result_validator()(envelope("READ_ONLY", result, calls))
    assert decision.status == CoreRunStatus.PASS, decision.reason


class WorkerRPC:
    methods = {"agent", "agent.wait", "chat.history", "sessions.describe"}
    def __init__(self, marker):
        self.marker = marker
        self.submitted = {}
    def request(self, method, params, timeout=15, **kwargs):
        if method == "agent":
            key = params["sessionKey"]
            assert key not in self.submitted, "worker resubmission"
            self.submitted[key] = dict(params)
            return {"status": "accepted", "runId": params["idempotencyKey"],
                    "sessionKey": key, "sessionId": "session-" + params["idempotencyKey"]}
        if method == "agent.wait":
            return {"status": "ok", "runId": params["runId"]}
        if method == "chat.history":
            submitted = self.submitted.get(params["sessionKey"])
            if not submitted:
                return {"messages": []}
            messages = envelope("ACKNOWLEDGEMENT", result_body(self.marker))["history"]["messages"]
            messages[0]["content"] = submitted["message"]
            return {"messages": messages}
        raise AssertionError(method)
    request_fresh = request
    def close(self):
        pass


class IndependentReviewer:
    def __init__(self, root):
        self.root, self.calls = root, 0
    def create_disposable_session(self, *, agent_id, project_id):
        return {"session_key": f"agent:{agent_id.lower()}:war-room-test:isolated-qa",
                "session_id": "qa-isolated", "purpose": "test", "disposable": True}
    def deliver(self, *, delivery_id, agent_id, instruction_id, body):
        self.calls += 1
        assert "[AUTO_QA_REVIEW]" in body
        packet = json.loads(body.split("[IMMUTABLE_GROUNDING_PACKET]\n", 1)[1].split("\n[ORIGINAL_INSTRUCTION_CONTEXT]", 1)[0])
        paths = list((self.root / "run-receipts").glob("*.json"))
        assert len(paths) == 1
        receipt = json.loads(paths[0].read_text())
        worker = json.loads(receipt["responses"][0]["body"])
        verdict = "PASS" if worker["summary"] == "SIMPLE_UI_TEST_PASS" else "FAIL"
        result = {"confirmed_worktree": packet["worktree"], "confirmed_revision": packet["revision"],
                  "verdict": verdict, "summary": "Independently checked requested marker.",
                  "evidence": [str(paths[0])], "representative_completion_claimed": False}
        return DeliveryReceipt(delivery_id, "responded", run_id="qa-run", session_id="qa-isolated",
                               response_body=json.dumps(result))
    def close(self):
        pass


def prepare_profiled(client, root, profile="ACKNOWLEDGEMENT"):
    main = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main", "Idempotency-Key": "prepare-v2"}
    rep = {"X-Authenticated-Principal": "human-representative",
           "X-War-Room-Proxy-Secret": "fixture-proxy-secret", "Idempotency-Key": "approve-v2"}
    response = client.post("/api/war-room/projects/plachem-agent-war-room/prepare", headers=main, json={
        "instruction": INSTRUCTION, "agent_ids": ["ERPmanager"], "reviewer_agent_id": "ERPqa",
        "execution_mode": "FAST_GATEWAY", "task_profile": profile,
        "document_version": "baseline-2026-08-23", "deadline_at": int(time.time()) + 9000,
        "grounding": {"worktree": str(root), "forbidden": ["production DB", "existing work sessions", "merge/push/deploy"]},
    })
    assert response.status_code == 201, response.text
    item = response.json()
    context = client.get("/api/war-room/projects/plachem-agent-war-room/mutation-context", headers=rep,
                         params={"action": "task_approve_execute", "target_id": item["task_id"]})
    assert context.status_code == 200, context.text
    approved = client.post(f"/api/war-room/tasks/{item['task_id']}/approve-execute", headers=rep,
                           json={"expires_at": int(time.time()) + 3600,
                                 "context_token": context.json()["context_token"]})
    assert approved.status_code == 200, approved.text
    return item, approved.json()


@pytest.mark.parametrize("marker", ["SIMPLE_UI_TEST_PASS", "WRONG_RESPONSE"])
def test_simple_to_real_adapter_validator_to_independent_qa(production, monkeypatch, marker):
    root, client, _, _ = production
    monkeypatch.setenv("PLACHEM_WAR_ROOM_AUTO_QA", "1")
    monkeypatch.setenv("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", "isolated-v2-qa-secret")
    rpc = WorkerRPC(marker)
    def adapter_factory(*args, **kwargs):
        adapter = OpenClawAdapter(*args, **kwargs)
        adapter.rpc = rpc
        return adapter
    monkeypatch.setattr(service, "OpenClawAdapter", adapter_factory)
    item, approval = prepare_profiled(client, root)
    reviewer = IndependentReviewer(root)
    runtime = WarRoomRuntime(adapter=reviewer)
    db_path = root / "war-room.sqlite3"
    core_id = "war-" + approval["deliveries"][0]["delivery_id"]
    try:
        for _ in range(100):
            runtime.tick(db_path=db_path)
            with sqlite3.connect(db_path) as con:
                verdict = con.execute("SELECT verdict FROM war_qa_verdicts WHERE task_id=?", (item["task_id"],)).fetchone()
            if verdict:
                break
            time.sleep(0.02)
        assert verdict is not None, "Worker response did not reach independent QA"
        assert verdict[0] == ("PASS" if marker == "SIMPLE_UI_TEST_PASS" else "FAIL")
        assert reviewer.calls == 1 and len(rpc.submitted) == 1
        assert next(iter(rpc.submitted.values()))["timeout"] == 3600
        record = service.get_persistent_harness().engine.status(core_id)
        assert record["status"] == "PASS" and record["result"]["artifacts"] == []
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            task = con.execute("SELECT * FROM war_tasks WHERE id=?", (item["task_id"],)).fetchone()
            if marker == "SIMPLE_UI_TEST_PASS":
                assert task["status"] == "qa", "Tests must not perform production representative approval"
                assert actions._representative_completion_checks(con, task)["missing"] == []
                evidence = con.execute("SELECT * FROM war_evidence WHERE task_id=?", (task["id"],)).fetchone()
                assert verify_receipt(con, task, evidence) is None
                Path(evidence["uri"]).write_text("tampered")
                assert verify_receipt(con, task, evidence) == "EVIDENCE_UNVERIFIED"
                assert "EVIDENCE_UNVERIFIED" in actions._representative_completion_checks(con, task)["missing"]
            else:
                assert task["status"] == "rework_required"
                assert actions._representative_completion_checks(con, task)["missing"]
    finally:
        runtime.close()


@pytest.mark.parametrize("profile", ["ACKNOWLEDGEMENT", "READ_ONLY", "CODE_CHANGE"])
def test_forbidden_actions_do_not_add_undeclared_snapshot_obligations(profile):
    forbidden = ["production DB", "existing work sessions", "merge/push/deploy"]
    packet = profile_grounding({"task_profile": profile}, {"forbidden": forbidden})
    assert packet["forbidden"] == forbidden
    assert packet["session_integrity_required"] is False


@pytest.mark.parametrize("extra", [
    {"session_integrity_required": True},
    {"required_evidence": ["session_integrity"]},
])
def test_explicit_strict_evidence_cannot_be_silently_dropped(extra):
    with pytest.raises(ValueError, match="EXPLICIT_EVIDENCE_CONTRACT_REQUIRES_ADVANCED_MODE"):
        profile_grounding({"task_profile": "READ_ONLY"}, extra)
