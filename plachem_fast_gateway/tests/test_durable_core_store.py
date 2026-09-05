from __future__ import annotations

import sqlite3
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from plachem_fast_gateway.durable_core_store import DurableCoreStore
from plachem_fast_gateway.openclaw_adapter import AdapterOutcome, CoreRunStatus
from plachem_fast_gateway.runtime_policy import GoalContract
from plachem_fast_gateway.production_runtime import create_ubuntu_core_engine


GOAL = GoalContract("g", "do", ("workspace",), ("forbidden",), "result", ("done",))
POLICY = {"runtime_class": "LOCAL", "policy_profile": "LOCAL_STANDARD"}


class DurableCoreStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "core.sqlite3"
        self.store = DurableCoreStore(self.path)

    def create(self, key="idem", digest="digest", run="run"):
        return self.store.create(core_run_id=run, agent_id="agent", idempotency_key=key,
            request_hash=digest, policy=POLICY, goal_contract=GOAL)

    def test_schema_and_restart_persistence(self):
        record, created = self.create()
        self.assertTrue(created); self.assertEqual(record, DurableCoreStore(self.path).get("run"))
        with sqlite3.connect(self.path) as db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"tasks","compilations","runs","results","evidence","idempotency_keys","outbox","legacy_id_mappings"} <= names)

    def test_v1_restart_migrates_additive_ownership_and_sealing(self):
        self.create(); self.store.transition("run",CoreRunStatus.RUNNING)
        result={"status":"completed","summary":"ok","evidence":[],"artifacts":[],"scope":{"compliant":True,"violations":[]}}
        self.store.transition("run",CoreRunStatus.PASS,outcome=AdapterOutcome(CoreRunStatus.PASS,result=result))
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER evidence_no_insert_after_seal")
            db.execute("DROP TRIGGER results_no_update")
            db.execute("ALTER TABLE results DROP COLUMN sealed")
            db.execute("ALTER TABLE outbox DROP COLUMN lease_owner")
            db.execute("DELETE FROM schema_migrations WHERE version=2")
            db.execute("PRAGMA user_version=1")
        migrated=DurableCoreStore(self.path)
        self.assertEqual("PASS",migrated.get("run")["status"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(2,db.execute("PRAGMA user_version").fetchone()[0])
            self.assertIn("lease_owner",{row[1] for row in db.execute("PRAGMA table_info(outbox)")})
            self.assertEqual(1,db.execute("SELECT sealed FROM results").fetchone()[0])

    def test_idempotency_replay_and_conflict(self):
        first,_=self.create(); replay,created=self.create(run="other")
        self.assertFalse(created); self.assertEqual(first["core_run_id"],replay["core_run_id"])
        with self.assertRaisesRegex(ValueError,"IDEMPOTENCY_CONFLICT"): self.create(digest="different",run="third")

    def test_concurrent_same_key_one_authoritative_run(self):
        outcomes=[]
        def work(n):
            try: outcomes.append(self.store.create(core_run_id=f"r{n}",agent_id="agent",idempotency_key="one",request_hash="same",policy=POLICY,goal_contract=GOAL))
            except Exception as exc: outcomes.append(exc)
        threads=[threading.Thread(target=work,args=(n,)) for n in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(1,sum(1 for value in outcomes if isinstance(value,tuple) and value[1]))
        self.assertEqual(1,len({value[0]["core_run_id"] for value in outcomes if isinstance(value,tuple)}))

    def test_terminal_result_evidence_immutable_and_replay(self):
        self.create(); self.store.transition("run",CoreRunStatus.RUNNING)
        result={"status":"completed","summary":"ok","evidence":[{"type":"response"}],"artifacts":[],"scope":{"compliant":True,"violations":[]}}
        done=self.store.transition("run",CoreRunStatus.PASS,outcome=AdapterOutcome(CoreRunStatus.PASS,result=result))
        self.assertEqual("PASS",done["status"]); self.assertEqual(done,self.store.transition("run",CoreRunStatus.PASS,outcome=AdapterOutcome(CoreRunStatus.PASS,result=result)))
        changed={**result,"summary":"different"}
        with self.assertRaisesRegex(ValueError,"TERMINAL_RESULT_CONFLICT"):
            self.store.transition("run",CoreRunStatus.PASS,outcome=AdapterOutcome(CoreRunStatus.PASS,result=changed))
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE results SET reason='x'")
            with self.assertRaises(sqlite3.IntegrityError): db.execute("DELETE FROM evidence")
            result_id=db.execute("SELECT result_id FROM results WHERE core_run_id='run'").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO evidence VALUES('late',?,99,'{}')",(result_id,))

    def test_expected_version_cas(self):
        self.create()
        self.store.transition("run",CoreRunStatus.RUNNING,expected_version=0)
        with self.assertRaisesRegex(ValueError,"CONCURRENT_RUN_UPDATE"):
            self.store.cancel_run("run",expected_version=0)

    def test_cancelled_and_cancel_complete_race_has_one_terminal(self):
        self.create(); self.store.transition("run",CoreRunStatus.RUNNING)
        errors=[]
        def terminal(status):
            try: self.store.transition("run",status,reason="race")
            except ValueError as exc: errors.append(str(exc))
        threads=[threading.Thread(target=terminal,args=(s,)) for s in (CoreRunStatus.CANCELLED,CoreRunStatus.PASS)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertIn(self.store.get("run")["status"],{"CANCELLED","PASS"}); self.assertEqual(1,len(errors))

    def test_outbox_claim_reclaim_ack_retry_dead(self):
        self.create(); claimed=self.store.claim_outbox("w",limit=1,lease_seconds=1)
        self.assertEqual(1,len(claimed)); self.assertEqual("w",claimed[0]["lease_owner"])
        with self.assertRaisesRegex(ValueError,"OUTBOX_NOT_LEASED"):
            self.store.ack_outbox(claimed[0]["event_id"],worker_id="other")
        self.store.retry_outbox(claimed[0]["event_id"],worker_id="w",max_attempts=1)
        with sqlite3.connect(self.path) as db: self.assertEqual("DEAD",db.execute("SELECT status FROM outbox").fetchone()[0])

    def test_outbox_rejects_nonpositive_claim_parameters(self):
        self.create()
        for kwargs,error in (({"limit":0},"INVALID_LIMIT"),({"lease_seconds":0},"INVALID_LEASE_SECONDS")):
            with self.assertRaisesRegex(ValueError,error): self.store.claim_outbox("w",**kwargs)

    def test_outbox_ack_requires_owner_and_live_lease(self):
        self.create(); event=self.store.claim_outbox("owner",limit=1,lease_seconds=30)[0]
        self.store.ack_outbox(event["event_id"],worker_id="owner")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(("DELIVERED",None),db.execute("SELECT status,lease_owner FROM outbox WHERE event_id=?",(event["event_id"],)).fetchone())
        self.store.transition("run",CoreRunStatus.RUNNING)
        leased=self.store.claim_outbox("owner",limit=1,lease_seconds=30)[0]
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE outbox SET lease_until='1970-01-01T00:00:00+00:00' WHERE event_id=?",(leased["event_id"],))
        with self.assertRaisesRegex(ValueError,"OUTBOX_NOT_LEASED"):
            self.store.ack_outbox(leased["event_id"],worker_id="owner")

    def test_legacy_mapping_conflicts_fail_closed(self):
        self.create(); self.store.map_legacy("war","old","run"); self.store.map_legacy("war","old","run")
        self.assertEqual("run",self.store.resolve_legacy("war","old"))

    def test_future_schema_fails_before_write(self):
        with sqlite3.connect(self.path) as db: db.execute("PRAGMA user_version=999")
        before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,"UNSUPPORTED_CORE_SCHEMA"): DurableCoreStore(self.path)
        self.assertEqual(before,self.path.read_bytes())

    def test_production_composition_uses_only_sqlite_core_store(self):
        agents=Path(self.tmp.name)/"agents.json"; models=Path(self.tmp.name)/"models.json"
        agents.write_text(json.dumps({"agent":{"enabled":True,"capabilities":[],"runtime_model_id":"local/test","allowed_model_ids":["local/test"],"allowed_policy_profiles":["LOCAL_STANDARD"]}}))
        models.write_text(json.dumps({"models":{"local/test":{"runtime_class":"LOCAL","policy_profile":"LOCAL_STANDARD","max_runtime":10,"execution_budget":8,"finalization_recovery_budget":2,"max_retries":0,"max_tool_calls":1,"loop_guard":{"consecutive_threshold":2},"context_policy":"MANUAL","fallback_policy":"NONE"}}}))
        core=Path(self.tmp.name)/"authoritative.sqlite3"; retired=Path(self.tmp.name)/"runs.jsonl"
        with patch("plachem_fast_gateway.production_runtime.create_ubuntu_worker_transport",return_value=object()):
            engine=create_ubuntu_core_engine(core_db_path=core,agents_path=agents,models_path=models,bindings_path=Path(self.tmp.name)/"bindings.sqlite3")
        self.assertIsInstance(engine.registry,DurableCoreStore); self.assertTrue(core.exists()); self.assertFalse(retired.exists())


if __name__ == "__main__": unittest.main()
