from __future__ import annotations

"""JEV Result Verifier PoC: parallel, advisory-only result signals."""
import copy, hashlib, json, os, sqlite3, time, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Protocol
from fastapi import APIRouter, Header, HTTPException, Request
import war_room
from jev_task_router import OpenConnectorJEVClient

router = APIRouter(prefix="/api/war-room", tags=["jev-result-verifier"])
SIGNALS = ("requirements_met", "evidence_sufficient", "contradiction_found", "qa_review_needed")
CHOICES = ("true", "false", "uncertain")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_result_verifier_advisories (
 advisory_id TEXT NOT NULL, task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, run_id TEXT,
 signal TEXT NOT NULL, state_digest TEXT NOT NULL, state_json TEXT NOT NULL,
 raw_probabilities_json TEXT, choice TEXT, latency_ms REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('AVAILABLE','ADVISORY_UNAVAILABLE')),
 error_code TEXT, stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1)),
 advisory_only INTEGER NOT NULL DEFAULT 1 CHECK(advisory_only=1), created_at INTEGER NOT NULL,
 PRIMARY KEY(advisory_id, signal));
CREATE INDEX IF NOT EXISTS idx_jev_rv_task ON jev_result_verifier_advisories(task_id, created_at DESC);
CREATE TRIGGER IF NOT EXISTS jev_rv_no_update BEFORE UPDATE ON jev_result_verifier_advisories BEGIN SELECT RAISE(ABORT, 'JEV Result Verifier advisories are append-only'); END;
CREATE TRIGGER IF NOT EXISTS jev_rv_no_delete BEFORE DELETE ON jev_result_verifier_advisories BEGIN SELECT RAISE(ABORT, 'JEV Result Verifier advisories are append-only'); END;
"""

QUESTION_DEFINITIONS = {
    "requirements_met": {
        "instructions": "Do the Worker result and artifacts semantically demonstrate that every stated completion condition and required outcome is met?",
        "criteria": {"true": "Every completion condition is supported by the result or artifacts.", "false": "At least one completion condition is unmet, unsupported, or missing."},
    },
    "evidence_sufficient": {
        "instructions": "Is the recorded evidence sufficient to independently support the Worker result claims and completion conditions?",
        "criteria": {"true": "Evidence is relevant, attributable, and sufficient.", "false": "Evidence is missing, irrelevant, unverifiable, or incomplete."},
    },
    "contradiction_found": {
        "instructions": "Does any Worker result, deterministic validator outcome, artifact, test, log, checksum, or other evidence contradict the claimed completion?",
        "criteria": {"true": "A material contradiction is present.", "false": "No material contradiction is present in the supplied state."},
    },
    "qa_review_needed": {
        "instructions": "Is independent QA review needed because the result is risky, uncertain, insufficiently evidenced, contradictory, or requires an independent quality judgment?",
        "criteria": {"true": "Independent QA review is warranted.", "false": "The supplied state does not indicate a need for independent QA review."},
    },
}

class ResultVerifierClient(Protocol):
    def evaluate(self, *, signal: str, state: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

class UnavailableResultVerifierClient:
    def evaluate(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("JEV Result Verifier runtime is unavailable")

class NoulResultVerifierClient:
    """One OpenConnector request carrying all four same-state Noul questions."""
    def __init__(self, endpoint: str | None = None, token_file: str | None = None, timeout: float = 12.0):
        self._jev = OpenConnectorJEVClient(endpoint=endpoint, token_file=token_file, timeout=timeout)

    @staticmethod
    def _question(signal: str) -> dict[str, Any]:
        return {"type": "boolean", **QUESTION_DEFINITIONS[signal]}

    def evaluate_signals(self, *, signals: tuple[str, ...], state: dict[str, Any], idempotency_key: str) -> dict[str, dict[str, Any]]:
        if not self._jev.endpoint:
            raise RuntimeError("JEV runtime endpoint is not configured")
        payload = {"connectionName": self._jev.connection, "input": {
            "model": "typesafe-ai/jev", "state": state,
            "questions": {signal: self._question(signal) for signal in signals},
        }}
        req = urllib.request.Request(
            self._jev.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._jev._token(), "Idempotency-Key": idempotency_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._jev.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("JEV evaluate request failed") from exc
        data = raw.get("data") if isinstance(raw, dict) and raw.get("success") is True else None
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise RuntimeError("JEV evaluate response is invalid")
        result: dict[str, dict[str, Any]] = {}
        for signal in signals:
            answer = answers.get(signal)
            if not isinstance(answer, dict):
                raise RuntimeError("JEV Noul answer is invalid")
            if answer.get("type") == "boolean" and isinstance(answer.get("probability"), (int, float)) and not isinstance(answer.get("probability"), bool):
                probability = float(answer["probability"])
            elif isinstance(answer.get("noul"), (int, float)):
                probability = float(answer["noul"])
            elif isinstance(answer.get("answer"), bool) and isinstance(answer.get("probabilities"), dict):
                probability = float(answer["probabilities"].get("true"))
            else:
                raise RuntimeError("JEV Noul answer is invalid")
            if probability < 0 or probability > 1:
                raise RuntimeError("JEV Noul probability is invalid")
            result[signal] = {
                "answer": probability >= 0.5,
                "probability": probability,
                "probabilities": {"true": probability, "false": 1.0 - probability},
            }
        return result

    def evaluate(self, *, signal: str, state: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.evaluate_signals(signals=(signal,), state=state, idempotency_key=idempotency_key)[signal]

_client: ResultVerifierClient = NoulResultVerifierClient() if os.getenv("JEV_OPENCONNECTOR_ENDPOINT") else UnavailableResultVerifierClient()
def set_result_verifier_client(client: ResultVerifierClient) -> None:
    global _client; _client = client
def provision_schema(path: str | None = None) -> str:
    target = path or str(war_room._db_path())
    with sqlite3.connect(target) as con: con.executescript(SCHEMA)
    return target
def _canon(value: Any) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _redact(value: Any) -> Any: return war_room._redact(value)
def _json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if isinstance(parsed, (dict,list,str,int,float,bool)) or parsed is None else fallback
    except (TypeError, ValueError): return fallback

def _state(con: sqlite3.Connection, task_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    con.row_factory = sqlite3.Row
    task = con.execute("SELECT * FROM war_tasks WHERE id=?", (task_id,)).fetchone()
    if not task: raise HTTPException(404, "Task not found")
    packet_row = con.execute("SELECT packet_json FROM war_grounding_packets WHERE task_id=?", (task_id,)).fetchone()
    packet = _json(packet_row[0], {}) if packet_row else {}; packet = packet if isinstance(packet, dict) else {}
    runs = con.execute("SELECT * FROM war_execution_runs WHERE war_task_id=? ORDER BY updated_at DESC LIMIT 10", (task_id,)).fetchall()
    typed, artifacts, validator, evidence = [], [], [], []
    for run in runs:
        typed.append({"run_id":run["core_run_id"], "agent_id":run["agent_id"], "status":run["run_status"], "result":_json(run["result_json"], None)})
        run_artifacts = _json(run["artifacts_json"], []); run_evidence = _json(run["evidence_json"], [])
        if isinstance(run_artifacts, list): artifacts.extend(run_artifacts)
        if isinstance(run_evidence, list): evidence.extend(run_evidence)
        validator.append({"run_id":run["core_run_id"], "policy_status":run["policy_status"], "validation_error":run["validation_error"], "run_status":run["run_status"]})
    for row in con.execute("SELECT evidence_type,uri,summary,sha256,run_id FROM war_evidence WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall(): evidence.append(dict(row))
    terminal = next((run for run in runs if str(run["run_status"]).lower() in {"pass", "fail", "completed", "failed", "blocked", "cancelled", "canceled", "timed_out", "stopped"} and run["result_json"]), None)
    if terminal is None:
        raise HTTPException(409, "latest Worker terminal result is required before advisory creation")
    state = {"goal":packet.get("purpose", task["scope"]), "scope":task["scope"], "completion_conditions":packet.get("completion_conditions", "unknown"), "worker":{"typed_results":typed, "artifacts":artifacts, "latest_terminal_result":{"run_id":terminal["core_run_id"], "status":terminal["run_status"], "result":_json(terminal["result_json"], None)}}, "deterministic_validator":validator, "evidence":{"test_log_checksum_link":evidence}, "revision":int(task["revision"]), "run_id":terminal["core_run_id"], "freshness":{"task_revision":int(task["revision"]), "latest_run_updated_at":terminal["updated_at"]}, "missing_evidence":[] if evidence else ["test_log_checksum_link"]}
    return task, _redact(state)

def _validate(raw: Any) -> tuple[str, dict[str, float]]:
    if isinstance(raw, dict) and isinstance(raw.get("answer"), bool):
        probabilities = raw.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != {"true", "false"}:
            raise ValueError("invalid Noul probabilities")
        probs = {key: float(probabilities[key]) for key in ("true", "false")}
        if any(value < 0 or value > 1 for value in probs.values()) or abs(sum(probs.values()) - 1) > .01:
            raise ValueError("invalid Noul probabilities")
        return "true" if raw["answer"] else "false", probs
    if not isinstance(raw, dict) or raw.get("choice") not in CHOICES or not isinstance(raw.get("probabilities"), dict): raise ValueError("invalid closed-set Result Verifier response")
    probs = {key:float(raw["probabilities"].get(key)) for key in CHOICES}
    if any(value < 0 or value > 1 for value in probs.values()) or abs(sum(probs.values())-1) > .01: raise ValueError("invalid Result Verifier probabilities")
    return str(raw["choice"]), probs

def create_result_verifier_advisory(task_id: str) -> dict[str, Any]:
    started, advisory_id = time.perf_counter(), str(uuid.uuid4())
    with sqlite3.connect(war_room._db_path()) as con:
        con.row_factory = sqlite3.Row; task, state = _state(con, task_id); digest = hashlib.sha256(_canon(state).encode()).hexdigest(); run_id = state["run_id"]
        def evaluate(signal: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
            t0 = time.perf_counter()
            try:
                choice, probabilities = _validate(raw if raw is not None else _client.evaluate(signal=signal, state=copy.deepcopy(state), idempotency_key=f"jev-result-verifier:{advisory_id}:{signal}"))
                return {"signal":signal,"choice":choice,"probabilities":probabilities,"status":"AVAILABLE","error_code":None,"latency_ms":round((time.perf_counter()-t0)*1000,3)}
            except Exception: return {"signal":signal,"choice":None,"probabilities":None,"status":"ADVISORY_UNAVAILABLE","error_code":"JEV_UNAVAILABLE","latency_ms":round((time.perf_counter()-t0)*1000,3)}
        if hasattr(_client, "evaluate_signals"):
            try:
                batch_started = time.perf_counter()
                batch = _client.evaluate_signals(signals=SIGNALS, state=copy.deepcopy(state), idempotency_key=f"jev-result-verifier:{advisory_id}")
                rows = [evaluate(signal, batch[signal]) for signal in SIGNALS]
                batch_latency = round((time.perf_counter() - batch_started) * 1000, 3)
                rows = [{**row, "latency_ms": batch_latency} for row in rows]
            except Exception:
                rows = [evaluate(signal, {"answer": None, "probabilities": {}}) for signal in SIGNALS]
        else:
            with ThreadPoolExecutor(max_workers=4) as pool: rows = [future.result() for future in as_completed([pool.submit(evaluate, signal) for signal in SIGNALS])]
        now = int(time.time())
        for row in sorted(rows, key=lambda item:item["signal"]): con.execute("INSERT INTO jev_result_verifier_advisories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (advisory_id,task_id,int(task["revision"]),run_id,row["signal"],digest,_canon(state),_canon(row["probabilities"]) if row["probabilities"] else None,row["choice"],row["latency_ms"],row["status"],row["error_code"],0,1,now))
        con.commit()
    return get_result_verifier_advisory(task_id, advisory_id)

def get_result_verifier_advisory(task_id: str, advisory_id: str | None = None) -> dict[str, Any]:
    with sqlite3.connect(war_room._db_path()) as con:
        con.row_factory = sqlite3.Row
        query = "SELECT * FROM jev_result_verifier_advisories WHERE task_id=? AND advisory_id=? ORDER BY signal" if advisory_id else "SELECT * FROM jev_result_verifier_advisories WHERE task_id=? AND advisory_id=(SELECT advisory_id FROM jev_result_verifier_advisories WHERE task_id=? ORDER BY created_at DESC LIMIT 1) ORDER BY signal"
        rows = con.execute(query, (task_id,advisory_id) if advisory_id else (task_id,task_id)).fetchall()
        if not rows: raise HTTPException(404, "No Result Verifier advisory recorded")
        _, state = _state(con, task_id); stale = any(row["task_revision"] != state["revision"] or row["state_digest"] != hashlib.sha256(_canon(state).encode()).hexdigest() for row in rows)
        return {"advisory_id":rows[0]["advisory_id"],"task_id":task_id,"task_revision":rows[0]["task_revision"],"run_id":rows[0]["run_id"],"state_digest":rows[0]["state_digest"],"signals":[{"signal":row["signal"],"answer":None if row["choice"] is None else row["choice"]=="true","probability":(_json(row["raw_probabilities_json"],{}).get("true") if row["raw_probabilities_json"] else None),"latency_ms":row["latency_ms"],"status":row["status"],"error_code":row["error_code"],"stale":bool(stale or row["stale"]),"advisory_only":True} for row in rows],"stale":bool(stale),"advisory_only":True}

def _authorize(task_id: str, request: Request, actor: str | None, token: str | None, permission: str) -> None:
    from war_room_actions import _actor, _connect_rw
    with _connect_rw() as con:
        task = con.execute("SELECT project_id FROM war_tasks WHERE id=?", (task_id,)).fetchone()
        if not task: raise HTTPException(404, "Task not found")
        _actor(con, actor, permission, task["project_id"], token, request)
@router.post("/tasks/{task_id}/jev-result-verifier", status_code=201)
def request_result_verifier(task_id: str, request: Request, x_war_room_actor: str|None=Header(default=None), x_war_room_token: str|None=Header(default=None)):
    _authorize(task_id, request, x_war_room_actor, x_war_room_token, "comment"); return create_result_verifier_advisory(task_id)
@router.get("/tasks/{task_id}/jev-result-verifier")
def read_result_verifier(task_id: str, request: Request, x_war_room_actor: str|None=Header(default=None), x_war_room_token: str|None=Header(default=None)):
    _authorize(task_id, request, x_war_room_actor, x_war_room_token, "read"); return get_result_verifier_advisory(task_id)
