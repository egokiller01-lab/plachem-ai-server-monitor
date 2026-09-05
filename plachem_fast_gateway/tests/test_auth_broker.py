from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plachem_fast_gateway.auth_broker import (
    AuthBrokerError, AuthScope, SQLiteAuthBroker, canonical_task_digest,
    create_production_auth_broker,
)


class Keys:
    def __init__(self): self.values = {"k1": b"test-pepper-not-a-credential"}
    def get(self, key_id):
        try: return self.values[key_id]
        except KeyError as exc: raise AuthBrokerError("AUTH_REQUIRED") from exc


class AuthBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "auth.sqlite3"
        self.keys = Keys()
        self.broker = SQLiteAuthBroker(self.path, self.keys, key_id="k1")
        self.scope = AuthScope("ERPcoder", "EXECUTE", "workspace-1", "project-1", {
            "goal_id": "goal-1", "objective": "build approved adapter",
            "allowed_scope": ["AUTH_BROKER"], "forbidden_scope": ["PRODUCTION"],
        })

    def tearDown(self): self.tmp.cleanup()

    def assert_code(self, code, fn):
        with self.assertRaises(AuthBrokerError) as caught: fn()
        self.assertEqual(code, caught.exception.code)

    def issue(self, **kw): return self.broker.issue(self.scope, ttl_seconds=60, created_by="main", **kw)

    def test_deny_default_without_token(self):
        self.assert_code("AUTH_REQUIRED", lambda: self.broker.verify_and_consume(None, self.scope, run_id="r1"))
        audit = self.broker.list_audit()
        self.assertEqual(("DENY", "AUTH_REQUIRED", None),
                         (audit[-1]["outcome"], audit[-1]["error_code"], audit[-1]["grant_id"]))

    def test_unknown_token_is_audited_without_token_identifier(self):
        marker = "unknown-bearer-marker"
        self.assert_code("INVALID_TOKEN", lambda: self.broker.verify_and_consume(marker, self.scope, run_id="r1"))
        audit = self.broker.list_audit()[-1]
        self.assertEqual(("DENY", "INVALID_TOKEN", None),
                         (audit["outcome"], audit["error_code"], audit["grant_id"]))
        self.assertNotIn(marker.encode(), self.path.read_bytes())

    def test_issue_exact_consume_and_no_raw_token(self):
        grant = self.issue(now_ms=1000)
        result = self.broker.verify_and_consume(grant.token, self.scope, run_id="r1", now_ms=1001)
        self.assertEqual(grant.grant_id, result.grant_id)
        raw = self.path.read_bytes()
        self.assertNotIn(grant.token.encode(), raw)
        self.assertNotIn(b"build approved adapter", raw)

    def test_replay_is_rejected_and_audited(self):
        grant = self.issue(now_ms=1000)
        self.broker.verify_and_consume(grant.token, self.scope, run_id="r1", now_ms=1001)
        self.assert_code("ALREADY_CONSUMED", lambda: self.broker.verify_and_consume(grant.token, self.scope, run_id="r2", now_ms=1002))
        self.assertEqual("DENY", self.broker.list_audit()[-1]["outcome"])

    def test_revoke_and_expiry(self):
        grant = self.issue(now_ms=1000)
        self.broker.revoke(grant.grant_id, revoked_by="main", now_ms=1001)
        self.assert_code("REVOKED", lambda: self.broker.verify_and_consume(grant.token, self.scope, run_id="r", now_ms=1002))
        expired = self.broker.issue(self.scope, ttl_seconds=1, created_by="main", now_ms=2000)
        self.assert_code("EXPIRED", lambda: self.broker.verify_and_consume(expired.token, self.scope, run_id="r", now_ms=3000))

    def test_consumed_grant_is_terminal_and_cannot_be_revoked(self):
        grant = self.issue(now_ms=1000)
        self.broker.verify_and_consume(grant.token, self.scope, run_id="r", now_ms=1001)
        self.assert_code("ALREADY_CONSUMED", lambda: self.broker.revoke(grant.grant_id, revoked_by="main", now_ms=1002))
        self.assertEqual(("REVOKE", "DENY", "ALREADY_CONSUMED"),
                         tuple(self.broker.list_audit()[-1][key] for key in ("event_type", "outcome", "error_code")))

    def test_tampered_token(self):
        grant = self.issue()
        self.assert_code("INVALID_TOKEN", lambda: self.broker.verify_and_consume(grant.token + "x", self.scope, run_id="r"))

    def test_each_binding_mismatch_and_task_digest(self):
        fields = ["agent_id", "action", "workspace_id", "project_id"]
        for field in fields:
            with self.subTest(field=field):
                grant = self.issue()
                values = self.scope.__dict__ | {field: "different"}
                other = AuthScope(**values)
                self.assert_code("BINDING_MISMATCH", lambda g=grant, s=other: self.broker.verify_and_consume(g.token, s, run_id="r"))
        grant = self.issue()
        other = AuthScope(self.scope.agent_id, self.scope.action, self.scope.workspace_id,
                          self.scope.project_id, self.scope.task_contract | {"goal_id": "other"})
        self.assert_code("TASK_DIGEST_MISMATCH", lambda: self.broker.verify_and_consume(grant.token, other, run_id="r"))

    def test_concurrency_exactly_one(self):
        grant = self.issue()
        def consume(n):
            try:
                self.broker.verify_and_consume(grant.token, self.scope, run_id=f"r{n}")
                return "ok"
            except AuthBrokerError as exc: return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(consume, range(8)))
        self.assertEqual(1, results.count("ok"))
        self.assertTrue(set(results) <= {"ok", "ALREADY_CONSUMED", "STORE_BUSY"})

    def test_restart_persistence_and_migration_idempotence(self):
        grant = self.issue()
        again = SQLiteAuthBroker(self.path, self.keys, key_id="k1")
        again.verify_and_consume(grant.token, self.scope, run_id="r")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])

    def test_busy_and_corrupt(self):
        grant = self.issue()
        lock = sqlite3.connect(self.path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            self.assert_code("STORE_BUSY", lambda: SQLiteAuthBroker(self.path, self.keys, key_id="k1", busy_retries=0).verify_and_consume(grant.token, self.scope, run_id="r"))
        finally:
            lock.execute("ROLLBACK"); lock.close()
        corrupt = Path(self.tmp.name) / "bad.sqlite3"; corrupt.write_bytes(b"not sqlite")
        self.assert_code("STORE_CORRUPT", lambda: SQLiteAuthBroker(corrupt, self.keys, key_id="k1"))

    def test_audit_tamper_delete_and_reorder_detected(self):
        for mutation in ("tamper", "delete", "reorder"):
            path = Path(self.tmp.name) / f"{mutation}.sqlite3"
            broker = SQLiteAuthBroker(path, self.keys, key_id="k1")
            first = broker.issue(self.scope, ttl_seconds=60, created_by="main")
            broker.revoke(first.grant_id, revoked_by="main")
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER auth_audit_no_update"); db.execute("DROP TRIGGER auth_audit_no_delete")
                if mutation == "tamper": db.execute("UPDATE auth_audit SET outcome='DENY' WHERE seq=1")
                elif mutation == "delete": db.execute("DELETE FROM auth_audit WHERE seq=1")
                else:
                    db.execute("UPDATE auth_audit SET seq=99 WHERE seq=1")
            self.assert_code("AUDIT_INTEGRITY_FAILED", lambda p=path: SQLiteAuthBroker(p, self.keys, key_id="k1"))

    def test_append_only_repository(self):
        self.issue()
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("DELETE FROM auth_audit")
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE auth_audit SET outcome='DENY'")

    def test_schema_and_audit_have_no_forbidden_execution_fields(self):
        with sqlite3.connect(self.path) as db:
            ddl = " ".join(row[0] or "" for row in db.execute("SELECT sql FROM sqlite_master"))
        lowered = ddl.lower()
        for forbidden in ("model", "provider", "endpoint", "credential"):
            self.assertNotIn(forbidden, lowered)

    def test_digest_is_canonical_and_rejects_sensitive_body(self):
        reordered = AuthScope(self.scope.agent_id, self.scope.action, self.scope.workspace_id,
                              self.scope.project_id, dict(reversed(list(self.scope.task_contract.items()))))
        self.assertEqual(canonical_task_digest(self.scope), canonical_task_digest(reordered))
        bad = AuthScope("a", "b", "c", "d", {"message": "secret body"})
        self.assert_code("FORBIDDEN_FIELD", lambda: canonical_task_digest(bad))

    def test_forbidden_fields_are_rejected_recursively_before_database_write(self):
        before = self.path.read_bytes()
        for key in ("model", "provider", "endpoint", "credential", "token", "secret",
                    "password", "api_key", "api-key", "apikey"):
            with self.subTest(key=key):
                scope = AuthScope("a", "b", "c", "d", {"outer": [{key: "marker"}]})
                self.assert_code("FORBIDDEN_FIELD", lambda s=scope: self.broker.issue(
                    s, ttl_seconds=1, created_by="main"))
        self.assertEqual(before, self.path.read_bytes())

    def test_actor_identity_is_hashed_and_raw_markers_are_absent(self):
        creator = "creator-unique-marker"
        revoker = "revoker-unique-marker"
        grant = self.broker.issue(self.scope, ttl_seconds=60, created_by=creator)
        self.broker.revoke(grant.grant_id, revoked_by=revoker)
        raw = self.path.read_bytes()
        self.assertNotIn(creator.encode(), raw)
        self.assertNotIn(revoker.encode(), raw)
        with sqlite3.connect(self.path) as db:
            stored = db.execute("SELECT created_by FROM auth_grants WHERE grant_id=?", (grant.grant_id,)).fetchone()[0]
        self.assertTrue(stored.startswith("id-v1:"))

    def test_future_schema_is_rejected_before_any_schema_write(self):
        path = Path(self.tmp.name) / "future.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL, checksum TEXT NOT NULL)")
            db.execute("INSERT INTO schema_migrations VALUES(99,1,'future')")
        with sqlite3.connect(path) as db:
            before = tuple(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))
        self.assert_code("SCHEMA_UNSUPPORTED", lambda: SQLiteAuthBroker(path, self.keys, key_id="k1"))
        with sqlite3.connect(path) as db:
            after = tuple(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))
            self.assertEqual([(99, 1, "future")], db.execute("SELECT * FROM schema_migrations").fetchall())
        self.assertEqual(before, after)

    def test_production_factory_is_fail_closed_and_sqlite_only(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assert_code("AUTH_REQUIRED", create_production_auth_broker)
        with patch.dict(os.environ, {"PLACHEM_AUTH_BROKER_DB": str(self.path),
                                     "PLACHEM_AUTH_BROKER_KEY_ID": "missing"}, clear=True):
            self.assert_code("AUTH_REQUIRED", create_production_auth_broker)


if __name__ == "__main__": unittest.main()
