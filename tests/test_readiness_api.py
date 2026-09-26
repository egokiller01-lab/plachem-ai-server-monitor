from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import unittest

from test_process_board_api import PROJECT_ID, ProcessBoardApiTests


class ReadinessApiTests(ProcessBoardApiTests):
    HEADERS = {"X-War-Room-Actor": "main", "X-War-Room-Token": "fixture-main-token"}

    def setUp(self) -> None:
        self.old_qa_secret = os.environ.get("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET")
        self.old_representatives = os.environ.get("PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS")
        self.old_test_adapter = os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER")
        self.old_test_representative = os.environ.get("PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE")
        os.environ["PLACHEM_WAR_ROOM_QA_SIGNING_SECRET"] = "fixture-qa-secret"
        os.environ["PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS"] = "main"
        os.environ["PLACHEM_WAR_ROOM_TEST_ADAPTER"] = "1"
        os.environ["PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE"] = "1"
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        for key, previous in (
            ("PLACHEM_WAR_ROOM_QA_SIGNING_SECRET", self.old_qa_secret),
            ("PLACHEM_WAR_ROOM_REPRESENTATIVE_PRINCIPALS", self.old_representatives),
            ("PLACHEM_WAR_ROOM_TEST_ADAPTER", self.old_test_adapter),
            ("PLACHEM_WAR_ROOM_TEST_ALLOW_AGENT_REPRESENTATIVE", self.old_test_representative),
        ):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    def _signed_pass(
        self,
        *,
        task_id: str,
        revision: int,
        scope_hash: str,
        document_version: str,
        qa_cycle: int,
        profile: str = "required:test,artifact",
    ) -> tuple[str, str]:
        payload = json.dumps(
            {
                "task_id": task_id,
                "verdict": "PASS",
                "evidence_profile": profile,
                "qa_principal": "ERPqa",
                "task_revision": revision,
                "scope_hash": scope_hash,
                "document_version": document_version,
                "qa_cycle": qa_cycle,
            },
            sort_keys=True,
        )
        signature = hmac.new(b"fixture-qa-secret", payload.encode(), hashlib.sha256).hexdigest()
        return payload, signature

    def _seed_completion_proof(self, con: sqlite3.Connection, task_id: str) -> None:
        scope, revision, document_version, qa_cycle = con.execute(
            "SELECT scope,revision,document_version,qa_cycle FROM war_tasks WHERE id=?", (task_id,)
        ).fetchone()
        scope_hash = hashlib.sha256(scope.encode()).hexdigest()
        binding = (task_id, revision, scope_hash, document_version, qa_cycle)
        con.execute(
            """INSERT INTO war_evidence
               (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"evidence-{task_id}", task_id, "test", f"fixture://{task_id}", "fixture",
             revision, scope_hash, document_version, qa_cycle, 1900000100),
        )
        signed_payload, signature = self._signed_pass(
            task_id=task_id,
            revision=revision,
            scope_hash=scope_hash,
            document_version=document_version,
            qa_cycle=qa_cycle,
        )
        con.execute(
            """INSERT INTO war_qa_verdicts
               (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,
                created_at,task_revision,scope_hash,document_version,qa_cycle)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"ready-verdict-{task_id}", task_id, "ERPqa", "PASS", "required:test,artifact",
             signature, signed_payload, 1900000200, revision, scope_hash, document_version, qa_cycle),
        )
        con.execute(
            """INSERT INTO war_representative_approvals
               (id,task_id,representative_id,decision,task_revision,scope_hash,document_version,qa_cycle,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"rep-{task_id}", task_id, "main", "approved", revision, scope_hash,
             document_version, qa_cycle, 1900000300),
        )

    def _make_all_done(self, *, with_completion_proof: bool = True, skip_proof: set[str] | None = None) -> None:
        skip_proof = skip_proof or set()
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM war_deliveries")
            con.execute("DELETE FROM war_qa_verdicts")
            con.execute("UPDATE war_tasks SET status='completed'")
            if with_completion_proof:
                task_ids = [row[0] for row in con.execute("SELECT id FROM war_tasks ORDER BY id")]
                for task_id in task_ids:
                    if task_id not in skip_proof:
                        self._seed_completion_proof(con, task_id)
            con.commit()

    def _supersede(self, task_id: str) -> None:
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO war_audit_events VALUES (?,?,?,?,?,?,?,?,?)",
                (f"sup-{task_id}", PROJECT_ID, "main", "task_superseded", "task", task_id, "{}", "fixture", 1900000000),
            )
            con.commit()

    def test_positive_and_superseded_history_are_ready_and_read_only(self) -> None:
        self._make_all_done()
        self._supersede("task-draft")
        before = self.db.read_bytes()
        response = self.client.get(f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload["ready_for_representative_completion"])
        self.assertEqual(8, payload["considered_task_count"])
        self.assertEqual(["task-draft"], payload["nonblocking_superseded_ids"])
        self.assertEqual([], payload["blocking_task_ids"])
        self.assertEqual(1, payload["state_counts"]["SUPERSEDED"])
        self.assertEqual(before, self.db.read_bytes())

    def test_negative_state_is_blocking_and_fail_closed(self) -> None:
        self._make_all_done()
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE war_tasks SET status='draft' WHERE id='task-draft'")
            con.execute("DELETE FROM war_qa_verdicts WHERE task_id='task-draft'")
            con.commit()
        payload = self.client.get(f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS).json()
        self.assertFalse(payload["ready_for_representative_completion"])
        self.assertEqual(["task-draft"], payload["blocking_task_ids"])
        self.assertEqual(9, payload["considered_task_count"])

    def test_done_without_current_completion_proof_is_blocked(self) -> None:
        self._make_all_done(skip_proof={"task-draft"})
        payload = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        ).json()
        self.assertFalse(payload["ready_for_representative_completion"])
        self.assertEqual(["task-draft"], payload["blocking_task_ids"])
        self.assertEqual(
            ["CURRENT_EVIDENCE", "REPRESENTATIVE_APPROVAL", "SIGNED_QA_PASS"],
            payload["blocking_reasons"]["task-draft"],
        )

    def test_pass_requires_current_evidence_and_session_integrity(self) -> None:
        self._make_all_done(skip_proof={"task-draft"})
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE war_tasks SET status='qa' WHERE id='task-draft'")
            scope, revision, document_version, qa_cycle = con.execute(
                "SELECT scope,revision,document_version,qa_cycle FROM war_tasks WHERE id='task-draft'"
            ).fetchone()
            scope_hash = hashlib.sha256(scope.encode()).hexdigest()
            signed_payload, signature = self._signed_pass(
                task_id="task-draft",
                revision=revision,
                scope_hash=scope_hash,
                document_version=document_version,
                qa_cycle=qa_cycle,
            )
            con.execute(
                """INSERT INTO war_qa_verdicts
                   (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,
                    created_at,task_revision,scope_hash,document_version,qa_cycle)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("pass-only-task-draft", "task-draft", "ERPqa", "PASS", "required:test,artifact",
                 signature, signed_payload, 1900000200, revision, scope_hash, document_version, qa_cycle),
            )
            con.commit()

        payload = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        ).json()
        self.assertEqual(["CURRENT_EVIDENCE"], payload["blocking_reasons"]["task-draft"])

        with sqlite3.connect(self.db) as con:
            con.execute(
                """INSERT INTO war_evidence
                   (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("pass-evidence-task-draft", "task-draft", "test", "fixture://pass", "fixture",
                 revision, scope_hash, document_version, qa_cycle, 1900000100),
            )
            con.execute(
                """INSERT INTO war_evidence
                   (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("integrity-evidence-task-draft", "task-draft", "session_integrity",
                 "fixture://integrity", "fixture", revision, scope_hash, document_version,
                 qa_cycle, 1900000150),
            )
            packet_json = json.dumps({"session_integrity_required": True}, sort_keys=True)
            con.execute(
                "INSERT INTO war_grounding_packets(task_id,packet_json,packet_hash,created_at) VALUES (?,?,?,?)",
                ("task-draft", packet_json, hashlib.sha256(packet_json.encode()).hexdigest(), 1900000000),
            )
            con.commit()

        payload = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        ).json()
        self.assertEqual(["SESSION_INTEGRITY"], payload["blocking_reasons"]["task-draft"])

        with sqlite3.connect(self.db) as con:
            con.execute(
                """INSERT INTO war_session_integrity
                   (evidence_id,task_id,scope,pre_count,post_count,changed_count,deleted_count,
                    uncertain_count,mtime_encoding,verified_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("integrity-evidence-task-draft", "task-draft", "fixture", 1, 2, 1, 0, 0,
                 "ns", 1900000140, 1900000150),
            )
            con.commit()
        payload = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        ).json()
        self.assertEqual(["SESSION_INTEGRITY"], payload["blocking_reasons"]["task-draft"])

        with sqlite3.connect(self.db) as con:
            con.execute(
                """UPDATE war_session_integrity
                   SET post_count=1,changed_count=0,mtime_encoding='decimal_string'
                   WHERE evidence_id='integrity-evidence-task-draft'"""
            )
            con.commit()
        payload = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        ).json()
        self.assertTrue(payload["ready_for_representative_completion"])
        self.assertEqual([], payload["blocking_task_ids"])

    def test_malformed_grounding_packet_fails_closed_without_500(self) -> None:
        self._make_all_done()
        raw = "{not-json"
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO war_grounding_packets(task_id,packet_json,packet_hash,created_at) VALUES (?,?,?,?)",
                ("task-draft", raw, hashlib.sha256(raw.encode()).hexdigest(), 1900000000),
            )
            con.commit()
        response = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        )
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertFalse(payload["ready_for_representative_completion"])
        self.assertEqual(["GROUNDING_PACKET_INVALID"], payload["blocking_reasons"]["task-draft"])

    def test_forged_qa_signature_blocks_readiness_and_representative_completion(self) -> None:
        self._make_all_done(skip_proof={"task-draft"})
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE war_tasks SET status='qa' WHERE id='task-draft'")
            scope, revision, document_version, qa_cycle = con.execute(
                "SELECT scope,revision,document_version,qa_cycle FROM war_tasks WHERE id='task-draft'"
            ).fetchone()
            scope_hash = hashlib.sha256(scope.encode()).hexdigest()
            signed_payload, _ = self._signed_pass(
                task_id="task-draft",
                revision=revision,
                scope_hash=scope_hash,
                document_version=document_version,
                qa_cycle=qa_cycle,
            )
            con.execute(
                """INSERT INTO war_evidence
                   (id,task_id,evidence_type,uri,summary,task_revision,scope_hash,document_version,qa_cycle,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("forged-evidence", "task-draft", "test", "fixture://forged", "fixture",
                 revision, scope_hash, document_version, qa_cycle, 1900000100),
            )
            con.execute(
                """INSERT INTO war_qa_verdicts
                   (id,task_id,qa_principal,verdict,evidence_profile,signature,signed_payload,
                    created_at,task_revision,scope_hash,document_version,qa_cycle)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("forged-verdict", "task-draft", "ERPqa", "PASS", "required:test,artifact",
                 "forged", signed_payload, 1900000200, revision, scope_hash, document_version, qa_cycle),
            )
            con.commit()

        payload = self.client.get(
            f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS
        ).json()
        self.assertFalse(payload["ready_for_representative_completion"])
        self.assertEqual(["SIGNED_QA_PASS"], payload["blocking_reasons"]["task-draft"])

        response = self.client.post(
            "/api/war-room/tasks/task-draft/representative-completion",
            json={"decision": "approved"},
            headers={**self.HEADERS, "Idempotency-Key": "forged-completion"},
        )
        self.assertEqual(409, response.status_code, response.text)

    def test_auth_and_empty_project_contract(self) -> None:
        self.assertEqual(401, self.client.get(f"/api/war-room/projects/{PROJECT_ID}/readiness").status_code)
        self.assertEqual(403, self.client.get("/api/war-room/projects/missing/readiness", headers=self.HEADERS).status_code)
        self._make_all_done()
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM war_tasks")
            con.commit()
        payload = self.client.get(f"/api/war-room/projects/{PROJECT_ID}/readiness", headers=self.HEADERS).json()
        self.assertFalse(payload["ready_for_representative_completion"])
        self.assertEqual(0, payload["considered_task_count"])
        self.assertEqual([], payload["blocking_task_ids"])


if __name__ == "__main__":
    unittest.main()
