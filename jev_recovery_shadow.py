from __future__ import annotations

"""Local-only shadow observer for JEV Agent Recovery Watchdog v0.1."""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

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
from war_room_adapter import OpenClawSessionAdapter, PersistentGatewayBridge

router = APIRouter()
SHADOW_HISTORY = Path(__file__).resolve().parent / "runtime" / "jev-recovery-shadow.jsonl"
GOLD_HISTORY = Path(__file__).resolve().parent / "runtime" / "jev-recovery-gold.jsonl"
_OBSERVATION_BASELINES: dict[str, dict[str, Any]] = {}
_OBSERVATION_LOCK = threading.Lock()
FIXTURE_ROOT = Path.home() / ".openclaw/agents/QwenTest/01_ACTIVE/jev-watchdog-realistic-fixtures"


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")).strip()
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def _trace_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _tool_result_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _read_session_trace(*, agent_id: str, session_id: str) -> dict[str, Any]:
    db = Path.home() / ".openclaw" / "agents" / agent_id.lower() / "agent" / "openclaw-agent.sqlite"
    if not db.exists():
        return {"available": False, "reason": "agent_session_db_unavailable"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT seq,event_json,created_at FROM transcript_events "
            "WHERE session_id=? ORDER BY seq DESC LIMIT 80",
            (session_id,),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"trace_read_failed:{type(exc).__name__}"}

    events = []
    for row in reversed(rows):
        try:
            value = json.loads(row["event_json"])
        except Exception:
            continue
        if isinstance(value, dict):
            events.append(value)

    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    assistant_turns = 0
    token_sum = 0
    latest_total_tokens = None
    trace_recent: list[dict[str, Any]] = []
    call_by_id: dict[str, dict[str, Any]] = {}
    last_tool_ts = None
    first_activity_ts = None
    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        timestamp = message.get("timestamp")
        if isinstance(timestamp, (int, float)):
            ts_int = int(timestamp)
            first_activity_ts = ts_int if first_activity_ts is None else min(first_activity_ts, ts_int)
        if role == "assistant":
            assistant_turns += 1
            usage = message.get("usage")
            if isinstance(usage, dict):
                total = usage.get("totalTokens")
                if isinstance(total, (int, float)):
                    latest_total_tokens = int(total)
                    token_sum += int(total)
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "toolCall":
                        continue
                    name = str(part.get("name") or "unknown")
                    call_id = str(part.get("id") or "")
                    arguments = part.get("arguments") if isinstance(part.get("arguments"), dict) else {}
                    signature_arguments = {
                        key: value for key, value in arguments.items()
                        if key not in {"title", "yieldMs", "timeout", "timeoutSeconds"}
                    }
                    signature = _trace_hash({"name": name, "arguments": signature_arguments})
                    call = {
                        "call_id": call_id,
                        "name": name,
                        "signature": signature,
                        "arguments": arguments,
                        "timestamp": timestamp,
                    }
                    tool_calls.append(call)
                    if call_id:
                        call_by_id[call_id] = call
                    trace_recent.append({"kind": "tool_call", "name": name, "signature": signature[:12]})
                    if isinstance(timestamp, (int, float)):
                        last_tool_ts = int(timestamp)
        elif role == "toolResult":
            name = str(message.get("toolName") or "unknown")
            call_id = str(message.get("toolCallId") or "")
            call = call_by_id.get(call_id, {})
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            details = message.get("details") if isinstance(message.get("details"), dict) else {}
            text = _tool_result_text(message)
            is_error = bool(message.get("isError"))
            result_hash = _trace_hash({
                "name": name,
                "text": text,
                "status": details.get("status"),
                "exit_code": details.get("exitCode"),
                "is_error": is_error,
            })
            tool_results.append({
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "result_hash": result_hash,
                "is_error": is_error,
                "status": details.get("status"),
                "exit_code": details.get("exitCode"),
                "duration_ms": details.get("durationMs"),
                "timestamp": timestamp,
            })
            trace_recent.append({
                "kind": "tool_result",
                "name": name,
                "status": details.get("status"),
                "is_error": is_error,
                "result_hash": result_hash[:12],
            })
            if isinstance(timestamp, (int, float)):
                last_tool_ts = int(timestamp)

    call_sigs = [item["signature"] for item in tool_calls]
    result_hashes = [item["result_hash"] for item in tool_results]
    mutating_names = {"write", "edit", "apply_patch"}
    nonterminal_statuses = {"running", "pending", "in_progress", "accepted"}
    terminal_results = [
        item for item in tool_results
        if str(item.get("status") or "").lower() not in nonterminal_statuses
    ]
    nonterminal_results = [
        item for item in tool_results
        if str(item.get("status") or "").lower() in nonterminal_statuses
    ]
    successful_results = [
        item for item in terminal_results if not item["is_error"]
    ]

    def is_state_probe(item: dict[str, Any]) -> bool:
        name = str(item.get("name") or "").lower()
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        if name == "process":
            return str(arguments.get("action") or "").lower() in {"list", "poll", "log"}
        if name != "exec":
            return False
        command = " ".join(str(arguments.get("command") or "").strip().lower().split())
        if command.startswith("cd ") and "&&" in command:
            command = command.split("&&", 1)[1].strip()
        probe_prefixes = (
            "ls", "pwd", "stat ", "cat ", "head ", "tail ",
            "grep ", "find ", "wc ", "git status", "git diff --stat",
        )
        return any(command == prefix.rstrip() or command.startswith(prefix) for prefix in probe_prefixes)

    probe_results = [item for item in successful_results if is_state_probe(item)]
    meaningful_results = [
        item for item in successful_results
        if not is_state_probe(item)
        and not (
            item["name"] == "exec"
            and isinstance(item.get("exit_code"), int)
            and item.get("exit_code") != 0
        )
    ]
    exec_nonzero = [
        item for item in terminal_results
        if item["name"] == "exec"
        and isinstance(item.get("exit_code"), int)
        and item.get("exit_code") != 0
    ]
    exec_success = sum(
        1 for item in meaningful_results
        if item["name"] == "exec"
        and (
            item.get("exit_code") == 0
            or str(item.get("status") or "").lower() in {"completed", "success"}
        )
    )
    meaningful_timestamps = [
        int(item["timestamp"]) for item in meaningful_results
        if isinstance(item.get("timestamp"), (int, float))
    ]
    last_meaningful_ts = max(meaningful_timestamps) if meaningful_timestamps else first_activity_ts
    now_ms = int(time.time() * 1000)
    meaningful_age = (
        None if last_meaningful_ts is None
        else round((now_ms - last_meaningful_ts) / 1000, 1)
    )
    return {
        "available": True,
        "tool_call_count": len(tool_calls),
        "tool_result_count": len(tool_results),
        "completed_tool_results": len(terminal_results),
        "nonterminal_tool_results": len(nonterminal_results),
        "inflight_tool_calls": max(0, len(tool_calls) - len(terminal_results)),
        "last_tool_name": tool_calls[-1]["name"] if tool_calls else None,
        "successful_tool_results": len(successful_results),
        "tool_error_count": sum(1 for item in tool_results if item["is_error"]),
        "mutating_tool_calls": sum(1 for item in tool_calls if item["name"] in mutating_names),
        "exec_success_count": exec_success,
        "exec_nonzero_count": len(exec_nonzero),
        "probe_result_count": len(probe_results),
        "meaningful_result_count": len(meaningful_results),
        "meaningful_progress_age_sec": meaningful_age,
        "repeated_tool_calls": len(call_sigs) - len(set(call_sigs)),
        "repeated_results": len(result_hashes) - len(set(result_hashes)),
        "unique_tool_count": len({item["name"] for item in tool_calls}),
        "dominant_tool_ratio": (
            round(
                max(
                    [sum(1 for item in tool_calls if item["name"] == name)
                     for name in {item["name"] for item in tool_calls}],
                    default=0,
                ) / len(tool_calls),
                3,
            )
            if tool_calls else None
        ),
        "assistant_turns": assistant_turns,
        "latest_total_tokens": latest_total_tokens,
        "token_sum": token_sum,
        "last_tool_event_age_sec": (
            None if last_tool_ts is None else round((now_ms - last_tool_ts) / 1000, 1)
        ),
        "recent_events": trace_recent[-10:],
    }


