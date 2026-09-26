"""JEV Session Recovery Watchdog v0.1.

This module is an advisory observer.  It does not own Process Board lifecycle
truth and can only request a fresh child after explicit watchdog opt-in and
deterministic safety interlocks pass.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from jev_task_router import OpenConnectorJEVClient
from jev_recovery_shadow import _read_session_trace

# General-agent production policy: allow the agent five minutes to work before
# the first JEV health decision, then ask JEV once per minute until terminal.
START_DELAY_SECONDS = 300.0
CADENCE_SECONDS = 60.0
RESTART_CONFIRMATIONS = 2
TERMINAL_STATES = frozenset({"PASS", "FAIL", "BLOCKED", "TIMEOUT", "CANCELLED"})
BOOLEAN_QUESTIONS = (
    "meaningful_progress", "productive_activity", "loop_suspected", "stalled",
    "continue_current_session", "restart_recommended",
)
CHOICE_VALUES = ("CONTINUE", "RECHECK", "RESTART_SESSION", "ESCALATE_MAIN")
UNAVAILABLE = {"available": False, "value": None, "reason": "not_observable"}
_SECRET_KEY = re.compile(r"(?i)(token|secret|password|credential|authorization|cookie|api[_-]?key|private[_-]?key)")
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+\S+|sk-[a-z0-9_-]{8,}|(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S+)")
_UNSAFE = re.compile(
    r"(?i)(production\s+db|database\s+(?:write|migration)|git\s+(?:push|merge)|\bdelete\b|service\s+restart|permission\s+change|external\s+(?:send|post)|uncertain\s+side effect)"
)


class WatchdogJEVClient(Protocol):
    def watchdog(self, *, state: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class HistoricalJEVClient:
    """Deterministic fake seam; never contacts a provider."""
    def __init__(self, response: Mapping[str, Any]):
        self.response = dict(response)
        self.calls: list[dict[str, Any]] = []

    def watchdog(self, *, state: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        self.calls.append({"state": state, "idempotency_key": idempotency_key})
        return dict(self.response)


def _safe(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _safe(v, key=str(k)) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [_safe(v) for v in value[:50]]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[REDACTED]", value[:1000])
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:200]


def sanitize_observed_facts(record: Mapping[str, Any], *, elapsed: float) -> dict[str, Any]:
    """Project server-owned observations; unavailable is explicit."""
    checkpoint = record.get("progress_checkpoint")
    verified = record.get("verified_progress")
    binding = record.get("openclaw_binding")
    policy_state = record.get("policy_state")
    binding_view = dict(binding) if isinstance(binding, Mapping) else {}
    resolved_session_id = str(binding_view.get("session_id") or "").strip()
    if (
        not resolved_session_id
        and binding_view.get("session_key")
        and record.get("agent_id")
    ):
        db = (
            Path.home() / ".openclaw" / "agents"
            / str(record.get("agent_id")).lower() / "agent"
            / "openclaw-agent.sqlite"
        )
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as con:
                row = con.execute(
                    "SELECT session_id FROM session_windows "
                    "WHERE session_key=? ORDER BY rowid DESC LIMIT 1",
                    (str(binding_view.get("session_key")),),
                ).fetchone()
            if row and row[0]:
                resolved_session_id = str(row[0])
                binding_view["resolved_session_id"] = resolved_session_id
        except Exception:
            pass

    trace: dict[str, Any] = {"available": False, "reason": "binding_unavailable"}
    if resolved_session_id and record.get("agent_id"):
        try:
            trace = _read_session_trace(
                agent_id=str(record.get("agent_id")),
                session_id=resolved_session_id,
            )
        except Exception as exc:
            trace = {"available": False, "reason": type(exc).__name__}

    trace_available = bool(trace.get("available"))
    trace_tool = (
        {
            "available": True,
            "value": {
                "tool_call_count": trace.get("tool_call_count"),
                "tool_result_count": trace.get("tool_result_count"),
                "completed_tool_results": trace.get("completed_tool_results"),
                "nonterminal_tool_results": trace.get("nonterminal_tool_results"),
                "inflight_tool_calls": trace.get("inflight_tool_calls"),
                "successful_tool_results": trace.get("successful_tool_results"),
                "tool_error_count": trace.get("tool_error_count"),
                "exec_success_count": trace.get("exec_success_count"),
                "exec_nonzero_count": trace.get("exec_nonzero_count"),
                "meaningful_result_count": trace.get("meaningful_result_count"),
            },
        }
        if trace_available else dict(UNAVAILABLE)
    )
    trace_repetition = (
        {
            "available": True,
            "value": {
                "repeated_tool_calls": trace.get("repeated_tool_calls"),
                "repeated_results": trace.get("repeated_results"),
                "unique_tool_count": trace.get("unique_tool_count"),
                "dominant_tool_ratio": trace.get("dominant_tool_ratio"),
            },
        }
        if trace_available else dict(UNAVAILABLE)
    )
    facts: dict[str, Any] = {
        "elapsed_seconds": round(max(0.0, float(elapsed)), 3),
        "lifecycle": record.get("status", "UNKNOWN"),
        "current_progress": _safe(checkpoint) if isinstance(checkpoint, Mapping) else dict(UNAVAILABLE),
        "changed_progress": bool(checkpoint or verified),
        "progress_checkpoint": _safe(checkpoint) if isinstance(checkpoint, Mapping) else dict(UNAVAILABLE),
        "tool_evidence": trace_tool if trace_available else (
            _safe(record.get("tool_call_count"))
            if record.get("tool_call_metric") == "SUPPORTED"
            else dict(UNAVAILABLE)
        ),
        "result_evidence": _safe(record.get("result")) if record.get("result") is not None else dict(UNAVAILABLE),
        "output_evidence": dict(UNAVAILABLE),
        "file_evidence": _safe((verified or {}).get("artifacts")) if isinstance(verified, Mapping) and verified.get("artifacts") else dict(UNAVAILABLE),
        "command_evidence": (
            {"available": True, "value": _safe(trace.get("recent_events") or [])}
            if trace_available else dict(UNAVAILABLE)
        ),
        "test_evidence": dict(UNAVAILABLE),
        "artifact_evidence": _safe((verified or {}).get("evidence")) if isinstance(verified, Mapping) and verified.get("evidence") else dict(UNAVAILABLE),
        "repetition_counts": trace_repetition if trace_available else (
            _safe((policy_state or {}).get("repetition_counts"))
            if isinstance(policy_state, Mapping) and "repetition_counts" in policy_state
            else dict(UNAVAILABLE)
        ),
        "meaningful_activity_age_seconds": (
            {"available": True, "value": trace.get("meaningful_progress_age_sec")}
            if trace_available else dict(UNAVAILABLE)
        ),
        "active_process_or_tool": (
            {
                "available": True,
                "value": {
                    "inflight_tool_calls": trace.get("inflight_tool_calls"),
                    "last_tool_name": trace.get("last_tool_name"),
                    "last_tool_event_age_sec": trace.get("last_tool_event_age_sec"),
                },
            }
            if trace_available else dict(UNAVAILABLE)
        ),
        "external_wait": dict(UNAVAILABLE),
        "local_inference_active": (
            {
                "available": True,
                "value": bool((trace.get("inflight_tool_calls") or 0) > 0),
            }
            if trace_available else dict(UNAVAILABLE)
        ),
        "binding_observed": {"available": bool(binding_view), "value": _safe(binding_view) if binding_view else None},
        "policy_events": _safe(record.get("policy_events") or []),
        "agent_stall_detector": _stall_detector_facts(
            agent_id=str(record.get("agent_id") or ""),
            session_id=resolved_session_id,
        ),
    }
    return _safe(facts)


def _stall_detector_facts(*, agent_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Read current detector output for this agent/session only; never write."""
    path = os.environ.get("PLACHEM_STALL_DETECTIONS_DB", "/home/plachem-sever/.openclaw/workspace/projects/agent-stall-detector/data/detections.sqlite")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            rows = con.execute(
                "SELECT agent_id,session_id,session_key,kind,severity,status,ts_detected_ms,evidence_json "
                "FROM detections WHERE status IN ('NEW','CONFIRMED') "
                "ORDER BY ts_detected_ms DESC LIMIT 50"
            ).fetchall()
        out = []
        wanted_agent = agent_id.casefold().strip()
        for agent, detector_session_id, session_key, kind, severity, status, ts, evidence in rows:
            if wanted_agent and str(agent or "").casefold() != wanted_agent:
                continue
            if session_id and detector_session_id and str(detector_session_id) != session_id:
                continue
            try:
                parsed = json.loads(evidence) if evidence else {}
            except (TypeError, ValueError):
                parsed = {}
            out.append(_safe({
                "agent_id": agent or "UNAVAILABLE",
                "session_id": detector_session_id,
                "session_key": session_key,
                "kind": kind or "UNAVAILABLE",
                "severity": severity or "UNAVAILABLE",
                "status": status or "UNAVAILABLE",
                "ts_detected_ms": ts,
                "evidence": parsed if isinstance(parsed, Mapping) else {},
            }))
            if len(out) >= 10:
                break
        return {"available": True, "deltas": out}
    except Exception as exc:
        return {"available": False, "deltas": [], "reason": type(exc).__name__}


