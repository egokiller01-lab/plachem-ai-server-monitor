from __future__ import annotations

"""Live JEV recovery advisory and guarded recovery for OpenClaw agent sessions."""

import json
import os
import re
import select
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request

from jev_agent_recovery_watchdog_v01 import (
    AgentActivity,
    Availability,
    ExistingOpenConnectorHealthJudge,
    FeatureBuilder,
    HandoffWriter,
    ObserverSnapshot,
    snapshot_state_digest,
)
from jev_recovery_shadow import _read_session_trace

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent
LIVE_HISTORY = BASE_DIR / "runtime" / "jev-recovery-live.jsonl"
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
OPENCLAW_BIN = Path.home() / ".npm-global-node24" / "bin" / "openclaw"
NODE24_BIN = Path.home() / ".local" / "node-v24.21.0" / "bin"
_ALLOWED_KINDS = {"STALL", "LOOP", "COMPACTION_STREAK", "CONTEXT_EVIDENCE", "PERIODIC_HEALTH"}
_TERMINAL = {"done", "failed", "killed", "timeout", "cancelled", "canceled"}
_INFLIGHT: set[str] = set()
_LOCK = threading.Lock()
HANDOFF_PHASE_TIMEOUT_SECONDS = 240
ACK_PHASE_TIMEOUT_SECONDS = 180
RESUME_PHASE_TIMEOUT_SECONDS = 600
AUTO_RECOVERY_MIN_CONFIDENCE = 0.70
AUTO_RECOVERY_MIN_MARGIN = 0.20
AUTO_RECOVERY_CONFIRMATIONS = 2
AUTO_RECOVERY_CONFIRMATION_GAP_SECONDS = 45.0
GENERAL_RECOVERY_GRACE_SECONDS = 300.0
HANDOFF_SESSION_TAG = ":jev-handoff:"
RECOVERY_SESSION_TAG = ":jev-recovery:"
_SALVAGE_CONFIRMATIONS: dict[str, tuple[int, float, str]] = {}
_CONTROL_PROMPT_PREFIXES = (
    "Agent-to-agent announce step.",
    "JEV RECOVERY MODE.",
)