def _artifact_contract(*, work_root: str | Path | None,
                       expected_artifacts: tuple[str, ...] = ()) -> Availability:
    if work_root is None:
        return Availability.unsupported("work root not supplied")
    root = Path(work_root).resolve()
    try:
        root.relative_to(FIXTURE_ROOT.resolve())
    except ValueError:
        return Availability.unsupported("work root outside approved fixture root")
    items = []
    for relative in expected_artifacts:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            continue
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        exists = target.is_file()
        stat = target.stat() if exists else None
        items.append({
            "path": str(rel),
            "exists": exists,
            "size": stat.st_size if stat else None,
            "mtime_ns": stat.st_mtime_ns if stat else None,
        })
    if not items:
        return Availability.unsupported("expected artifacts not supplied")
    return Availability(True, {
        "expected_count": len(items),
        "existing_count": sum(1 for item in items if item["exists"]),
        "missing_count": sum(1 for item in items if not item["exists"]),
        "items": items,
    })


def build_shadow_snapshot(*, data: dict[str, Any], agent_id: str,
                          session_id: str, task_id: str,
                          work_root: str | Path | None = None,
                          expected_artifacts: tuple[str, ...] = ()) -> tuple[AgentActivity, ObserverSnapshot]:
    messages = data.get("messages", []) if isinstance(data.get("messages"), list) else []
    info = data.get("sessionInfo", {}) if isinstance(data.get("sessionInfo"), dict) else {}
    now_ms = int(time.time() * 1000)
    timestamps = [
        int(item["timestamp"]) for item in messages
        if isinstance(item, dict) and isinstance(item.get("timestamp"), (int, float))
    ]
    last_ts = max(timestamps) if timestamps else None
    recent_events: list[dict[str, Any]] = []
    recent_texts: list[str] = []
    assistant_texts: list[str] = []
    for item in messages[-10:]:
        if not isinstance(item, dict):
            continue
        text = _message_text(item)
        if not text:
            continue
        normalized = " ".join(text.split())
        recent_texts.append(normalized)
        if item.get("role") == "assistant":
            assistant_texts.append(normalized)
        recent_events.append({
            "role": item.get("role"),
            "text": normalized[:240],
            "timestamp": item.get("timestamp"),
        })
    duplicates = len(recent_texts) - len(set(recent_texts))
    assistant_duplicates = len(assistant_texts) - len(set(assistant_texts))
    assistant_unique_ratio = (
        round(len(set(assistant_texts)) / len(assistant_texts), 3)
        if assistant_texts else None
    )
    trace = _read_session_trace(agent_id=agent_id, session_id=session_id)
    trace_available = bool(trace.get("available"))
    contract_artifacts = _artifact_contract(
        work_root=work_root, expected_artifacts=expected_artifacts
    )
    active_ids = info.get("activeRunIds", []) if isinstance(info.get("activeRunIds"), list) else []
    active = bool(info.get("hasActiveRun") is True or active_ids)
    age = None if last_ts is None else round((now_ms - last_ts) / 1000, 1)

    if trace_available:
        progress_value = {
            "last_message_age_sec": age,
            "message_count": len(messages),
            "tool_call_count": trace["tool_call_count"],
            "tool_result_count": trace["tool_result_count"],
            "completed_tool_results": trace["completed_tool_results"],
            "nonterminal_tool_results": trace["nonterminal_tool_results"],
            "inflight_tool_calls": trace["inflight_tool_calls"],
            "last_tool_name": trace["last_tool_name"],
            "successful_tool_results": trace["successful_tool_results"],
            "tool_error_count": trace["tool_error_count"],
            "mutating_tool_calls": trace["mutating_tool_calls"],
            "exec_success_count": trace["exec_success_count"],
            "exec_nonzero_count": trace["exec_nonzero_count"],
            "probe_result_count": trace["probe_result_count"],
            "meaningful_result_count": trace["meaningful_result_count"],
            "meaningful_progress_age_sec": trace["meaningful_progress_age_sec"],
            "last_tool_event_age_sec": trace["last_tool_event_age_sec"],
        }
        context = Availability(True, {
            "assistant_turns": trace["assistant_turns"],
            "latest_total_tokens": trace["latest_total_tokens"],
            "token_sum": trace["token_sum"],
        })
        safe_recent_events = trace["recent_events"]
        if contract_artifacts.available:
            progress_value["expected_artifact_existing"] = contract_artifacts.value["existing_count"]
            progress_value["expected_artifact_missing"] = contract_artifacts.value["missing_count"]
            missing_required = int(contract_artifacts.value["missing_count"])
            progress_value["session_recently_responsive"] = bool(age is not None and age <= 30)
            progress_value["task_completion_state"] = (
                "COMPLETE_BY_ARTIFACT"
                if missing_required == 0
                else ("INCOMPLETE_ACTIVE" if active else "INCOMPLETE_IDLE")
            )
            artifacts = Availability(True, {
                **contract_artifacts.value,
                "mutating_tool_calls": trace["mutating_tool_calls"],
                "tool_result_count": trace["tool_result_count"],
            })
        else:
            artifacts = Availability(True, {
                "mutating_tool_calls": trace["mutating_tool_calls"],
                "tool_result_count": trace["tool_result_count"],
            })
        test_result = (
            Availability(True, {"exec_success_count": trace["exec_success_count"]})
            if trace["exec_success_count"] > 0
            else Availability.unsupported("no completed exec result observed")
        )
    else:
        progress_value = {"last_message_age_sec": age, "message_count": len(messages)}
        context = Availability.unsupported(str(trace.get("reason") or "session trace unavailable"))
        safe_recent_events = [
            {
                "role": item.get("role"),
                "text_hash": _trace_hash(" ".join(_message_text(item).split()))[:12],
                "chars": len(_message_text(item)),
                "timestamp": item.get("timestamp"),
            }
            for item in messages[-10:]
            if isinstance(item, dict) and _message_text(item)
        ]
        artifacts = Availability.unsupported("session trace unavailable")
        test_result = Availability.unsupported("session trace unavailable")

    repeat_value = {
        "duplicate_recent_texts": duplicates,
        "assistant_duplicate_outputs": assistant_duplicates,
        "assistant_unique_ratio": assistant_unique_ratio,
        "repeated_tool_calls": trace.get("repeated_tool_calls") if trace_available else None,
        "repeated_results": trace.get("repeated_results") if trace_available else None,
        "unique_tool_count": trace.get("unique_tool_count") if trace_available else None,
        "dominant_tool_ratio": trace.get("dominant_tool_ratio") if trace_available else None,
        "same_failure_pattern": bool(
            trace_available
            and int(trace.get("exec_nonzero_count") or 0) >= 3
            and float(trace.get("dominant_tool_ratio") or 0) >= 0.75
            and int(trace.get("meaningful_result_count") or 0) == 0
        ),
        "count": (
            assistant_duplicates
            + int(trace.get("repeated_tool_calls") or 0)
            + int(trace.get("repeated_results") or 0)
        ),
        "window": len(recent_texts),
    }

    artifact_existing = (
        int(contract_artifacts.value.get("existing_count") or 0)
        if contract_artifacts.available else None
    )
    current_observation = {
        "observed_at": time.time(),
        "tool_call_count": int(trace.get("tool_call_count") or 0) if trace_available else None,
        "tool_result_count": int(trace.get("tool_result_count") or 0) if trace_available else None,
        "meaningful_result_count": int(trace.get("meaningful_result_count") or 0) if trace_available else None,
        "artifact_existing": artifact_existing,
        "message_count": len(messages),
    }
    with _OBSERVATION_LOCK:
        previous = _OBSERVATION_BASELINES.get(session_id)
        _OBSERVATION_BASELINES[session_id] = dict(current_observation)
    if previous:
        elapsed = max(0.0, current_observation["observed_at"] - float(previous["observed_at"]))
        tool_delta = (
            None if current_observation["tool_call_count"] is None or previous.get("tool_call_count") is None
            else current_observation["tool_call_count"] - int(previous["tool_call_count"])
        )
        result_delta = (
            None if current_observation["tool_result_count"] is None or previous.get("tool_result_count") is None
            else current_observation["tool_result_count"] - int(previous["tool_result_count"])
        )
        meaningful_delta = (
            None if current_observation["meaningful_result_count"] is None or previous.get("meaningful_result_count") is None
            else current_observation["meaningful_result_count"] - int(previous["meaningful_result_count"])
        )
        artifact_delta = (
            None if artifact_existing is None or previous.get("artifact_existing") is None
            else artifact_existing - int(previous["artifact_existing"])
        )
        message_delta = len(messages) - int(previous.get("message_count") or 0)
        no_forward_change = bool(
            active
            and elapsed >= 5
            and (tool_delta in {0, None})
            and (result_delta in {0, None})
            and (meaningful_delta in {0, None})
            and (artifact_delta in {0, None})
            and message_delta == 0
        )
        progress_value["observation_delta"] = {
            "elapsed_sec": round(elapsed, 1),
            "tool_call_delta": tool_delta,
            "tool_result_delta": result_delta,
            "meaningful_result_delta": meaningful_delta,
            "artifact_delta": artifact_delta,
            "message_delta": message_delta,
            "no_forward_change": no_forward_change,
        }
        repeat_value["no_forward_change"] = no_forward_change
        repeat_value["no_forward_change_sec"] = round(elapsed, 1) if no_forward_change else 0
    else:
        progress_value["observation_delta"] = {
            "available": False,
            "reason": "first_observation_for_session",
        }

    activity = AgentActivity(
        agent_id=agent_id,
        session_id=session_id,
        status="RUNNING" if active else "IDLE",
        last_meaningful_activity=Availability(True, progress_value),
        active_process=Availability(
            True, {"has_active_run": active, "active_run_count": len(active_ids)}
        ),
        context=context,
        scope=Availability(True, {
            "production": False,
            "destructive": False,
            "external_send": False,
            "unknown": False,
            "purpose": "disposable_test",
        }),
        recent_events=Availability(True, safe_recent_events),
        task_id=task_id,
        recovery_generation=0,
    )
    snapshot = ObserverSnapshot(
        snapshot_id=f"shadow-{now_ms}",
        captured_at=time.time(),
        activity=activity,
        progress=activity.last_meaningful_activity,
        artifacts=artifacts,
        git_diff=Availability.unsupported("session-only shadow probe"),
        checkpoint=Availability.unsupported("session checkpoint not exposed"),
        test_result=test_result,
        repetition=Availability(True, repeat_value),
    )
    return activity, snapshot


