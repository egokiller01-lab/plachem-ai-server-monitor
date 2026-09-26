"""Focused tests for the trusted login -> representative principal mapping.

Covers the five required security behaviors for War Room approve-execute:
1. Trusted server-side login identity maps to the representative principal
   and approve-execute succeeds.
2. Non-representative identity remains 403.
3. Missing identity remains 401 (no authentication at all).
4. Client-side actor/header claim alone cannot elevate.
5. Existing representative check, RBAC, Auth/Policy protections remain.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

LOGIN = "egokiller01@gmail.com"
PRINCIPAL = "human-representative"
PROXY_SECRET = "fixture-proxy-secret"
SESSION_SECRET = "fixture-session-secret"


def _cookie_for(principal: str) -> str:
    return principal + "." + hmac.new(
        SESSION_SECRET.encode(), principal.encode(), hashlib.sha256
    ).hexdigest()


class WarRoomRepresentativeMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.old = {
            key: os.environ.get(key)
            for key in (
                "PLACHEM_WAR_ROOM_DB",
                "OPENCLAW_HOME",
                "PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS",
                "PLACHEM_WAR_ROOM_TEST_ADAPTER",
                "PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS",
                "PLACHEM_WAR_ROOM_HUMAN_TAILSCALE_LOGIN",
                "PLACHEM_WAR_ROOM_HUMAN_REPRESENTATIVE_PRINCIPAL",
                "PLACHEM_WAR_ROOM_SESSION_SECRET",
                "PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET",
                "PLACHEM_WAR_ROOM_QA_SIGNING_SECRET",
            )
        }
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(root / "war-room.sqlite3")
        os.environ["OPENCLAW_HOME"] = str(root / "openclaw")
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = json.dumps(
            {"main": "fixture-main-token", "ERPcoder": "fixture-erpcoder-token"}
        )
        os.environ["PLACHEM_WAR_ROOM_TEST_ADAPTER"] = "1"
        # main is NOT a representative: it is a normal authenticated principal
        # with project_manager RBAC but must not pass the representative gate.
        os.environ["PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS"] = PRINCIPAL
        os.environ.pop("PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE", None)
        # Confirmed runtime config: trusted login -> representative principal.
        os.environ["PLACHEM_WAR_ROOM_HUMAN_TAILSCALE_LOGIN"] = LOGIN
        os.environ["PLACHEM_WAR_ROOM_HUMAN_REPRESENTATIVE_PRINCIPAL"] = PRINCIPAL
        os.environ["PLACHEM_WAR_ROOM_SESSION_SECRET"] = SESSION_SECRET
        os.environ["PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET"] = PROXY_SECRET
        os.environ["PLACHEM_WAR_ROOM_QA_SIGNING_SECRET"] = "fixture-qa-secret"

        import war_room

        war_room.provision_database()
        # The mapping is applied at provisioning: the representative principal
        # must exist as an active participant with approve/execute rights.
        with sqlite3.connect(os.environ["PLACHEM_WAR_ROOM_DB"]) as con:
            row = con.execute(
                "SELECT role,can_approve,can_execute,active FROM war_participants WHERE principal_id=?",
                (PRINCIPAL,),
            ).fetchone()
        self.assertEqual(("project_manager", 1, 1, 1), row)

        from app import app

        self.client = TestClient(app)

    def tearDown(self) -> None:
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    # -- helpers ------------------------------------------------------------
    def prepare_task(self, key: str) -> str:
        headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}
        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={
                "instruction": "mapping fixture",
                "agent_ids": ["ERPcoder"],
                "deadline_at": int(time.time()) + 1800,
                "document_version": "baseline-2026-08-23",
                "execution_mode": "LEGACY",
            },
            headers={**headers, "Idempotency-Key": key + "-prepare"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        return prepared.json()["task_id"]

    def approve(self, task_id: str, headers: dict[str, str], key: str, cookies=None):
        return self.client.post(
            f"/api/war-room/tasks/{task_id}/approve-execute",
            json={"expires_at": int(time.time()) + 1200},
            headers={**headers, "Idempotency-Key": key},
            cookies=cookies,
        )

    # -- case 1: trusted identity maps and approve-execute succeeds ----------
    def test_trusted_proxy_login_maps_to_representative_and_approves(self) -> None:
        task_id = self.prepare_task("case1")
        headers = {
            "X-Authenticated-Principal": LOGIN,
            "X-War-Room-Proxy-Secret": PROXY_SECRET,
        }
        result = self.approve(task_id, headers, "case1-approve")
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("running", result.json()["status"])
        with sqlite3.connect(os.environ["PLACHEM_WAR_ROOM_DB"]) as con:
            approver = con.execute(
                "SELECT approver_id FROM war_approvals WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            deliveries = con.execute(
                "SELECT COUNT(*) FROM war_deliveries d JOIN war_tasks t ON d.message_id=t.source_message_id WHERE t.id=?",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(PRINCIPAL, approver)
        self.assertEqual(1, deliveries)

    def test_trusted_signed_session_cookie_maps_and_approves(self) -> None:
        task_id = self.prepare_task("case1b")
        cookie = {"war_room_session": _cookie_for(PRINCIPAL)}
        result = self.approve(task_id, {}, "case1b-approve", cookies=cookie)
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("running", result.json()["status"])

    def test_page_login_sets_mapped_session_cookie(self) -> None:
        page = self.client.get(
            "/war-room",
            headers={
                "X-Authenticated-Principal": LOGIN,
                "X-War-Room-Proxy-Secret": PROXY_SECRET,
            },
        )
        self.assertEqual(200, page.status_code)
        cookie = page.cookies.get("war_room_session")
        self.assertIsNotNone(cookie)
        self.assertEqual(_cookie_for(PRINCIPAL), cookie)
        # The raw login email is not embedded in the issued cookie.
        self.assertNotIn(LOGIN, cookie or "")

    # -- case 2: non-representative identity remains 403 ---------------------
    def test_non_representative_principal_is_403(self) -> None:
        task_id = self.prepare_task("case2")
        headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}
        result = self.approve(task_id, headers, "case2-approve")
        self.assertEqual(403, result.status_code)
        self.assertIn("representative", result.json()["detail"].lower())
        with sqlite3.connect(os.environ["PLACHEM_WAR_ROOM_DB"]) as con:
            self.assertEqual(
                "awaiting_approval",
                con.execute("SELECT status FROM war_tasks WHERE id=?", (task_id,)).fetchone()[0],
            )

    def test_unknown_principal_is_rejected_before_mapping(self) -> None:
        # A trusted proxy may only present principals the server already knows.
        task_id = self.prepare_task("case2b")
        headers = {
            "X-Authenticated-Principal": "stranger@example.com",
            "X-War-Room-Proxy-Secret": PROXY_SECRET,
        }
        result = self.approve(task_id, headers, "case2b-approve")
        self.assertEqual(401, result.status_code)

    # -- case 3: missing identity remains 401 --------------------------------
    def test_missing_identity_is_401(self) -> None:
        task_id = self.prepare_task("case3")
        result = self.approve(task_id, {}, "case3-approve")
        self.assertEqual(401, result.status_code)

    # -- case 4: client-side claims alone cannot elevate ---------------------
    def test_actor_header_alone_cannot_elevate(self) -> None:
        task_id = self.prepare_task("case4")
        result = self.approve(task_id, {"X-War-Room-Actor": PRINCIPAL}, "case4-actor")
        self.assertEqual(401, result.status_code)

    def test_proxy_header_without_secret_cannot_elevate(self) -> None:
        task_id = self.prepare_task("case4b")
        result = self.approve(task_id, {"X-Authenticated-Principal": LOGIN}, "case4b-nosecret")
        self.assertEqual(401, result.status_code)

    def test_wrong_proxy_secret_cannot_elevate(self) -> None:
        task_id = self.prepare_task("case4c")
        headers = {"X-Authenticated-Principal": LOGIN, "X-War-Room-Proxy-Secret": "wrong-secret"}
        result = self.approve(task_id, headers, "case4c-wrongsecret")
        self.assertEqual(401, result.status_code)

    def test_forged_session_cookie_cannot_elevate(self) -> None:
        task_id = self.prepare_task("case4d")
        forged = PRINCIPAL + "." + "0" * 64
        result = self.approve(task_id, {}, "case4d-forged", cookies={"war_room_session": forged})
        self.assertEqual(401, result.status_code)

    # -- case 5: existing protections remain ----------------------------------
    def test_representative_gate_still_enforced_and_rbac_intact(self) -> None:
        task_id = self.prepare_task("case5")
        headers = {
            "X-Authenticated-Principal": LOGIN,
            "X-War-Room-Proxy-Secret": PROXY_SECRET,
        }
        # approve-execute without execution rights would be 403; here the
        # mapped principal has full rights, so a malformed mutation must be
        # rejected by the existing validator, not by the representative gate.
        stale = self.client.post(
            f"/api/war-room/tasks/{task_id}/approve-execute",
            json={"expires_at": int(time.time()) + 1200, "contract_version": 1,
                  "project_id": "plachem-agent-war-room", "task_id": "wrong-task", "task_revision": 1},
            headers={**headers, "Idempotency-Key": "case5-stale"},
        )
        self.assertEqual(409, stale.status_code)
        # main can still read but cannot approve (RBAC unchanged).
        main_headers = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}
        denied = self.client.post(
            f"/api/war-room/tasks/{task_id}/approvals",
            json={"decision": "approved", "expires_at": int(time.time()) + 1200},
            headers={**main_headers, "Idempotency-Key": "case5-main-approve"},
        )
        self.assertEqual(403, denied.status_code)

    def test_agent_principal_is_never_mapped_to_representative(self) -> None:
        # The mapping target must be a non-Agent representative; an agent
        # name must not widen access even if misconfigured.
        os.environ["PLACHEM_WAR_ROOM_HUMAN_REPRESENTATIVE_PRINCIPAL"] = "ERPcoder"
        import war_room

        self.assertEqual({}, war_room._human_login_principal_map())
        task_id = self.prepare_task("case5b")
        headers = {
            "X-Authenticated-Principal": LOGIN,
            "X-War-Room-Proxy-Secret": PROXY_SECRET,
        }
        result = self.approve(task_id, headers, "case5b-approve")
        # The raw login is a Tailscale account identity, not a server principal:
        # without a valid mapping there is no authenticated principal at all.
        self.assertEqual(401, result.status_code)
        os.environ["PLACHEM_WAR_ROOM_HUMAN_REPRESENTATIVE_PRINCIPAL"] = PRINCIPAL


if __name__ == "__main__":
    unittest.main()
