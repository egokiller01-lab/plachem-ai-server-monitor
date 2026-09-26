"""Production supervisor: JEV-check every running OpenClaw task after 5 minutes."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

from jev_recovery_live import _session_context, _source_task_active

OPENCLAW = str(Path.home() / ".openclaw/tmp/agent-cli/openclaw")
ENDPOINT = "http://127.0.0.1:8088/internal/jev-recovery-live-candidate"
GRACE_SECONDS = 300
CADENCE_SECONDS = 60
ERROR_RETRY_SECONDS = 15
EXCLUDED_AGENTS = {"fastgatewaytest"}
HANDOFF_SESSION_TAG = ":jev-handoff:"
RECOVERY_SESSION_TAG = ":jev-recovery:"
TOKEN_TELEMETRY_DB = Path(
    "/home/plachem-sever/.openclaw/workspace/local_agents/context_index_v3/03_WORK/data/context_index_v3.sqlite"
)
TOKEN_TELEMETRY_MAX_AGE_SECONDS = 15 * 60
_last: dict[str, float] = {}
_first_seen: dict[str, float] = {}


def _session_class(session_key: str) -> str:
    if HANDOFF_SESSION_TAG in session_key:
        return "handoff"
    if RECOVERY_SESSION_TAG in session_key:
        return "recovery"
    return "task"


def sessions():
    p = subprocess.run(
        [OPENCLAW, "sessions", "--all-agents", "--active", "120", "--limit", "all", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    start = p.stdout.find("{")
    return json.loads(p.stdout[start:]).get("sessions", []) if start >= 0 else []


def _abnormal_token_evidence(agent_id: str, session_id: str, now: float | None = None) -> dict | None:
    """Return only fresh ABNORMAL telemetry for the exact current session."""
    if not TOKEN_TELEMETRY_DB.is_file():
        return None
    current = time.time() if now is None else float(now)
    try:
        con = sqlite3.connect(f"file:{TOKEN_TELEMETRY_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                """SELECT * FROM token_telemetry_samples
                   WHERE lower(agent)=lower(?) AND session_id=?
                   ORDER BY sampled_at_epoch DESC,sample_id DESC LIMIT 1""",
                (agent_id, session_id),
            ).fetchone()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None
    if row is None or str(row["classification"] or "") != "ABNORMAL":
        return None
    age = max(0.0, current - float(row["sampled_at_epoch"] or 0))
    if age > TOKEN_TELEMETRY_MAX_AGE_SECONDS:
        return None
    try:
        reasons = json.loads(row["reasons_json"] or "[]")
    except (TypeError, ValueError):
        reasons = []
    return {
        "classification": "ABNORMAL",
        "sample_age_seconds": round(age, 1),
        "current_prompt_tokens": int(row["current_prompt_tokens"] or 0),
        "context_limit": int(row["context_limit"]) if row["context_limit"] is not None else None,
        "context_pressure": round(float(row["context_pressure"]), 4) if row["context_pressure"] is not None else None,
        "delta_processed": int(row["delta_processed"] or 0),
        "velocity_tokens_per_hour": round(float(row["velocity_tokens_per_hour"] or 0), 1),
        "calls_per_hour": round(float(row["calls_per_hour"] or 0), 1),
        "cache_ratio": round(float(row["delta_cache_ratio"]), 4) if row["delta_cache_ratio"] is not None else None,
        "reasons": reasons,
    }


def post(row):
    session_key = str(row["key"])
    evidence = {
        "source": "periodic_supervisor",
        "policy": "session_start+5m_then_60s",
        "session_class": _session_class(session_key),
    }
    token_evidence = _abnormal_token_evidence(
        str(row.get("agentId") or ""), str(row.get("sessionId") or "")
    )
    if token_evidence:
        evidence["token_telemetry"] = token_evidence
    body = json.dumps(
        {
            "agent_id": row["agentId"],
            "session_id": row["sessionId"],
            "session_key": session_key,
            "kind": "PERIODIC_HEALTH",
            "severity": "INFO",
            "evidence": evidence,
        }
    ).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def tick(now=None):
    now = time.time() if now is None else now
    out = []
    active_ids: set[str] = set()
    for s in sessions():
        if s.get("status") != "running" or not s.get("sessionId") or not s.get("key"):
            continue
        if str(s.get("agentId", "")).casefold() in EXCLUDED_AGENTS:
            continue

        sid = str(s["sessionId"])
        session_key = str(s["key"])
        active_ids.add(sid)

        # Handoff sessions are watchdog control-plane work, never task work.
        if _session_class(session_key) == "handoff":
            continue

        # The grace period is anchored to the session start. User/agent
        # interactions must never reset the five-minute watchdog clock.
        started_ms = s.get("sessionStartedAt")
        if started_ms:
            started = float(started_ms) / 1000.0
            _first_seen[sid] = started
        else:
            # Stable fallback when OpenClaw omits sessionStartedAt. Anchor once
            # when the supervisor first observes the session; interactions must
            # never move this anchor forward.
            started = _first_seen.setdefault(sid, now)
        age = now - started
        if age < GRACE_SECONDS:
            continue
        if now - _last.get(sid, 0) < CADENCE_SECONDS:
            continue

        try:
            ctx = _session_context(str(s.get("agentId") or ""), sid)
            active, reason = _source_task_active(ctx)
            if not active:
                out.append(
                    {
                        "session_id": sid,
                        "agent_id": s.get("agentId"),
                        "age": round(age, 1),
                        "skipped": reason,
                    }
                )
                _last[sid] = now
                continue
        except Exception:
            # Endpoint repeats the same guards. Telemetry lookup failure here
            # must not take the supervisor down.
            pass

        try:
            result = post(s)
            _last[sid] = now
            out.append(
                {
                    "session_id": sid,
                    "agent_id": s.get("agentId"),
                    "age": round(age, 1),
                    "session_class": _session_class(session_key),
                    "choice": result.get("choice"),
                    "action": result.get("action"),
                }
            )
        except Exception as exc:
            # Do not retry inside the same tick: the server may have completed
            # the request before the response path broke. Re-observe this
            # session after a short bounded delay instead of consuming the
            # whole 60-second cadence.
            retry_delay = min(ERROR_RETRY_SECONDS, CADENCE_SECONDS)
            _last[sid] = now - CADENCE_SECONDS + retry_delay
            out.append(
                {
                    "session_id": sid,
                    "agent_id": s.get("agentId"),
                    "age": round(age, 1),
                    "error": type(exc).__name__,
                    "retry_after_seconds": retry_delay,
                }
            )

    # Avoid unbounded supervisor memory from old session ids.
    for sid in list(_last):
        if sid not in active_ids:
            _last.pop(sid, None)
    for sid in list(_first_seen):
        if sid not in active_ids:
            _first_seen.pop(sid, None)
    return out


def main():
    while True:
        for row in tick():
            print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