def _collect_live_snapshot(*, session_key: str, session_id: str,
                           agent_id: str, task_id: str,
                           work_root: str | Path | None = None,
                           expected_artifacts: tuple[str, ...] = ()) -> tuple[AgentActivity, ObserverSnapshot]:
    bridge = PersistentGatewayBridge()
    try:
        data, _ = bridge.request(
            "chat.history",
            {"sessionKey": session_key, "agentId": agent_id.lower(), "limit": 20},
            timeout_ms=10000,
        )
    finally:
        bridge.close()
    return build_shadow_snapshot(
        data=data,
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        work_root=work_root,
        expected_artifacts=expected_artifacts,
    )


def run_shadow(*, session_key: str, session_id: str,
               agent_id: str, task_id: str,
               work_root: str | Path | None = None,
               expected_artifacts: tuple[str, ...] = ()) -> dict[str, Any]:
    expected_prefix = f"agent:{agent_id.lower()}:war-room-test:"
    if not session_key.startswith(expected_prefix):
        raise ValueError("ONLY_DISPOSABLE_TEST_SESSION_ALLOWED")
    activity, snapshot = _collect_live_snapshot(
        session_key=session_key,
        session_id=session_id,
        agent_id=agent_id,
        task_id=task_id,
        work_root=work_root,
        expected_artifacts=expected_artifacts,
    )
    features = FeatureBuilder().build(snapshot)
    decision = ExistingOpenConnectorHealthJudge().decide(
        feature_state=features.features,
        idempotency_key=f"shadow:{session_id}:{features.state_digest}",
    )
    result = {
        "mode": "SHADOW_ONLY",
        "observed_at": time.time(),
        "task_id": task_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "state_digest": features.state_digest,
        "stable_state_digest": snapshot_state_digest(snapshot),
        "activity": {
            "status": activity.status,
            "progress": activity.last_meaningful_activity.value,
            "active_process": activity.active_process.value,
        },
        "repetition": snapshot.repetition.value,
        "unsupported": {
            "context": not activity.context.available,
            "artifacts": not snapshot.artifacts.available,
            "checkpoint": not snapshot.checkpoint.available,
            "test_result": not snapshot.test_result.available,
        },
        "jev_decision": {
            "choice": decision.choice,
            "confidence": decision.confidence,
            "probabilities": dict(decision.probabilities),
        },
        "mutations": {
            "message_send": 0,
            "session_create": 0,
            "session_stop": 0,
            "recovery_apply": 0,
        },
    }
    SHADOW_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return result


