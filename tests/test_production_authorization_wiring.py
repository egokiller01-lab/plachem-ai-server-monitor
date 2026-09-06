"""Production composition with real approval/Broker/Core stores and no network."""

import hashlib
import json
import sqlite3
import time
from threading import Event

import pytest
from fastapi.testclient import TestClient

import fast_gateway_api as api
import fast_gateway_service as service
import war_room
import war_room_actions
from plachem_fast_gateway.auth_broker import SQLiteAuthBroker, EnvironmentPepperRef
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from war_room_runtime import WarRoomRuntime


class Transport:
    def __init__(self, *args, **kwargs):
        self.rpc = self
        self.submitted = {}
        self.finished = set()
        self.wake = Event()
        self.aborts = []
        self.close_calls = 0

    def submit(self, run_id, payload):
        assert run_id not in self.submitted, "duplicate worker execution"
        self.submitted[run_id] = dict(payload)
        return RunBinding(run_id, "oc-" + run_id, payload["agentId"],
                          "agent:" + payload["agentId"] + ":" + run_id,
                          "session-" + run_id, payload["idempotencyKey"], CoreRunStatus.RUNNING)

    def wait(self, run_id, *, timeout_seconds):
        self.wake.wait(min(timeout_seconds, 0.01))
        if run_id in self.finished:
            return AdapterOutcome(CoreRunStatus.PASS, "", result={
                "status": "completed", "summary": "fixture complete",
                "evidence": [{"type": "fixture", "detail": "test boundary"}],
                "artifacts": [], "scope": {"compliant": True, "violations": []},
            })
        return AdapterOutcome(CoreRunStatus.RUNNING, "")

    def request_on_owner_connection(self, method, params, timeout):
        assert method == "sessions.abort"
        self.aborts.append(dict(params))
        return {"status": "aborted"}

    def close(self):
        self.close_calls += 1


@pytest.fixture
def production(tmp_path, monkeypatch):
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({a: {"enabled": True, "capabilities": ["safe-smoke"]}
                                 for a in ["erpcoder", "erpqa"]}))
    settings = {
        "PLACHEM_FAST_GATEWAY_AGENTS": str(agents),
        "PLACHEM_FAST_GATEWAY_RUNS": str(tmp_path / "runs.jsonl"),
        "PLACHEM_FAST_GATEWAY_BINDINGS": str(tmp_path / "bindings.sqlite3"),
        "PLACHEM_WAR_ROOM_DB": str(tmp_path / "war-room.sqlite3"),
        "OPENCLAW_HOME": str(tmp_path / "openclaw"),
        "PLACHEM_WAR_ROOM_REAL_ADAPTER": "0",
        "PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS": '{"main":"fixture-main"}',
        "PLACHEM_FAST_GATEWAY_ENABLED": "1",
        "PLACHEM_FAST_GATEWAY_ADMIN_SECRET": "fixture-admin",
        "PLACHEM_AUTH_BROKER_DB": str(tmp_path / "broker.sqlite3"),
        "PLACHEM_AUTH_BROKER_KEY_ID": "fixture",
        "PLACHEM_AUTH_BROKER_PEPPER_FIXTURE": "fixture-only-dedicated-key-never-for-production",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PLACHEM_FAST_GATEWAY_CORE_DB", raising=False)
    monkeypatch.setattr(service, "_HARNESSES", {})
    monkeypatch.setattr(war_room_actions, "_EXECUTION_ORCHESTRATORS", {})
    owner = Transport()
    monkeypatch.setattr(service, "OpenClawAdapter", lambda *a, **k: owner)
    war_room.provision_database()
    from app import app
    client = TestClient(app)
    broker = SQLiteAuthBroker(tmp_path / "broker.sqlite3", EnvironmentPepperRef(), key_id="fixture")
    yield tmp_path, client, owner, broker
    for harness in service._HARNESSES.values():
        for timer in harness.core_engine._deadline_timers.values():
            timer.cancel()
        harness.close()
    client.close()


def prepared(production, *, agents=None, approved=True, key="fixture"):
    root, client, _, _ = production
    headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main",
               "Idempotency-Key": "prepare-" + key}
    response = client.post("/api/war-room/projects/plachem-agent-war-room/prepare", json={
        "instruction": "Return the approved fixture response only.",
        "agent_ids": agents or ["ERPcoder"], "deadline_at": int(time.time()) + 900,
        "document_version": "baseline-2026-08-23", "execution_mode": "FAST_GATEWAY",
    }, headers=headers)
    assert response.status_code == 201, response.text
    item = response.json()
    if approved:
        response = client.post(f"/api/war-room/tasks/{item['task_id']}/approve-execute",
                               json={"expires_at": int(time.time()) + 600},
                               headers={**headers, "Idempotency-Key": "approve-" + key})
        assert response.status_code == 200, response.text
        item.update(response.json())
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        item["body"] = db.execute("SELECT body FROM war_messages WHERE id=?", (item["message_id"],)).fetchone()[0]
        if approved:
            # Simulate only the existing worker's durable lease claim.
            db.execute("UPDATE war_deliveries SET status='sent',claim_token='fixture-lease',claim_expires_at=?",
                       (int(time.time()) + 30,))
    return item


