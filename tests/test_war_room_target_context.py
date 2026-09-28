"""Unrelated work must not invalidate a task action; target changes still do."""
import sqlite3
import time
import pytest
from test_production_authorization_wiring import production, prepared

HEADERS = {"X-Authenticated-Principal": "human-representative",
           "X-War-Room-Proxy-Secret": "fixture-proxy-secret"}
PROJECT = "plachem-agent-war-room"


def context(client, task):
    response = client.get(f"/api/war-room/projects/{PROJECT}/mutation-context",
        headers=HEADERS, params={"action": "task_approve_execute", "target_id": task})
    assert response.status_code == 200, response.text
    return response.json()["context_token"]


def approve(client, task, token):
    return client.post(f"/api/war-room/tasks/{task}/approve-execute",
        headers={**HEADERS, "Idempotency-Key": "approve-target-" + task},
        json={"expires_at": int(time.time())+600, "context_token": token})


def test_unrelated_worker_does_not_invalidate_target_approval(production):
    _, client, _, _ = production
    first = prepared(production, approved=False, key="first")
    token = context(client, first["task_id"])
    prepared(production, approved=True, key="second")
    response = approve(client, first["task_id"], token)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("change", ["scope", "revision", "project_stop"])
def test_target_or_project_control_change_still_invalidates_approval(production, change):
    root, client, _, _ = production
    first = prepared(production, approved=False, key="protected")
    token = context(client, first["task_id"])
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        if change == "scope":
            db.execute("UPDATE war_tasks SET scope='changed request' WHERE id=?", (first["task_id"],))
        elif change == "revision":
            db.execute("UPDATE war_tasks SET revision=revision+1 WHERE id=?", (first["task_id"],))
        else:
            db.execute("UPDATE war_project_control SET stop_state='stop_requested' WHERE project_id=?", (PROJECT,))
    response = approve(client, first["task_id"], token)
    assert response.status_code == 409, response.text
    with sqlite3.connect(root / "war-room.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM war_deliveries WHERE message_id=?", (first["message_id"],)).fetchone()[0] == 0