@router.post("/internal/jev-recovery-shadow")
async def shadow_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    body = await request.json()
    try:
        return run_shadow(
            session_key=str(body["session_key"]),
            session_id=str(body["session_id"]),
            agent_id=str(body["agent_id"]),
            task_id=str(body["task_id"]),
        )
    except KeyError as exc:
        raise HTTPException(422, f"missing_{exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def run_safe_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("SAFE_FIXTURE_REQUIRES_QWENTEST")
    adapter = OpenClawSessionAdapter()
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-shadow"
        )
        delivery_id = f"shadow-fixture-{uuid.uuid4()}"
        adapter.bind_delivery(
            delivery_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        receipt = adapter.deliver(
            delivery_id=delivery_id,
            agent_id=agent_id,
            instruction_id=delivery_id,
            body="Reply with exactly SHADOW_OK. Do not use tools. Do not modify files.",
        )
        if receipt.status != "received" or not receipt.run_id:
            raise RuntimeError(f"SAFE_FIXTURE_DELIVERY_FAILED:{receipt.error_code}")

        task_id = f"shadow-safe-fixture-{uuid.uuid4()}"
        time.sleep(1)
        shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
        )

        final = receipt
        deadline = time.time() + 60
        while time.time() < deadline:
            final = adapter.poll(run_id=receipt.run_id, agent_id=agent_id)
            if final.status in {"responded", "failed", "timed_out"}:
                break
            time.sleep(1)
        if final.status != "responded":
            raise RuntimeError(f"SAFE_FIXTURE_NOT_COMPLETED:{final.status}:{final.error_code}")
    finally:
        adapter.close()

    outcome = {
        "type": "shadow_outcome",
        "observed_at": time.time(),
        "task_id": task_id,
        "session_id": session["session_id"],
        "state_digest": shadow["state_digest"],
        "jev_choice": shadow["jev_decision"]["choice"],
        "final_status": final.status,
        "response_body": final.response_body,
        "success": final.status == "responded" and final.response_body == "SHADOW_OK",
    }
    SHADOW_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")

    shadow["fixture"] = {
        "agent_id": agent_id,
        "local_model_only": True,
        "decision_phase": "while_run_active",
        "delivery_status": final.status,
        "response_body": final.response_body,
        "outcome_success": outcome["success"],
    }
    return shadow


@router.post("/internal/jev-recovery-shadow-fixture")
async def shadow_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_safe_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def _wait_for_trace_activity(*, agent_id: str, session_id: str,
                             min_tool_results: int = 1, timeout_seconds: int = 20) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    latest: dict[str, Any] = {"available": False}
    while time.time() < deadline:
        latest = _read_session_trace(agent_id=agent_id, session_id=session_id)
        if latest.get("available") and int(latest.get("tool_result_count") or 0) >= min_tool_results:
            return latest
        time.sleep(0.25)
    return latest


def _wait_for_response(adapter: OpenClawSessionAdapter, *, run_id: str,
                       agent_id: str, timeout_seconds: int = 60):
    final = None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        final = adapter.poll(run_id=run_id, agent_id=agent_id)
        if final.status in {"responded", "failed", "timed_out"}:
            return final
        time.sleep(1)
    return final


def run_repetition_fixture(*, repeats: int = 4, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("REPETITION_FIXTURE_REQUIRES_QWENTEST")
    if repeats < 3 or repeats > 6:
        raise ValueError("REPEATS_OUT_OF_RANGE")
    adapter = OpenClawSessionAdapter()
    responses: list[str | None] = []
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-repetition-shadow"
        )
        for index in range(repeats):
            delivery_id = f"shadow-repeat-{index}-{uuid.uuid4()}"
            adapter.bind_delivery(
                delivery_id,
                session_key=session["session_key"],
                session_id=session["session_id"],
                disposable=True,
                purpose="test",
                agent_id=agent_id,
            )
            receipt = adapter.deliver(
                delivery_id=delivery_id,
                agent_id=agent_id,
                instruction_id=delivery_id,
                body="Reply with exactly LOOP_MARKER. Do not use tools. Do not modify files.",
            )
            if receipt.status != "received" or not receipt.run_id:
                raise RuntimeError(f"REPETITION_DELIVERY_FAILED:{receipt.error_code}")
            final = _wait_for_response(adapter, run_id=receipt.run_id, agent_id=agent_id)
            if final is None or final.status != "responded":
                raise RuntimeError("REPETITION_RESPONSE_FAILED")
            responses.append(final.response_body)

        active_delivery = f"shadow-repeat-active-{uuid.uuid4()}"
        adapter.bind_delivery(
            active_delivery,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        active_receipt = adapter.deliver(
            delivery_id=active_delivery,
            agent_id=agent_id,
            instruction_id=active_delivery,
            body="Reply with exactly LOOP_MARKER. Do not use tools. Do not modify files.",
        )
        if active_receipt.status != "received" or not active_receipt.run_id:
            raise RuntimeError("REPETITION_ACTIVE_DELIVERY_FAILED")
        time.sleep(1)
        task_id = f"shadow-repetition-fixture-{uuid.uuid4()}"
        shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
        )
        active_final = _wait_for_response(
            adapter, run_id=active_receipt.run_id, agent_id=agent_id
        )
    finally:
        adapter.close()

    outcome = {
        "type": "shadow_outcome",
        "observed_at": time.time(),
        "task_id": task_id,
        "session_id": session["session_id"],
        "state_digest": shadow["state_digest"],
        "jev_choice": shadow["jev_decision"]["choice"],
        "fixture": "controlled_repetition",
        "repeats": repeats,
        "final_status": active_final.status if active_final else None,
        "success": bool(
            active_final
            and active_final.status == "responded"
            and active_final.response_body == "LOOP_MARKER"
        ),
    }
    SHADOW_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    shadow["fixture"] = {
        "type": "controlled_repetition",
        "agent_id": agent_id,
        "local_model_only": True,
        "completed_repeats": repeats,
        "responses": responses,
        "decision_phase": "while_repeated_run_active",
        "final_status": active_final.status if active_final else None,
        "final_response": active_final.response_body if active_final else None,
        "outcome_success": outcome["success"],
    }
    return shadow


