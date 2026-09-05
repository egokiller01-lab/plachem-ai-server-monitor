"""Authoritative SQLite store for Command Center core state.

This database is deliberately independent of War Room, OpenConnector and the
authorization broker.  Core state and its projection outbox commit together.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core_engine import RunRegistry, _TERMINAL, _TRANSITIONS, _utcnow
from .openclaw_adapter import AdapterOutcome, CoreRunStatus, RunBinding
from .runtime_policy import GoalContract, RuntimeClass

SCHEMA_VERSION = 1


class DurableCoreStore(RunRegistry):
    """Repository-compatible durable registry plus normalized core entities."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = _utcnow,
                 run_id_factory: Callable[[], str] | None = None, busy_timeout_ms: int = 5000) -> None:
        super().__init__(path, clock=clock, run_id_factory=run_id_factory)
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.DatabaseError as exc:
            raise ValueError("CORE_STORE_UNAVAILABLE") from exc

    def _migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            current = int(db.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise ValueError("UNSUPPORTED_CORE_SCHEMA")
            if current == SCHEMA_VERSION:
                return
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE tasks(task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, task_digest TEXT NOT NULL,
              goal_contract_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE compilations(compilation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              policy_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE runs(core_run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              compilation_id TEXT NOT NULL REFERENCES compilations(compilation_id), agent_id TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','PASS','FAIL','BLOCKED','TIMEOUT','CANCELLED')),
              version INTEGER NOT NULL DEFAULT 0, correlation_id TEXT NOT NULL UNIQUE,
              transport_run_id TEXT, session_key TEXT, record_json TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE results(result_id TEXT PRIMARY KEY, core_run_id TEXT NOT NULL UNIQUE REFERENCES runs(core_run_id),
              status TEXT NOT NULL, result_json TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES results(result_id),
              ordinal INTEGER NOT NULL, evidence_json TEXT NOT NULL, UNIQUE(result_id, ordinal));
            CREATE TABLE idempotency_keys(idempotency_key TEXT PRIMARY KEY, task_digest TEXT NOT NULL,
              core_run_id TEXT NOT NULL REFERENCES runs(core_run_id), created_at TEXT NOT NULL);
            CREATE TABLE outbox(event_id TEXT PRIMARY KEY, core_run_id TEXT NOT NULL REFERENCES runs(core_run_id),
              event_type TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('PENDING','LEASED','DELIVERED','DEAD')),
              attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_until TEXT, created_at TEXT NOT NULL);
            CREATE TABLE legacy_id_mappings(namespace TEXT NOT NULL, legacy_id TEXT NOT NULL,
              core_run_id TEXT NOT NULL REFERENCES runs(core_run_id), PRIMARY KEY(namespace, legacy_id),
              UNIQUE(namespace, core_run_id));
            CREATE TRIGGER results_no_update BEFORE UPDATE ON results BEGIN SELECT RAISE(ABORT,'RESULT_IMMUTABLE'); END;
            CREATE TRIGGER results_no_delete BEFORE DELETE ON results BEGIN SELECT RAISE(ABORT,'RESULT_IMMUTABLE'); END;
            CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'EVIDENCE_IMMUTABLE'); END;
            CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'EVIDENCE_IMMUTABLE'); END;
            INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,datetime('now'));
            PRAGMA user_version=1;
            COMMIT;
            """)
        except Exception:
            if db.in_transaction: db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _loads(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return json.loads(row["record_json"]) if row else None

    @staticmethod
    def _dump(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _event(self, db: sqlite3.Connection, run_id: str, event_type: str, record: Mapping[str, Any], now: str) -> None:
        db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,'PENDING',0,?,NULL,?)",
                   (uuid.uuid4().hex, run_id, event_type, f"{run_id}:{event_type}:{record.get('status')}:{record.get('updated_at')}", self._dump(record), now, now))

    def create(self, *, core_run_id: str | None, agent_id: str, idempotency_key: str,
               request_hash: str, policy: Mapping[str, Any], goal_contract: GoalContract,
               parent_core_run_id: str | None = None, context_reset_count: int = 0) -> tuple[dict[str, Any], bool]:
        actual = core_run_id or self._run_id_factory()
        now = self._clock().astimezone(timezone.utc).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            idem = db.execute("SELECT task_digest,core_run_id FROM idempotency_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if idem:
                if idem["task_digest"] != request_hash: raise ValueError("IDEMPOTENCY_CONFLICT")
                row = db.execute("SELECT record_json FROM runs WHERE core_run_id=?", (idem["core_run_id"],)).fetchone()
                db.commit(); return self._loads(row), False  # type: ignore[return-value]
            existing = db.execute("SELECT record_json FROM runs WHERE core_run_id=?", (actual,)).fetchone()
            if existing: raise ValueError("IDEMPOTENCY_CONFLICT")
            record = self._new_record(actual, agent_id, idempotency_key, request_hash, policy, goal_contract,
                                      parent_core_run_id, context_reset_count, now)
            task_id, compilation_id = f"task-{actual}", f"comp-{actual}"
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (task_id, agent_id, request_hash, self._dump(goal_contract.as_dict()), now))
            db.execute("INSERT INTO compilations VALUES(?,?,?,?)", (compilation_id, task_id, self._dump(policy), now))
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,0,?,?,?, ?,?,?)",
                       (actual, task_id, compilation_id, agent_id, 'QUEUED', actual, None, None, self._dump(record), now, now))
            db.execute("INSERT INTO idempotency_keys VALUES(?,?,?,?)", (idempotency_key, request_hash, actual, now))
            self._event(db, actual, "RUN_CREATED", record, now)
            db.commit(); return copy.deepcopy(record), True
        except sqlite3.IntegrityError as exc:
            if db.in_transaction: db.rollback()
            raise ValueError("CORE_STORE_CONFLICT") from exc
        except Exception:
            if db.in_transaction: db.rollback()
            raise
        finally:
            db.close()

    def _new_record(self, actual: str, agent_id: str, idem: str, digest: str, policy: Mapping[str, Any],
                    goal: GoalContract, parent: str | None, resets: int, now: str) -> dict[str, Any]:
        return {"core_run_id":actual,"agent_id":agent_id,"idempotency_key":idem,"request_hash":digest,
          "status":"QUEUED","created_at":now,"updated_at":now,"started_at":None,"completed_at":None,"reason":"","result":None,
          "format_error":"","format_recovery_attempts":0,"format_recovery_rejection":"","openclaw_binding":None,
          "runtime_class":policy.get("runtime_class",RuntimeClass.UNKNOWN.value),"model_profile":policy.get("model_profile"),
          "policy_profile":policy.get("policy_profile","UNKNOWN"),"max_runtime":policy.get("max_runtime"),
          "execution_budget":policy.get("execution_budget"),"finalization_recovery_budget":policy.get("finalization_recovery_budget"),
          "max_retries":policy.get("max_retries"),"max_tool_calls":policy.get("max_tool_calls"),"context_policy":policy.get("context_policy","MANUAL"),
          "fallback_policy":policy.get("fallback_policy","NONE"),"runtime_seconds":0.0,"retry_count":0,"tool_call_count":None,
          "tool_call_metric":"UNSUPPORTED","policy_status":"NORMAL","policy_events":[],"cancel_reason":"",
          "policy_state":{"retry_count":0,"tool_call_count":None},"goal_contract":goal.as_dict(),"goal_status":"NORMAL",
          "goal_reinjection_ready":False,"progress_checkpoint":None,"verified_progress":{"completed_conditions":[],"artifacts":[],"evidence":[],"scope":[]},
          "context_reset_count":resets,"max_context_resets":policy.get("max_context_resets",0),"parent_core_run_id":parent,
          "escalation_required":False,"escalation_reason":"","escalation_package":None}

    def _mutate(self, run_id: str, fn: Callable[[dict[str, Any]], None], event: str) -> dict[str, Any]:
        db=self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT record_json,version FROM runs WHERE core_run_id=?",(run_id,)).fetchone()
            if not row: raise ValueError(f"UNKNOWN_CORE_RUN:{run_id}")
            rec=self._loads(row); assert rec is not None; fn(rec)
            now=rec["updated_at"]
            cur=db.execute("UPDATE runs SET status=?,version=version+1,transport_run_id=?,session_key=?,record_json=?,updated_at=? WHERE core_run_id=? AND version=?",
              (rec["status"],(rec.get("openclaw_binding") or {}).get("openclaw_run_id"),(rec.get("openclaw_binding") or {}).get("session_key"),self._dump(rec),now,run_id,row["version"]))
            if cur.rowcount != 1: raise ValueError("CONCURRENT_RUN_UPDATE")
            if CoreRunStatus(rec["status"]) in _TERMINAL:
                existing=db.execute("SELECT status,result_json,reason FROM results WHERE core_run_id=?",(run_id,)).fetchone()
                result_json=self._dump(rec["result"]) if rec.get("result") is not None else None
                if existing:
                    if (existing["status"],existing["result_json"],existing["reason"]) != (rec["status"],result_json,rec.get("reason", "")):
                        raise ValueError("TERMINAL_RESULT_CONFLICT")
                else:
                    result_id=f"result-{run_id}"
                    db.execute("INSERT INTO results VALUES(?,?,?,?,?,?)",(result_id,run_id,rec["status"],result_json,rec.get("reason", ""),now))
                    evidence=((rec.get("result") or {}).get("evidence") if isinstance(rec.get("result"),dict) else None) or rec.get("verified_progress",{}).get("evidence",[]) or []
                    for index,item in enumerate(evidence): db.execute("INSERT INTO evidence VALUES(?,?,?,?)",(uuid.uuid4().hex,result_id,index,self._dump(item if isinstance(item,dict) else {"value":item})))
            self._event(db,run_id,event,rec,now); db.commit(); return copy.deepcopy(rec)
        except Exception:
            if db.in_transaction: db.rollback()
            raise
        finally: db.close()

    def transition(self, core_run_id: str, status: CoreRunStatus, *, binding: RunBinding|None=None,
                   outcome: AdapterOutcome|None=None, reason: str="") -> dict[str,Any]:
        # Keep mutation rules byte-for-byte equivalent in semantics to RunRegistry.
        current=self.get(core_run_id)
        if current is None: raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        current_status=CoreRunStatus(current["status"])
        if status==current_status: return current
        if status not in _TRANSITIONS[current_status]: raise ValueError(f"INVALID_RUN_TRANSITION:{current_status.value}->{status.value}")
        def apply(rec: dict[str,Any]) -> None:
            if CoreRunStatus(rec["status"]) != current_status: raise ValueError("CONCURRENT_RUN_UPDATE")
            now=self._clock().astimezone(timezone.utc).isoformat(); rec["status"]=status.value; rec["updated_at"]=now
            if status==CoreRunStatus.RUNNING: rec["started_at"]=now
            if binding: rec["openclaw_binding"]={"core_run_id":binding.core_run_id,"openclaw_run_id":binding.openclaw_run_id,"agent_id":binding.agent_id,"session_key":binding.session_key,"session_id":binding.session_id}
            if status in _TERMINAL:
                rec["completed_at"]=now; rec["reason"]=reason or (outcome.reason if outcome else "")
                rec["result"]=copy.deepcopy(dict(outcome.result)) if outcome and outcome.result is not None else None
                if outcome:
                    rec["format_error"] = getattr(outcome, "format_error", "")
                    rec["format_recovery_attempts"] = getattr(outcome, "format_recovery_attempts", 0)
                    rec["format_recovery_rejection"] = getattr(outcome, "format_recovery_rejection", "")
                if status==CoreRunStatus.CANCELLED: rec["cancel_reason"]=rec["reason"]; rec["policy_status"]="CANCELLED"
        return self._mutate(core_run_id,apply,"RUN_TERMINAL" if status in _TERMINAL else "RUN_UPDATED")

    def get(self, core_run_id: str) -> dict[str,Any]|None:
        db=self._connect()
        try: return copy.deepcopy(self._loads(db.execute("SELECT record_json FROM runs WHERE core_run_id=?",(core_run_id,)).fetchone()))
        finally: db.close()
    def get_by_idempotency(self,key:str)->dict[str,Any]|None:
        db=self._connect()
        try: return copy.deepcopy(self._loads(db.execute("SELECT r.record_json FROM runs r JOIN idempotency_keys i ON i.core_run_id=r.core_run_id WHERE i.idempotency_key=?",(key,)).fetchone()))
        finally: db.close()
    def recent(self,limit:int=50)->list[dict[str,Any]]:
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=200: raise ValueError("INVALID_LIMIT")
        db=self._connect()
        try: return [json.loads(r[0]) for r in db.execute("SELECT record_json FROM runs ORDER BY rowid DESC LIMIT ?",(limit,))]
        finally: db.close()
    def _append(self, record: Mapping[str,Any]) -> None:
        run_id=str(record["core_run_id"])
        def replace(rec:dict[str,Any])->None: rec.clear(); rec.update(copy.deepcopy(dict(record)))
        self._mutate(run_id,replace,"RUN_UPDATED")

    def reconcile_abort_observation(self, core_run_id: str) -> dict[str,Any]:
        rec=self.get(core_run_id)
        if not rec: raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        if rec.get("status")!="FAIL" or rec.get("reason")!="OPENCLAW_ERROR": return rec
        # Existing JSONL registry permitted this corrective terminal rewrite; durable results do not.
        raise ValueError("TERMINAL_RESULT_IMMUTABLE")

    def get_task(self,task_id:str)->dict[str,Any]|None: return self._entity("tasks","task_id",task_id)
    def get_compilation(self,compilation_id:str)->dict[str,Any]|None: return self._entity("compilations","compilation_id",compilation_id)
    def get_run(self,run_id:str)->dict[str,Any]|None: return self.get(run_id)
    def transition_run(self, run_id: str, status: CoreRunStatus, **kwargs: Any) -> dict[str, Any]:
        return self.transition(run_id, status, **kwargs)
    def complete_run(self, run_id: str, outcome: AdapterOutcome) -> dict[str, Any]:
        return self.transition(run_id, outcome.status, outcome=outcome)
    def cancel_run(self, run_id: str, reason: str = "USER_CANCEL") -> dict[str, Any]:
        return self.transition(run_id, CoreRunStatus.CANCELLED, reason=reason)
    def _entity(self,table:str,key:str,value:str)->dict[str,Any]|None:
        db=self._connect()
        try:
            row=db.execute(f"SELECT * FROM {table} WHERE {key}=?",(value,)).fetchone(); return dict(row) if row else None
        finally: db.close()
    def map_legacy(self,namespace:str,legacy_id:str,core_run_id:str)->None:
        db=self._connect()
        try:
            db.execute("BEGIN IMMEDIATE"); db.execute("INSERT INTO legacy_id_mappings VALUES(?,?,?)",(namespace,legacy_id,core_run_id)); db.commit()
        except sqlite3.IntegrityError as exc:
            if db.in_transaction: db.rollback()
            row=db.execute("SELECT core_run_id FROM legacy_id_mappings WHERE namespace=? AND legacy_id=?",(namespace,legacy_id)).fetchone()
            if not row or row[0]!=core_run_id: raise ValueError("LEGACY_ID_CONFLICT") from exc
        finally: db.close()
    def resolve_legacy(self,namespace:str,legacy_id:str)->str|None:
        db=self._connect()
        try:
            row=db.execute("SELECT core_run_id FROM legacy_id_mappings WHERE namespace=? AND legacy_id=?",(namespace,legacy_id)).fetchone(); return row[0] if row else None
        finally: db.close()
    def claim_outbox(self,worker_id:str,*,limit:int=10,lease_seconds:int=30)->list[dict[str,Any]]:
        now=self._clock().astimezone(timezone.utc); until=(now+timedelta(seconds=lease_seconds)).isoformat(); db=self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows=db.execute("SELECT * FROM outbox WHERE (status='PENDING' AND available_at<=?) OR (status='LEASED' AND lease_until<=?) ORDER BY created_at LIMIT ?",(now.isoformat(),now.isoformat(),limit)).fetchall()
            for row in rows: db.execute("UPDATE outbox SET status='LEASED',lease_until=? WHERE event_id=?",(until,row["event_id"]))
            db.commit(); return [dict(r) for r in rows]
        finally: db.close()
    def ack_outbox(self,event_id:str)->None: self._outbox_update(event_id,"DELIVERED",None)
    def retry_outbox(self,event_id:str,*,max_attempts:int=5,backoff_seconds:int=1)->None:
        db=self._connect()
        try:
            db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT attempts FROM outbox WHERE event_id=? AND status='LEASED'",(event_id,)).fetchone()
            if not row: raise ValueError("OUTBOX_NOT_LEASED")
            attempts=row[0]+1; status="DEAD" if attempts>=max_attempts else "PENDING"; available=(self._clock()+timedelta(seconds=backoff_seconds*(2**max(0,attempts-1)))).astimezone(timezone.utc).isoformat()
            db.execute("UPDATE outbox SET status=?,attempts=?,available_at=?,lease_until=NULL WHERE event_id=?",(status,attempts,available,event_id)); db.commit()
        finally: db.close()
    def _outbox_update(self,event_id:str,status:str,lease:Any)->None:
        db=self._connect()
        try:
            cur=db.execute("UPDATE outbox SET status=?,lease_until=? WHERE event_id=? AND status='LEASED'",(status,lease,event_id))
            if cur.rowcount!=1: raise ValueError("OUTBOX_NOT_LEASED")
        finally: db.close()