class WatchdogAbortBridge:
    """Dedicated Gateway bridge for one guarded operation: sessions.abort."""

    def __init__(self) -> None:
        node = os.environ.get(
            "PLACHEM_WAR_ROOM_NODE_BIN",
            str(NODE24_BIN / "node"),
        )
        script = BASE_DIR / "jev_recovery_abort_bridge.mjs"
        self._process = subprocess.Popen(
            [node, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._counter = 0
        hello = self._read_line(15)
        if not hello.get("ready") or not hello.get("connectionId"):
            raise RuntimeError("watchdog_abort_bridge_not_ready")

    def _read_line(self, timeout_seconds: int) -> dict[str, Any]:
        if not self._process.stdout:
            raise RuntimeError("watchdog_abort_bridge_stdout_missing")
        ready, _, _ = select.select(
            [self._process.stdout], [], [], timeout_seconds
        )
        if not ready:
            raise TimeoutError("watchdog_abort_bridge_timeout")
        line = self._process.stdout.readline()
        if not line:
            raise RuntimeError("watchdog_abort_bridge_closed")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("watchdog_abort_bridge_invalid_response")
        return value

    def request_abort(
        self, *, agent_id: str, session_key: str, run_id: str
    ) -> dict[str, Any]:
        self._counter += 1
        request_id = f"jev-abort-{self._counter}"
        if not self._process.stdin:
            raise RuntimeError("watchdog_abort_bridge_stdin_missing")
        self._process.stdin.write(
            json.dumps(
                {
                    "id": request_id,
                    "method": "sessions.abort",
                    "params": {
                        "key": session_key,
                        "agentId": agent_id,
                        "runId": run_id,
                    },
                    "timeoutMs": 10000,
                }
            )
            + "\n"
        )
        self._process.stdin.flush()
        response = self._read_line(15)
        if (
            response.get("id") != request_id
            or response.get("ok") is not True
            or not isinstance(response.get("result"), dict)
        ):
            raise RuntimeError(
                str(response.get("error") or "watchdog_abort_bridge_rejected")
            )
        return response["result"]

    def close(self) -> None:
        if self._process.stdin:
            self._process.stdin.close()

_RISK_PATTERNS = (
    r"\bproduction\b", r"\bprod\b", r"\bdeploy\b", r"\bgit\s+push\b",
    r"\bmerge\b", r"\brm\s+-", r"\bdrop\s+table\b", r"\btruncate\b",
    r"\bsystemctl\s+restart\b", r"\bservice\s+restart\b",
    r"\bdelete\b", r"\bexternal\s+send\b",
    r"운영\s*(서버|DB|디비|환경|배포)", r"배포", r"삭제", r"재시작", r"외부\s*발송",
    r"권한\s*(변경|수정)", r"DB\s*(write|update|delete|insert)",
)


def _append(record: Mapping[str, Any]) -> None:
    LIVE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with LIVE_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _agent_db(agent_id: str) -> Path:
    return Path.home() / ".openclaw" / "agents" / agent_id.lower() / "agent" / "openclaw-agent.sqlite"


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")).strip()
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        ).strip()
    return ""


def _latest_run_state(con: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Return authoritative state for the newest runtime run in a session.

    session_windows.status can lag behind a finished run.  Recovery therefore
    checks trajectory_runtime_events before treating a session as active work.
    Control-only turns (announce/recovery housekeeping) are not task work.
    """
    rows = con.execute(
        """SELECT seq,run_id,event_json
           FROM trajectory_runtime_events
           WHERE session_id=? AND run_id IS NOT NULL
           ORDER BY seq DESC LIMIT 120""",
        (session_id,),
    ).fetchall()
    if not rows:
        return {
            "available": False,
            "run_id": None,
            "terminal": False,
            "active": False,
            "control_only": False,
            "prompt": "",
            "status": None,
        }
    latest_run_id = str(rows[0]["run_id"])
    events: list[dict[str, Any]] = []
    for row in rows:
        if str(row["run_id"]) != latest_run_id:
            continue
        try:
            events.append(json.loads(row["event_json"]))
        except Exception:
            continue
    prompt = ""
    terminal = False
    terminal_status = None
    for event in reversed(events):
        etype = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        if etype == "prompt.submitted" and not prompt:
            prompt = str(data.get("prompt") or "").strip()
        if etype == "session.ended":
            terminal = True
            terminal_status = str(data.get("status") or "").lower() or None
            break
    control_only = any(prompt.startswith(prefix) for prefix in _CONTROL_PROMPT_PREFIXES)
    return {
        "available": True,
        "run_id": latest_run_id,
        "terminal": terminal,
        "active": not terminal,
        "control_only": control_only,
        "prompt": prompt[:400],
        "status": terminal_status,
    }


def _session_role(session_key: str) -> str:
    if HANDOFF_SESSION_TAG in session_key:
        return "handoff"
    if RECOVERY_SESSION_TAG in session_key:
        return "recovery"
    return "task"


def _source_task_active(ctx: Mapping[str, Any]) -> tuple[bool, str]:
    session_role = _session_role(str(ctx.get("session_key") or ""))
    if session_role == "handoff":
        return False, "control_handoff_session"
    status = str(ctx.get("status") or "").lower()
    if status in _TERMINAL:
        return False, f"session_terminal:{status}"
    run = ctx.get("latest_run")
    if not isinstance(run, Mapping) or not run.get("available"):
        # Preserve existing behavior when trajectory evidence is unavailable.
        return True, "runtime_run_unavailable"
    if run.get("control_only"):
        return False, "control_only_run"
    if run.get("terminal"):
        return False, f"latest_run_terminal:{run.get('status') or 'unknown'}"
    if not run.get("active"):
        return False, "no_active_runtime_run"
    return True, "active_task_run"


def _session_age_seconds(ctx: Mapping[str, Any], *, now: float | None = None) -> float | None:
    raw = ctx.get("started_at")
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None
    started = float(raw)
    if started > 10_000_000_000:
        started /= 1000.0
    current = time.time() if now is None else float(now)
    return max(0.0, current - started)


def _auto_recovery_execution_enabled() -> bool:
    return str(os.getenv("PLACHEM_JEV_AUTO_RECOVERY_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _runtime_recovery_safe(
    ctx: Mapping[str, Any],
    snapshot: ObserverSnapshot | None = None,
) -> bool:
    run = ctx.get("latest_run")
    if isinstance(run, Mapping) and run.get("available"):
        return bool(
            run.get("active")
            and not run.get("terminal")
            and not run.get("control_only")
        )

    # Some OpenClaw local-model turns do not publish trajectory_runtime_events
    # while running. In that case use the independently collected transcript
    # trace plus the concrete run_id as the fallback lifecycle evidence.
    if not ctx.get("run_id") or snapshot is None:
        return False
    active = snapshot.activity.active_process
    if not active.available or not isinstance(active.value, Mapping):
        return False
    return bool(
        active.value.get("session_nonterminal")
        and str(ctx.get("status") or "").lower() not in _TERMINAL
    )


def _reset_salvage_confirmation(session_id: str) -> None:
    with _LOCK:
        _SALVAGE_CONFIRMATIONS.pop(session_id, None)


def _confirm_salvage(
    session_id: str,
    *,
    stable_digest: str,
    now: float | None = None,
) -> int:
    """Require two spaced SALVAGE decisions on the same stable task state."""
    current = time.time() if now is None else float(now)
    digest = str(stable_digest or "")
    with _LOCK:
        previous = _SALVAGE_CONFIRMATIONS.get(session_id)
        if previous is None:
            count = 1
            last_at = current
        else:
            count, last_at, previous_digest = previous
            if previous_digest != digest:
                count = 1
                last_at = current
            elif current - last_at >= AUTO_RECOVERY_CONFIRMATION_GAP_SECONDS:
                count += 1
                last_at = current
        _SALVAGE_CONFIRMATIONS[session_id] = (count, last_at, digest)
        return count


def _recovery_attempt_exists(session_id: str) -> bool:
    if not LIVE_HISTORY.exists():
        return False
    try:
        lines = LIVE_HISTORY.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return False
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if row.get("source_session_id") != session_id:
            continue
        if row.get("type") == "live_recovery_phase" and row.get("phase") in {
            "A_HANDOFF", "B_TERMINATE", "C_RESTART", "D_ACK", "E_COMPLETE"
        }:
            return True
        if row.get("type") == "live_recovery_outcome":
            return True
    return False


def _salvage_is_confident(decision: Any) -> tuple[bool, str, float | None]:
    try:
        confidence = float(decision.confidence)
    except (TypeError, ValueError):
        confidence = None
    probs = dict(decision.probabilities) if isinstance(decision.probabilities, Mapping) else {}
    try:
        salvage = float(probs.get("SALVAGE"))
    except (TypeError, ValueError):
        salvage = None
    competitors = []
    for key in ("WATCH", "CONTINUE", "DEAD"):
        try:
            competitors.append(float(probs.get(key)))
        except (TypeError, ValueError):
            pass
    margin = (salvage - max(competitors)) if salvage is not None and competitors else None
    if confidence is None or confidence < AUTO_RECOVERY_MIN_CONFIDENCE:
        return False, "low_confidence", margin
    if margin is not None and margin < AUTO_RECOVERY_MIN_MARGIN:
        return False, "low_margin", margin
    return True, "confident", margin


def _session_context(agent_id: str, session_id: str) -> dict[str, Any]:
    db = _agent_db(agent_id)
    if not db.exists():
        raise RuntimeError("AGENT_DB_UNAVAILABLE")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        window = con.execute(
            """SELECT session_id,session_key,status,started_at,ended_at,
                      transcript_updated_at,model,chat_type,channel
               FROM session_windows WHERE session_id=? LIMIT 1""",
            (session_id,),
        ).fetchone()
        if not window:
            raise RuntimeError("SESSION_WINDOW_NOT_FOUND")
        rows = con.execute(
            "SELECT event_json FROM transcript_events WHERE session_id=? ORDER BY seq DESC LIMIT 80",
            (session_id,),
        ).fetchall()
        run_row = con.execute(
            "SELECT run_id FROM trajectory_runtime_events WHERE session_id=? AND run_id IS NOT NULL ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        latest_run = _latest_run_state(con, session_id)
    finally:
        con.close()
    latest_user = ""
    command_texts: list[str] = []
    assistant_texts: list[str] = []
    transcript_run_id = None
    for row in rows:
        try:
            event = json.loads(row["event_json"])
        except Exception:
            continue
        if event.get("type") != "message" or not isinstance(event.get("message"), dict):
            continue
        msg = event["message"]
        if transcript_run_id is None:
            meta = msg.get("__openclaw") if isinstance(msg.get("__openclaw"), dict) else {}
            if meta.get("runId"):
                transcript_run_id = str(meta["runId"])
        if msg.get("role") == "user" and not latest_user:
            latest_user = _message_text(msg)
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            text = _message_text(msg)
            if text:
                assistant_texts.append(text[:1800])
            for part in msg["content"]:
                if not isinstance(part, dict) or part.get("type") != "toolCall":
                    continue
                args = part.get("arguments") if isinstance(part.get("arguments"), dict) else {}
                for key in ("command", "path", "workdir"):
                    if args.get(key):
                        command_texts.append(str(args[key])[:500])
    return {
        **dict(window),
        "latest_user": latest_user[:1200],
        "command_texts": command_texts[-12:],
        "recent_assistant_texts": assistant_texts[:4],
        "latest_run": latest_run,
        "run_id": (run_row["run_id"] if run_row else None) or transcript_run_id,
    }
def _workspace_for(agent_id: str) -> Path:
    data = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    entries = data.get("agents", {}).get("entries", {})
    entry = entries.get(agent_id) or entries.get(agent_id.lower()) or {}
    workspace = entry.get("workspace") if isinstance(entry, dict) else None
    if not workspace:
        return Path.home() / ".openclaw" / "agents" / agent_id
    return Path(str(workspace)).expanduser()


def _risk_scope(ctx: Mapping[str, Any]) -> dict[str, Any]:
    session_key = str(ctx.get("session_key") or "")
    channel = str(ctx.get("channel") or "").lower()
    text = "\n".join([
        str(ctx.get("latest_user") or ""),
        *[str(x) for x in ctx.get("command_texts", [])],
    ])
    risk_text = re.sub(r"\bnon[- ]?production\b", "SAFE_BOUNDARY", text, flags=re.I)
    risk_text = re.sub(r"비\s*production", "SAFE_BOUNDARY", risk_text, flags=re.I)
    # Explicit prohibitions are safety boundaries, not requested side effects.
    # Mask only the negated phrase itself; any later affirmative risky command
    # remains visible to the normal risk patterns below.
    negated_patterns = (
        r"\bno\s+production\b",
        r"\bno\s+deployment\b",
        r"\bdo\s+not\s+(?:deploy|push|merge|delete|restart|send)\b",
        r"\b(?:production|deployment|deploy|merge|delete)\s+(?:forbidden|prohibited|not\s+allowed)\b",
        r"(?:운영|배포|삭제|재시작|외부\s*발송|권한\s*변경)\s*(?:금지|하지\s*마|없음|안\s*함)",
    )
    for pattern in negated_patterns:
        risk_text = re.sub(pattern, "SAFE_BOUNDARY", risk_text, flags=re.I)
    matches = [pattern for pattern in _RISK_PATTERNS if re.search(pattern, risk_text, re.I)]
    protected_key = any(
        tag in session_key
        for tag in (":telegram:", ":cron:", ":fast-gateway", HANDOFF_SESSION_TAG, RECOVERY_SESSION_TAG)
    )
    protected = protected_key or channel in {"telegram", "whatsapp", "slack", "email"} or bool(matches)
    return {
        "production": bool(matches),
        "destructive": any(re.search(p, text, re.I) for p in (r"delete", r"삭제", r"drop", r"truncate", r"rm\s+-")),
        "external_send": channel in {"telegram", "whatsapp", "slack", "email"} or bool(re.search(r"외부\s*발송|external\s+send", text, re.I)),
        "unknown": False,
        "protected_session_key": protected_key,
        "risk_matches": matches[:8],
        "auto_recovery_allowed": not protected and str(ctx.get("status") or "").lower() not in _TERMINAL,
    }


def _make_snapshot(*, agent_id: str, session_id: str, detection: Mapping[str, Any]) -> tuple[dict[str, Any], AgentActivity, ObserverSnapshot]:
    ctx = _session_context(agent_id, session_id)
    trace = _read_session_trace(agent_id=agent_id, session_id=session_id)
    if not trace.get("available"):
        raise RuntimeError("SESSION_TRACE_UNAVAILABLE")
    scope = _risk_scope(ctx)
    now_ms = int(time.time() * 1000)
    updated = ctx.get("transcript_updated_at") or ctx.get("started_at")
    age = round((now_ms - int(updated)) / 1000, 1) if isinstance(updated, (int, float)) else None
    progress = {
        "session_status": ctx.get("status"),
        "latest_run": ctx.get("latest_run"),
        "last_activity_age_sec": age,
        "tool_call_count": trace.get("tool_call_count"),
        "tool_result_count": trace.get("tool_result_count"),
        "inflight_tool_calls": trace.get("inflight_tool_calls"),
        "successful_tool_results": trace.get("successful_tool_results"),
        "tool_error_count": trace.get("tool_error_count"),
        "exec_nonzero_count": trace.get("exec_nonzero_count"),
        "meaningful_result_count": trace.get("meaningful_result_count"),
        "meaningful_progress_age_sec": trace.get("meaningful_progress_age_sec"),
        "latest_total_tokens": trace.get("latest_total_tokens"),
        "detection_kind": detection.get("kind"),
        "detection_severity": detection.get("severity"),
        "detection_evidence": detection.get("evidence") or {},
    }
    repetition = {
        "repeated_tool_calls": trace.get("repeated_tool_calls"),
        "repeated_results": trace.get("repeated_results"),
        "unique_tool_count": trace.get("unique_tool_count"),
        "dominant_tool_ratio": trace.get("dominant_tool_ratio"),
        "exec_nonzero_count": trace.get("exec_nonzero_count"),
        "meaningful_result_count": trace.get("meaningful_result_count"),
        "detector_kind": detection.get("kind"),
        "count": max(int(trace.get("repeated_tool_calls") or 0), int(trace.get("repeated_results") or 0)),
    }
    recent = [
        {"type": "detector", "kind": detection.get("kind"), "severity": detection.get("severity"), "evidence": detection.get("evidence") or {}},
        *list(trace.get("recent_events") or [])[-8:],
    ]
    if ctx.get("latest_user"):
        recent.append({"type": "task_context", "text": str(ctx["latest_user"])[:700]})
    status = str(ctx.get("status") or "").lower()
    activity = AgentActivity(
        agent_id=agent_id,
        session_id=session_id,
        status="RUNNING" if status not in _TERMINAL else status.upper(),
        last_meaningful_activity=Availability(True, progress),
        active_process=Availability(True, {"session_nonterminal": status not in _TERMINAL, "inflight_tool_calls": trace.get("inflight_tool_calls")}),
        context=Availability(True, {"latest_total_tokens": trace.get("latest_total_tokens"), "token_sum": trace.get("token_sum")}),
        scope=Availability(True, scope),
        recent_events=Availability(True, recent),
        task_id=f"live:{agent_id}:{session_id}",
        recovery_generation=0,
    )
    snapshot = ObserverSnapshot(
        snapshot_id=str(uuid.uuid4()),
        captured_at=time.time(),
        activity=activity,
        progress=activity.last_meaningful_activity,
        artifacts=Availability.unsupported("generic live task artifact contract unavailable"),
        git_diff=Availability.unsupported("generic live task git contract unavailable"),
        checkpoint=Availability.unsupported("generic live checkpoint unavailable"),
        test_result=Availability(True, {"exec_success_count": trace.get("exec_success_count")}) if int(trace.get("exec_success_count") or 0) > 0 else Availability.unsupported("no successful exec evidence"),
        repetition=Availability(True, repetition),
    )
    return ctx, activity, snapshot


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{NODE24_BIN}:{OPENCLAW_BIN.parent}:" + env.get("PATH", "")
    return env


def _agent_command(*, agent_id: str, session_key: str, message: str, timeout: int) -> list[str]:
    return [
        str(OPENCLAW_BIN), "agent", "--agent", agent_id,
        "--session-key", session_key, "--message", message,
        "--json", "--timeout", str(timeout),
    ]


def _parse_agent_turn_stdout(stdout: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OPENCLAW_AGENT_INVALID_JSON") from exc
    payloads = ((value.get("result") or {}).get("payloads") or []) if isinstance(value, dict) else []
    text = "\n".join(str(x.get("text") or "") for x in payloads if isinstance(x, dict)).strip()
    meta = (value.get("result") or {}).get("meta") or {}
    agent_meta = meta.get("agentMeta") or {} if isinstance(meta, dict) else {}
    return {
        "status": value.get("status"),
        "text": text,
        "session_id": agent_meta.get("sessionId"),
        "raw_status": value.get("summary"),
    }


def _agent_turn(*, agent_id: str, session_key: str, message: str, timeout: int = 180) -> dict[str, Any]:
    proc = subprocess.run(
        _agent_command(agent_id=agent_id, session_key=session_key, message=message, timeout=timeout),
        capture_output=True,
        text=True,
        timeout=timeout + 30,
        env=_cli_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"OPENCLAW_AGENT_FAILED:{proc.returncode}")
    return _parse_agent_turn_stdout(proc.stdout)


def _append_recovery_phase(
    *,
    source_session_id: str,
    agent_id: str,
    phase: str,
    status: str,
    **extra: Any,
) -> None:
    _append({
        "type": "live_recovery_phase",
        "observed_at": time.time(),
        "source_session_id": source_session_id,
        "agent_id": agent_id,
        "phase": phase,
        "status": status,
        **extra,
    })


def _validate_handoff(handoff: Path, handoff_dir: Path) -> bool:
    if not handoff.exists():
        return False
    try:
        HandoffWriter(handoff_dir).validate(handoff)
    except Exception:
        return False
    return True


def _bounded_lines(values: Any, *, fallback: str, limit: int = 3, width: int = 500) -> list[str]:
    if not isinstance(values, (list, tuple)):
        values = []
    out: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        out.append(text[:width])
        if len(out) >= limit:
            break
    return out or [fallback]


def _write_code_handoff(
    *,
    agent_id: str,
    session_id: str,
    session_key: str,
    detection: Mapping[str, Any],
    handoff: Path,
    handoff_dir: Path,
) -> None:
    """Write the recovery handoff deterministically without creating an Agent session."""
    if _validate_handoff(handoff, handoff_dir):
        _append_recovery_phase(
            source_session_id=session_id,
            agent_id=agent_id,
            phase="A_HANDOFF",
            status="REUSED",
            handoff_path=str(handoff),
            writer="code",
        )
        return

    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="A_HANDOFF",
        status="STARTED",
        handoff_path=str(handoff),
        writer="code",
    )
    try:
        source_ctx = _session_context(agent_id, session_id)
    except RuntimeError:
        source_ctx = {}

    latest_user = re.sub(r"\s+", " ", str(source_ctx.get("latest_user") or "")).strip()
    task = latest_user[:1200] or f"Continue unresolved task from source session {session_id}."

    completed = _bounded_lines(
        source_ctx.get("recent_assistant_texts"),
        fallback="No verified completed-work summary is available from generic watchdog evidence.",
        limit=3,
        width=700,
    )
    latest_run = source_ctx.get("latest_run") if isinstance(source_ctx.get("latest_run"), Mapping) else {}
    run_status = str(latest_run.get("status") or "unknown")
    run_id = str(latest_run.get("run_id") or source_ctx.get("run_id") or "unavailable")
    tests = [f"Latest runtime run_id={run_id}; status={run_status}; verify task-specific tests in the recovery session."]

    trigger_kind = str(detection.get("kind") or "UNKNOWN").upper()
    severity = str(detection.get("severity") or "UNSPECIFIED")
    current_problem = (
        f"JEV recovery trigger={trigger_kind}, severity={severity}. "
        "The source session showed sustained no-progress/abnormal evidence and requires a fresh bounded recovery attempt."
    )

    commands = _bounded_lines(
        source_ctx.get("command_texts"),
        fallback="Exact failed command unavailable; do not repeat the recent no-progress method identified by the watchdog.",
        limit=4,
        width=500,
    )
    if trigger_kind != "LOOP":
        commands = [
            "No single failed method was deterministically identified. "
            "Do not repeat any recent approach that produces the same no-progress state."
        ]

    remaining = (
        "Re-check current workspace/task state, preserve completed work, and finish only unresolved requirements "
        f"from this source task: {task[:800]}"
    )
    first_action = (
        "Read this handoff, inspect only directly relevant current task files/state, "
        "and continue with a method different from anything listed under Failed Methods."
    )

    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {v}" for v in values)

    content = (
        "# RECOVERY_HANDOFF\n\n"
        f"## Task\n{task}\n\n"
        f"## Previous Agent\n{agent_id}\n\n"
        f"## Session ID\n{session_id}\n\n"
        "## Recovery Generation\n1\n\n"
        f"## Completed Work\n{bullets(completed)}\n\n"
        "## Changed Files\n- No changed-file evidence is available from the generic watchdog snapshot; verify current workspace state before editing.\n\n"
        f"## Test Results\n{bullets(tests)}\n\n"
        f"## Current Problem\n{current_problem}\n\n"
        f"## Failed Methods\n{bullets(commands)}\n\n"
        f"## Remaining Work\n- {remaining}\n\n"
        f"## Next Agent First Action\n{first_action}\n\n"
        "## Recovery Metadata\n"
        f"- source_session_key: {session_key}\n"
        "- writer: deterministic-code\n"
        f"- created_at: {time.time()}\n\n"
        "## Safety\n"
        "- This handoff is evidence, not authorization. Preserve original approval/policy boundaries.\n"
    )

    handoff_dir.mkdir(parents=True, exist_ok=True)
    temp = handoff.with_suffix(".md.tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    HandoffWriter(handoff_dir).validate(temp)
    temp.replace(handoff)
    HandoffWriter(handoff_dir).validate(handoff)
    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="A_HANDOFF",
        status="COMPLETE",
        handoff_path=str(handoff),
        writer="code",
    )


def _phase_a_prepare_handoff(
    *,
    agent_id: str,
    session_id: str,
    session_key: str,
    detection: Mapping[str, Any],
    handoff: Path,
    handoff_dir: Path,
    timeout: int = HANDOFF_PHASE_TIMEOUT_SECONDS,
) -> None:
    # timeout is retained for call compatibility; no helper process is launched.
    del timeout
    _write_code_handoff(
        agent_id=agent_id,
        session_id=session_id,
        session_key=session_key,
        detection=detection,
        handoff=handoff,
        handoff_dir=handoff_dir,
    )


def _phase_b_terminate_source(
    *,
    agent_id: str,
    session_id: str,
    session_key: str,
) -> tuple[bool, bool, bool]:
    """Terminate only an actually-active source after a durable handoff."""
    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="B_TERMINATE",
        status="STARTED",
    )
    fresh = _session_context(agent_id, session_id)
    active, active_reason = _source_task_active(fresh)
    if not active:
        _append_recovery_phase(
            source_session_id=session_id,
            agent_id=agent_id,
            phase="B_TERMINATE",
            status="SKIPPED_NO_ACTIVE_TASK",
            reason=active_reason,
            abort_attempted=False,
            abort_confirmed=False,
        )
        return False, False, False

    run_id = str(fresh.get("run_id") or "") or None
    if not run_id:
        raise RuntimeError("SOURCE_RUN_ID_MISSING")

    abort_confirmed = _abort_session(
        agent_id=agent_id,
        session_key=session_key,
        run_id=run_id,
    )
    if not abort_confirmed:
        time.sleep(0.5)
        after = _session_context(agent_id, session_id)
        after_active, after_reason = _source_task_active(after)
        if after_active:
            raise RuntimeError("SOURCE_ABORT_FAILED")
        _append_recovery_phase(
            source_session_id=session_id,
            agent_id=agent_id,
            phase="B_TERMINATE",
            status="SOURCE_TERMINATED_ABORT_UNCONFIRMED",
            reason=after_reason,
            abort_attempted=True,
            abort_confirmed=False,
        )
        return True, False, False

    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="B_TERMINATE",
        status="COMPLETE",
        abort_attempted=True,
        abort_confirmed=True,
    )
    return True, True, True


def _latest_target_key(source_session_id: str) -> str | None:
    if not LIVE_HISTORY.exists():
        return None
    for raw in reversed(LIVE_HISTORY.read_text(encoding="utf-8").splitlines()[-1000:]):
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if (
            row.get("type") == "live_recovery_phase"
            and row.get("source_session_id") == source_session_id
            and row.get("phase") == "C_RESTART"
            and row.get("target_session_key")
        ):
            return str(row["target_session_key"])
    return None


def _phase_c_restart_and_resume(
    *,
    agent_id: str,
    session_id: str,
    handoff: Path,
    target_key: str,
    timeout: int = RESUME_PHASE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Create/reuse exactly one recovery session and execute one recovery turn."""
    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="C_RESTART",
        status="STARTED",
        target_session_key=target_key,
    )
    result = _agent_turn(
        agent_id=agent_id,
        session_key=target_key,
        message=(
            f"JEV RECOVERY MODE. Read {handoff} first. "
            "Continue the interrupted task from that bounded handoff and current directly relevant workspace state. "
            "Do not repeat completed work or any command/method listed under ## Failed Methods. "
            "Do not search global OpenClaw history, unrelated sessions, detector logs, or the wider ~/.openclaw tree. "
            "Respect all original approval, production, destructive-action, external-send, and permission boundaries. "
            "If the handoff lacks enough evidence to continue safely, reply RECOVERY_BLOCKED with the missing evidence. "
            "Otherwise finish and verify the legitimate remaining work, then reply RECOVERY_COMPLETE with a concise result."
        ),
        timeout=timeout,
    )
    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="C_RESTART",
        status="COMPLETE",
        target_session_key=target_key,
        target_session_id=result.get("session_id"),
    )
    final_status = (
        "RECOVERY_COMPLETE"
        if "RECOVERY_COMPLETE" in result.get("text", "")
        else "RECOVERY_BLOCKED"
        if "RECOVERY_BLOCKED" in result.get("text", "")
        else "RECOVERY_UNVERIFIED"
    )
    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="D_ACK",
        status="IMPLICIT_BY_RECOVERY_TURN",
        target_session_key=target_key,
        target_session_id=result.get("session_id"),
    )
    _append_recovery_phase(
        source_session_id=session_id,
        agent_id=agent_id,
        phase="E_COMPLETE",
        status=final_status,
        target_session_key=target_key,
        target_session_id=result.get("session_id"),
    )
    return {**result, "recovery_status": final_status, "handoff_ack": True}