@router.post("/internal/jev-recovery-shadow-repetition-fixture")
async def repetition_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    body = await request.json()
    repeats = int(body.get("repeats", 4))
    try:
        return run_repetition_fixture(repeats=repeats)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def run_file_work_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("FILE_FIXTURE_REQUIRES_QWENTEST")
    fixture_id = str(uuid.uuid4())
    workdir = FIXTURE_ROOT / fixture_id
    workdir.mkdir(parents=True, exist_ok=False)
    source = workdir / "input.txt"
    source.write_text(" Beta \nalpha\nALPHA\n beta\n", encoding="utf-8")
    expected = "alpha\nbeta\n"

    adapter = OpenClawSessionAdapter()
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-file-work-shadow"
        )
        delivery_id = f"shadow-file-{uuid.uuid4()}"
        adapter.bind_delivery(
            delivery_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        body = (
            f"Safe non-production fixture. Work only inside {workdir}. "
            f"Read {source}. Normalize each non-empty line by trim+lowercase, "
            f"deduplicate, sort ascending, and write exactly to {workdir / 'output.txt'}. "
            "Then verify the output with a shell command. Do not touch any other path. "
            "When complete, reply FILE_FIXTURE_DONE."
        )
        receipt = adapter.deliver(
            delivery_id=delivery_id,
            agent_id=agent_id,
            instruction_id=delivery_id,
            body=body,
        )
        if receipt.status != "received" or not receipt.run_id:
            raise RuntimeError(f"FILE_FIXTURE_DELIVERY_FAILED:{receipt.error_code}")
        task_id = f"shadow-file-fixture-{fixture_id}"
        _wait_for_trace_activity(
            agent_id=agent_id,
            session_id=session["session_id"],
            min_tool_results=1,
            timeout_seconds=30,
        )
        shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
        final = _wait_for_response(adapter, run_id=receipt.run_id, agent_id=agent_id, timeout_seconds=90)
        if final is None:
            raise RuntimeError("FILE_FIXTURE_NO_FINAL")
    finally:
        adapter.close()

    output = workdir / "output.txt"
    actual = output.read_text(encoding="utf-8") if output.exists() else None
    success = final.status == "responded" and actual == expected
    outcome = {
        "type": "shadow_outcome",
        "fixture": "real_file_work",
        "observed_at": time.time(),
        "task_id": task_id,
        "session_id": session["session_id"],
        "state_digest": shadow["state_digest"],
        "jev_choice": shadow["jev_decision"]["choice"],
        "final_status": final.status,
        "response_body": final.response_body,
        "output_exists": output.exists(),
        "output_matches_expected": actual == expected,
        "success": success,
        "workdir": str(workdir),
    }
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    shadow["fixture"] = outcome
    return shadow


@router.post("/internal/jev-recovery-shadow-file-fixture")
async def file_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_file_work_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def run_tool_loop_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("TOOL_LOOP_FIXTURE_REQUIRES_QWENTEST")
    fixture_id = str(uuid.uuid4())
    workdir = FIXTURE_ROOT / f"loop-{fixture_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    source = workdir / "input.txt"
    source.write_text("alpha\nbeta\n", encoding="utf-8")

    adapter = OpenClawSessionAdapter()
    stop_receipt = None
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-tool-loop-shadow"
        )
        delivery_id = f"shadow-tool-loop-{uuid.uuid4()}"
        adapter.bind_delivery(
            delivery_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        exact_cmd = f"cd {workdir} && grep -n '^OMEGA$' input.txt"
        body = (
            "Safe non-production loop fixture. Do not modify files. "
            f"Run exactly this same exec command repeatedly until it succeeds: {exact_cmd}. "
            "If it fails, immediately run the identical command again. "
            "Do not switch methods and do not conclude NOT_FOUND."
        )
        receipt = adapter.deliver(
            delivery_id=delivery_id,
            agent_id=agent_id,
            instruction_id=delivery_id,
            body=body,
        )
        if receipt.status != "received" or not receipt.run_id:
            raise RuntimeError(f"TOOL_LOOP_DELIVERY_FAILED:{receipt.error_code}")

        deadline = time.time() + 45
        trace = {"available": False}
        while time.time() < deadline:
            trace = _read_session_trace(agent_id=agent_id, session_id=session["session_id"])
            if (
                trace.get("available")
                and int(trace.get("repeated_tool_calls") or 0) >= 2
                and int(trace.get("tool_result_count") or 0) >= 3
            ):
                break
            time.sleep(0.25)

        task_id = f"shadow-tool-loop-fixture-{fixture_id}"
        shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
        )
        if shadow["activity"]["active_process"]["has_active_run"]:
            stop_receipt = adapter.stop(delivery_id=delivery_id, agent_id=agent_id)
    finally:
        adapter.close()

    outcome = {
        "type": "shadow_outcome",
        "fixture": "real_tool_loop",
        "observed_at": time.time(),
        "task_id": task_id,
        "session_id": session["session_id"],
        "state_digest": shadow["state_digest"],
        "jev_choice": shadow["jev_decision"]["choice"],
        "trace": {
            "tool_call_count": trace.get("tool_call_count"),
            "tool_result_count": trace.get("tool_result_count"),
            "repeated_tool_calls": trace.get("repeated_tool_calls"),
            "repeated_results": trace.get("repeated_results"),
            "tool_error_count": trace.get("tool_error_count"),
        },
        "cleanup_stop_status": getattr(stop_receipt, "status", None),
        "success": shadow["jev_decision"]["choice"] in {"SALVAGE", "WATCH"},
    }
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    shadow["fixture"] = outcome
    return shadow