def send(item, delivery=None):
    delivery = delivery or item["deliveries"][0]
    return service.get_persistent_harness().engine.dispatch(
        agent_id=delivery["agent_id"].casefold(), message=item["body"], timeout_seconds=300,
        core_run_id="war-" + delivery["delivery_id"], idempotency_key=delivery["delivery_id"],
    )


def test_shared_factory_attaches_existing_broker(production):
    assert isinstance(service.get_persistent_harness().core_engine.auth_broker, SQLiteAuthBroker)


def test_configured_core_denies_unapproved_direct_dispatch(production):
    _, _, owner, broker = production
    record = service.get_persistent_harness().engine.dispatch(
        agent_id="erpcoder", message="unapproved", timeout_seconds=300, core_run_id="unapproved")
    assert record["status"] == "BLOCKED"
    assert owner.submitted == {}
    assert broker.list_audit() == ()


@pytest.mark.parametrize("missing", ["PLACHEM_AUTH_BROKER_DB", "PLACHEM_AUTH_BROKER_KEY_ID"])
def test_missing_config_is_fail_closed(production, monkeypatch, missing):
    monkeypatch.delenv(missing)
    item = prepared(production)
    assert send(item)["status"] == "BLOCKED"
    assert production[2].submitted == {}


def test_approved_request_consumes_once_and_hides_credentials(production):
    root, _, owner, broker = production
    item = prepared(production)
    first = send(item)
    second = send(item)
    assert first["status"] == second["status"] == "RUNNING"
    audit = broker.list_audit()
    assert [row["event_type"] for row in audit] == ["ISSUED", "CONSUME"]
    assert all(row["outcome"] == "ALLOW" for row in audit)
    assert audit[-1]["workspace_id"] == "war-room:" + item["task_id"]
    assert audit[-1]["project_id"] == "plachem-agent-war-room"
    assert len(owner.submitted) == 1
    assert set(next(iter(owner.submitted.values()))) == {"agentId", "message", "timeout", "idempotencyKey"}
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        assert db.execute("SELECT call_count FROM war_task_calls WHERE task_id=?", (item["task_id"],)).fetchone() == (1,)
    assert "fixture-only-dedicated-key" not in (root / "runs.jsonl").read_text()
    assert "auth_token" not in json.dumps(first)
    broker.verify_audit()


@pytest.mark.parametrize("change", ["revoke", "expiry", "deadline", "scope", "version", "target",
                                    "stop", "archive", "assignee", "inactive", "call_limit", "turn_limit"])