def _started_epoch(record: Mapping[str, Any], now: float) -> float:
    value = record.get("started_at")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    legacy = (record.get("policy_state") or {}).get("watchdog_started_at")
    return float(legacy) if isinstance(legacy, (int, float)) else now


def deterministic_interlock(record: Mapping[str, Any], snapshot: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Return safe only when no protected or uncertain side effect is observed."""
    material = json.dumps(_safe({"record": record, "snapshot": snapshot or {}}), ensure_ascii=False)
    if _UNSAFE.search(material):
        return False, "IRREVERSIBLE_OR_UNCERTAIN_SIDE_EFFECT"
    if record.get("escalation_required") or record.get("policy_status") in {"BLOCKED", "CANCELLED"}:
        return False, "POLICY_OR_SECURITY_STOP"
    return True, "INTERLOCKS_PASS"


def recovery_snapshot(record: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    goal = record.get("goal_contract") if isinstance(record.get("goal_contract"), Mapping) else {}
    checkpoint = record.get("progress_checkpoint") if isinstance(record.get("progress_checkpoint"), Mapping) else {}
    return _safe({
        "original_goal": goal.get("primary_objective", "UNAVAILABLE"),
        "completed_work": (record.get("verified_progress") or {}).get("completed_conditions", []),
        "current_step": checkpoint.get("current_step", "UNAVAILABLE"),
        "files": (record.get("verified_progress") or {}).get("artifacts", []),
        "tests": (record.get("verified_progress") or {}).get("evidence", []),
        "blocker": checkpoint.get("blocking_reason", "UNAVAILABLE"),
        "tried_approaches": (record.get("policy_state") or {}).get("tried_approaches", []),
        "do_not_repeat": (record.get("policy_state") or {}).get("do_not_repeat", []),
        "remaining_conditions": checkpoint.get("remaining_conditions", []),
        "next_recommendation": "fresh child session with same goal contract",
    })


class JEVSessionRecoveryWatchdog:
    def __init__(self, registry: Any, *, history_path: str | Path, client: WatchdogJEVClient | None = None,
                 clock: Callable[[], float] = time.time,
                 recovery: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None):
        self.registry = registry
        self.history_path = Path(history_path)
        self.client = client or OpenConnectorJEVClient()
        self.clock = clock
        self.recovery = recovery
        self._last_check: dict[str, float] = {}

    def _history(self, entry: Mapping[str, Any]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.history_path.exists():
            self.history_path.touch(mode=0o600)
        else:
            self.history_path.chmod(0o600)
        with self.history_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_safe(dict(entry)), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def _recent(self, run_id: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.history_path.is_file():
            return []
        rows = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get("run_id") == run_id:
                rows.append(value)
        return rows[-limit:]

    def observe(self, run_id: str, *, now: float | None = None) -> dict[str, Any]:
        record = self.registry.get(run_id)
        if record is None:
            raise ValueError("UNKNOWN_RUN")
        now = self.clock() if now is None else float(now)
        if record.get("status") in TERMINAL_STATES:
            return {"status": "TERMINAL", "polled": False, "record": record}
        if not record.get("watchdog_managed", False):
            return {"status": "LEGACY", "polled": False, "record": record}
        elapsed = max(0.0, now - _started_epoch(record, now))
        if elapsed < START_DELAY_SECONDS:
            return {"status": "WAITING", "polled": False, "elapsed": elapsed}
        prior = self._last_check.get(run_id)
        if prior is not None and now - prior < CADENCE_SECONDS:
            return {"status": "CADENCE_WAIT", "polled": False, "elapsed": elapsed}
        facts = sanitize_observed_facts(record, elapsed=elapsed)
        recent = self._recent(run_id)
        state = {"facts": facts, "last_decisions": recent[-5:], "activity_deltas": recent[-5:]}
        started_call = self.clock()
        self._last_check[run_id] = now
        raw = self.client.watchdog(state=state, idempotency_key=f"jev-watchdog:{run_id}:{int(now)}")
        latency_ms = round(max(0.0, self.clock() - started_call) * 1000, 3)
        requested_choice = str(raw.get("choice") or "ESCALATE_MAIN")
        choice = requested_choice
        probabilities = {name: float((raw.get("probabilities") or {}).get(name, 0.0)) for name in CHOICE_VALUES}
        confidence = float(raw.get("confidence") or max(probabilities.values()))
        safe, reason = deterministic_interlock(record, recovery_snapshot(record, facts))
        follow_up = "NO_ACTION"
        restart = False
        snapshot = None
        recovery_result: dict[str, Any] | None = None
        if choice == "RESTART_SESSION":
            prior_restart = bool(
                recent
                and str(recent[-1].get("requested_choice") or recent[-1].get("choice") or "") == "RESTART_SESSION"
                and str(recent[-1].get("follow_up") or "") == "RESTART_CONFIRMATION_REQUIRED"
            )
            if not prior_restart:
                follow_up = "RESTART_CONFIRMATION_REQUIRED"
            elif safe and self.recovery is not None:
                snapshot = recovery_snapshot(record, facts)
                recovery_result = dict(self.recovery(run_id, snapshot) or {})
                recovery_status = str(recovery_result.get("status") or "UNKNOWN")
                if recovery_status == "DISPATCHED":
                    restart, follow_up = True, "FRESH_CHILD_CREATED"
                elif recovery_status == "REQUEUE_REQUIRED":
                    restart, follow_up = False, "CONTROLLED_REQUEUE_REQUIRED"
                else:
                    choice = "ESCALATE_MAIN"
                    follow_up = f"RECOVERY_{recovery_status}"
            else:
                choice, follow_up = "ESCALATE_MAIN", f"{reason}:NO_TERMINATION"
        elif choice in {"CONTINUE", "RECHECK"}:
            follow_up = choice
        else:
            choice, follow_up = "ESCALATE_MAIN", "ESCALATE_NO_TERMINATION"
        entry = {"timestamp": time.time(), "agent_id": record.get("agent_id"), "session_id": (record.get("openclaw_binding") or {}).get("session_id"),
                 "run_id": run_id, "task_id": record.get("task_id"), "elapsed": elapsed,
                 "state_digest": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
                 "boolean_questions": list(BOOLEAN_QUESTIONS), "boolean_probabilities": {k: raw.get("boolean_probabilities", {}).get(k) for k in BOOLEAN_QUESTIONS},
                 "requested_choice": requested_choice,
                 "choice": choice, "choice_probabilities": probabilities, "confidence": confidence,
                 "latency_ms": latency_ms, "follow_up": follow_up, "restart": restart,
                 "recovery_result": _safe(recovery_result) if recovery_result is not None else None,
                 "snapshot_id": hashlib.sha256(json.dumps(snapshot or {}, sort_keys=True).encode()).hexdigest()[:16] if snapshot else None}
        self._history(entry)
        return {"status": "OBSERVED", "polled": True, "choice": choice, "follow_up": follow_up, "restart": restart, "history": entry}