def _abort_session(*, agent_id: str, session_key: str, run_id: str | None = None) -> bool:
    if not run_id:
        return False
    bridge = WatchdogAbortBridge()
    try:
        data = bridge.request_abort(
            agent_id=agent_id,
            session_key=session_key,
            run_id=run_id,
        )
    except (OSError, RuntimeError, TimeoutError, json.JSONDecodeError):
        return False
    finally:
        bridge.close()
    return bool(
        data.get("status") == "aborted"
        or data.get("abortedRunId")
        or data.get("aborted") is True
    )

def _already_recovered(session_id: str) -> bool:
    if not LIVE_HISTORY.exists():
        return False
    for raw in LIVE_HISTORY.read_text(encoding="utf-8").splitlines()[-500:]:
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if (
            row.get("type") == "live_recovery_outcome"
            and row.get("source_session_id") == session_id
            and row.get("status") in {"RECOVERY_COMPLETE", "RECOVERY_BLOCKED", "RECOVERY_UNVERIFIED"}
        ):
            return True
    return False


def _recover_live(*, ctx: Mapping[str, Any], snapshot: ObserverSnapshot, detection: Mapping[str, Any], decision_choice: str, confidence: Any) -> None:
    session_id = str(ctx["session_id"])
    agent_id = str(ctx["agent_id"])
    session_key = str(ctx["session_key"])
    current_phase = "PRECHECK"
    try:
        fresh_ctx, _, fresh_snapshot = _make_snapshot(
            agent_id=agent_id, session_id=session_id,
            detection=detection,
        )
        if snapshot_state_digest(fresh_snapshot) != snapshot_state_digest(snapshot):
            fresh_features = FeatureBuilder().build(fresh_snapshot)
            fresh_decision = ExistingOpenConnectorHealthJudge().decide(
                feature_state=fresh_features.features,
                idempotency_key=f"jev-live-recheck:{agent_id}:{session_id}:{fresh_features.state_digest}",
            )
            fresh_scope = (
                fresh_snapshot.activity.scope.value
                if fresh_snapshot.activity.scope.available
                and isinstance(fresh_snapshot.activity.scope.value, Mapping)
                else {}
            )
            _append({
                "type": "live_stale_recheck",
                "observed_at": time.time(),
                "source_session_id": session_id,
                "agent_id": agent_id,
                "choice": fresh_decision.choice,
                "confidence": fresh_decision.confidence,
                "stable_state_digest": snapshot_state_digest(fresh_snapshot),
            })
            if fresh_decision.choice != "SALVAGE" or not fresh_scope.get("auto_recovery_allowed"):
                _append({
                    "type": "live_recovery_outcome",
                    "source_session_id": session_id,
                    "agent_id": agent_id,
                    "status": "STALE_REEVALUATED_NO_ACTION",
                    "observed_at": time.time(),
                    "fresh_choice": fresh_decision.choice,
                })
                return
            snapshot = fresh_snapshot
            ctx = {**fresh_ctx, "agent_id": agent_id, "session_id": session_id, "session_key": session_key}
            decision_choice = fresh_decision.choice
            confidence = fresh_decision.confidence

        active, active_reason = _source_task_active(ctx)
        if not active:
            _append({
                "type": "live_recovery_outcome",
                "source_session_id": session_id,
                "agent_id": agent_id,
                "status": "STALE_REEVALUATED_NO_ACTION",
                "observed_at": time.time(),
                "fresh_choice": decision_choice,
                "reason": active_reason,
            })
            return

        workspace = _workspace_for(agent_id).resolve()
        handoff_dir = workspace / ".jev-recovery" / session_id
        handoff_dir.mkdir(parents=True, exist_ok=True)
        handoff = handoff_dir / "RECOVERY_HANDOFF.md"

        # Phase A — produce a durable handoff.  A valid file is enough; do not
        # burn the remaining source-turn timeout waiting for HANDOFF_WRITTEN.
        current_phase = "A_HANDOFF"
        _phase_a_prepare_handoff(
            agent_id=agent_id,
            session_id=session_id,
            session_key=session_key,
            detection=detection,
            handoff=handoff,
            handoff_dir=handoff_dir,
        )

        # Phase B — only after the handoff is durable, terminate the source run.
        current_phase = "B_TERMINATE"
        abort_attempted, abort_confirmed, should_restart = _phase_b_terminate_source(
            agent_id=agent_id,
            session_id=session_id,
            session_key=session_key,
        )
        if not should_restart:
            _append({
                "type": "live_recovery_outcome",
                "observed_at": time.time(),
                "source_session_id": session_id,
                "source_session_key": session_key,
                "agent_id": agent_id,
                "jev_choice": decision_choice,
                "jev_confidence": confidence,
                "handoff_path": str(handoff),
                "handoff_valid": True,
                "abort_attempted": abort_attempted,
                "abort_confirmed": abort_confirmed,
                "status": "STALE_REEVALUATED_NO_ACTION",
                "reason": "source_finished_before_confirmed_abort",
            })
            return

        # Phase C-E — one deterministic target key, one recovery session, one turn.
        current_phase = "C_RESTART"
        target_key = _latest_target_key(session_id) or f"agent:{agent_id}:jev-recovery:{session_id}"
        resume = _phase_c_restart_and_resume(
            agent_id=agent_id,
            session_id=session_id,
            handoff=handoff,
            target_key=target_key,
        )
        current_phase = "E_COMPLETE"
        _append({
            "type": "live_recovery_outcome",
            "observed_at": time.time(),
            "source_session_id": session_id,
            "source_session_key": session_key,
            "target_session_id": resume.get("session_id"),
            "target_session_key": target_key,
            "agent_id": agent_id,
            "jev_choice": decision_choice,
            "jev_confidence": confidence,
            "handoff_path": str(handoff),
            "handoff_valid": True,
            "handoff_ack": bool(resume.get("handoff_ack")),
            "abort_attempted": abort_attempted,
            "abort_confirmed": abort_confirmed,
            "logical_close": True,
            "force_kill": False,
            "delete": False,
            "target_reply": resume.get("text", "")[:2000],
            "status": resume["recovery_status"],
        })
    except Exception as exc:
        _append({
            "type": "live_recovery_outcome",
            "observed_at": time.time(),
            "source_session_id": session_id,
            "agent_id": agent_id,
            "status": "RECOVERY_ERROR",
            "phase": current_phase,
            "error": type(exc).__name__,
            "error_detail": str(exc)[:240],
        })
    finally:
        with _LOCK:
            _INFLIGHT.discard(session_id)