def test_dispatch_rechecks_approval_and_task_controls(production, change):
    root, _, owner, broker = production
    item = prepared(production)
    changes = {
        "revoke": "UPDATE war_approvals SET revoked_at=1",
        "expiry": "UPDATE war_approvals SET expires_at=1",
        "deadline": "UPDATE war_tasks SET deadline_at=1",
        "scope": "UPDATE war_tasks SET scope='changed scope'",
        "version": "UPDATE war_tasks SET manyfast_version='changed'",
        "target": "DELETE FROM war_task_agents",
        "stop": "UPDATE war_project_control SET stop_state='stop_requested'",
        "archive": "UPDATE war_project_control SET archived_at=1",
        "assignee": "UPDATE war_tasks SET assignee_agent_id='ERPqa'",
        "inactive": "UPDATE war_participants SET active=0 WHERE principal_id='ERPcoder'",
        "call_limit": "UPDATE war_tasks SET call_limit=0",
        "turn_limit": "UPDATE war_tasks SET turn_limit=0",
    }
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        db.execute(changes[change])
    assert send(item)["status"] == "BLOCKED"
    assert owner.submitted == {}
    assert broker.list_audit() == ()


def test_changed_instruction_is_not_self_authorized(production):
    item = prepared(production)
    item["body"] += " Perform unrelated work."
    assert send(item)["status"] == "BLOCKED"
    assert production[2].submitted == {}


def test_http_and_war_room_use_one_owner(production):
    assert api._engine() is service.get_persistent_harness().engine


def test_cancel_remains_available_after_approval_revocation(production):
    root, _, owner, broker = production
    item = prepared(production)
    record = send(item)
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        db.execute("UPDATE war_approvals SET revoked_at=1")
    orchestrator = war_room_actions._execution_orchestrator()
    stopped = orchestrator.stop_controller.stop_core_run(core_run_id=record["core_run_id"])
    assert stopped.status == "stopped"
    assert len(owner.aborts) == 1
    assert len(broker.list_audit()) == 2


def test_production_context_reset_requires_new_authorization_before_cancelling(production):
    item = prepared(production)
    record = send(item)
    engine = service.get_persistent_harness().engine
    engine.registry.update_goal_state(record["core_run_id"], reinjection_ready=True)
    with pytest.raises(ValueError, match="AUTH_REAUTHORIZATION_REQUIRED"):
        engine.fresh_context(record["core_run_id"])
    assert engine.status(record["core_run_id"])["status"] == "RUNNING"
    assert len(production[2].submitted) == 1


def manual_grant(production, **issue_options):
    from plachem_fast_gateway.auth_broker import execution_auth_scope
    from plachem_fast_gateway.runtime_policy import normalize_goal_contract
    request = dict(agent_id="erpcoder", message="approved exact operation", timeout_seconds=300,
                   core_run_id="manual-run", idempotency_key="manual-key")
    scope = execution_auth_scope(
        **{k: v for k, v in request.items() if k != "timeout_seconds"},
        goal_contract=normalize_goal_contract(None).as_dict(),
    )
    grant = production[3].issue(scope, ttl_seconds=30, created_by="main", **issue_options)
    return request, grant


@pytest.mark.parametrize("changed", ["message", "agent", "run", "key", "action", "workspace", "project", "goal"])
def test_grant_cannot_authorize_another_request(production, changed):
    request, grant = manual_grant(production)
    changes = {
        "message": {"message": "unapproved operation"}, "agent": {"agent_id": "erpqa"},
        "run": {"core_run_id": "another-run"}, "key": {"idempotency_key": "another-key"},
        "action": {"action": "another-action"}, "workspace": {"workspace_id": "another-workspace"},
        "project": {"project_id": "another-project"},
        "goal": {"goal_contract": {"primary_objective": "CHANGED", "allowed_scope": ["CHANGED"],
                 "forbidden_scope": ["OTHER"], "expected_result": "CHANGED", "completion_conditions": ["CHANGED"]}},
    }
    record = service.get_persistent_harness().engine.dispatch(**(request | changes[changed]), auth_token=grant.token)
    assert record["status"] == "BLOCKED"
    assert production[2].submitted == {}
    assert production[3].list_audit()[-1]["outcome"] == "DENY"


