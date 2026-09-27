from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class WarRoomDocumentRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.db = self.root / "war-room.sqlite3"
        self.previous = {key: os.environ.get(key) for key in (
            "PLACHEM_WAR_ROOM_DB", "OPENCLAW_HOME", "PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS",
            "PLACHEM_WAR_ROOM_TEST_ADAPTER", "PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS",
            "PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE", "PLACHEM_WAR_ROOM_QA_SIGNING_SECRET",
            "PLACHEM_WAR_ROOM_WORKTREE",
        )}
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(self.db)
        os.environ["OPENCLAW_HOME"] = str(self.root / "openclaw")
        os.environ["PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS"] = json.dumps({
            "main": "fixture-main-token", "ERPcoder": "fixture-erpcoder-token",
            "ERPmanager": "fixture-erpmanager-token", "ERPqa": "fixture-erpqa-token",
        })
        os.environ["PLACHEM_WAR_ROOM_TEST_ADAPTER"] = "1"
        os.environ["PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS"] = "main"
        os.environ["PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE"] = "1"
        os.environ["PLACHEM_WAR_ROOM_QA_SIGNING_SECRET"] = "fixture-qa-secret"
        os.environ["PLACHEM_WAR_ROOM_WORKTREE"] = str(self.worktree)

        import war_room
        war_room.provision_database()
        from app import app
        self.client = TestClient(app)
        self.client.__enter__()
        self.headers = {
            "X-War-Room-Actor": "main",
            "X-War-Room-Token": "fixture-main-token",
        }

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def _write(self, name: str, body: str) -> Path:
        path = self.worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _register(self, path: Path, **extra):
        payload = {
            "title": extra.pop("title", path.stem),
            "category": extra.pop("category", "architecture"),
            "uri": str(path),
            "summary": extra.pop("summary", "registry test"),
            **extra,
        }
        return self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/documents/register",
            json=payload,
            headers={**self.headers, "Idempotency-Key": f"doc-{time.time_ns()}"},
        )

    def test_registry_schema_and_project_document_api(self) -> None:
        path = self._write("docs/ARCHITECTURE.md", "# v1")
        created = self._register(path)
        self.assertEqual(201, created.status_code, created.text)
        self.assertEqual(1, created.json()["version"])

        listed = self.client.get(
            "/api/war-room/projects/plachem-agent-war-room/documents",
            headers=self.headers,
        )
        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual(1, len(listed.json()["items"]))
        row = listed.json()["items"][0]
        self.assertEqual("architecture", row["category"])
        self.assertEqual(str(path.resolve()), row["uri"])

        detail = self.client.get(f"/api/war-room/documents/{row['id']}", headers=self.headers)
        self.assertEqual(200, detail.status_code, detail.text)
        versions = self.client.get(
            f"/api/war-room/documents/{row['id']}/versions", headers=self.headers
        )
        self.assertEqual([1], [item["version"] for item in versions.json()["items"]])

    def test_versioning_noop_conflict_and_append_only(self) -> None:
        path = self._write("docs/DECISIONS.md", "decision-v1")
        first = self._register(path, category="decision")
        self.assertEqual(201, first.status_code, first.text)
        document_id = first.json()["document_id"]

        noop = self._register(
            path, category="decision", document_id=document_id, expected_version=1
        )
        self.assertEqual(201, noop.status_code, noop.text)
        self.assertFalse(noop.json()["changed"])
        self.assertEqual(1, noop.json()["version"])

        path.write_text("decision-v2", encoding="utf-8")
        second = self._register(
            path, category="decision", document_id=document_id, expected_version=1
        )
        self.assertEqual(201, second.status_code, second.text)
        self.assertEqual(2, second.json()["version"])

        conflict = self._register(
            path, category="decision", document_id=document_id, expected_version=1
        )
        self.assertEqual(409, conflict.status_code, conflict.text)

        with sqlite3.connect(self.db) as con:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute(
                    "UPDATE war_document_versions SET summary='mutated' WHERE document_id=?",
                    (document_id,),
                )

    def test_path_escape_and_archived_project_are_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        escaped = self._register(outside)
        self.assertEqual(422, escaped.status_code, escaped.text)

        inside = self._write("docs/REPORT.md", "inside")
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE war_projects SET status='archived' WHERE id='plachem-agent-war-room'"
            )
            con.commit()
        archived = self._register(inside, category="report")
        self.assertIn(archived.status_code, {409, 422})

    def test_prepare_packet_contains_project_document_metadata(self) -> None:
        path = self._write("docs/REQUIREMENTS.md", "# requirements")
        registered = self._register(path, category="requirements")
        self.assertEqual(201, registered.status_code, registered.text)
        document_id = registered.json()["document_id"]

        prepared = self.client.post(
            "/api/war-room/projects/plachem-agent-war-room/prepare",
            json={
                "instruction": "프로젝트 문서를 기준으로 작업",
                "agent_ids": ["ERPcoder"],
                "reviewer_agent_id": "ERPqa",
                "deadline_at": int(time.time()) + 1800,
                "document_version": "baseline-2026-08-23",
                "grounding": {
                    "worktree": str(self.worktree),
                    "approved_paths": [str(self.worktree)],
                    "required_document_ids": [document_id],
                },
            },
            headers={**self.headers, "Idempotency-Key": "prepare-with-docs"},
        )
        self.assertEqual(201, prepared.status_code, prepared.text)
        with sqlite3.connect(self.db) as con:
            packet = json.loads(
                con.execute(
                    "SELECT packet_json FROM war_grounding_packets WHERE task_id=?",
                    (prepared.json()["task_id"],),
                ).fetchone()[0]
            )
        self.assertEqual([document_id], packet["required_document_ids"])
        self.assertEqual(document_id, packet["project_documents"][0]["document_id"])
        self.assertEqual(str(path.resolve()), packet["project_documents"][0]["uri"])


    def test_fast_gateway_document_artifact_is_auto_registered(self) -> None:
        artifact = self._write("outputs/FINAL_PROJECT_RESULT.md", "# final result")
        now = int(time.time())
        task_id = "task-fast-doc"
        message_id = "message-fast-doc"
        response_id = "response-fast-doc"
        delivery_id = "delivery-fast-doc"
        packet = {
            "worktree": str(self.worktree),
            "approved_paths": [str(self.worktree)],
            "revision": "r1",
        }
        response = {
            "status": "completed",
            "summary": "final project report",
            "evidence": [{"type": "test", "detail": "pass"}],
            "artifacts": [{"path": str(artifact)}],
            "scope": {"compliant": True, "violations": []},
        }
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            con.execute(
                """INSERT INTO war_messages
                   (id,project_id,message_type,author_type,author_id,body,created_at,redaction_state,original_body)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (message_id, "plachem-agent-war-room", "instruction", "agent", "main", "do work", now, "clean", "do work"),
            )
            con.execute(
                """INSERT INTO war_tasks
                   (id,project_id,source_message_id,assignee_agent_id,reviewer_agent_id,scope,status,manyfast_version,
                    document_version,call_limit,turn_limit,execution_mode,deadline_at,revision,qa_cycle,created_at,updated_at)
                   VALUES (?,?,?,?,?,?, 'running', ?,?,?,?,?,?,1,0,?,?)""",
                (
                    task_id, "plachem-agent-war-room", message_id, "ERPcoder", "ERPqa", "fast document",
                    "baseline", "r1", 1, 1, "FAST_GATEWAY", now + 600, now, now,
                ),
            )
            packet_json = json.dumps(packet, sort_keys=True)
            con.execute(
                "INSERT INTO war_grounding_packets(task_id,packet_json,packet_hash,created_at) VALUES (?,?,?,?)",
                (task_id, packet_json, "hash", now),
            )
            con.execute(
                """INSERT INTO war_messages
                   (id,project_id,message_type,author_type,author_id,body,source_message_id,created_at,redaction_state,original_body)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    response_id, "plachem-agent-war-room", "result", "agent", "ERPcoder",
                    json.dumps(response), message_id, now, "clean", json.dumps(response),
                ),
            )
            con.execute(
                """INSERT INTO war_deliveries
                   (id,message_id,agent_id,status,attempt_count,max_attempts,run_id,response_message_id,session_id,created_at)
                   VALUES (?,?,?,'responded',1,3,?,?,?,?)""",
                (delivery_id, message_id, "ERPcoder", "run-fast-doc", response_id, "session-fast-doc", now),
            )
            task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
            delivery = con.execute(
                """SELECT d.*,t.id AS task_id,t.execution_mode FROM war_deliveries d
                   JOIN war_tasks t ON t.source_message_id=d.message_id WHERE d.id=?""",
                (delivery_id,),
            ).fetchone()
            from war_room_worker import _capture_project_documents
            _capture_project_documents(
                con, task=task, delivery_row=delivery, structured_result=None, now=now
            )
            con.commit()
            row = con.execute(
                """SELECT d.title,d.category,v.version,v.uri,v.source_task_id,v.source_agent_id,
                          v.source_session_id,v.source_run_id
                   FROM war_documents d JOIN war_document_versions v ON v.id=d.current_version_id
                   WHERE d.project_id='plachem-agent-war-room'"""
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("report", row["category"])
        self.assertEqual(1, row["version"])
        self.assertEqual(str(artifact.resolve()), row["uri"])
        self.assertEqual(task_id, row["source_task_id"])
        self.assertEqual("ERPcoder", row["source_agent_id"])
        self.assertEqual("session-fast-doc", row["source_session_id"])
        self.assertEqual("run-fast-doc", row["source_run_id"])



if __name__ == "__main__":
    unittest.main()