def _is_periodic_supervisor_candidate(kind: str, detection: Mapping[str, Any]) -> bool:
    evidence = detection.get("evidence") if isinstance(detection, Mapping) else {}
    source = evidence.get("source") if isinstance(evidence, Mapping) else None
    return kind == "PERIODIC_HEALTH" and source == "periodic_supervisor"


def evaluate_live_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    agent_id = str(payload.get("agent_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    session_key = str(payload.get("session_key") or "").strip()
    kind = str(payload.get("kind") or "").strip().upper()
    if not agent_id or not session_id or not session_key or kind not in _ALLOWED_KINDS:
        raise ValueError("INVALID_LIVE_CANDIDATE")

    session_role = _session_role(session_key)
    detection = {
        "kind": kind,
        "severity": payload.get("severity"),
        "evidence": payload.get("evidence") or {},
    }

    # Handoff sessions are control-plane helpers. They must never be judged as
    # task work and must never spawn another recovery chain.
    if session_role == "handoff":
        _reset_salvage_confirmation(session_id)
        record = {
            "type": "live_candidate_skip",
            "observed_at": time.time(),
            "agent_id": agent_id,
            "session_id": session_id,
            "session_key": session_key,
            "kind": kind,
            "status": "CONTROL_SESSION_SKIP",
            "reason": "jev_handoff_session",
            "action": "OBSERVE_ONLY",
        }
        _append(record)
        return record

    ctx, activity, snapshot = _make_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        detection=detection,
    )
    if str(ctx.get("status") or "").lower() in _TERMINAL:
        _reset_salvage_confirmation(session_id)
        return {
            "status": "TERMINAL_SKIP",
            "agent_id": agent_id,
            "session_id": session_id,
            "action": "OBSERVE_ONLY",
        }

    session_age = _session_age_seconds(ctx)
    if session_age is not None and session_age < GENERAL_RECOVERY_GRACE_SECONDS:
        _reset_salvage_confirmation(session_id)
        record = {
            "type": "live_candidate_skip",
            "observed_at": time.time(),
            "agent_id": agent_id,
            "session_id": session_id,
            "session_key": session_key,
            "kind": kind,
            "status": "GRACE_PERIOD",
            "reason": "session_under_5m",
            "session_age_seconds": round(session_age, 1),
            "action": "OBSERVE_ONLY",
        }
        _append(record)
        return record

    active, active_reason = _source_task_active(ctx)
    if not active:
        _reset_salvage_confirmation(session_id)
        record = {
            "type": "live_candidate_skip",
            "observed_at": time.time(),
            "agent_id": agent_id,
            "session_id": session_id,
            "session_key": session_key,
            "kind": kind,
            "status": "NO_ACTIVE_TASK",
            "reason": active_reason,
            "latest_run": ctx.get("latest_run") or {},
            "action": "OBSERVE_ONLY",
        }
        _append(record)
        return record

    features = FeatureBuilder().build(snapshot)
    stable_digest = snapshot_state_digest(snapshot)
    decision = ExistingOpenConnectorHealthJudge().decide(
        feature_state=features.features,
        idempotency_key=f"jev-live:{agent_id}:{session_id}:{features.state_digest}",
    )
    scope = (
        activity.scope.value
        if activity.scope.available and isinstance(activity.scope.value, Mapping)
        else {}
    )
    record = {
        "type": "live_jev_decision",
        "observed_at": time.time(),
        "agent_id": agent_id,
        "session_id": session_id,
        "session_key": session_key,
        "session_role": session_role,
        "kind": kind,
        "state_digest": features.state_digest,
        "stable_state_digest": stable_digest,
        "choice": decision.choice,
        "confidence": decision.confidence,
        "probabilities": dict(decision.probabilities),
        "auto_recovery_allowed": bool(scope.get("auto_recovery_allowed")),
        "risk": dict(scope),
    }

    action = "OBSERVE_ONLY"
    should_start_recovery = False

    if decision.choice != "SALVAGE":
        _reset_salvage_confirmation(session_id)

    if decision.choice == "DEAD":
        action = "ESCALATE_MAIN"
    elif decision.choice == "SALVAGE":
        confident, confidence_reason, salvage_margin = _salvage_is_confident(decision)
        record["salvage_margin"] = salvage_margin
        if not confident:
            _reset_salvage_confirmation(session_id)
            action = "SALVAGE_DEFERRED_RECHECK"
            record["auto_recovery_deferred"] = True
            record["defer_reason"] = confidence_reason
        elif session_role == "recovery":
            # Automatic recovery is one generation only. A recovery session may
            # still be judged by JEV, but a second recovery session is forbidden.
            _reset_salvage_confirmation(session_id)
            action = "RECOVERY_GENERATION_LIMIT"
            record["recovery_generation_limit"] = 1
        elif agent_id.lower() == "main":
            _reset_salvage_confirmation(session_id)
            action = "ESCALATE_MAIN"
        elif not _runtime_recovery_safe(ctx, snapshot):
            _reset_salvage_confirmation(session_id)
            action = "SALVAGE_BLOCKED_NO_RUNTIME_EVIDENCE"
        elif not scope.get("auto_recovery_allowed"):
            _reset_salvage_confirmation(session_id)
            action = "SALVAGE_BLOCKED_BY_SCOPE"
        elif _recovery_attempt_exists(session_id):
            _reset_salvage_confirmation(session_id)
            action = "RECOVERY_ATTEMPT_EXISTS"
        elif not _is_periodic_supervisor_candidate(kind, detection):
            _reset_salvage_confirmation(session_id)
            action = "EVENT_EVIDENCE_ONLY"
            record["auto_recovery_deferred"] = True
            record["defer_reason"] = "single_entry_periodic_supervisor_only"
        else:
            confirmation_count = _confirm_salvage(
                session_id,
                stable_digest=stable_digest,
            )
            record["salvage_confirmation_count"] = confirmation_count
            record["salvage_confirmation_required"] = AUTO_RECOVERY_CONFIRMATIONS
            if confirmation_count < AUTO_RECOVERY_CONFIRMATIONS:
                action = "SALVAGE_DEFERRED_RECHECK"
                record["auto_recovery_deferred"] = True
                record["defer_reason"] = "confirmation_required"
            else:
                if _auto_recovery_execution_enabled():
                    should_start_recovery = True
                else:
                    action = "RECOVERY_READY_OBSERVE_ONLY"
                    record["auto_recovery_execution_enabled"] = False
                    record["recovery_ready"] = True

    if should_start_recovery:
        _reset_salvage_confirmation(session_id)
        with _LOCK:
            if session_id in _INFLIGHT:
                action = "RECOVERY_INFLIGHT"
            else:
                _INFLIGHT.add(session_id)
                action = "RECOVERY_STARTED"
                threading.Thread(
                    target=_recover_live,
                    kwargs={
                        "ctx": {
                            **ctx,
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "session_key": session_key,
                        },
                        "snapshot": snapshot,
                        "detection": detection,
                        "decision_choice": decision.choice,
                        "confidence": decision.confidence,
                    },
                    daemon=True,
                    name=f"jev-recovery-{agent_id}-{session_id[:8]}",
                ).start()

    record["action"] = action
    _append(record)
    return record


@router.post("/internal/jev-recovery-live-candidate")
async def live_candidate_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    body = await request.json()
    try:
        return evaluate_live_candidate(body)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/internal/jev-recovery-live-status")
async def live_status_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    rows = []
    if LIVE_HISTORY.exists():
        for raw in LIVE_HISTORY.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                rows.append(json.loads(raw))
            except Exception:
                pass
    with _LOCK:
        inflight = sorted(_INFLIGHT)
    return {
        "auto_recovery_execution_enabled": _auto_recovery_execution_enabled(),
        "inflight": inflight,
        "recent": rows[-25:],
    }


@router.post("/internal/jev-recovery-live-abort-smoke")
async def live_abort_smoke_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    body = await request.json()
    agent_id = str(body.get("agent_id") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    session_key = str(body.get("session_key") or "").strip()
    if agent_id.lower() != "qwentest" or not session_id or not session_key:
        raise HTTPException(422, "qwentest_smoke_only")
    ctx = _session_context(agent_id, session_id)
    if str(ctx.get("session_key") or "") != session_key:
        raise HTTPException(422, "session_mismatch")
    run_id = str(ctx.get("run_id") or "") or None
    raw: Any = None
    error_type = None
    error_code = None
    if run_id:
        bridge = WatchdogAbortBridge()
        try:
            raw = bridge.request_abort(
                agent_id=agent_id,
                session_key=session_key,
                run_id=run_id,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            error_code = str(exc)[:120]
        finally:
            bridge.close()
    confirmed = bool(
        isinstance(raw, dict)
        and (
            raw.get("status") == "aborted"
            or raw.get("abortedRunId")
            or raw.get("aborted") is True
        )
    )
    safe_raw = raw if isinstance(raw, dict) else None
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "session_key": session_key,
        "run_id_present": bool(run_id),
        "abort_confirmed": confirmed,
        "rpc": safe_raw,
        "error_type": error_type,
        "error_code": error_code,
    }