@pytest.mark.parametrize("state", ["expired", "revoked", "consumed", "valid"])
def test_manual_grant_lifecycle_and_no_secret_leak(production, state):
    root, _, owner, broker = production
    request, grant = manual_grant(production, **({"now_ms": 1} if state == "expired" else {}))
    if state == "revoked":
        broker.revoke(grant.grant_id, revoked_by="main")
    if state == "consumed":
        from plachem_fast_gateway.auth_broker import execution_auth_scope
        from plachem_fast_gateway.runtime_policy import normalize_goal_contract
        scope = execution_auth_scope(**{k: v for k, v in request.items() if k != "timeout_seconds"},
                                     goal_contract=normalize_goal_contract(None).as_dict())
        broker.verify_and_consume(grant.token, scope, run_id="already-consumed")
    record = service.get_persistent_harness().engine.dispatch(**request, auth_token=grant.token)
    assert record["status"] == ("RUNNING" if state == "valid" else "BLOCKED")
    assert len(owner.submitted) == (1 if state == "valid" else 0)
    assert grant.token not in (root / "runs.jsonl").read_text()
    assert grant.token not in json.dumps(broker.list_audit(), default=str)
    assert grant.token not in json.dumps(owner.submitted)


@pytest.mark.parametrize("revoke_child", [False, True])
def test_phase2_continuation_rechecks_authorization(production, revoke_child):
    root, _, owner, broker = production
    item = prepared(production, agents=["ERPcoder", "ERPqa"])
    orchestrator = war_room_actions._execution_orchestrator()
    compiled = orchestrator.compile_and_persist(
        war_project_id="plachem-agent-war-room", war_task_id=item["task_id"], agents=["erpcoder", "erpqa"],
        workflow={"erpcoder": {"depends_on": [], "message": item["body"], "timeout_seconds": 300},
                  "erpqa": {"depends_on": ["erpcoder"], "message": item["body"], "timeout_seconds": 300}},
    )
    units = compiled["execution_units"]
    first = orchestrator.dispatch_execution(execution_id=units[0]["execution_id"], message=item["body"], timeout_seconds=300)
    assert first["status"] == "RUNNING"
    if revoke_child:
        with sqlite3.connect(root / "war-room.sqlite3") as db:
            db.execute("UPDATE war_approvals SET revoked_at=1")
    completed = Event()
    harness = service.get_persistent_harness()
    def subscriber(record):
        orchestrator.on_core_run_terminal(record)
        completed.set()
    harness.set_terminal_completion_subscriber(subscriber)
    owner.finished.add(first["core_run_id"])
    owner.wake.set()
    assert completed.wait(2), "no automatic continuation observation"
    child = orchestrator.store.get_unit(units[1]["execution_id"])
    record = harness.engine.status(child["core_run_id"])
    assert record["status"] == ("BLOCKED" if revoke_child else "RUNNING")
    assert len(owner.submitted) == (1 if revoke_child else 2)
    assert len([r for r in broker.list_audit() if r["event_type"] == "CONSUME" and r["outcome"] == "ALLOW"]) == len(owner.submitted)


def test_worker_reserves_call_once_and_http_projects_same_run(production):
    root, client, owner, broker = production
    item = prepared(production)
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        db.execute("UPDATE war_deliveries SET status='queued'")
    runtime = WarRoomRuntime(adapter=object())
    result = runtime.tick(db_path=root / "war-room.sqlite3")
    assert result[0]["status"] == "received"
    run_id = "war-" + item["deliveries"][0]["delivery_id"]
    assert client.get(f"/api/fast-gateway/runs/{run_id}").json()["status"] == "RUNNING"
    assert len(client.get("/api/fast-gateway/runs").json()["runs"]) == 1
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        assert db.execute("SELECT call_count FROM war_task_calls WHERE task_id=?", (item["task_id"],)).fetchone() == (1,)
    assert runtime.tick(db_path=root / "war-room.sqlite3") == []
    assert len(owner.submitted) == 1
    assert len(broker.list_audit()) == 2


