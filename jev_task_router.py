from __future__ import annotations

"""JEV task-router PoC: typed advisory evidence only; never executes work."""
import hashlib, json, os, sqlite3, time, uuid
from pathlib import Path
import urllib.error, urllib.request
from typing import Any, Protocol
from fastapi import APIRouter, Header, HTTPException, Request
import war_room
from war_room_agents import load_agent_catalog

router = APIRouter(prefix="/api/war-room", tags=["jev-advisory"])
DOMAINS = ("server_operations", "office_admin", "research", "erp_implementation", "erp_management", "qa", "media", "insufficient_evidence")
DECISIONS = frozenset(("ACCEPT", "OVERRIDE", "INSUFFICIENT"))
DOMAIN_CRITERIA = {
    "server_operations": "Server, Gateway, service, GPU, network, or infrastructure operation.",
    "office_admin": "Company administration, schedules, records, coordination, or office support.",
    "research": "Evidence collection, source analysis, or research synthesis.",
    "erp_implementation": "ERP code, backend, data, integration, or implementation work.",
    "erp_management": "ERP scope, requirements, planning, or project management.",
    "qa": "Independent verification, testing, or PASS/FAIL judgment.",
    "media": "Video, audio, image, publishing, or media production.",
    "insufficient_evidence": "The task facts do not support one safe domain choice.",
}
DOMAIN_AGENT_IDS = {
    "server_operations": ("main",),
    "office_admin": ("secretary",),
    "research": ("researcher",),
    "erp_implementation": ("erpcoder", "processdata", "processsupport"),
    "erp_management": ("erpmanager",),
    "qa": ("erpqa",),
    "media": ("producer",),
    "insufficient_evidence": (),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_task_advisories (
 advisory_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES war_tasks(id), task_revision INTEGER NOT NULL,
 state_digest TEXT NOT NULL, state_json TEXT NOT NULL, domain_choice TEXT, domain_probabilities_json TEXT,
 domain_confidence REAL, agent_choice TEXT, agent_probabilities_json TEXT, agent_confidence REAL,
 latency_ms REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('AVAILABLE','ADVISORY_UNAVAILABLE')),
 error_code TEXT, error_detail TEXT, created_at INTEGER NOT NULL, stale_at INTEGER, advisory_only INTEGER NOT NULL DEFAULT 1 CHECK(advisory_only=1)
);
CREATE INDEX IF NOT EXISTS idx_jev_advisories_task ON jev_task_advisories(task_id, created_at DESC);
CREATE TABLE IF NOT EXISTS jev_task_decisions (
 decision_id TEXT PRIMARY KEY, advisory_id TEXT NOT NULL REFERENCES jev_task_advisories(advisory_id), task_id TEXT NOT NULL REFERENCES war_tasks(id),
 task_revision INTEGER NOT NULL, state_digest TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('ACCEPT','OVERRIDE','INSUFFICIENT')),
 override_agent_id TEXT, override_reason TEXT, stale INTEGER NOT NULL CHECK(stale IN (0,1)), actor_id TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jev_decisions_task ON jev_task_decisions(task_id, created_at DESC);