@router.post("/internal/jev-recovery-shadow-tool-loop-fixture")
async def tool_loop_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_tool_loop_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def run_stall_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("STALL_FIXTURE_REQUIRES_QWENTEST")
    fixture_id = str(uuid.uuid4())
    workdir = FIXTURE_ROOT / f"stall-{fixture_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    process_script = workdir / "process.py"
    process_script.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(90)\n"
        "Path('output.txt').write_text('done\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )

    adapter = OpenClawSessionAdapter()
    stop_receipt = None
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-stall-shadow"
        )
        delivery_id = f"shadow-stall-{uuid.uuid4()}"
        adapter.bind_delivery(
            delivery_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        body = (
            "Safe non-production stall fixture. Work only inside "
            f"{workdir}. Run python3 process.py from that directory. "
            "The script is expected to generate output.txt. After the command finishes, "
            "verify output.txt and reply STALL_DONE. Do not modify process.py or use another method."
        )
        receipt = adapter.deliver(
            delivery_id=delivery_id,
            agent_id=agent_id,
            instruction_id=delivery_id,
            body=body,
        )
        if receipt.status != "received" or not receipt.run_id:
            raise RuntimeError(f"STALL_DELIVERY_FAILED:{receipt.error_code}")

        deadline = time.time() + 25
        trace = {"available": False}
        while time.time() < deadline:
            trace = _read_session_trace(agent_id=agent_id, session_id=session["session_id"])
            if trace.get("available") and int(trace.get("inflight_tool_calls") or 0) >= 1:
                break
            time.sleep(0.25)
        time.sleep(12)
        task_id = f"shadow-stall-fixture-{fixture_id}"
        first = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id + "-early",
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
        time.sleep(12)
        second = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id + "-late",
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
        if second["activity"]["active_process"]["has_active_run"]:
            stop_receipt = adapter.stop(delivery_id=delivery_id, agent_id=agent_id)
    finally:
        adapter.close()

    outcome = {
        "type": "shadow_outcome",
        "fixture": "real_stall",
        "observed_at": time.time(),
        "session_id": session["session_id"],
        "early_choice": first["jev_decision"]["choice"],
        "early_confidence": first["jev_decision"]["confidence"],
        "late_choice": second["jev_decision"]["choice"],
        "late_confidence": second["jev_decision"]["confidence"],
        "early_progress": first["activity"]["progress"],
        "late_progress": second["activity"]["progress"],
        "cleanup_stop_status": getattr(stop_receipt, "status", None),
        "success": second["jev_decision"]["choice"] in {"WATCH", "SALVAGE"},
    }
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    return {"early": first, "late": second, "fixture": outcome}


@router.post("/internal/jev-recovery-shadow-stall-fixture")
async def stall_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_stall_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def run_forced_same_run_loop_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("FORCED_LOOP_REQUIRES_QWENTEST")
    fixture_id = str(uuid.uuid4())
    workdir = FIXTURE_ROOT / f"forced-loop-{fixture_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    (workdir / "input.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    exact_cmd = f"cd {workdir} && grep -n '^OMEGA$' input.txt"

    adapter = OpenClawSessionAdapter()
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-forced-loop-shadow"
        )
        delivery_id = f"forced-loop-{uuid.uuid4()}"
        adapter.bind_delivery(
            delivery_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        body = (
            "This is a safe watchdog loop simulation. Use exec exactly four separate times. "
            f"Each of the four exec tool calls must use this exact command with no changes: {exact_cmd}. "
            "Do not use any other tool, do not change strategy, and do not modify files. "
            "After the fourth identical failed result, reply LOOP_SIM_DONE."
        )
        receipt = adapter.deliver(
            delivery_id=delivery_id, agent_id=agent_id,
            instruction_id=delivery_id, body=body,
        )
        if receipt.status != "received" or not receipt.run_id:
            raise RuntimeError("FORCED_LOOP_DELIVERY_FAILED")

        deadline = time.time() + 60
        trace = {"available": False}
        while time.time() < deadline:
            trace = _read_session_trace(agent_id=agent_id, session_id=session["session_id"])
            if (
                trace.get("available")
                and int(trace.get("tool_result_count") or 0) >= 4
            ):
                break
            time.sleep(0.25)

        task_id = f"forced-loop-{fixture_id}"
        shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
            work_root=workdir,
        )
        final = _wait_for_response(
            adapter, run_id=receipt.run_id, agent_id=agent_id, timeout_seconds=30
        )
    finally:
        adapter.close()

    outcome = {
        "type": "shadow_outcome",
        "fixture": "forced_same_run_loop",
        "observed_at": time.time(),
        "task_id": task_id,
        "session_id": session["session_id"],
        "state_digest": shadow["state_digest"],
        "jev_choice": shadow["jev_decision"]["choice"],
        "trace": {
            "tool_call_count": trace.get("tool_call_count"),
            "tool_result_count": trace.get("tool_result_count"),
            "exec_nonzero_count": trace.get("exec_nonzero_count"),
            "repeated_tool_calls": trace.get("repeated_tool_calls"),
            "repeated_results": trace.get("repeated_results"),
            "dominant_tool_ratio": trace.get("dominant_tool_ratio"),
            "meaningful_result_count": trace.get("meaningful_result_count"),
        },
        "final_status": getattr(final, "status", None),
        "success": shadow["jev_decision"]["choice"] in {"SALVAGE", "WATCH"},
    }
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    shadow["fixture"] = outcome
    return shadow


