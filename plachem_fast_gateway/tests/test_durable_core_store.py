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
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE results SET reason='x'")
            with self.assertRaises(sqlite3.IntegrityError): db.execute("DELETE FROM evidence")

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
        self.create(); claimed=self.store.claim_outbox("w",limit=1,lease_seconds=0)
        self.assertEqual(1,len(claimed)); reclaimed=self.store.claim_outbox("w2",limit=1)
        self.assertEqual(claimed[0]["event_id"],reclaimed[0]["event_id"])
        self.store.retry_outbox(reclaimed[0]["event_id"],max_attempts=1)
        with sqlite3.connect(self.path) as db: self.assertEqual("DEAD",db.execute("SELECT status FROM outbox").fetchone()[0])

    def test_legacy_mapping_conflicts_fail_closed(self):
        self.create(); self.store.map_legacy("war","old","run"); self.store.map_legacy("war","old","run")
        self.assertEqual("run",self.store.resolve_legacy("war","old"))

    def test_future_schema_fails_before_write(self):
        with sqlite3.connect(self.path) as db: db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ValueError,"UNSUPPORTED_CORE_SCHEMA"): DurableCoreStore(self.path)

    def test_production_composition_uses_only_sqlite_core_store(self):
        agents=Path(self.tmp.name)/"agents.json"; models=Path(self.tmp.name)/"models.json"
        agents.write_text(json.dumps({"agent":{"enabled":True,"capabilities":[],"runtime_model_id":"local/test","allowed_model_ids":["local/test"],"allowed_policy_profiles":["LOCAL_STANDARD"]}}))
        models.write_text(json.dumps({"models":{"local/test":{"runtime_class":"LOCAL","policy_profile":"LOCAL_STANDARD","max_runtime":10,"execution_budget":8,"finalization_recovery_budget":2,"max_retries":0,"max_tool_calls":1,"loop_guard":{"consecutive_threshold":2},"context_policy":"MANUAL","fallback_policy":"NONE"}}}))
        core=Path(self.tmp.name)/"authoritative.sqlite3"; retired=Path(self.tmp.name)/"runs.jsonl"
        with patch("plachem_fast_gateway.production_runtime.create_ubuntu_worker_transport",return_value=object()):
            engine=create_ubuntu_core_engine(core_db_path=core,agents_path=agents,models_path=models,bindings_path=Path(self.tmp.name)/"bindings.sqlite3")
        self.assertIsInstance(engine.registry,DurableCoreStore); self.assertTrue(core.exists()); self.assertFalse(retired.exists())


if __name__ == "__main__": unittest.main()