CREATE TABLE IF NOT EXISTS jev_decision_idempotency (
 actor_id TEXT NOT NULL, scope TEXT NOT NULL, idempotency_key_hash TEXT NOT NULL,
 request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
 PRIMARY KEY(actor_id, scope, idempotency_key_hash)
);
CREATE TRIGGER IF NOT EXISTS jev_advisories_no_update BEFORE UPDATE ON jev_task_advisories BEGIN SELECT RAISE(ABORT, 'JEV advisories are append-only'); END;
CREATE TRIGGER IF NOT EXISTS jev_advisories_no_delete BEFORE DELETE ON jev_task_advisories BEGIN SELECT RAISE(ABORT, 'JEV advisories are append-only'); END;
CREATE TRIGGER IF NOT EXISTS jev_decisions_no_update BEFORE UPDATE ON jev_task_decisions BEGIN SELECT RAISE(ABORT, 'JEV decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS jev_decisions_no_delete BEFORE DELETE ON jev_task_decisions BEGIN SELECT RAISE(ABORT, 'JEV decisions are append-only'); END;
"""

class JEVClient(Protocol):
    def choice(self, *, question: str, state: dict[str, Any], criteria: dict[str, str], idempotency_key: str) -> dict[str, Any]: ...

class UnavailableJEVClient:
    def choice(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("JEV/OpenConnector runtime adapter is unavailable")

class OpenConnectorJEVClient:
    """Fail-closed client for the one allowed OpenConnector runtime action."""
    def __init__(self, endpoint: str | None = None, token_file: str | None = None, timeout: float = 12.0):
        self.endpoint = endpoint or os.getenv("JEV_OPENCONNECTOR_ENDPOINT")
        self.token_file = token_file or os.getenv("JEV_OPENCONNECTOR_TOKEN_FILE")
        self.timeout = timeout
        self.connection = os.getenv("JEV_OPENCONNECTOR_CONNECTION", "default")

    def _token(self) -> str:
        if not self.token_file:
            raise RuntimeError("JEV runtime credential is not configured")
        try:
            token = Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("JEV runtime credential is unavailable") from exc
        if not token:
            raise RuntimeError("JEV runtime credential is empty")
        return token

    def choice(self, *, question: str, state: dict[str, Any], criteria: dict[str, str], idempotency_key: str) -> dict[str, Any]:
        if not self.endpoint:
            raise RuntimeError("JEV runtime endpoint is not configured")
        payload = {"connectionName": self.connection, "input": {"model": "typesafe-ai/jev", "state": state, "questions": {"result": {"type": "choice", "instructions": question, "criteria": criteria}}}}
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST", headers={"Content-Type":"application/json", "Authorization":"Bearer " + self._token(), "Idempotency-Key": idempotency_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RuntimeError("JEV evaluate request failed") from exc
        data = raw.get("data") if isinstance(raw, dict) and raw.get("success") is True else None
        answers = data.get("answers") if isinstance(data, dict) else None
        value = answers.get("result") if isinstance(answers, dict) else None
        if not isinstance(value, dict): raise RuntimeError("JEV evaluate response is invalid")
        probabilities = value.get("probabilities")
        return {"choice": value.get("choice"), "probabilities": probabilities, "confidence": max(probabilities.values()) if isinstance(probabilities, dict) and probabilities else None}

    def watchdog(self, *, state: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        """Send the single typed v0.1 watchdog batch over the existing path."""
        if not self.endpoint:
            raise RuntimeError("JEV runtime endpoint is not configured")
        boolean_names = (
            "meaningful_progress", "productive_activity", "loop_suspected", "stalled",
            "continue_current_session", "restart_recommended",
        )
        instructions = {
            "meaningful_progress": "Is there verified progress toward the stated completion conditions?",
            "productive_activity": "Is the current activity productive and evidenced by the observed facts?",
            "loop_suspected": "Do the observed facts show repeated or looping work with no new result?",
            "stalled": "Is the run stalled or waiting without observable progress?",
            "continue_current_session": "Should the current session continue without recovery?",
            "restart_recommended": "Is a fresh child session recommended based on the observed facts?",
        }
        questions = {name: {"type": "boolean", "instructions": instructions[name]} for name in boolean_names}
        questions["result"] = {
            "type": "choice",
            "instructions": "Choose one watchdog follow-up action.",
            "criteria": {name: name for name in ("CONTINUE", "RECHECK", "RESTART_SESSION", "ESCALATE_MAIN")},
        }
        payload = {"connectionName": self.connection, "input": {"model": "typesafe-ai/jev", "state": state, "questions": questions}}
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST", headers={"Content-Type":"application/json", "Authorization":"Bearer " + self._token(), "Idempotency-Key": idempotency_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RuntimeError("JEV evaluate request failed") from exc
        data = raw.get("data") if isinstance(raw, dict) and raw.get("success") is True else None
        answers = data.get("answers") if isinstance(data, dict) else None
        result = answers.get("result") if isinstance(answers, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError("JEV watchdog response is invalid")
        probs = result.get("probabilities") if isinstance(result.get("probabilities"), dict) else {}
        parsed = parse_boolean_answers(answers, boolean_names)
        return {"boolean_answers": {name: parsed[name] >= 0.5 for name in boolean_names},
                "boolean_probabilities": parsed,
                "choice": result.get("choice"), "probabilities": probs,
                "confidence": max(probs.values()) if probs else None}

def parse_boolean_answers(answers: Any, names: tuple[str, ...]) -> dict[str, float]:
    if not isinstance(answers, dict):
        raise RuntimeError("JEV boolean answers are invalid")
    parsed = {}
    for name in names:
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != "boolean":
            raise RuntimeError("JEV boolean answer is invalid")
        probability = answer.get("probability")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0 <= float(probability) <= 1:
            raise RuntimeError("JEV boolean probability is invalid")
        parsed[name] = float(probability)
    return parsed

_jev_client: JEVClient = OpenConnectorJEVClient() if os.getenv("JEV_OPENCONNECTOR_ENDPOINT") else UnavailableJEVClient()
def set_jev_client(client: JEVClient) -> None:
    global _jev_client
    _jev_client = client
def provision_schema(path: str | None = None) -> str:
    target = path or str(war_room._db_path())
    with sqlite3.connect(target) as con: con.executescript(SCHEMA)
    return target
def _canon(value: Any) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def state_digest(state: dict[str, Any]) -> str: return hashlib.sha256(_canon(state).encode()).hexdigest()
def _clean(value: Any) -> Any: return war_room._redact(value)

def _task_state(con: sqlite3.Connection, task_id: str):
    task = con.execute("SELECT t.*,m.original_body AS instruction_body FROM war_tasks t LEFT JOIN war_messages m ON m.id=t.source_message_id WHERE t.id=?", (task_id,)).fetchone()
    if not task: raise HTTPException(404, "Task not found")
    packet = {}
    row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
    if row:
        try: packet = json.loads(row[0]) if isinstance(json.loads(row[0]), dict) else {}
        except (TypeError, ValueError): packet = {}
    agents = [r[0] for r in con.execute("SELECT agent_id FROM war_task_agents WHERE task_id=? ORDER BY agent_id", (task_id,))]
    state = _clean({"task":{"task_id":task_id,"purpose":packet.get("purpose",task["scope"]),"instructions":task["instruction_body"] or packet.get("instructions",task["scope"]),"expected_result":packet.get("expected_result","unknown"),"completion_conditions":packet.get("completion_conditions","unknown"),"risk":packet.get("risk","unknown"),"revision":int(task["revision"])},"observed_state":{"lifecycle":task["status"],"document_version":task["document_version"]},"policy_facts":{"production":packet.get("production","unknown"),"destructive":packet.get("destructive","unknown")},"official_candidates":[],"assigned_agents":agents,"freshness":{"task_revision":int(task["revision"])}})
    return task, state

def registry_candidates(domain: str | None = None) -> list[dict[str, Any]]:
    allowed_ids = None
    if domain and domain != "insufficient_evidence":
        allowed_ids = {value.casefold() for value in DOMAIN_AGENT_IDS.get(domain, ())}
    out=[]
    for item in load_agent_catalog().values():
        eligible = bool(getattr(item, "execution_eligible", False))
        if not hasattr(item, "execution_eligible"):
            eligible = bool(item.registered and item.enabled and item.gateway_allowed)
        if not eligible:
            continue
        if allowed_ids is not None and item.agent_id.casefold() not in allowed_ids:
            continue
        out.append({"agent_id":item.agent_id,"capabilities":list(item.capabilities),"registered":item.registered,"enabled":item.enabled,"gateway_allowed":item.gateway_allowed,"execution_eligible":eligible})
    return sorted(out,key=lambda x:x["agent_id"].casefold())

def _choice(raw: Any, allowed: list[str]):
    if not isinstance(raw,dict) or raw.get("choice") not in allowed or not isinstance(raw.get("probabilities"),dict): raise ValueError("invalid closed-set Choice response")
    probs={str(k):float(v) for k,v in raw["probabilities"].items() if str(k) in allowed}
    if set(probs) != set(allowed) or any(v < 0 or v > 1 for v in probs.values()): raise ValueError("invalid Choice probabilities")
    confidence = max(probs.values()) if raw.get("confidence") is None else float(raw["confidence"])
    if confidence < 0 or confidence > 1: raise ValueError("invalid Choice confidence")
    return str(raw["choice"]),probs,confidence
def _call(question: str, state: dict[str,Any], criteria: dict[str,str], idempotency_key: str):
    return _choice(_jev_client.choice(question=question,state=state,criteria=criteria,idempotency_key=idempotency_key),list(criteria))

def create_advisory(task_id: str) -> dict[str, Any]:
    started=time.perf_counter()
    advisory_id=str(uuid.uuid4())
    with sqlite3.connect(war_room._db_path()) as con:
        con.row_factory=sqlite3.Row
        task,state=_task_state(con,task_id)
        domain=dp=agent=ap=dc=ac=None; status="AVAILABLE"; code=detail=None
        try:
            domain,dp,dc=_call("Select one task domain; use insufficient_evidence when facts are inadequate.",state,DOMAIN_CRITERIA,f"jev-router:{advisory_id}:domain")
            candidates=registry_candidates(domain); state["official_candidates"]=candidates
            if not candidates:
                agent,ap,ac="insufficient_evidence",{"insufficient_evidence":1.0},1.0
            else:
                agent_criteria={x["agent_id"]:"Execution-eligible PLACHEM Agent with capabilities: "+(", ".join(x["capabilities"]) or "registered role mapping") for x in candidates}
                agent_criteria["insufficient_evidence"]="No listed Agent is supported by the available task facts."
                agent,ap,ac=_call("Select one listed execution-eligible Agent; never invent an ID.",state,agent_criteria,f"jev-router:{advisory_id}:agent")
        except Exception: status,code,detail="ADVISORY_UNAVAILABLE","JEV_ERROR","JEV advisory could not be produced; use manual assignment."
        digest=state_digest(state)
        con.execute("INSERT INTO jev_task_advisories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(advisory_id,task_id,int(task["revision"]),digest,_canon(state),domain,_canon(dp) if dp is not None else None,dc,agent,_canon(ap) if ap is not None else None,ac,round((time.perf_counter()-started)*1000,3),status,code,detail,int(time.time()),None,1)); con.commit()
        return _advisory_response(con,advisory_id)

def _stale(con,row):
    task,state=_task_state(con,row["task_id"]); state["official_candidates"]=registry_candidates(row["domain_choice"] or "insufficient_evidence"); return int(task["revision"]) != int(row["task_revision"]) or state_digest(state) != row["state_digest"]
def _advisory_response(con, advisory_id):
    row=con.execute("SELECT * FROM jev_task_advisories WHERE advisory_id=?",(advisory_id,)).fetchone()
    if not row: raise HTTPException(404,"Advisory not found")
    latest=con.execute("SELECT decision_id,decision,stale,actor_id,created_at,override_agent_id,override_reason FROM jev_task_decisions WHERE advisory_id=? ORDER BY created_at DESC LIMIT 1",(advisory_id,)).fetchone()
    return _clean({"advisory_id":row["advisory_id"],"task_id":row["task_id"],"task_revision":row["task_revision"],"state_digest":row["state_digest"],"domain_choice":row["domain_choice"],"domain_probabilities":json.loads(row["domain_probabilities_json"]) if row["domain_probabilities_json"] else None,"domain_confidence":row["domain_confidence"],"agent_choice":row["agent_choice"],"agent_probabilities":json.loads(row["agent_probabilities_json"]) if row["agent_probabilities_json"] else None,"agent_confidence":row["agent_confidence"],"latency_ms":row["latency_ms"],"status":row["status"],"error":{"code":row["error_code"],"detail":row["error_detail"]} if row["error_code"] else None,"stale":_stale(con,row),"latest_decision":dict(latest) if latest else None,"advisory_only":True,"created_at":row["created_at"]})

@router.post("/tasks/{task_id}/jev-advisory",status_code=201)
def request_advisory(task_id: str,request: Request,x_war_room_actor: str|None=Header(default=None),x_war_room_token: str|None=Header(default=None)):
    from war_room_actions import _actor,_connect_rw
    with _connect_rw() as con:
        task=con.execute("SELECT project_id FROM war_tasks WHERE id=?",(task_id,)).fetchone()
        if not task: raise HTTPException(404,"Task not found")
        _actor(con,x_war_room_actor,"comment",task["project_id"],x_war_room_token,request)
    return create_advisory(task_id)

@router.get("/tasks/{task_id}/jev-advisory")
def get_advisory(task_id: str,request: Request,x_war_room_actor: str|None=Header(default=None),x_war_room_token: str|None=Header(default=None)):
    from war_room_actions import _actor,_connect_rw
    with _connect_rw() as con:
        task=con.execute("SELECT project_id FROM war_tasks WHERE id=?",(task_id,)).fetchone()
        if not task: raise HTTPException(404,"Task not found")
        _actor(con,x_war_room_actor,"read",task["project_id"],x_war_room_token,request); row=con.execute("SELECT advisory_id FROM jev_task_advisories WHERE task_id=? ORDER BY created_at DESC LIMIT 1",(task_id,)).fetchone()
        if not row: raise HTTPException(404,"No advisory recorded")
        return _advisory_response(con,row[0])

@router.post("/tasks/{task_id}/jev-advisory/{advisory_id}/decision",status_code=201)
async def decide_advisory(task_id: str,advisory_id: str,request: Request,x_war_room_actor: str|None=Header(default=None),x_war_room_token: str|None=Header(default=None),idempotency_key: str|None=Header(default=None,alias="Idempotency-Key")):
    from war_room_actions import _actor,_connect_rw,_audit
    if not idempotency_key or len(idempotency_key.encode("utf-8")) > 255: raise HTTPException(400,"A valid Idempotency-Key is required")
    body=await request.json(); decision=body.get("decision") if isinstance(body,dict) else None; idem=request.headers.get("Idempotency-Key")
    if decision not in DECISIONS: raise HTTPException(422,"decision must be ACCEPT, OVERRIDE, or INSUFFICIENT")
    with _connect_rw() as con:
        task=con.execute("SELECT * FROM war_tasks WHERE id=?",(task_id,)).fetchone()
        if not task: raise HTTPException(404,"Task not found")
        actor=_actor(con,x_war_room_actor,"approve",task["project_id"],x_war_room_token,request); row=con.execute("SELECT * FROM jev_task_advisories WHERE advisory_id=? AND task_id=?",(advisory_id,task_id)).fetchone()
        if not row: raise HTTPException(404,"Advisory not found")
        if decision=="ACCEPT" and row["status"]!="AVAILABLE": raise HTTPException(409,"unavailable advisory cannot be accepted")
        override,reason=body.get("override_agent_id"),body.get("override_reason")
        if decision=="OVERRIDE":
            if override not in {x["agent_id"] for x in registry_candidates()}: raise HTTPException(422,"override target must be registered and execution-eligible")
            if not isinstance(reason,str) or not reason.strip(): raise HTTPException(422,"override_reason is required")
        if decision=="ACCEPT" and _stale(con,row): raise HTTPException(409,"stale ACCEPT rejected; request a new advisory")
        if decision=="ACCEPT" and row["agent_choice"] not in {x["agent_id"] for x in registry_candidates(row["domain_choice"])}: raise HTTPException(409,"recommended agent is no longer execution-eligible")
        stale=int(_stale(con,row)); scope=f"POST:/tasks/{task_id}/jev-advisory/{advisory_id}/decision"; req_hash=hashlib.sha256(_canon(body).encode()).hexdigest(); key_hash=hashlib.sha256(idempotency_key.encode()).hexdigest()
        old=con.execute("SELECT request_hash,response_json FROM jev_decision_idempotency WHERE actor_id=? AND scope=? AND idempotency_key_hash=?",(actor,scope,key_hash)).fetchone()
        if old:
            if old[0]!=req_hash: raise HTTPException(409,"Idempotency-Key reuse with different request")
            return json.loads(old[1])
        decision_id=str(uuid.uuid4())
        con.execute("INSERT INTO jev_task_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",(decision_id,advisory_id,task_id,int(task["revision"]),row["state_digest"],decision,override if decision=="OVERRIDE" else None,reason.strip() if isinstance(reason,str) and decision=="OVERRIDE" else None,stale,actor,int(time.time())))
        response={"decision_id":decision_id,"advisory_id":advisory_id,"task_id":task_id,"decision":decision,"stale":bool(stale),"advisory_only":True}
        _audit(con,task["project_id"],actor,"jev_advisory_decision","jev_advisory",advisory_id,{"decision":decision,"stale":stale,"override_agent_id":override if decision=="OVERRIDE" else None},str(uuid.uuid4()))
        con.execute("INSERT INTO jev_decision_idempotency VALUES (?,?,?,?,?)",(actor,scope,key_hash,req_hash,_canon(response)))
        con.commit()
    return response