@router.post("/internal/jev-recovery-shadow-forced-loop-fixture")
async def forced_loop_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_forced_same_run_loop_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def run_unfinished_responsive_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("UNFINISHED_FIXTURE_REQUIRES_QWENTEST")
    fixture_id = str(uuid.uuid4())
    workdir = FIXTURE_ROOT / f"unfinished-{fixture_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    source = workdir / "input.txt"
    source.write_text("gamma\nalpha\n", encoding="utf-8")

    adapter = OpenClawSessionAdapter()
    try:
        session = adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-unfinished-shadow"
        )

        first_id = f"unfinished-first-{uuid.uuid4()}"
        adapter.bind_delivery(
            first_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        first = adapter.deliver(
            delivery_id=first_id,
            agent_id=agent_id,
            instruction_id=first_id,
            body=(
                f"Safe non-production fixture. Work only inside {workdir}. "
                f"Read {source}, but intentionally do not create output.txt and do not finish the task. "
                "After inspection reply exactly NEED_MORE_INPUT."
            ),
        )
        if first.status != "received" or not first.run_id:
            raise RuntimeError("UNFINISHED_FIRST_DELIVERY_FAILED")
        first_final = _wait_for_response(
            adapter, run_id=first.run_id, agent_id=agent_id, timeout_seconds=60
        )
        if first_final is None or first_final.status != "responded":
            raise RuntimeError("UNFINISHED_FIRST_RESPONSE_FAILED")

        probe_id = f"unfinished-probe-{uuid.uuid4()}"
        adapter.bind_delivery(
            probe_id,
            session_key=session["session_key"],
            session_id=session["session_id"],
            disposable=True,
            purpose="test",
            agent_id=agent_id,
        )
        probe = adapter.deliver(
            delivery_id=probe_id,
            agent_id=agent_id,
            instruction_id=probe_id,
            body=(
                "Health probe only. Do not resume or complete the previous task. "
                "Reply exactly ALIVE_ACK and do not use tools."
            ),
        )
        if probe.status != "received" or not probe.run_id:
            raise RuntimeError("UNFINISHED_PROBE_DELIVERY_FAILED")
        probe_final = _wait_for_response(
            adapter, run_id=probe.run_id, agent_id=agent_id, timeout_seconds=60
        )
        if probe_final is None or probe_final.status != "responded":
            raise RuntimeError("UNFINISHED_PROBE_RESPONSE_FAILED")

        task_id = f"unfinished-responsive-{fixture_id}"
        first_shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id + "-first",
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
        time.sleep(10)
        second_shadow = run_shadow(
            session_key=session["session_key"],
            session_id=session["session_id"],
            agent_id=agent_id,
            task_id=task_id + "-second",
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
    finally:
        adapter.close()

    output_exists = (workdir / "output.txt").exists()
    outcome = {
        "type": "shadow_outcome",
        "fixture": "unfinished_but_responsive",
        "observed_at": time.time(),
        "task_id": task_id,
        "session_id": session["session_id"],
        "first_state_digest": first_shadow["state_digest"],
        "second_state_digest": second_shadow["state_digest"],
        "first_choice": first_shadow["jev_decision"]["choice"],
        "second_choice": second_shadow["jev_decision"]["choice"],
        "first_response": first_final.response_body,
        "probe_response": probe_final.response_body,
        "output_exists": output_exists,
        "session_responsive": probe_final.response_body == "ALIVE_ACK",
        "task_incomplete": not output_exists,
        "success": (
            probe_final.response_body == "ALIVE_ACK"
            and not output_exists
            and first_shadow["jev_decision"]["choice"] in {"WATCH", "SALVAGE"}
            and second_shadow["jev_decision"]["choice"] == "SALVAGE"
        ),
    }
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    return {"first": first_shadow, "second": second_shadow, "fixture": outcome}


@router.post("/internal/jev-recovery-shadow-unfinished-fixture")
async def unfinished_fixture_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_unfinished_responsive_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def _deliver_wait(adapter: OpenClawSessionAdapter, *, session: dict[str, Any],
                  agent_id: str, body: str, label: str, timeout_seconds: int = 90):
    delivery_id = f"{label}-{uuid.uuid4()}"
    adapter.bind_delivery(
        delivery_id,
        session_key=session["session_key"],
        session_id=session["session_id"],
        disposable=True,
        purpose="test",
        agent_id=agent_id,
    )
    receipt = adapter.deliver(
        delivery_id=delivery_id,
        agent_id=agent_id,
        instruction_id=delivery_id,
        body=body,
    )
    if receipt.status != "received" or not receipt.run_id:
        raise RuntimeError(f"{label.upper()}_DELIVERY_FAILED:{receipt.error_code}")
    final = _wait_for_response(
        adapter, run_id=receipt.run_id, agent_id=agent_id,
        timeout_seconds=timeout_seconds,
    )
    if final is None or final.status != "responded":
        raise RuntimeError(f"{label.upper()}_RESPONSE_FAILED")
    return final


def run_safe_salvage_recovery_fixture(*, agent_id: str = "qwentest") -> dict[str, Any]:
    if agent_id.lower() != "qwentest":
        raise ValueError("SAFE_RECOVERY_REQUIRES_QWENTEST")
    fixture_id = str(uuid.uuid4())
    workdir = FIXTURE_ROOT / f"recovery-{fixture_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    source_file = workdir / "input.txt"
    source_file.write_text(" Beta \nalpha\nALPHA\n beta\n", encoding="utf-8")
    output_file = workdir / "output.txt"
    handoff_path = workdir / "RECOVERY_HANDOFF.md"
    expected = "alpha\nbeta\n"
    exact_cmd = f"cd {workdir} && grep -n '^OMEGA$' input.txt"

    source_adapter = OpenClawSessionAdapter()
    target_adapter = OpenClawSessionAdapter()
    source_session = None
    target_session = None
    try:
        source_session = source_adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-auto-salvage"
        )
        source_task = (
            f"Safe non-production recovery fixture. Final goal: read {source_file}, normalize "
            f"non-empty lines by trim+lowercase, deduplicate and sort, then write {output_file}. "
            "For this recovery simulation, do NOT create output.txt yet. First run exactly four "
            f"separate exec calls using this identical command: {exact_cmd}. "
            "Do not change strategy. After the fourth failed result reply exactly LOOP_BLOCKED."
        )
        source_final = _deliver_wait(
            source_adapter, session=source_session, agent_id=agent_id,
            body=source_task, label="recovery-source", timeout_seconds=90,
        )

        task_id = f"safe-recovery-{fixture_id}"
        decision = run_shadow(
            session_key=source_session["session_key"],
            session_id=source_session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
        if decision["jev_decision"]["choice"] != "SALVAGE":
            raise RuntimeError(
                f"RECOVERY_EXPECTED_SALVAGE:{decision['jev_decision']['choice']}"
            )

        _, fresh_snapshot = _collect_live_snapshot(
            session_key=source_session["session_key"],
            session_id=source_session["session_id"],
            agent_id=agent_id,
            task_id=task_id,
            work_root=workdir,
            expected_artifacts=("output.txt",),
        )
        fresh_stable_digest = snapshot_state_digest(fresh_snapshot)
        if fresh_stable_digest != decision["stable_state_digest"]:
            raise RuntimeError("RECOVERY_STALE_DECISION_REJECTED")

        salvage_prompt = (
            "RECOVERY MODE. Stop trying to solve the original task. Do not create output.txt. "
            f"Write {handoff_path} and only that recovery file. It must contain these exact "
            "non-empty markdown sections: ## Task, ## Previous Agent, ## Session ID, "
            "## Recovery Generation, ## Completed Work, ## Changed Files, ## Test Results, "
            "## Current Problem, ## Failed Methods, ## Remaining Work, ## Next Agent First Action. "
            f"Task must state the original normalization goal. Previous Agent={agent_id}. "
            f"Session ID={source_session['session_id']}. Recovery Generation=1. "
            "Record the repeated failed grep method, that output.txt is still missing, and that "
            "remaining work is to normalize input.txt and create output.txt. "
            "After writing the handoff reply exactly HANDOFF_WRITTEN."
        )
        salvage_final = _deliver_wait(
            source_adapter, session=source_session, agent_id=agent_id,
            body=salvage_prompt, label="recovery-salvage", timeout_seconds=90,
        )
        if not handoff_path.is_file():
            raise RuntimeError("RECOVERY_HANDOFF_NOT_CREATED")
        HandoffWriter(workdir).validate(handoff_path)
        target_session = target_adapter.create_disposable_session(
            agent_id=agent_id, project_id="jev-recovery-auto-salvage-target"
        )
        ack_prompt = (
            f"Read {handoff_path}. Do not perform the remaining work yet. "
            "If the handoff is readable and contains the remaining work, reply exactly HANDOFF_ACK."
        )
        ack_final = _deliver_wait(
            target_adapter, session=target_session, agent_id=agent_id,
            body=ack_prompt, label="recovery-ack", timeout_seconds=60,
        )
        if ack_final.response_body != "HANDOFF_ACK":
            raise RuntimeError("RECOVERY_HANDOFF_ACK_INVALID")

        logical_close = {
            "source_session_id": source_session["session_id"],
            "closed_after_ack": True,
            "force_kill": False,
            "delete": False,
        }

        resume_prompt = (
            f"Continue the original task using only {handoff_path} and current files in {workdir}. "
            f"Read the handoff and {source_file}. Complete the remaining work by writing exactly "
            f"{output_file}: normalized trim+lowercase unique sorted lines. Verify it with a shell "
            "command. Do not modify RECOVERY_HANDOFF.md. Reply exactly RECOVERY_DONE when complete."
        )
        target_final = _deliver_wait(
            target_adapter, session=target_session, agent_id=agent_id,
            body=resume_prompt, label="recovery-resume", timeout_seconds=90,
        )
    finally:
        source_adapter.close()
        target_adapter.close()

    actual = output_file.read_text(encoding="utf-8") if output_file.exists() else None
    success = (
        source_final.response_body == "LOOP_BLOCKED"
        and salvage_final.response_body == "HANDOFF_WRITTEN"
        and ack_final.response_body == "HANDOFF_ACK"
        and target_final.response_body == "RECOVERY_DONE"
        and actual == expected
    )
    outcome = {
        "type": "recovery_outcome",
        "fixture": "safe_salvage_recovery",
        "observed_at": time.time(),
        "task_id": task_id,
        "source_session_id": source_session["session_id"],
        "target_session_id": target_session["session_id"],
        "recovery_generation": 1,
        "jev_choice": decision["jev_decision"]["choice"],
        "jev_confidence": decision["jev_decision"]["confidence"],
        "stale_check_pass": True,
        "decision_stable_digest": decision["stable_state_digest"],
        "execution_stable_digest": fresh_stable_digest,
        "handoff_valid": True,
        "handoff_bytes": handoff_path.stat().st_size,
        "handoff_ack": ack_final.response_body,
        "logical_close": logical_close,
        "target_status": target_final.status,
        "output_exists": output_file.exists(),
        "output_matches_expected": actual == expected,
        "success": success,
        "workdir": str(workdir),
    }
    SHADOW_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
    if success and outcome["stale_check_pass"] and outcome["handoff_valid"]:
        GOLD_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        gold = {
            "type": "gold_recovery",
            "observed_at": outcome["observed_at"],
            "task_id": task_id,
            "scenario": "safe_salvage_recovery",
            "expected_choice": "SALVAGE",
            "actual_choice": outcome["jev_choice"],
            "decision_correct": outcome["jev_choice"] == "SALVAGE",
            "recovery_success": True,
            "stale_check_pass": True,
            "handoff_valid": True,
            "output_matches_expected": True,
            "source_session_id": source_session["session_id"],
            "target_session_id": target_session["session_id"],
        }
        with GOLD_HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(gold, ensure_ascii=False, sort_keys=True) + "\n")
    return {"decision": decision, "outcome": outcome}


@router.post("/internal/jev-recovery-safe-auto-salvage-fixture")
async def safe_auto_salvage_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    try:
        return run_safe_salvage_recovery_fixture()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc


def recovery_shadow_stats() -> dict[str, Any]:
    choices = {name: 0 for name in ("CONTINUE", "WATCH", "SALVAGE", "DEAD")}
    total_decisions = 0
    recovery_total = 0
    recovery_success = 0
    fixture_total = 0
    fixture_success = 0
    paired_by_choice = {
        name: {"outcomes": 0, "success": 0}
        for name in choices
    }
    if not SHADOW_HISTORY.exists():
        return {
            "total_decisions": 0,
            "choices": choices,
            "recovery": {"total": 0, "success": 0, "success_rate": None},
            "fixtures": {"total": 0, "success": 0, "success_rate": None},
            "paired_by_choice": paired_by_choice,
        }

    decisions_by_task: dict[str, str] = {}
    for raw in SHADOW_HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("mode") == "SHADOW_ONLY" and isinstance(row.get("jev_decision"), dict):
            choice = str(row["jev_decision"].get("choice") or "")
            if choice in choices:
                choices[choice] += 1
                total_decisions += 1
                task_id = str(row.get("task_id") or "")
                if task_id:
                    decisions_by_task[task_id] = choice
            continue
        if row.get("type") not in {"shadow_outcome", "recovery_outcome"}:
            continue
        success = row.get("success")
        if not isinstance(success, bool):
            continue
        fixture_total += 1
        fixture_success += int(success)
        if row.get("type") == "recovery_outcome":
            recovery_total += 1
            recovery_success += int(success)
        task_id = str(row.get("task_id") or "")
        choice = str(row.get("jev_choice") or decisions_by_task.get(task_id) or "")
        if choice in paired_by_choice:
            paired_by_choice[choice]["outcomes"] += 1
            paired_by_choice[choice]["success"] += int(success)

    for value in paired_by_choice.values():
        total = value["outcomes"]
        value["success_rate"] = round(value["success"] / total, 3) if total else None
    return {
        "total_decisions": total_decisions,
        "choices": choices,
        "recovery": {
            "total": recovery_total,
            "success": recovery_success,
            "success_rate": round(recovery_success / recovery_total, 3) if recovery_total else None,
        },
        "fixtures": {
            "total": fixture_total,
            "success": fixture_success,
            "success_rate": round(fixture_success / fixture_total, 3) if fixture_total else None,
        },
        "paired_by_choice": paired_by_choice,
    }


@router.get("/internal/jev-recovery-shadow-stats")
async def shadow_stats_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    return recovery_shadow_stats()


def recovery_gold_stats() -> dict[str, Any]:
    total = correct = recovered = 0
    rows: list[dict[str, Any]] = []
    if GOLD_HISTORY.exists():
        for raw in GOLD_HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("type") != "gold_recovery":
                continue
            total += 1
            correct += int(row.get("decision_correct") is True)
            recovered += int(row.get("recovery_success") is True)
            rows.append({
                "task_id": row.get("task_id"),
                "scenario": row.get("scenario"),
                "expected_choice": row.get("expected_choice"),
                "actual_choice": row.get("actual_choice"),
                "decision_correct": row.get("decision_correct"),
                "recovery_success": row.get("recovery_success"),
                "observed_at": row.get("observed_at"),
            })
    return {
        "gold_total": total,
        "decision_correct": correct,
        "decision_accuracy": round(correct / total, 3) if total else None,
        "recovery_success": recovered,
        "recovery_success_rate": round(recovered / total, 3) if total else None,
        "recent": rows[-10:],
    }


@router.get("/internal/jev-recovery-gold-stats")
async def gold_stats_endpoint(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost_only")
    return recovery_gold_stats()
