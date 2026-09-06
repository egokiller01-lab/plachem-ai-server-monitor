"""Translate existing War Room approvals into one-use Core authorization.

No HTTP grant endpoint, token persistence, or second execution registry.
Automatic issuance is limited to the immutable approved instruction and the
default goal. Caller-authored replacement instructions/goals need a separate
approval contract; matching a task ID alone never authorizes them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from plachem_fast_gateway.auth_broker import AuthBrokerError, AuthScope, SQLiteAuthBroker
from plachem_fast_gateway.runtime_policy import normalize_goal_contract


class WarRoomGrantAuthorizer:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @staticmethod
    def owns_run(core_run_id: str) -> bool:
        return core_run_id.startswith(("war-", "core-exec-"))

    @contextmanager
    def authorize(self, broker: SQLiteAuthBroker, scope: AuthScope):
        """Serialize approval/stop changes through grant consumption.

        Successful admission reserves the existing task call counter before
        the worker submit. Revocation prevents subsequent admission; stopping
        an already admitted run remains the Core cancellation operation.
        """
        if (scope.action != "dispatch" or scope.workspace_id != "command-center"
                or scope.project_id != "fast-gateway" or not self.db_path.is_file()):
            raise AuthBrokerError("AUTH_REQUIRED")
        envelope = scope.task_contract
        if envelope.get("goal_contract") != normalize_goal_contract(None).as_dict():
            raise AuthBrokerError("TASK_DIGEST_MISMATCH")
        run_id = envelope["core_run_id"]
        key = envelope["idempotency_key"]
        con = None
        try:
            con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            con.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            if run_id.startswith("war-") and key == run_id[4:]:
                source = con.execute(
                    """SELECT t.*,m.body AS instruction_body,m.project_id AS instruction_project,
                              d.agent_id AS dispatch_agent,d.deadline_at AS delivery_deadline,
                              d.status AS delivery_status,d.claim_token,d.claim_expires_at
                       FROM war_deliveries d JOIN war_messages m ON m.id=d.message_id
                       JOIN war_tasks t ON t.source_message_id=m.id WHERE d.id=?""", (key,),
                ).fetchone()
                if (source is None or source["delivery_status"] != "sent" or not source["claim_token"]
                        or (source["claim_expires_at"] or 0) <= now
                        or (source["delivery_deadline"] or 0) <= now):
                    raise AuthBrokerError("AUTH_REQUIRED")
            elif run_id.startswith("core-exec-") and key == "war-exec-" + run_id[10:]:
                source = con.execute(
                    """SELECT t.*,m.body AS instruction_body,m.project_id AS instruction_project,
                              u.agent_id AS dispatch_agent,u.war_project_id AS unit_project,
                              u.dispatch_claimed_at,u.dispatch_message
                       FROM war_execution_units u JOIN war_tasks t ON t.id=u.war_task_id
                       JOIN war_messages m ON m.id=t.source_message_id WHERE u.execution_id=?""",
                    (run_id[10:],),
                ).fetchone()
                if (source is None or source["dispatch_claimed_at"] is None
                        or source["unit_project"] != source["project_id"]
                        or source["dispatch_message"] != source["instruction_body"]):
                    raise AuthBrokerError("AUTH_REQUIRED")
            else:
                raise AuthBrokerError("AUTH_REQUIRED")

            task_id, project_id = source["id"], source["project_id"]
            if (source["execution_mode"] != "FAST_GATEWAY" or source["status"] != "running"
                    or source["instruction_project"] != project_id
                    or source["dispatch_agent"].casefold() != scope.agent_id
                    or (source["deadline_at"] or 0) <= now
                    or not source["document_version"]
                    or source["document_version"] != source["manyfast_version"]):
                raise AuthBrokerError("BINDING_MISMATCH")
            if envelope["instruction_sha256"] != hashlib.sha256(source["instruction_body"].encode()).hexdigest():
                raise AuthBrokerError("TASK_DIGEST_MISMATCH")
            project = con.execute("SELECT status FROM war_projects WHERE id=?", (project_id,)).fetchone()
            control = con.execute("SELECT * FROM war_project_control WHERE project_id=?", (project_id,)).fetchone()
            if (not project or project["status"] == "archived" or not control
                    or control["archived_at"] is not None or control["stop_state"] != "running"):
                raise AuthBrokerError("AUTH_REQUIRED")
            targets = sorted(r[0] for r in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=?", (task_id,)))
            if scope.agent_id not in {a.casefold() for a in targets}:
                raise AuthBrokerError("BINDING_MISMATCH")
            participant = con.execute(
                "SELECT active,can_read FROM war_participants WHERE project_id=? AND lower(principal_id)=?",
                (project_id, scope.agent_id),
            ).fetchone()
            if not participant or not participant["active"] or not participant["can_read"]:
                raise AuthBrokerError("AUTH_REQUIRED")
            # The latest decision controls admission; do not fall back to an
            # older approval after the latest decision was revoked/rejected.
            approval = con.execute(
                "SELECT * FROM war_approvals WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if (not approval or approval["decision"] != "approved" or approval["revoked_at"] is not None
                    or (approval["expires_at"] or 0) <= now
                    or approval["scope_hash"] != hashlib.sha256(source["scope"].encode()).hexdigest()
                    or approval["document_version"] != source["document_version"]
                    or approval["assignee_agent_id"] != source["assignee_agent_id"]
                    or approval["target_set_hash"] != hashlib.sha256(json.dumps(targets).encode()).hexdigest()):
                raise AuthBrokerError("AUTH_REQUIRED")
            approver = con.execute(
                "SELECT active,can_read,can_approve,role FROM war_participants WHERE project_id=? AND principal_id=?",
                (project_id, approval["approver_id"]),
            ).fetchone()
            from war_room_actions import ROLE_PERMISSIONS
            if (not approver or not approver["active"] or not approver["can_read"] or not approver["can_approve"]
                    or "approve" not in ROLE_PERMISSIONS.get(approver["role"], set())):
                raise AuthBrokerError("AUTH_REQUIRED")
            calls = con.execute("SELECT call_count,turn_count FROM war_task_calls WHERE task_id=?", (task_id,)).fetchone()
            if ((calls["call_count"] if calls else 0) >= (source["call_limit"] or 0)
                    or (calls["turn_count"] if calls else 0) >= (source["turn_limit"] or 0)):
                raise AuthBrokerError("AUTH_REQUIRED")
            authorized_scope = replace(scope, workspace_id="war-room:" + task_id, project_id=project_id)
            ttl = min(30, int(approval["expires_at"]) - now, int(source["deadline_at"]) - now)
            grant = broker.issue(authorized_scope, ttl_seconds=ttl, created_by=approval["approver_id"])
            yield authorized_scope, grant.token
            con.execute(
                """INSERT INTO war_task_calls(task_id,call_count,turn_count,updated_at) VALUES (?,1,0,?)
                   ON CONFLICT(task_id) DO UPDATE SET call_count=call_count+1,updated_at=excluded.updated_at""",
                (task_id, now),
            )
            con.commit()
        except sqlite3.Error as exc:
            raise AuthBrokerError("STORE_BUSY" if "locked" in str(exc).lower() else "STORE_CORRUPT") from exc
        finally:
            if con is not None:
                con.close()