def test_api_caller_cannot_inject_grant_issuer(production):
    _, client, owner, _ = production
    response = client.post("/api/fast-gateway/runs/dispatch", headers={"X-Fast-Gateway-Secret": "fixture-admin"},
                           json={"agent_id": "erpcoder", "message": "safe", "timeout_seconds": 300,
                                 "grant_authorizer": {"approved": True}})
    assert response.status_code == 422
    assert owner.submitted == {}


def test_external_token_cannot_bypass_war_room_approval(production):
    from plachem_fast_gateway.auth_broker import execution_auth_scope
    from plachem_fast_gateway.runtime_policy import normalize_goal_contract
    item = prepared(production)
    delivery = item["deliveries"][0]
    request = dict(agent_id="erpcoder", message=item["body"], core_run_id="war-" + delivery["delivery_id"],
                   idempotency_key=delivery["delivery_id"])
    token = production[3].issue(execution_auth_scope(**request, goal_contract=normalize_goal_contract(None).as_dict()),
                                ttl_seconds=30, created_by="main").token
    record = service.get_persistent_harness().engine.dispatch(**request, timeout_seconds=300, auth_token=token)
    assert record["status"] == "BLOCKED"
    assert production[2].submitted == {}


@pytest.mark.parametrize("mutation", ["instruction", "goal", "unclaimed", "project"])
def test_phase2_cannot_self_approve_dispatch_input(production, mutation):
    root, _, owner, broker = production
    item = prepared(production)
    orchestrator = war_room_actions._execution_orchestrator()
    compiled = orchestrator.compile_and_persist(
        war_project_id="plachem-agent-war-room", war_task_id=item["task_id"], agents=["erpcoder"])
    unit = compiled["execution_units"][0]
    if mutation == "unclaimed":
        record = service.get_persistent_harness().engine.dispatch(
            agent_id="erpcoder", message=item["body"], timeout_seconds=300,
            core_run_id="core-exec-" + unit["execution_id"], idempotency_key="war-exec-" + unit["execution_id"])
    else:
        if mutation == "project":
            with sqlite3.connect(root / "war-room.sqlite3") as db:
                db.execute("UPDATE war_execution_units SET war_project_id='other-project' WHERE execution_id=?",
                           (unit["execution_id"],))
        goal = None
        if mutation == "goal":
            goal = {"primary_objective": "CHANGED", "allowed_scope": ["CHANGED"], "forbidden_scope": ["OTHER"],
                    "expected_result": "CHANGED", "completion_conditions": ["CHANGED"]}
        record = orchestrator.dispatch_execution(execution_id=unit["execution_id"],
                    message="Unapproved replacement" if mutation == "instruction" else item["body"],
                    timeout_seconds=300, goal_contract=goal)
    assert record["status"] == "BLOCKED"
    assert owner.submitted == {}
    assert broker.list_audit() == ()


def test_concurrent_admission_respects_existing_task_call_limit(production):
    from concurrent.futures import ThreadPoolExecutor
    root, _, owner, broker = production
    item = prepared(production, agents=["ERPcoder", "ERPqa"])
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        db.execute("UPDATE war_tasks SET call_limit=1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda d: send(item, d), item["deliveries"]))
    assert sorted(r["status"] for r in results) == ["BLOCKED", "RUNNING"]
    assert len(owner.submitted) == 1
    assert len(broker.list_audit()) == 2


def test_missing_pepper_never_contacts_worker(production, monkeypatch):
    from plachem_fast_gateway.auth_broker import AuthBrokerError
    monkeypatch.delenv("PLACHEM_AUTH_BROKER_PEPPER_FIXTURE")
    with pytest.raises(AuthBrokerError, match="AUTH_REQUIRED"):
        service.get_persistent_harness()
    assert production[2].submitted == {}


def test_legacy_second_store_configuration_requires_review(production, monkeypatch):
    root, _, owner, _ = production
    legacy = root / "old-core.sqlite3"
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_CORE_DB", str(legacy))
    with pytest.raises(ValueError, match="LEGACY_RUN_STORE_CONFIG_REQUIRES_REVIEW"):
        api._engine()
    assert not legacy.exists()
    assert owner.submitted == {}
