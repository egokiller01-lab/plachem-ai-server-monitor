import json
import sqlite3
import os
from pathlib import Path
from types import SimpleNamespace
import urllib.request

from fastapi import FastAPI
from fastapi.testclient import TestClient

import jev_task_router as jev


class FakeJEV:
    def __init__(self): self.calls = []
    def choice(self, *, question, state, criteria, idempotency_key):
        choices = list(criteria)
        self.calls.append((question, state, criteria, idempotency_key))
        if "task domain" in question:
            return {"choice": "qa", "probabilities": {x: (1.0 if x == "qa" else 0.0) for x in choices}, "confidence": 0.9}
        return {"choice": "ERPqa", "probabilities": {x: (1.0 if x == "ERPqa" else 0.0) for x in choices}, "confidence": 0.8}


def make_db(tmp_path, monkeypatch):
    path = tmp_path / "war.sqlite3"
    monkeypatch.setattr(jev.war_room, "_db_path", lambda: path)
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE war_tasks(id TEXT PRIMARY KEY, project_id TEXT, source_message_id TEXT, scope TEXT,
          status TEXT, revision INTEGER, document_version TEXT, qa_cycle INTEGER, assignee_agent_id TEXT,
          reviewer_agent_id TEXT, created_at INTEGER, updated_at INTEGER);
        CREATE TABLE war_messages(id TEXT PRIMARY KEY, original_body TEXT);
        CREATE TABLE war_grounding_packets(task_id TEXT PRIMARY KEY, packet_json TEXT);
        CREATE TABLE war_task_agents(task_id TEXT, agent_id TEXT);
        """)
        con.execute("INSERT INTO war_tasks VALUES ('t1','p1','m1','qa task','draft',1,'v1',0,NULL,NULL,1,1)")
        con.execute("INSERT INTO war_messages VALUES ('m1','do QA; token=should-not-escape')")
        con.execute("INSERT INTO war_grounding_packets VALUES ('t1',?)", (json.dumps({"expected_result":"report","completion_conditions":["test"],"risk":{"production":False}}),))
        con.execute("INSERT INTO war_task_agents VALUES ('t1','ERPqa')")
    jev.provision_schema(str(path))
    return path


def test_schema_is_idempotent_and_digest_is_deterministic(tmp_path, monkeypatch):
    path = make_db(tmp_path, monkeypatch)
    jev.provision_schema(str(path))
    assert jev.state_digest({"b": 2, "a": 1}) == jev.state_digest({"a": 1, "b": 2})


def test_two_stage_choice_persists_latency_and_redacts(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    monkeypatch.setattr(jev, "registry_candidates", lambda domain: [{"agent_id":"ERPqa","capabilities":["qa"],"registered":True,"enabled":True}])
    fake = FakeJEV(); jev.set_jev_client(fake)
    result = jev.create_advisory("t1")
    assert result["status"] == "AVAILABLE"
    assert result["domain_choice"] == "qa" and result["agent_choice"] == "ERPqa"
    assert result["latency_ms"] >= 0 and len(fake.calls) == 2
    assert "should-not-escape" not in json.dumps(result)
    with sqlite3.connect(jev.war_room._db_path()) as con:
        persisted = con.execute("SELECT state_json FROM jev_task_advisories").fetchone()[0]
    assert "should-not-escape" not in persisted
    assert "token" not in persisted.lower()

def test_openconnector_client_fails_closed_without_runtime_secret(monkeypatch):
    client = jev.OpenConnectorJEVClient(endpoint="https://example.invalid/evaluate", token_file="/missing/jev-token")
    try:
        client.choice(question="q", state={}, criteria={"a":"first"}, idempotency_key="test")
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("missing runtime secret must fail closed")


def test_outage_is_unavailable_and_does_not_mutate_task(tmp_path, monkeypatch):
    path = make_db(tmp_path, monkeypatch); jev.set_jev_client(jev.UnavailableJEVClient())
    result = jev.create_advisory("t1")
    assert result["status"] == "ADVISORY_UNAVAILABLE" and result["error"]["code"] == "JEV_ERROR"
    with sqlite3.connect(path) as con:
        task = con.execute("SELECT status,revision,assignee_agent_id FROM war_tasks WHERE id='t1'").fetchone()
    assert task == ("draft", 1, None)


def test_invalid_choice_fails_closed_and_stale_is_detected(tmp_path, monkeypatch):
    path = make_db(tmp_path, monkeypatch)
    class Bad:
        def choice(self, **kwargs): return {"choice":"not-closed","probabilities":{}}
    jev.set_jev_client(Bad()); result = jev.create_advisory("t1")
    assert result["status"] == "ADVISORY_UNAVAILABLE"
    with sqlite3.connect(path) as con:
        con.execute("UPDATE war_tasks SET revision=2 WHERE id='t1'")
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM jev_task_advisories").fetchone()
        assert jev._stale(con, row)


def test_registry_filtering_and_closed_set_validation(monkeypatch):
    monkeypatch.setattr(jev, "load_agent_catalog", lambda: {
        "ERPqa": SimpleNamespace(agent_id="ERPqa", registered=True, enabled=True, gateway_allowed=True, capabilities=("qa",)),
        "disabled": SimpleNamespace(agent_id="disabled", registered=True, enabled=False, gateway_allowed=True, capabilities=("qa",)),
        "unregistered": SimpleNamespace(agent_id="unregistered", registered=False, enabled=True, gateway_allowed=True, capabilities=("qa",)),
        "blocked": SimpleNamespace(agent_id="blocked", registered=True, enabled=True, gateway_allowed=False, capabilities=("qa",)),
        "ERPcoder": SimpleNamespace(agent_id="ERPcoder", registered=True, enabled=True, gateway_allowed=True, capabilities=("erp_implementation",)),
    })
    assert [x["agent_id"] for x in jev.registry_candidates("qa")] == ["ERPqa"]
    assert {x["agent_id"] for x in jev.registry_candidates()} == {"ERPcoder", "ERPqa"}
    try:
        jev._choice({"choice":"not-a-domain","probabilities":{}}, list(jev.DOMAINS))
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-registry Choice must be rejected")


def test_openconnector_client_uses_real_runtime_contract(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("opaque-test-token", encoding="utf-8")
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self):
            return json.dumps({"success": True, "data": {"answers": {"result": {
                "type": "choice", "choice": "qa", "probabilities": {"qa": 0.8, "research": 0.2}
            }}}}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = jev.OpenConnectorJEVClient(
        endpoint="http://127.0.0.1:3001/v1/actions/vercel_ai_gateway.evaluate",
        token_file=str(token),
    )
    result = client.choice(
        question="Choose a domain.",
        state={"task": "test"},
        criteria={"qa": "quality", "research": "research"},
        idempotency_key="advisory-domain",
    )
    assert captured["url"].endswith("/v1/actions/vercel_ai_gateway.evaluate")
    assert captured["body"]["connectionName"] == "default"
    question = captured["body"]["input"]["questions"]["result"]
    assert question == {"type":"choice", "instructions":"Choose a domain.", "criteria":{"qa":"quality", "research":"research"}}
    assert result == {"choice":"qa", "probabilities":{"qa":0.8, "research":0.2}, "confidence":0.8}


def test_decisions_are_idempotent_and_never_dispatch(tmp_path, monkeypatch):
    path = make_db(tmp_path, monkeypatch)
    monkeypatch.setattr(jev, "registry_candidates", lambda domain=None: [{"agent_id":"ERPqa","capabilities":["qa"],"registered":True,"enabled":True,"gateway_allowed":True,"execution_eligible":True}])
    jev.set_jev_client(FakeJEV())
    advisory = jev.create_advisory("t1")

    import war_room_actions
    def connect_rw():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con
    monkeypatch.setattr(war_room_actions, "_connect_rw", connect_rw)
    monkeypatch.setattr(war_room_actions, "_actor", lambda *args, **kwargs: "main")
    monkeypatch.setattr(war_room_actions, "_audit", lambda *args, **kwargs: None)

    app = FastAPI(); app.include_router(jev.router)
    client = TestClient(app)
    url = f"/api/war-room/tasks/t1/jev-advisory/{advisory['advisory_id']}/decision"
    headers = {"Idempotency-Key":"same-key"}
    first = client.post(url, json={"decision":"ACCEPT"}, headers=headers)
    replay = client.post(url, json={"decision":"ACCEPT"}, headers=headers)
    conflict = client.post(url, json={"decision":"INSUFFICIENT"}, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT count(*) FROM jev_task_decisions").fetchone()[0] == 1
        task = con.execute("SELECT status,revision,assignee_agent_id FROM war_tasks WHERE id='t1'").fetchone()
    assert task == ("draft", 1, None)


def test_ui_exposes_advisory_only_decisions():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "war-room.html").read_text(encoding="utf-8")
    javascript = (root / "static" / "war-room-ui.js").read_text(encoding="utf-8")
    assert "JEV Advisory · advisory only" in html
    assert all(label in javascript for label in ("ACCEPT", "OVERRIDE", "INSUFFICIENT"))
    assert "/jev-advisory" in javascript
    assert "assignment/dispatch/lifecycle 변경 없음" in javascript
