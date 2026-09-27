from __future__ import annotations

import json
import hashlib
import hmac
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import war_room
from war_room import router as war_room_router
from war_room_actions import router as war_room_actions_router
from jev_task_router import router as jev_task_router, provision_schema as provision_jev_schema
from jev_result_verifier import router as jev_result_verifier_router, provision_schema as provision_result_verifier_schema
from jev_recovery_shadow import router as jev_recovery_shadow_router
from jev_recovery_live import LIVE_HISTORY as JEV_LIVE_HISTORY
from jev_recovery_live import router as jev_recovery_live_router
from fast_gateway_api import router as fast_gateway_router
from war_room_command_center import router as war_room_command_center_router


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="PLACHEM AI Server Monitor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/vocal-coach", StaticFiles(directory=STATIC_DIR / "vocal-coach", html=True), name="vocal-coach")
app.include_router(war_room_router)
app.include_router(war_room_actions_router)
app.include_router(jev_task_router)
app.include_router(jev_result_verifier_router)
app.include_router(jev_recovery_shadow_router)
app.include_router(jev_recovery_live_router)
app.include_router(fast_gateway_router)
app.include_router(war_room_command_center_router)


@app.on_event("startup")
def provision_war_room_on_startup() -> None:
    """Idempotently provision the local War Room store before serving UI/API."""
    war_room.provision_database()
    provision_jev_schema()
    provision_result_verifier_schema()
    if os.environ.get("PLACHEM_WAR_ROOM_REAL_ADAPTER") == "1" and os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER") != "1":
        from war_room_runtime import get_runtime
        get_runtime().start(db_path=war_room._db_path())


@app.on_event("shutdown")
def stop_war_room_runtime() -> None:
    from fast_gateway_service import close_persistent_harnesses
    try:
        if os.environ.get("PLACHEM_WAR_ROOM_REAL_ADAPTER") == "1" and os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER") != "1":
            from war_room_runtime import get_runtime
            get_runtime().close()
    finally:
        # Phase 2 can own a Harness even when the delivery worker is disabled.
        close_persistent_harnesses()


def _war_room_principal(request: Request) -> str | None:
    """Resolve a trusted reverse-proxy, signed-session, or server token principal.

    The request object carries the trusted proxy headers and the signed server
    session cookie; the server-side token map is consulted only after those
    effective authentication paths have not resolved an identity.  This keeps
    the token/actor path exactly as the first effective path while the
    login-to-representative mapping is applied only after an identity has been
    authenticated and resolved.
    """
    resolved = war_room._request_principal(request, None, None)
    if resolved is not None:
        return resolved
    token = request.headers.get("X-War-Room-Token")
    actor = request.headers.get("X-War-Room-Actor")
    if not token and not actor:
        return None
    return war_room._request_principal(None, actor, token)


def _war_room_read_project(path: str) -> str | None:
    parts = path.split("/")
    # /api/war-room/projects/{project_id}/...
    if len(parts) >= 5 and parts[3] == "projects":
        return parts[4]
    return None


@app.middleware("http")
async def war_room_read_rbac(request: Request, call_next):
    """Require authenticated membership for every War Room GET route.

    This middleware is intentionally HTTP-only, so the pure read-model
    functions remain usable by isolated unit tests without bypassing production
    route protection.
    """
    if request.url.path == "/war-room":
        response = await call_next(request)
        proxy_secret = os.environ.get("PLACHEM_WAR_ROOM_REVERSE_PROXY_SECRET", "")
        proxy_principal = request.headers.get("X-Authenticated-Principal") or request.headers.get("X-Forwarded-User")
        presented_secret = request.headers.get("X-War-Room-Proxy-Secret")
        if (
            proxy_secret
            and proxy_principal
            and hmac.compare_digest(proxy_secret, presented_secret or "")
            and (war_room._known_principal(proxy_principal) or war_room._resolve_trusted_principal(proxy_principal))
            and os.environ.get("PLACHEM_WAR_ROOM_SESSION_SECRET")
        ):
            session_secret = os.environ["PLACHEM_WAR_ROOM_SESSION_SECRET"]
            mapped = war_room._resolve_trusted_principal(proxy_principal) or proxy_principal
            signature = hmac.new(session_secret.encode(), mapped.encode(), hashlib.sha256).hexdigest()
            response.set_cookie("war_room_session", f"{mapped}.{signature}", httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
        return response
    if request.url.path.startswith("/api/war-room"):
        principal = _war_room_principal(request)
        if principal is None:
            return JSONResponse({"detail": "War Room authentication required"}, status_code=401)
        if request.url.path == "/api/war-room/demo-mode":
            return await call_next(request)
    if request.url.path.startswith("/api/war-room") and request.method == "GET":
        import sqlite3
        project_id = _war_room_read_project(request.url.path)
        db_path = war_room._db_path()
        if not db_path.is_file():
            return JSONResponse({"detail": "War Room data unavailable"}, status_code=503)
        try:
            with sqlite3.connect(db_path) as con:
                con.row_factory = sqlite3.Row
                if project_id is None and request.url.path.rstrip("/") == "/api/war-room/projects":
                    allowed = con.execute("SELECT 1 FROM war_participants WHERE principal_id=? AND can_read=1 AND active=1 LIMIT 1", (principal,)).fetchone()
                elif project_id is not None:
                    allowed = con.execute("SELECT 1 FROM war_participants WHERE project_id=? AND principal_id=? AND can_read=1 AND active=1", (project_id, principal)).fetchone()
                else:
                    # Task/message detail routes resolve their project through
                    # the referenced row before applying membership.
                    ref = request.url.path.split("/")
                    allowed = None
                    if "tasks" in ref:
                        task_id = ref[ref.index("tasks") + 1]
                        allowed = con.execute("SELECT 1 FROM war_participants p JOIN war_tasks t ON t.project_id=p.project_id WHERE t.id=? AND p.principal_id=? AND p.can_read=1 AND p.active=1", (task_id, principal)).fetchone()
                    elif "messages" in ref:
                        message_id = ref[ref.index("messages") + 1]
                        allowed = con.execute("SELECT 1 FROM war_participants p JOIN war_messages m ON m.project_id=p.project_id WHERE m.id=? AND p.principal_id=? AND p.can_read=1 AND p.active=1", (message_id, principal)).fetchone()
                    elif "documents" in ref:
                        document_id = ref[ref.index("documents") + 1]
                        allowed = con.execute("SELECT 1 FROM war_participants p JOIN war_documents d ON d.project_id=p.project_id WHERE d.id=? AND p.principal_id=? AND p.can_read=1 AND p.active=1", (document_id, principal)).fetchone()
                if not allowed:
                    return JSONResponse({"detail": "War Room permission denied"}, status_code=403)
        except sqlite3.Error:
            return JSONResponse({"detail": "War Room data unavailable"}, status_code=503)
    return await call_next(request)

_network_prev: dict[str, float | int] | None = None
_runtime_status_cache: dict[str, Any] = {}
_runtime_status_cache_at = 0.0
_runtime_status_cache_lock = threading.Lock()
_RUNTIME_STATUS_CACHE_TTL_SECONDS = 10.0

TOKEN_TELEMETRY_DB_PATH = Path(
    os.environ.get(
        "PLACHEM_CONTEXT_INDEX_DB",
        "/home/plachem-sever/.openclaw/workspace/local_agents/context_index_v3/03_WORK/data/context_index_v3.sqlite",
    )
)
TOKEN_TELEMETRY_MAX_AGE_SECONDS = int(os.environ.get("PLACHEM_TOKEN_TELEMETRY_MAX_AGE_SECONDS", "900"))


def read_token_telemetry(agent_id: str, session_id: str | None = None) -> dict[str, Any]:
    """Read latest deterministic token telemetry without influencing Agent state."""
    result: dict[str, Any] = {
        "available": False,
        "status": "UNAVAILABLE",
        "source": None,
        "sampled_at": None,
        "sample_age_seconds": None,
        "session_id": session_id,
    }
    if not TOKEN_TELEMETRY_DB_PATH.is_file():
        result["error"] = "telemetry_db_missing"
        return result
    try:
        con = sqlite3.connect(f"file:{TOKEN_TELEMETRY_DB_PATH}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            session = None
            if session_id:
                session = con.execute(
                    """SELECT * FROM token_telemetry_samples
                       WHERE lower(agent)=lower(?) AND session_id=?
                       ORDER BY sampled_at_epoch DESC,sample_id DESC LIMIT 1""",
                    (agent_id, session_id),
                ).fetchone()
            agent = con.execute(
                """SELECT * FROM token_telemetry_agent_samples
                   WHERE lower(agent)=lower(?)
                   ORDER BY sampled_at_epoch DESC,sample_id DESC LIMIT 1""",
                (agent_id,),
            ).fetchone()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        result["error"] = type(exc).__name__
        return result

    row = session or agent
    if row is None:
        result["error"] = "telemetry_sample_missing"
        return result

    sampled_epoch = int(row["sampled_at_epoch"] or 0)
    age = max(0, int(time.time()) - sampled_epoch) if sampled_epoch else None
    fresh = age is None or age <= TOKEN_TELEMETRY_MAX_AGE_SECONDS
    result.update({
        "available": fresh,
        "status": str(row["classification"] or "NORMAL") if fresh else "UNAVAILABLE",
        "source": "session" if session is not None else "agent",
        "sampled_at": row["sampled_at"],
        "sample_age_seconds": age,
    })
    if session is not None:
        pressure = row["context_pressure"]
        result.update({
            "session_id": row["session_id"],
            "model": row["model_key"],
            "current_prompt_tokens": int(row["current_prompt_tokens"] or 0),
            "max_prompt_tokens": int(row["max_prompt_tokens"] or 0),
            "prompt_source": row["prompt_source"] if "prompt_source" in row.keys() else None,
            "prompt_reliable": bool(row["prompt_reliable"]) if "prompt_reliable" in row.keys() else False,
            "context_limit": int(row["context_limit"]) if row["context_limit"] is not None else None,
            "context_pressure": round(float(pressure), 4) if pressure is not None else None,
            "delta_processed": int(row["delta_processed"] or 0),
            "velocity_tokens_per_hour": round(float(row["velocity_tokens_per_hour"] or 0), 1),
            "calls_per_hour": round(float(row["calls_per_hour"] or 0), 1),
            "cache_ratio": round(float(row["delta_cache_ratio"]), 4) if row["delta_cache_ratio"] is not None else None,
            "reasons": json.loads(row["reasons_json"] or "[]"),
        })
    if agent is not None:
        result.update({
            "processed_24h": int(agent["processed_24h"] or 0),
            "calls_24h": int(agent["calls_24h"] or 0),
            "fresh_input_24h": int(agent["fresh_input_24h"] or 0),
            "cache_read_24h": int(agent["cache_read_24h"] or 0),
            "output_24h": int(agent["output_24h"] or 0),
        })
    return result


def pct(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def bytes_to_gb(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024**3), 1)


def safe_run(args: list[str], timeout: float = 1.5) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None


SENSITIVE_KEY_PATTERN = re.compile(r"(token|secret|api[-_]?key|password|credential|private[-_]?key)", re.I)

def openclaw_home() -> Path:
    return Path(os.getenv("OPENCLAW_HOME", str(Path.home() / ".openclaw"))).expanduser()


def read_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                result[str(key)] = item if item is None or item == "" else "***REDACTED***"
            else:
                result[str(key)] = redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def normalize_model_ref(value: Any, fallback: str = "unknown") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("primary", "default", "id", "name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return fallback


def read_identity_value(paths: list[Path], label: str) -> str | None:
    pattern = re.compile(rf"\*\*{re.escape(label)}:\*\*\s*(.+)", re.I)
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in content.splitlines():
            match = pattern.search(line)
            if match:
                value = match.group(1).strip().strip("_*` ")
                if value:
                    return value
    return None


def identity_paths(home: Path, agent_id: str, agent: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in ("agentDir", "workspace"):
        raw = agent.get(key)
        if isinstance(raw, str) and raw:
            paths.append(Path(raw).expanduser() / "IDENTITY.md")
    paths.extend([home / "agents" / agent_id / "agent" / "IDENTITY.md", home / f"workspace-{agent_id}" / "IDENTITY.md"])
    if agent_id == "main":
        paths.append(home / "workspace" / "IDENTITY.md")
    return paths


def configured_openclaw_agents(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only agents registered in the current OpenClaw config schema."""
    agents_config = config.get("agents", {}) if isinstance(config.get("agents"), dict) else {}
    entries = agents_config.get("entries")
    if isinstance(entries, dict):
        return {
            str(agent_id): {**agent, "id": str(agent_id)}
            for agent_id, agent in entries.items()
            if isinstance(agent_id, str) and agent_id and isinstance(agent, dict)
        }
    legacy = agents_config.get("list")
    if isinstance(legacy, list):
        return {
            str(agent["id"]): dict(agent)
            for agent in legacy
            if isinstance(agent, dict) and agent.get("id")
        }
    return {}


def openclaw_agent_ids(config: dict[str, Any], home: Path) -> list[str]:
    del home  # Kept in the signature for compatibility with existing callers.
    ids = list(configured_openclaw_agents(config))
    return ids or ["main"]


def parse_session_type(key: str) -> str:
    markers = {":feishu:direct:": "feishu-dm", ":feishu:group:": "feishu-group", ":discord:direct:": "discord-dm", ":discord:channel:": "discord-channel", ":telegram:direct:": "telegram-dm", ":telegram:group:": "telegram-group", ":whatsapp:direct:": "whatsapp-dm", ":whatsapp:group:": "whatsapp-group", ":cron:": "cron"}
    if key.endswith(":main"):
        return "main"
    for marker, label in markers.items():
        if marker in key:
            return label
    return "unknown"


def read_openclaw_runtime_status() -> dict[str, Any]:
    """Return one short-lived session snapshot shared by all monitor cards."""
    global _runtime_status_cache, _runtime_status_cache_at

    now = time.monotonic()
    if _runtime_status_cache and now - _runtime_status_cache_at < _RUNTIME_STATUS_CACHE_TTL_SECONDS:
        return _runtime_status_cache

    # FastAPI runs these synchronous endpoints in worker threads. Keep the
    # lock through refresh so simultaneous card polls launch only one CLI.
    with _runtime_status_cache_lock:
        now = time.monotonic()
        if _runtime_status_cache and now - _runtime_status_cache_at < _RUNTIME_STATUS_CACHE_TTL_SECONDS:
            return _runtime_status_cache

        result = safe_run(["openclaw", "sessions", "--all-agents", "--limit", "all", "--json"], timeout=8.0)
        if result is None or result.returncode != 0:
            return _runtime_status_cache or {}
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            return _runtime_status_cache or {}
        if not isinstance(payload, dict):
            return _runtime_status_cache or {}

        _runtime_status_cache = payload
        _runtime_status_cache_at = time.monotonic()
        return _runtime_status_cache


# ---------------------------------------------------------------------------
# Agents table "State" column source of truth
#
# Read-only overlay: OpenClaw real session state first, then the latest JEV
# verdict recorded for *that* session_id.  No idle-time guessing, no WARMUP
# label, no STALL synthesis, and no reuse of another session's JEV verdict.
# Authoritative JEV source is the same file the JEV Live status endpoint reads
# (jev_recovery_live.LIVE_HISTORY).  JEV judgement logic is never modified here.
# ---------------------------------------------------------------------------
_JEV_DECISIONS = {"CONTINUE", "WATCH", "SALVAGE", "DEAD"}
_OPENCLAW_TERMINAL_STATE = {
    "done": "DONE",
    "timeout": "TIMEOUT",
    "failed": "FAILED",
    "killed": "FAILED",
    "cancelled": "FAILED",
    "canceled": "FAILED",
}
_JEV_WARMUP_MS = 5 * 60 * 1000
_JEV_DECISION_CACHE_TTL_SECONDS = 10.0
_jev_decision_cache: dict[str, dict[str, Any]] = {}
_jev_decision_cache_at = 0.0
_jev_decision_cache_lock = threading.Lock()


def read_jev_decisions() -> dict[str, dict[str, Any]]:
    """Latest JEV decision per session_id from the live JEV history (read-only)."""
    global _jev_decision_cache, _jev_decision_cache_at
    now = time.monotonic()
    with _jev_decision_cache_lock:
        if _jev_decision_cache and now - _jev_decision_cache_at < _JEV_DECISION_CACHE_TTL_SECONDS:
            return _jev_decision_cache
        try:
            lines = JEV_LIVE_HISTORY.read_text(encoding="utf-8").splitlines()[-400:]
        except OSError:
            return _jev_decision_cache
        decisions: dict[str, dict[str, Any]] = {}
        for raw in lines:
            try:
                row = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict) or str(row.get("type") or "") != "live_jev_decision":
                continue
            session_id = str(row.get("session_id") or "")
            choice = str(row.get("choice") or "")
            if not session_id or choice not in _JEV_DECISIONS:
                continue
            decisions[session_id] = {
                "decision": choice,
                "confidence": row.get("confidence"),
                "observed_at": row.get("observed_at"),
                "kind": str(row.get("kind") or ""),
            }
        if decisions:
            _jev_decision_cache = decisions
            _jev_decision_cache_at = now
        return decisions


def compute_agent_display_state(agent_sessions: list[dict[str, Any]], jev_decisions: dict[str, dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """State = OpenClaw terminal truth, else active-session JEV verdict, else IDLE."""
    detail: dict[str, Any] = {
        "display_state": "IDLE",
        "session_id": None,
        "session_age_seconds": None,
        "jev_decision": None,
        "jev_confidence": None,
        "jev_observed_at": None,
        "basis": "no_active_session",
    }
    usable = [item for item in agent_sessions if isinstance(item, dict)]
    if not usable:
        return detail
    running = [item for item in usable if str(item.get("status") or "") == "running"]
    if running:
        current = max(running, key=lambda item: int(item.get("updated_at") or 0))
        session_id = str(current.get("session_id") or "") or None
        started = int(current.get("session_started_at") or current.get("updated_at") or 0)
        age_ms = max(0, now_ms - started) if started else None
        detail["session_id"] = session_id
        detail["session_age_seconds"] = round(age_ms / 1000.0, 1) if age_ms is not None else None
        raw_verdict = jev_decisions.get(session_id) if session_id else None
        verdict = raw_verdict if isinstance(raw_verdict, dict) and str(raw_verdict.get("decision") or "") in _JEV_DECISIONS else None
        if age_ms is not None and age_ms <= _JEV_WARMUP_MS:
            detail["display_state"] = "WORKING"
            detail["basis"] = "active_session_within_5m"
            return detail
        if verdict:
            detail["display_state"] = verdict["decision"]
            detail["jev_decision"] = verdict["decision"]
            detail["jev_confidence"] = verdict.get("confidence")
            detail["jev_observed_at"] = verdict.get("observed_at")
            detail["basis"] = "jev_current_session"
            return detail
        detail["display_state"] = "WORKING"
        detail["basis"] = "jev_pending_for_current_session"
        return detail
    latest = max(usable, key=lambda item: int(item.get("updated_at") or 0))
    terminal = _OPENCLAW_TERMINAL_STATE.get(str(latest.get("status") or "").lower())
    session_id = str(latest.get("session_id") or "") or None
    started = int(latest.get("session_started_at") or latest.get("updated_at") or 0)
    detail["session_id"] = session_id
    detail["session_age_seconds"] = round(max(0, now_ms - started) / 1000.0, 1) if started else None
    if terminal:
        detail["display_state"] = terminal
        detail["basis"] = "openclaw_terminal_" + str(latest.get("status") or "").lower()
    return detail


def read_agent_sessions(runtime_status: dict[str, Any], agent_id: str) -> dict[str, Any]:
    all_sessions = runtime_status.get("sessions")
    source = "openclaw sessions --all-agents --limit all --json"
    if not isinstance(all_sessions, list):
        return {"state": "unknown", "last_active": None, "session_count": None, "total_tokens": None, "context_tokens": None, "recent_sessions": [], "source": source, "error": "session_status_unavailable",
                "display_state": "IDLE", "session_id": None, "session_age_seconds": None, "jev_decision": None, "jev_confidence": None, "jev_observed_at": None, "basis": "session_status_unavailable",
                "token_telemetry": read_token_telemetry(agent_id, None)}
    sessions = [
        item
        for item in all_sessions
        if isinstance(item, dict) and str(item.get("agentId")) == agent_id
    ]
    rows: list[dict[str, Any]] = []
    last_active: int | None = None
    total_tokens = 0
    context_tokens = 0
    for item in sessions:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        updated = int(item.get("updatedAt") or 0)
        last_active = max(last_active or 0, updated) or None
        total_tokens += int(item.get("totalTokens") or 0)
        context_tokens += int(item.get("contextTokens") or 0)
        rows.append({"key": key, "type": parse_session_type(key), "updated_at": updated, "total_tokens": int(item.get("totalTokens") or 0), "context_tokens": int(item.get("contextTokens") or 0), "system_sent": bool(item.get("systemSent", False)), "status": item.get("status"), "session_id": str(item.get("sessionId") or "") or None, "session_started_at": int(item.get("sessionStartedAt") or 0) or None})
    now_ms = int(time.time() * 1000)
    state = "offline"
    if last_active:
        diff = now_ms - last_active
        if diff < 180000:
            state = "working"
        elif diff < 86400000:
            state = "idle"
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    display = compute_agent_display_state(rows, read_jev_decisions(), now_ms)
    token_telemetry = read_token_telemetry(agent_id, display.get("session_id"))
    return {"state": state, "last_active": last_active, "session_count": len(rows), "total_tokens": total_tokens, "context_tokens": context_tokens, "recent_sessions": redact_sensitive(rows[:8]), "source": source, "token_telemetry": token_telemetry, **display}


def collect_openclaw_agents(config: dict[str, Any], home: Path, runtime_status: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    defaults = config.get("agents", {}).get("defaults", {})
    default_model = normalize_model_ref(defaults.get("model"), "unknown")
    configured = configured_openclaw_agents(config)
    runtime_status = runtime_status if isinstance(runtime_status, dict) else read_openclaw_runtime_status()
    bindings = config.get("bindings", []) if isinstance(config.get("bindings"), list) else []
    channels = config.get("channels", {}) if isinstance(config.get("channels"), dict) else {}
    rows: list[dict[str, Any]] = []
    for agent_id in openclaw_agent_ids(config, home):
        agent = configured.get(agent_id, {"id": agent_id})
        paths = identity_paths(home, agent_id, agent)
        name = read_identity_value(paths, "Name") or agent.get("name") or agent.get("identity", {}).get("name") or agent_id
        emoji = read_identity_value(paths, "Emoji") or agent.get("identity", {}).get("emoji") or agent.get("emoji") or ""
        platforms: set[str] = set()
        for binding in bindings:
            if isinstance(binding, dict) and binding.get("agentId") == agent_id:
                channel = binding.get("match", {}).get("channel")
                if channel:
                    platforms.add(str(channel))
        if agent_id == "main":
            for channel, channel_config in channels.items():
                if isinstance(channel_config, dict) and channel_config.get("enabled") is not False:
                    platforms.add(str(channel))
        rows.append({"id": agent_id, "name": str(name), "emoji": str(emoji), "model": normalize_model_ref(agent.get("model"), default_model), "platforms": sorted(platforms), "session": read_agent_sessions(runtime_status, agent_id)})
    return rows


def collect_openclaw_providers(config: dict[str, Any], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    auth_profiles = config.get("auth", {}).get("profiles", {})
    auth_ids = {str(profile.get("provider") or key.split(":")[0]) for key, profile in auth_profiles.items() if isinstance(profile, dict)} if isinstance(auth_profiles, dict) else set()
    providers: dict[str, dict[str, Any]] = {}
    providers_config = config.get("models", {}).get("providers", {})
    if isinstance(providers_config, dict):
        for provider_id, provider in providers_config.items():
            provider = provider if isinstance(provider, dict) else {}
            models = []
            for model in provider.get("models", []) or []:
                if isinstance(model, dict):
                    models.append({"id": model.get("id") or model.get("name") or "unknown", "name": model.get("name") or model.get("id") or "unknown", "context_window": model.get("contextWindow"), "max_tokens": model.get("maxTokens"), "reasoning": model.get("reasoning")})
            providers[str(provider_id)] = {"id": str(provider_id), "api": provider.get("api"), "access_mode": "auth" if str(provider_id) in auth_ids else "api_key", "models": models, "used_by": []}
    def add_ref(ref: str | None) -> None:
        if not ref or "/" not in ref:
            return
        provider_id, model_id = ref.split("/", 1)
        provider = providers.setdefault(provider_id, {"id": provider_id, "api": None, "access_mode": "auth" if provider_id in auth_ids else "api_key", "models": [], "used_by": []})
        if not any(model.get("id") == model_id for model in provider["models"]):
            provider["models"].append({"id": model_id, "name": model_id, "context_window": None, "max_tokens": None, "reasoning": None})
    defaults = config.get("agents", {}).get("defaults", {})
    add_ref(normalize_model_ref(defaults.get("model"), ""))
    if isinstance(defaults.get("model"), dict):
        for fallback in defaults["model"].get("fallbacks", []) or []:
            add_ref(fallback)
    for agent in agents:
        add_ref(agent.get("model"))
    for provider in providers.values():
        provider["used_by"] = [{"id": agent["id"], "name": agent["name"], "emoji": agent["emoji"]} for agent in agents if isinstance(agent.get("model"), str) and agent["model"].startswith(provider["id"] + "/")]
    return sorted(providers.values(), key=lambda item: item["id"])


def parse_skill_frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    result: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip()
    return result


def scan_openclaw_skills(directory: Path, source: str) -> list[dict[str, Any]]:
    try:
        children = sorted(path for path in directory.iterdir() if path.is_dir())
    except Exception:
        return []
    skills: list[dict[str, Any]] = []
    for child in children:
        skill_md = child / "SKILL.md"
        try:
            content = skill_md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        fm = parse_skill_frontmatter(content)
        skills.append({"id": child.name, "name": fm.get("name") or child.name, "description": fm.get("description") or "", "source": source, "location": str(skill_md)})
    return skills


def collect_openclaw_skills(config: dict[str, Any], home: Path) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    package_candidates = [os.getenv("OPENCLAW_PACKAGE_DIR"), str(Path.home() / ".local/lib/node_modules/openclaw"), "/usr/local/lib/node_modules/openclaw", "/usr/lib/node_modules/openclaw", "/opt/homebrew/lib/node_modules/openclaw"]
    package_dir = next((Path(path).expanduser() for path in package_candidates if path and (Path(path).expanduser() / "package.json").exists()), None)
    if package_dir:
        skills.extend(scan_openclaw_skills(package_dir / "skills", "builtin"))
    skills.extend(scan_openclaw_skills(home / "skills", "custom"))
    skills.extend(scan_openclaw_skills(home / "workspace" / "skills", "workspace:main"))
    for agent in configured_openclaw_agents(config).values():
        if isinstance(agent, dict):
            for key in ("workspace", "agentDir"):
                raw = agent.get(key)
                if isinstance(raw, str) and raw:
                    skills.extend(scan_openclaw_skills(Path(raw).expanduser() / "skills", "workspace:" + str(agent.get("id", "agent"))))
    by_source: dict[str, int] = {}
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for skill in skills:
        key = (skill["source"], skill["id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(skill)
        by_source[skill["source"]] = by_source.get(skill["source"], 0) + 1
    return {"total": len(unique), "by_source": by_source, "items": unique[:80]}


def probe_openclaw_gateway(config: dict[str, Any]) -> dict[str, Any]:
    gateway = config.get("gateway", {}) if isinstance(config.get("gateway"), dict) else {}
    port = int(gateway.get("port") or 18789)
    token = gateway.get("auth", {}).get("token") if isinstance(gateway.get("auth"), dict) else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.time()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers=headers)
        with urllib.request.urlopen(request, timeout=2.5) as response:
            response_ms = round((time.time() - started) * 1000)
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            health_ok = response.status == 200 and (payload.get("ok") is True if isinstance(payload, dict) else False)
            if not health_ok:
                return {"state": "probe_failed", "ok": False, "port": port, "response_ms": response_ms, "checked_path": "/health", "http_status": response.status, "error": "invalid_health_response"}
            return {"state": "degraded" if response_ms > 1500 else "healthy", "ok": True, "port": port, "response_ms": response_ms, "checked_path": "/health", "http_status": response.status}
    except urllib.error.HTTPError as exc:
        return {"state": "probe_failed" if exc.code < 500 else "down", "ok": False, "port": port, "response_ms": round((time.time() - started) * 1000), "checked_path": "/health", "http_status": exc.code, "error": "health_http_error"}
    except (ConnectionRefusedError, TimeoutError, urllib.error.URLError, OSError):
        return {"state": "down", "ok": False, "port": port, "response_ms": round((time.time() - started) * 1000), "checked_path": "/health", "error": "gateway_unreachable"}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"state": "probe_failed", "ok": False, "port": port, "response_ms": round((time.time() - started) * 1000), "checked_path": "/health", "error": "invalid_health_response"}


def collect_openclaw_status() -> dict[str, Any]:
    home = openclaw_home()
    config_path = home / "openclaw.json"
    base = {"timestamp": int(time.time()), "last_updated": time.strftime("%H:%M:%S"), "home": str(home), "config_path": str(config_path)}
    empty = {"summary": {"connection": "Missing config", "agent_count": 0, "model_count": 0, "provider_count": 0, "skill_count": 0, "gateway": "unknown"}, "agents": [], "providers": [], "skills": {"total": 0, "by_source": {}, "items": []}, "gateway": {"state": "unknown", "ok": False}, "raw": {}}
    if not config_path.exists():
        return {**base, **empty, "status": "not_configured"}
    config = read_json_file(config_path)
    if not isinstance(config, dict):
        return {**base, **empty, "status": "error", "summary": {**empty["summary"], "connection": "Invalid config"}}
    runtime_status = read_openclaw_runtime_status()
    agents = collect_openclaw_agents(config, home, runtime_status)
    providers = collect_openclaw_providers(config, agents)
    skills = collect_openclaw_skills(config, home)
    gateway = probe_openclaw_gateway(config)
    model_count = sum(len(provider.get("models", [])) for provider in providers)
    payload = {**base, "status": "ok", "config_modified": int(config_path.stat().st_mtime), "summary": {"connection": "Connected", "agent_count": len(agents), "model_count": model_count, "provider_count": len(providers), "skill_count": skills["total"], "gateway": gateway["state"]}, "agents": agents, "providers": providers, "skills": skills, "gateway": gateway}
    payload["raw"] = redact_sensitive({"config": config, "agents": agents, "providers": providers, "skills": skills, "gateway": gateway})
    return payload


def get_cpu() -> dict[str, Any]:
    try:
        temperature_c = None
        temperature_source = None
        try:
            temperatures = psutil.sensors_temperatures(fahrenheit=False)
        except Exception:
            temperatures = {}
        for chip, label in (("k10temp", "Tctl"), ("coretemp", "Package id 0")):
            sensor = next((entry for entry in temperatures.get(chip, []) if entry.label == label), None)
            if sensor and isinstance(sensor.current, (int, float)):
                temperature_c = round(sensor.current, 1)
                temperature_source = f"{chip}:{label}"
                break
        return {
            "usage_percent": pct(psutil.cpu_percent(interval=None)),
            "temperature_c": temperature_c,
            "temperature_source": temperature_source,
            "status": "ok",
        }
    except Exception as exc:
        return {"usage_percent": None, "temperature_c": None, "temperature_source": None, "status": "error", "error": str(exc)}


def get_memory() -> dict[str, Any]:
    try:
        mem = psutil.virtual_memory()
        return {
            "used_gb": bytes_to_gb(mem.used),
            "total_gb": bytes_to_gb(mem.total),
            "usage_percent": pct(mem.percent),
            "status": "ok",
        }
    except Exception as exc:
        return {"used_gb": None, "total_gb": None, "usage_percent": None, "status": "error", "error": str(exc)}


def get_disk() -> dict[str, Any]:
    try:
        disk = psutil.disk_usage("/")
        return {
            "used_gb": bytes_to_gb(disk.used),
            "total_gb": bytes_to_gb(disk.total),
            "usage_percent": pct(disk.percent),
            "status": "ok",
        }
    except Exception as exc:
        return {"used_gb": None, "total_gb": None, "usage_percent": None, "status": "error", "error": str(exc)}


def get_network() -> dict[str, Any]:
    global _network_prev
    try:
        counters = psutil.net_io_counters()
        now = time.time()
        if _network_prev is None:
            _network_prev = {"time": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
            return {"upload_bps": 0, "download_bps": 0, "status": "warming"}

        elapsed = max(now - float(_network_prev["time"]), 0.001)
        upload = max(counters.bytes_sent - int(_network_prev["sent"]), 0) / elapsed
        download = max(counters.bytes_recv - int(_network_prev["recv"]), 0) / elapsed
        _network_prev = {"time": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
        return {"upload_bps": round(upload, 1), "download_bps": round(download, 1), "status": "ok"}
    except Exception as exc:
        return {"upload_bps": None, "download_bps": None, "status": "error", "error": str(exc)}


VRAM_HEADROOM_THRESHOLD_GB = 2.0
VRAM_HEADROOM_THRESHOLD_PERCENT = 10.0
# A resident inference model commonly keeps VRAM above 97%. Treat capacity as
# critical only when it is effectively exhausted; lower headroom remains a
# warning unless an actual GPU/OOM error is observed.
VRAM_CRITICAL_PERCENT = 99.5


def _gpu_headroom_state(gpu: dict[str, Any]) -> tuple[str, float | None]:
    vram_used = gpu.get("vram_used_gb")
    vram_total = gpu.get("vram_total_gb")
    vram_pct = gpu.get("vram_usage_percent")
    if vram_used is None or vram_total is None:
        return "unknown", None
    headroom_gb = round(max(0.0, vram_total - vram_used), 2)
    if vram_pct is not None and vram_pct >= VRAM_CRITICAL_PERCENT:
        return "critical", headroom_gb
    if headroom_gb < VRAM_HEADROOM_THRESHOLD_GB or vram_pct is not None and vram_pct >= 100 - VRAM_HEADROOM_THRESHOLD_PERCENT:
        return "headroom_low", headroom_gb
    return "ok", headroom_gb


def gpu_risk_summary(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate all adapters; high VRAM is safe when measurable headroom remains."""
    risks: list[str] = []
    for item in gpus:
        if item.get("status") == "error":
            risks.append("error")
            continue
        state = item.get("vram_headroom_state")
        if not isinstance(state, str):
            state, _ = _gpu_headroom_state(item)
        risks.append("error" if state == "critical" else "warning" if state in {"headroom_low", "unknown"} else "ok")
    if not risks:
        return {"risk": "warning", "headroom_gb": None, "temperature_c": None, "worst_index": None}
    priority = {"error": 3, "warning": 2, "ok": 1}
    worst_index = max(range(len(risks)), key=lambda index: priority[risks[index]])
    worst = gpus[worst_index]
    return {"risk": risks[worst_index], "headroom_gb": worst.get("vram_headroom_gb"), "temperature_c": worst.get("temperature_c"), "worst_index": worst_index}


def _nvidia_smi_number(value: str | float | int | None) -> float | None:
    """Parse nvidia-smi numeric fields, preserving unsupported values as null."""
    if value is None:
        return None
    token = str(value).strip()
    if token.upper().strip("[]") in {"N/A", "NA"}:
        return None
    try:
        return round(float(token), 1)
    except (TypeError, ValueError):
        return None


def _gpu_metrics_from_nvidia_smi_row(row: list[str], index: int) -> dict[str, Any] | None:
    name, gpu_util, mem_used, mem_total, temp = row[:5]
    uuid = row[5] if len(row) > 5 and row[5] else None
    used = _nvidia_smi_number(mem_used)
    total = _nvidia_smi_number(mem_total)
    used_gb = round(used / 1024, 1) if used is not None else None
    total_gb = round(total / 1024, 1) if total is not None else None
    vram_percent = round((used / total) * 100, 1) if used is not None and total else None
    power_draw = _nvidia_smi_number(row[6]) if len(row) > 6 else None
    power_limit = _nvidia_smi_number(row[7]) if len(row) > 7 else None
    fan_speed = _nvidia_smi_number(row[8]) if len(row) > 8 else None
    pci_bus_id = row[9] if len(row) > 9 and row[9] else None
    pcie_gen_current = _nvidia_smi_number(row[10]) if len(row) > 10 else None
    pcie_gen_max = _nvidia_smi_number(row[11]) if len(row) > 11 else None
    pcie_width_current = _nvidia_smi_number(row[12]) if len(row) > 12 else None
    pcie_width_max = _nvidia_smi_number(row[13]) if len(row) > 13 else None
    headroom_state, headroom_gb = _gpu_headroom_state({
        "vram_used_gb": used_gb, "vram_total_gb": total_gb, "vram_usage_percent": vram_percent,
    })
    return {
        "index": index,
        "uuid": uuid,
        "name": name,
        "usage_percent": _nvidia_smi_number(gpu_util),
        "vram_used_gb": used_gb,
        "vram_total_gb": total_gb,
        "vram_usage_percent": vram_percent,
        "vram_headroom_gb": headroom_gb,
        "vram_headroom_state": headroom_state,
        "temperature_c": _nvidia_smi_number(temp),
        "power_draw_w": power_draw,
        "power_limit_w": power_limit,
        "fan_speed_percent": fan_speed,
        "pci_bus_id": pci_bus_id,
        "pcie_gen_current": int(pcie_gen_current) if pcie_gen_current is not None else None,
        "pcie_gen_max": int(pcie_gen_max) if pcie_gen_max is not None else None,
        "pcie_width_current": int(pcie_width_current) if pcie_width_current is not None else None,
        "pcie_width_max": int(pcie_width_max) if pcie_width_max is not None else None,
        "status": "ok",
        "source": "nvidia-smi",
    }


def get_gpus_from_nvidia_smi() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,uuid,power.draw,power.limit,fan.speed,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max"
    result = safe_run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    if not result or result.returncode != 0 or not result.stdout.strip():
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(result.stdout.strip().splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            out.append({
                "index": i,
                "uuid": None,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "fan_speed_percent": None,
                "pci_bus_id": None,
                "pcie_gen_current": None,
                "pcie_gen_max": None,
                "pcie_width_current": None,
                "pcie_width_max": None,
                "status": "error",
                "source": "nvidia-smi",
                "error": "invalid nvidia-smi row",
            })
            continue
        metrics = _gpu_metrics_from_nvidia_smi_row(parts, i)
        if metrics is None:
            out.append({
                "index": i,
                "uuid": None,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "fan_speed_percent": None,
                "pci_bus_id": None,
                "pcie_gen_current": None,
                "pcie_gen_max": None,
                "pcie_width_current": None,
                "pcie_width_max": None,
                "status": "error",
                "source": "nvidia-smi",
                "error": "parse failure",
            })
        else:
            out.append(metrics)
    return out


def get_gpus_from_pynvml() -> list[dict[str, Any]]:
    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception:
        return []
    count = 0
    out: list[dict[str, Any]] = []
    try:
        count = pynvml.nvmlDeviceGetCount()
    except Exception:
        count = 0
    for i in range(count):
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            raw_name = pynvml.nvmlDeviceGetName(handle)
            name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
            uuid = pynvml.nvmlDeviceGetUUID(handle)
            uuid = uuid.decode("utf-8") if isinstance(uuid, bytes) else str(uuid)
        except Exception as exc:
            out.append({
                "index": i,
                "uuid": None,
                "name": "Unknown",
                "usage_percent": None,
                "vram_used_gb": None,
                "vram_total_gb": None,
                "vram_usage_percent": None,
                "temperature_c": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "fan_speed_percent": None,
                "pci_bus_id": None,
                "pcie_gen_current": None,
                "pcie_gen_max": None,
                "pcie_width_current": None,
                "pcie_width_max": None,
                "status": "error",
                "source": "pynvml",
                "error": str(exc),
            })
            continue
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            util_gpu = util.gpu
        except Exception:
            util_gpu = None
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            mem_used = mem.used
            mem_total = mem.total
        except Exception:
            mem_used = None
            mem_total = None
        try:
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        except Exception:
            temp = None
        try:
            power_draw_w = pct(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)
        except Exception:
            power_draw_w = None
        try:
            power_limit_w = pct(pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0)
        except Exception:
            power_limit_w = None
        try:
            fan_speed = pct(pynvml.nvmlDeviceGetFanSpeed(handle))
        except Exception:
            fan_speed = None
        try:
            pci = pynvml.nvmlDeviceGetPciInfo(handle)
            pci_bus_id = f"{pci.domain:08X}:{pci.bus:02X}:{pci.device:02X}.0"
        except Exception:
            pci_bus_id = None
        try:
            pcie_gen_current = int(pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle))
        except Exception:
            pcie_gen_current = None
        try:
            pcie_gen_max = int(pynvml.nvmlDeviceGetMaxPcieLinkGeneration(handle))
        except Exception:
            pcie_gen_max = None
        try:
            pcie_width_current = int(pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle))
        except Exception:
            pcie_width_current = None
        try:
            pcie_width_max = int(pynvml.nvmlDeviceGetMaxPcieLinkWidth(handle))
        except Exception:
            pcie_width_max = None
        out.append({
            "index": i,
            "uuid": uuid,
            "name": name,
            "usage_percent": util_gpu,
            "vram_used_gb": bytes_to_gb(mem_used),
            "vram_total_gb": bytes_to_gb(mem_total),
            "vram_usage_percent": pct((mem_used / mem_total) * 100 if mem_total else None),
            "vram_headroom_gb": round(max(0.0, bytes_to_gb(mem_total) - bytes_to_gb(mem_used)), 2) if mem_used is not None and mem_total is not None else None,
            "temperature_c": temp,
            "power_draw_w": power_draw_w,
            "power_limit_w": power_limit_w,
            "fan_speed_percent": fan_speed,
            "pci_bus_id": pci_bus_id,
            "pcie_gen_current": pcie_gen_current,
            "pcie_gen_max": pcie_gen_max,
            "pcie_width_current": pcie_width_current,
            "pcie_width_max": pcie_width_max,
            "status": "ok",
            "source": "pynvml",
        })
        out[-1]["vram_headroom_state"], _ = _gpu_headroom_state(out[-1])
    try:
        pynvml.nvmlShutdown()
    except Exception:
        pass
    return out


def get_gpus() -> list[dict[str, Any]]:
    try:
        gpus = get_gpus_from_nvidia_smi() or get_gpus_from_pynvml()
        return gpus if gpus else [{
            "index": 0,
            "uuid": None,
            "name": "Unknown",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "power_draw_w": None,
            "power_limit_w": None,
            "fan_speed_percent": None,
            "pci_bus_id": None,
            "pcie_gen_current": None,
            "pcie_gen_max": None,
            "pcie_width_current": None,
            "pcie_width_max": None,
            "status": "unknown",
            "source": "none",
        }]
    except Exception as exc:
        return [{
            "index": 0,
            "uuid": None,
            "name": "Error",
            "usage_percent": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "vram_usage_percent": None,
            "temperature_c": None,
            "power_draw_w": None,
            "power_limit_w": None,
            "fan_speed_percent": None,
            "pci_bus_id": None,
            "pcie_gen_current": None,
            "pcie_gen_max": None,
            "pcie_width_current": None,
            "pcie_width_max": None,
            "status": "error",
            "source": "none",
            "error": str(exc),
        }]


def get_gpu() -> dict[str, Any]:
    gpus = get_gpus()
    return gpus[0] if gpus else {
        "uuid": None,
        "name": "Unknown",
        "usage_percent": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_usage_percent": None,
        "temperature_c": None,
        "power_draw_w": None,
        "power_limit_w": None,
        "fan_speed_percent": None,
        "pci_bus_id": None,
        "pcie_gen_current": None,
        "pcie_gen_max": None,
        "pcie_width_current": None,
        "pcie_width_max": None,
        "status": "unknown",
        "source": "none",
    }


def systemctl_status(service: str) -> str | None:
    if not shutil.which("systemctl"):
        return None
    result = safe_run(["systemctl", "is-active", service], timeout=1.0)
    if not result:
        return None
    state = result.stdout.strip()
    if state in {"active", "inactive", "failed", "activating", "deactivating"}:
        return state
    return None


def process_running(names: list[str]) -> bool:
    wanted = {name.lower() for name in names}
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if any(item in name or item in cmdline for item in wanted):
                return True
    except Exception:
        return False
    return False


def service_status(service: str, process_names: list[str]) -> dict[str, str]:
    try:
        state = systemctl_status(service)
        if state == "active":
            return {"state": "running", "label": "Running"}
        if process_running(process_names):
            return {"state": "running", "label": "Running"}
        if state in {"inactive", "failed"}:
            return {"state": "stopped", "label": "Stopped"}
        return {"state": "unknown", "label": "Unknown"}
    except Exception:
        return {"state": "error", "label": "Error"}


OPENCONNECTOR_CONTAINER = "openconnector-test"
OPENCONNECTOR_HEALTH_URL = "http://127.0.0.1:3001/health"
OPENCONNECTOR_CONSOLE_URL = "https://openclaw.tail8eba4b.ts.net:3001/"
OPENCONNECTOR_API_BASE = "http://127.0.0.1:3001"
OPENCONNECTOR_ENV_FILE = Path("/opt/openconnector/.env")
OPENCONNECTOR_LEASE_LEDGER = Path(
    "/home/plachem-sever/.openclaw/workspace-secretary/operations/openconnector-auth-leases.json"
)
OPENCONNECTOR_MANAGED_ACTIONS = {
    "github": "github.get_current_user",
    # Gmail operations use the centrally managed IMAP connection.  The old
    # gmail OAuth connection is retained in OpenConnector history, but it is
    # not part of the 13-item operational verification set.
    "gmail": "generic_imap.list_folders",
    "googledrive": "googledrive.about.get",
    "google_search_console": "google_search_console.list_sites",
    "google_analytics": "google_analytics.list_account_summaries",
    "notion": "notion.search",
    "supabase": "supabase.list_organizations",
    "cloudflare_dns": "cloudflare_dns.list_accounts",
    "cloudflare_worker": "cloudflare_worker.list_accounts",
    "wordpress": "wordpress.get_current_user",
    "elevenlabs": "elevenlabs.get_user_info",
    "gemini": "gemini.list_models",
    "telegram": "telegram.get_me",
}
OPENCONNECTOR_MANAGED_CONNECTION_SERVICES = {
    **{service: service for service in OPENCONNECTOR_MANAGED_ACTIONS},
    "gmail": "generic_imap",
}

OPENCONNECTOR_UNTIL_REVOKED_SERVICES = {
    "gemini", "notion", "supabase", "telegram", "wordpress",
}
OPENCONNECTOR_PROVIDER_EXPIRY_SERVICES = {
    "cloudflare_dns", "cloudflare_worker", "elevenlabs", "github",
}
OPENCONNECTOR_PROVIDER_EXPIRY_GUIDANCE = {
    "cloudflare_dns": "Cloudflare token verify의 expires_on 연동 필요",
    "cloudflare_worker": "Cloudflare token verify의 expires_on 연동 필요",
    "elevenlabs": "ElevenLabs API Key 목록의 expires_at 연동 필요",
    "github": "GitHub PAT 만료 헤더 또는 조직 정책의 만료일 연동 필요",
}
OPENCONNECTOR_AUTH_ERROR_MARKERS = (
    "auth", "credential", "forbidden", "oauth", "permission", "token", "unauthorized",
)


def _read_env_value(path: Path, key: str) -> str | None:
    """Read one plain dotenv value without loading secrets into process env."""
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip("'\"") or None
    except OSError:
        return None
    return None


def get_openconnector_connection_updates() -> dict[str, dict[str, Any]]:
    """Return per-service connection and OAuth client modification times."""
    node_script = """const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/app/data/connect.sqlite", { readOnly: true });
const connections = db.prepare("SELECT service, MAX(updated_at) AS updated_at FROM connections GROUP BY service").all();
const oauth = db.prepare("SELECT service, MAX(updated_at) AS updated_at FROM oauth_client_configs GROUP BY service").all();
process.stdout.write(JSON.stringify({ connections, oauth }));"""
    result = subprocess.run(
        ["docker", "exec", "-i", OPENCONNECTOR_CONTAINER, "node", "-e", node_script],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"OpenConnector DB read failed: {result.stderr.strip() or 'unknown error'}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenConnector DB returned invalid JSON: {exc}")
    updates: dict[str, dict[str, Any]] = {}
    for row in payload.get("connections", []):
        updates[row["service"]] = {"connection_last_modified_at": row.get("updated_at")}
    for row in payload.get("oauth", []):
        updates.setdefault(row["service"], {})[
            "oauth_client_last_modified_at"
        ] = row.get("updated_at")
    return updates


def _openconnector_admin_get(path: str, timeout: float = 2.5) -> Any:
    token = os.environ.get("OOMOL_CONNECT_ADMIN_TOKEN") or _read_env_value(
        OPENCONNECTOR_ENV_FILE, "OOMOL_CONNECT_ADMIN_TOKEN"
    )
    if not token:
        raise RuntimeError("OpenConnector admin token is unavailable")
    request = urllib.request.Request(
        f"{OPENCONNECTOR_API_BASE}{path}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _expiration_status(value: Any, now: float) -> str:
    expires_at = _parse_iso_timestamp(value)
    if expires_at is None:
        return "unknown"
    remaining = expires_at - now
    if remaining <= 0:
        return "overdue"
    if remaining <= 2 * 60 * 60:
        return "due_2h"
    if remaining <= 24 * 60 * 60:
        return "due_24h"
    return "normal"


def _is_authentication_failure(run: dict[str, Any]) -> bool:
    """Only credential failures affect authentication health.

    Invalid input, business validation and provider-side data errors are still
    useful activity records, but they do not prove that a credential is bad.
    """
    if run.get("ok") is not False:
        return False
    text = " ".join(str(run.get(key) or "") for key in ("errorCode", "error", "message")).lower()
    return any(marker in text for marker in OPENCONNECTOR_AUTH_ERROR_MARKERS)


def _credential_lifecycle(connection: dict[str, Any], now: float) -> dict[str, Any]:
    service = str(connection.get("service") or "")
    auth_type = str(connection.get("authType") or "")
    expires_at = connection.get("credentialExpiresAt")
    refreshable = bool(connection.get("refreshable"))
    auth_health = str(connection.get("authHealth") or "")
    expiry_status = _expiration_status(expires_at, now)
    if expires_at:
        if refreshable:
            return {
                "kind": "oauth_auto_refresh", "label": "자동 갱신 관리",
                "detail": "액세스 토큰 만료 후 Refresh Token으로 자동 갱신",
                "expires_at": expires_at, "expiration_status": expiry_status,
                "action_required": auth_health == "reauth_required",
            }
        return {
            "kind": "fixed_expiry", "label": "실제 만료일",
            "detail": "Provider가 제공한 실제 만료시각",
            "expires_at": expires_at, "expiration_status": expiry_status,
            "action_required": expiry_status in {"overdue", "due_2h"},
        }
    if auth_type == "oauth2" and refreshable:
        return {
            "kind": "oauth_auto_refresh", "label": "자동 갱신 관리",
            "detail": "Refresh Token 보유 · 다음 사용 시 액세스 토큰 갱신",
            "expires_at": None, "expiration_status": "managed",
            "action_required": auth_health == "reauth_required",
        }
    if service in OPENCONNECTOR_UNTIL_REVOKED_SERVICES:
        return {
            "kind": "until_revoked", "label": "폐기 전까지 유효",
            "detail": "Provider가 고정 만료일을 제공하지 않음 · 연결 성공과 회전일로 관리",
            "expires_at": None, "expiration_status": "until_revoked", "action_required": False,
        }
    if service in OPENCONNECTOR_PROVIDER_EXPIRY_SERVICES:
        return {
            "kind": "provider_registration", "label": "Provider 만료정보 등록 필요",
            "detail": OPENCONNECTOR_PROVIDER_EXPIRY_GUIDANCE[service],
            "expires_at": None, "expiration_status": "registration_required", "action_required": True,
        }
    return {
        "kind": "rotation_policy", "label": "회전 정책으로 관리",
        "detail": "실제 사용 성공과 마지막 연결 변경일을 기준으로 관리",
        "expires_at": None, "expiration_status": "policy_managed", "action_required": False,
    }


def _lease_access_class(actions: list[str]) -> str:
    lowered = [str(action).lower() for action in actions]
    if any(
        "production" in action
        and any(word in action for word in ("deploy", "migration", "merge", "rollback", "execute"))
        for action in lowered
    ):
        return "production_high"
    read_markers = (
        ".get", ".list", ".search", ".read", ".about", ".inspect",
        "read_only", "health", "status", "whoami", "current_user",
    )
    return "read" if lowered and all(any(marker in action for marker in read_markers) for action in lowered) else "write"


def get_openconnector_lease_review(now: float | None = None) -> dict[str, Any]:
    """Return secret-free lease age/review metadata from the central broker ledger.

    The ledger has no enforced expiry. ``review_due_at`` is therefore labelled
    as an operational review deadline, never as credential expiration.
    """
    checked_at = time.time() if now is None else now
    try:
        payload = json.loads(OPENCONNECTOR_LEASE_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"items": [], "error": _sanitize_error(exc)}
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return {"items": [], "error": "lease ledger schema is invalid"}

    items: list[dict[str, Any]] = []
    for token_id, entry in tokens.items():
        if not isinstance(entry, dict):
            continue
        token_name = str(entry.get("tokenName") or "등록 이름 없음")
        leases = entry.get("leases")
        if not isinstance(leases, dict):
            continue
        for request_id, lease in leases.items():
            if not isinstance(lease, dict):
                continue
            actions = [str(action) for action in lease.get("actions", []) if isinstance(action, str)]
            connections = [str(value) for value in lease.get("connections", []) if isinstance(value, str)]
            access_class = _lease_access_class(actions)
            created_at = lease.get("createdAt")
            created_epoch = _parse_iso_timestamp(created_at)
            review_hours = 24 if access_class == "read" else 8
            review_due_epoch = created_epoch + review_hours * 60 * 60 if created_epoch is not None else None
            review_due_at = (
                datetime.fromtimestamp(review_due_epoch, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
                if review_due_epoch is not None
                else None
            )
            items.append({
                "token_id": str(token_id),
                "token_name": token_name,
                "request_id": str(request_id),
                "created_at": created_at if isinstance(created_at, str) else None,
                "review_due_at": review_due_at,
                "review_status": "overdue" if review_due_epoch is not None and review_due_epoch <= checked_at else "normal" if review_due_epoch is not None else "unknown",
                "access_class": access_class,
                "action_count": len(actions),
                "connection_count": len(connections),
                "expiration_kind": "review_deadline",
            })
    items.sort(key=lambda item: (item["review_status"] != "overdue", item["review_due_at"] or ""))
    return {"items": items, "error": None}


def get_openconnector_dashboard() -> dict[str, Any]:
    """Return a secret-free, live authentication control view."""
    now = time.time()
    collected_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    raw_connections = _openconnector_admin_get("/api/connections")
    runtime_tokens = _openconnector_admin_get("/api/runtime-tokens")
    run_page = _openconnector_admin_get("/api/runs?limit=100")
    runs = [item for item in (run_page.get("items", []) if isinstance(run_page, dict) else []) if isinstance(item, dict)]

    # The control screen follows the same canonical 13-item inventory as the
    # fixed verification job.  Legacy/extra OpenConnector connections remain
    # available in its own console but must not distort this dashboard.
    by_service: dict[str, dict[str, Any]] = {}
    canonical_by_source = {
        source: canonical
        for canonical, source in OPENCONNECTOR_MANAGED_CONNECTION_SERVICES.items()
    }
    for item in raw_connections if isinstance(raw_connections, list) else []:
        if not isinstance(item, dict) or item.get("authType") == "no_auth":
            continue
        source_service = str(item.get("service") or "")
        service = canonical_by_source.get(source_service)
        if not service:
            continue
        if service not in by_service or item.get("default") is True:
            by_service[service] = item
    try:
        updates = get_openconnector_connection_updates() or {}
    except Exception:
        updates = {}

    managed_sources = set(canonical_by_source)
    day_runs = [
        run for run in runs
        if str(run.get("service") or "") in managed_sources
        and (_parse_iso_timestamp(run.get("completedAt")) or 0) >= now - 86400
    ]
    service_runs: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        canonical = canonical_by_source.get(str(run.get("service") or ""))
        if canonical:
            service_runs.setdefault(canonical, []).append(run)

    services: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []
    for service, connection in sorted(by_service.items()):
        source_service = OPENCONNECTOR_MANAGED_CONNECTION_SERVICES[service]
        related = service_runs.get(service, [])
        latest = related[0] if related else None
        latest_success = next((run for run in related if run.get("ok") is True), None)
        latest_auth_failure = next((run for run in related if _is_authentication_failure(run)), None)
        success_epoch = _parse_iso_timestamp(latest_success.get("completedAt")) if latest_success else None
        configured = bool(connection.get("configured"))
        if not configured:
            state, reason = "error", "not_configured"
        elif latest_auth_failure and (not latest_success or (_parse_iso_timestamp(latest_auth_failure.get("completedAt")) or 0) > (success_epoch or 0)):
            state, reason = "error", "authentication_failed"
        elif success_epoch is not None and now - success_epoch <= 86400:
            state, reason = "healthy", "recent_authenticated_use"
        elif success_epoch is not None:
            state, reason = "warning", "authenticated_use_stale"
        else:
            state, reason = "attention", "first_verification_required"

        lifecycle = _credential_lifecycle({**connection, "service": service}, now)
        if reason == "not_configured":
            actions.append({"severity": "error", "service": service, "title": "연결 복구", "detail": "OpenConnector 연결 설정이 완료되지 않았습니다."})
        elif reason == "authentication_failed":
            actions.append({"severity": "error", "service": service, "title": "재인증 확인", "detail": str(latest_auth_failure.get("errorCode") or "인증 실패")[:120]})
        elif reason == "first_verification_required":
            actions.append({"severity": "warning", "service": service, "title": "첫 실사용 검증", "detail": f"{OPENCONNECTOR_MANAGED_ACTIONS.get(service, '안전한 READ 작업')}을 실행해 연결을 확인하세요."})
        if lifecycle["action_required"]:
            actions.append({"severity": "warning", "service": service, "title": lifecycle["label"], "detail": lifecycle["detail"]})

        day_related = [run for run in day_runs if str(run.get("service") or "") == source_service]
        successes = sum(1 for run in day_related if run.get("ok") is True)
        auth_failures = sum(1 for run in day_related if _is_authentication_failure(run))
        durations = [float(run["durationMs"]) for run in day_related if isinstance(run.get("durationMs"), (int, float))]
        update = updates.get(source_service) or {}
        services.append({
            "service": service, "connection_id": connection.get("id"), "configured": configured,
            "auth_type": connection.get("authType"), "state": state, "state_reason": reason,
            "last_activity_at": latest.get("completedAt") if latest else None,
            "last_activity_ok": latest.get("ok") if latest else None,
            "last_activity_error": latest.get("errorCode") if latest and latest.get("ok") is False else None,
            "last_verified_at": latest_success.get("completedAt") if latest_success else None,
            "last_verified_action": latest_success.get("actionId") if latest_success else None,
            "last_duration_ms": latest_success.get("durationMs") if latest_success else None,
            "calls_24h": len(day_related), "successes_24h": successes,
            "failures_24h": len(day_related) - successes, "auth_failures_24h": auth_failures,
            "success_rate_24h": round(successes / len(day_related) * 100, 1) if day_related else None,
            "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "connection_last_modified_at": update.get("connection_last_modified_at"),
            "oauth_client_last_modified_at": update.get("oauth_client_last_modified_at"),
            "refreshable": bool(connection.get("refreshable")), "auth_health": connection.get("authHealth"),
            "credential_expires_at": lifecycle["expires_at"], "expiration_status": lifecycle["expiration_status"],
            "lifecycle_kind": lifecycle["kind"], "lifecycle_label": lifecycle["label"],
            "lifecycle_detail": lifecycle["detail"], "action_required": lifecycle["action_required"],
            "metadata_source": "openconnector_live_api+local_read_only_db", "metadata_collected_at": collected_at,
        })

    def summarize_runs(group_key: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for run in day_runs:
            grouped.setdefault(str(run.get(group_key) or "식별자 없음"), []).append(run)
        output = []
        for label, items in grouped.items():
            successes = sum(1 for item in items if item.get("ok") is True)
            durations = [float(item["durationMs"]) for item in items if isinstance(item.get("durationMs"), (int, float))]
            output.append({group_key: label, "runs": len(items), "successes": successes,
                "failures": len(items) - successes, "success_rate": round(successes / len(items) * 100, 1),
                "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else None})
        return sorted(output, key=lambda item: (-item["runs"], item[group_key]))

    safe_tokens: list[dict[str, Any]] = []
    for token in runtime_tokens if isinstance(runtime_tokens, list) else []:
        if not isinstance(token, dict):
            continue
        allowed_actions = [str(x) for x in token.get("allowedActions", []) if isinstance(x, str) and x != "broker.none"]
        allowed_connections = [str(x) for x in token.get("allowedConnections", []) if isinstance(x, str) and x != "__lease_required__"]
        name = str(token.get("name") or "등록 이름 없음")
        agent = name.split("agent:", 1)[1].split()[0] if "agent:" in name else name
        safe_tokens.append({"id": token.get("id"), "name": name, "agent": agent,
            "created_at": token.get("createdAt"), "last_used_at": token.get("lastUsedAt"),
            "allowed_action_count": len(allowed_actions), "allowed_connection_count": len(allowed_connections),
            "allowed_actions": allowed_actions, "has_current_grant": bool(allowed_actions),
            "nearest_lease_expires_at": token.get("nearestLeaseExpiresAt"),
            "active_lease_count": token.get("activeLeaseCount", 0)})

    lease_review = get_openconnector_lease_review(now)
    lease_items = lease_review["items"]
    lease_overdue = sum(1 for item in lease_items if item["review_status"] == "overdue")
    if lease_overdue:
        actions.append({"severity": "warning", "service": "권한 원장", "title": "검토기한 초과 정리", "detail": f"자동 만료가 아닌 수동 검토 대상 {lease_overdue}건"})
    coalesced_actions: dict[str, dict[str, str]] = {}
    for item in actions:
        service = item["service"]
        if service not in coalesced_actions:
            coalesced_actions[service] = dict(item)
            continue
        current = coalesced_actions[service]
        if item["title"] not in current["title"]:
            current["title"] += f" · {item['title']}"
        if item["detail"] not in current["detail"]:
            current["detail"] += f" / {item['detail']}"
        if item["severity"] == "error":
            current["severity"] = "error"
    actions = sorted(coalesced_actions.values(), key=lambda item: (item["severity"] != "error", item["service"]))
    counts = {key: sum(1 for item in services if item["state"] == key) for key in ("healthy", "warning", "error", "attention")}
    success_count = sum(1 for run in day_runs if run.get("ok") is True)
    provider_registration = sum(1 for item in services if item["lifecycle_kind"] == "provider_registration")
    return {
        "summary": {"managed": len(services), **counts, "runs_24h": len(day_runs),
            "success_rate_24h": round(success_count / len(day_runs) * 100, 1) if day_runs else None,
            "oauth_auto_refresh": sum(1 for item in services if item["lifecycle_kind"] == "oauth_auto_refresh"),
            "until_revoked": sum(1 for item in services if item["lifecycle_kind"] == "until_revoked"),
            "provider_registration": provider_registration, "action_count": len(actions),
            "runtime_tokens": len(safe_tokens), "current_grants": sum(1 for item in safe_tokens if item["has_current_grant"]),
            "lease_review_records": len(lease_items), "lease_review_overdue": lease_overdue},
        "services": services, "runtime_tokens": safe_tokens, "lease_reviews": lease_items,
        "lease_review_error": lease_review["error"], "service_usage_24h": summarize_runs("service"),
        "caller_usage_24h": summarize_runs("caller"), "action_queue": actions,
        "recent_activity": [{"service": run.get("service"), "action": run.get("actionId"),
            "caller": run.get("caller"), "ok": run.get("ok"), "error_code": run.get("errorCode"),
            "completed_at": run.get("completedAt"), "duration_ms": run.get("durationMs")}
            for run in runs[:20]],
        "checked_at": int(now), "metadata_collected_at": collected_at,
        "console_url": OPENCONNECTOR_CONSOLE_URL,
    }


def get_openconnector_status() -> dict[str, Any]:
    started = time.monotonic()
    api_ok = False
    http_status: int | None = None
    try:
        request = urllib.request.Request(OPENCONNECTOR_HEALTH_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=1.2) as response:
            http_status = response.status
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            api_ok = response.status == 200 and payload.get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        pass
    response_ms = round((time.monotonic() - started) * 1000, 1)

    details: dict[str, Any] = {
        "state": "running" if api_ok else "stopped",
        "label": "Running" if api_ok else "Stopped",
        "api_ok": api_ok,
        "http_status": http_status,
        "response_ms": response_ms,
        "container": OPENCONNECTOR_CONTAINER,
        "console_url": OPENCONNECTOR_CONSOLE_URL,
    }
    inspected = safe_run(
        ["docker", "inspect", OPENCONNECTOR_CONTAINER, "--format", "{{json .}}"],
        timeout=1.5,
    )
    if not inspected or inspected.returncode != 0:
        return details
    try:
        container = json.loads(inspected.stdout)
        state = container.get("State") or {}
        health = (state.get("Health") or {}).get("Status") or "unknown"
        details.update({
            "container_status": state.get("Status") or "unknown",
            "container_health": health,
            "started_at": state.get("StartedAt"),
            "restart_count": container.get("RestartCount", 0),
            "image": (container.get("Config") or {}).get("Image") or "unknown",
        })
        if state.get("Running") and api_ok:
            details.update({"state": "running", "label": "Running"})
        elif state.get("Running"):
            details.update({"state": "unknown", "label": "Degraded"})
        else:
            details.update({"state": "stopped", "label": "Stopped"})
    except (TypeError, ValueError):
        pass
    return details


def get_services() -> dict[str, dict[str, Any]]:
    return {
        "openclaw": service_status("openclaw", ["openclaw"]),
        "ollama": service_status("ollama", ["ollama"]),
        "tailscale": service_status("tailscaled", ["tailscaled", "tailscale"]),
        "openconnector": get_openconnector_status(),
    }


def get_uptime() -> dict[str, Any]:
    try:
        seconds = int(time.time() - psutil.boot_time())
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        return {"seconds": seconds, "label": f"{days}d {hours}h {minutes}m", "status": "ok"}
    except Exception:
        return {"seconds": None, "label": "Unknown", "status": "error"}


def mb(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024**2), 1)


def gb(value: float | int | None) -> float | None:
    return bytes_to_gb(value)


def process_name(pid: int | None) -> str | None:
    if pid is None:
        return None
    try:
        return psutil.Process(pid).name()
    except Exception:
        return None


def top_processes(kind: str, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    attrs = ["pid", "name", "cpu_percent", "memory_percent", "memory_info"]
    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            mem_info = info.get("memory_info")
            rows.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "Unknown",
                "cpu_percent": pct(info.get("cpu_percent") or 0),
                "memory_percent": pct(info.get("memory_percent") or 0),
                "rss_mb": mb(mem_info.rss if mem_info else None),
            })
        except Exception:
            continue
    key = "cpu_percent" if kind == "cpu" else "rss_mb"
    return sorted(rows, key=lambda item: item.get(key) or 0, reverse=True)[:limit]


def disk_partitions() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            rows.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": gb(usage.total),
                "used_gb": gb(usage.used),
                "free_gb": gb(usage.free),
                "usage_percent": pct(usage.percent),
            })
        except Exception:
            continue
    return rows


def folder_size(path: str) -> dict[str, Any]:
    result = safe_run(["du", "-sb", path], timeout=1.2)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return {"path": path, "size_gb": None, "status": "unknown"}
    try:
        size = int(result.stdout.split()[0])
        return {"path": path, "size_gb": gb(size), "status": "ok"}
    except Exception:
        return {"path": path, "size_gb": None, "status": "error"}


def major_folder_sizes() -> list[dict[str, Any]]:
    raw_folders = os.getenv("PLACHEM_MONITOR_FOLDERS", "/tmp")
    candidates = [item.strip() for item in raw_folders.split(",") if item.strip()]
    rows = [folder_size(path) for path in candidates if Path(path).exists()]
    return sorted(rows, key=lambda item: item.get("size_gb") or 0, reverse=True)


def gpu_processes() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    query = "gpu_bus_id,gpu_uuid,pid,process_name,used_memory"
    result = safe_run(["nvidia-smi", f"--query-compute-apps={query}", "--format=csv,noheader,nounits"], timeout=1.5)
    if not result or result.returncode != 0 or not result.stdout.strip():
        return []
    gpus = get_gpus()
    uuid_to_index: dict[str, int] = {}
    bus_to_index: dict[str, int] = {}
    for g in gpus:
        if g.get("uuid"):
            uuid_to_index[str(g["uuid"])] = g["index"]
        if g.get("pci_bus_id"):
            bus_to_index[str(g["pci_bus_id"])] = g["index"]
    rows: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            bus_id, uuid, pid, name, vram = parts[0], parts[1], int(parts[2]), parts[3], float(parts[4])
        except Exception:
            continue
        gpu_index = uuid_to_index.get(uuid) if uuid else None
        if gpu_index is None:
            gpu_index = bus_to_index.get(bus_id) if bus_id else None
        rows.append({
            "gpu_index": gpu_index,
            "gpu_uuid": uuid if uuid else None,
            "gpu_bus_id": bus_id if bus_id else None,
            "pid": pid,
            "name": name,
            "vram_mb": vram,
        })
    return sorted(rows, key=lambda item: item["vram_mb"], reverse=True)


LLAMA_VALUE_FLAGS = {
    "-m": "model",
    "--model": "model",
    "-c": "context_size",
    "--ctx-size": "context_size",
    "-ngl": "gpu_layers",
    "--n-gpu-layers": "gpu_layers",
    "--tensor-split": "tensor_split",
    "--host": "host",
    "-H": "host",
    "--port": "port",
    "-p": "port",
}

LLAMA_BOOL_FLAGS = {"--metrics", "--help", "-h"}


def _parse_llama_args(args: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in LLAMA_BOOL_FLAGS:
            i += 1
            continue
        if arg in LLAMA_VALUE_FLAGS:
            if i + 1 < len(args):
                out[LLAMA_VALUE_FLAGS[arg]] = args[i + 1]
                i += 2
                continue
        i += 1
    return out


def _llama_health_probe(host: str | None, port: int | None) -> dict[str, Any] | None:
    if not host or not port:
        return None
    url = f"http://{host}:{port}/health"
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            status = resp.status
            resp.read()
        return {"ok": status == 200, "http_status": status, "response_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "http_status": None, "response_ms": int((time.time() - started) * 1000), "error": str(exc)}


def _llama_gpu_usage(pid: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in gpu_processes():
        if item.get("pid") == pid:
            rows.append({"gpu_index": item.get("gpu_index"), "gpu_uuid": item.get("gpu_uuid"), "vram_mb": item.get("vram_mb")})
    return rows


def detect_llama_server() -> dict[str, Any]:
    now = time.time()
    for proc in psutil.process_iter(["pid", "exe", "cmdline", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            exe = info.get("exe") or ""
            cmd = info.get("cmdline") or []
            base = exe.rsplit("/", 1)[-1]
            if "llama-server" not in base and not any("llama-server" in a for a in cmd):
                continue
            args = _parse_llama_args(cmd)
            model_path = args.get("model")
            model = model_path.rsplit("/", 1)[-1] if model_path else None
            gpu_layers = args.get("gpu_layers")
            if isinstance(gpu_layers, str) and gpu_layers.isdigit():
                gpu_layers = int(gpu_layers)
            tensor_split = args.get("tensor_split")
            if isinstance(tensor_split, str):
                try:
                    tensor_split = [float(x) for x in tensor_split.split(",") if x]
                except Exception:
                    tensor_split = None
            port = args.get("port")
            if isinstance(port, str) and port.isdigit():
                port = int(port)
            try:
                rss_mb = round(info["memory_info"].rss / 1024 / 1024, 1)
            except Exception:
                rss_mb = None
            runtime = {
                "running": True,
                "pid": info["pid"],
                "process_name": base if base else None,
                "executable": exe or None,
                "model": model,
                "model_path": model_path,
                "port": port,
                "host": args.get("host"),
                "context_size": int(args["context_size"]) if isinstance(args.get("context_size"), str) and args["context_size"].isdigit() else None,
                "gpu_layers": gpu_layers,
                "tensor_split": tensor_split,
                "uptime_seconds": int(now - psutil.Process(info["pid"]).create_time()),
                "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
                "memory_rss_mb": rss_mb,
                "gpus": _llama_gpu_usage(info["pid"]),
                "health": _llama_health_probe(args.get("host"), port),
            }
            return runtime
        except Exception:
            continue
    return {"running": False}


def network_interfaces() -> list[dict[str, Any]]:
    counters = psutil.net_io_counters(pernic=True)
    stats = psutil.net_if_stats()
    rows: list[dict[str, Any]] = []
    for name, item in counters.items():
        stat = stats.get(name)
        rows.append({
            "name": name,
            "is_up": bool(stat.isup) if stat else None,
            "speed_mbps": stat.speed if stat else None,
            "sent_mb": mb(item.bytes_sent),
            "recv_mb": mb(item.bytes_recv),
            "packets_sent": item.packets_sent,
            "packets_recv": item.packets_recv,
            "errin": item.errin,
            "errout": item.errout,
            "dropin": item.dropin,
            "dropout": item.dropout,
        })
    return sorted(rows, key=lambda row: (not bool(row.get("is_up")), row["name"]))


def listening_ports(limit: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            ip = conn.laddr.ip
            port = conn.laddr.port
            rows.append({"ip": ip, "port": port, "pid": conn.pid, "process": process_name(conn.pid) or "Unknown"})
    except Exception:
        return []
    return sorted(rows, key=lambda row: (row["port"], row["ip"]))[:limit]


def extended_services() -> dict[str, dict[str, Any]]:
    services = {
        "openclaw": ("openclaw", ["openclaw"]),
        "ollama": ("ollama", ["ollama"]),
        "tailscale": ("tailscaled", ["tailscaled", "tailscale"]),
        "open_webui": ("open-webui", ["open-webui", "openwebui"]),
        "postiz": ("postiz", ["postiz"]),
        "paperclip": ("paperclip", ["paperclip"]),
        "server_monitor": ("plachem-ai-server-monitor", ["uvicorn", "plachem-ai-server-monitor"]),
    }
    statuses: dict[str, dict[str, Any]] = {
        key: service_status(service, names) for key, (service, names) in services.items()
    }
    statuses["openconnector"] = get_openconnector_status()
    return statuses


def detail_response(name: str, payload: dict[str, Any], status: str = "ok") -> dict[str, Any]:
    return {"name": name, "status": status, "timestamp": int(time.time()), "last_updated": time.strftime("%H:%M:%S"), **payload}


@app.get("/api/detail/cpu")
def detail_cpu() -> dict[str, Any]:
    try:
        return detail_response("cpu", {
            "summary": get_cpu(),
            "core_percent": [pct(value) for value in psutil.cpu_percent(interval=0.1, percpu=True)],
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "top_processes": top_processes("cpu"),
        })
    except Exception as exc:
        return detail_response("cpu", {"error": str(exc)}, "error")


@app.get("/api/detail/memory")
def detail_memory() -> dict[str, Any]:
    try:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return detail_response("memory", {
            "summary": get_memory(),
            "memory": {
                "total_gb": gb(mem.total),
                "used_gb": gb(mem.used),
                "free_gb": gb(mem.free),
                "available_gb": gb(mem.available),
                "cached_gb": gb(getattr(mem, "cached", 0)),
                "buffers_gb": gb(getattr(mem, "buffers", 0)),
                "usage_percent": pct(mem.percent),
            },
            "swap": {
                "total_gb": gb(swap.total),
                "used_gb": gb(swap.used),
                "free_gb": gb(swap.free),
                "usage_percent": pct(swap.percent),
            },
            "top_processes": top_processes("memory"),
        })
    except Exception as exc:
        return detail_response("memory", {"error": str(exc)}, "error")


@app.get("/api/detail/gpu")
def detail_gpu() -> dict[str, Any]:
    try:
        return detail_response("gpu", {"summary": get_gpu(), "processes": gpu_processes()})
    except Exception as exc:
        return detail_response("gpu", {"error": str(exc)}, "error")


@app.get("/api/detail/llama")
def detail_llama() -> dict[str, Any]:
    try:
        return detail_response("llama", {"runtime": detect_llama_server()})
    except Exception as exc:
        return detail_response("llama", {"runtime": {"running": False}}, "error")


def _clamp_percent(value: Any) -> int | None:
    if value is None:
        return None
    try:
        v = int(round(float(value)))
    except Exception:
        return None
    return max(0, min(100, v))


def _sanitize_error(exc: Exception) -> str:
    try:
        msg = str(exc)
    except Exception:
        msg = "internal error"
    msg = msg[:200]
    msg = re.sub(r"(?i)(token|secret|api[_-]?key|password|credential|private[_-]?key|authorization|cookie|oauth)[^,\s}]*",
                 "[REDACTED]", msg)
    return msg or "unknown error"


def _whitelist_bucket(bucket: Any) -> dict[str, Any] | None:
    if not isinstance(bucket, dict):
        return None
    used = bucket.get("usedPercent")
    resets = bucket.get("resetsAt")
    window = bucket.get("windowDurationMins")
    remaining = _clamp_percent(100 - used if isinstance(used, (int, float)) else None)
    return {
        "used_percent": used if isinstance(used, (int, float)) else None,
        "remaining_percent": remaining,
        "resets_at": resets if isinstance(resets, (int, float)) else None,
        "window_duration_mins": window if isinstance(window, (int, float)) else None,
    }


def _collect_codex_rate_limits(timeout: float = 12.0) -> dict[str, Any]:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise RuntimeError("codex CLI not found on PATH")

    import subprocess as _sp
    import select as _sel
    import os as _os

    proc = _sp.Popen(
        [codex_bin, "app-server", "--listen", "stdio://"],
        stdin=_sp.PIPE,
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
    )

    def _send(payload: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.flush()

    def _read(timeout: float) -> dict[str, Any]:
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            ready, _, _ = _sel.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = _os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                # Drain all available lines, return the last JSON-RPC response
                result: dict[str, Any] = {}
                while b"\n" in buf:
                    idx = buf.index(b"\n")
                    line = buf[:idx].decode("utf-8", errors="ignore").strip()
                    buf = buf[idx+1:]
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "id" in obj:
                            result = obj
                        elif isinstance(obj, dict) and obj.get("id") is not None:
                            result = obj
                    except Exception:
                        pass
                if result:
                    return result
        return {}

    try:
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"clientInfo": {"name": "plachem-monitor", "version": "1.0.0"}}})
        init = _read(8)
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init.get('error', {}).get('message', 'unknown')}")

        _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        resp = _read(8)
        if "error" in resp:
            raise RuntimeError(f"rateLimits/read failed: {resp.get('error', {}).get('message', 'unknown')}")

        result = resp.get("result", {})
        rate_limits = result.get("rateLimits", {})
        rate_limits_by_id = result.get("rateLimitsByLimitId", {})

        weekly = {
            "limit_id": rate_limits.get("limitId"),
            "used_percent": None,
            "remaining_percent": None,
            "resets_at": None,
            "window_duration_mins": None,
        }
        primary = rate_limits.get("primary") or {}
        if isinstance(primary, dict):
            weekly.update(_whitelist_bucket(primary))

        spark: dict[str, Any] = {
            "limit_name": None,
            "primary": None,
            "secondary": None,
        }
        bengalfox = rate_limits_by_id.get("codex_bengalfox") or {}
        if isinstance(bengalfox, dict):
            spark["limit_name"] = bengalfox.get("limitName")
            spark["primary"] = _whitelist_bucket(bengalfox.get("primary"))
            spark["secondary"] = _whitelist_bucket(bengalfox.get("secondary"))

        return {"weekly": weekly, "spark": spark}
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


@app.get("/api/detail/codex")
def detail_codex() -> dict[str, Any]:
    try:
        data = _collect_codex_rate_limits()
        return detail_response("codex", {
            "codex": {
                "weekly": data["weekly"],
            },
            "spark": {
                "limit_name": data["spark"].get("limit_name"),
                "primary": data["spark"].get("primary"),
                "secondary": data["spark"].get("secondary"),
            },
        })
    except Exception as exc:
        return detail_response("codex", {"codex": None, "spark": None, "error": _sanitize_error(exc)}, "error")


@app.get("/api/detail/disk")
def detail_disk() -> dict[str, Any]:
    try:
        return detail_response("disk", {"summary": get_disk(), "partitions": disk_partitions(), "major_folders": major_folder_sizes()})
    except Exception as exc:
        return detail_response("disk", {"error": str(exc)}, "error")


@app.get("/api/detail/network")
def detail_network() -> dict[str, Any]:
    try:
        return detail_response("network", {
            "summary": get_network(),
            "interfaces": network_interfaces(),
            "listening_ports": listening_ports(),
            "tailscale": service_status("tailscaled", ["tailscaled", "tailscale"]),
        })
    except Exception as exc:
        return detail_response("network", {"error": str(exc)}, "error")


@app.get("/api/openclaw/status")
def api_openclaw_status() -> dict[str, Any]:
    try:
        return collect_openclaw_status()
    except Exception as exc:
        return {"status": "error", "timestamp": int(time.time()), "last_updated": time.strftime("%H:%M:%S"), "error": str(exc), "summary": {"connection": "Error", "agent_count": 0, "model_count": 0, "provider_count": 0, "skill_count": 0, "gateway": "unknown"}, "agents": [], "providers": [], "skills": {"total": 0, "by_source": {}, "items": []}, "gateway": {"state": "unknown", "ok": False}, "raw": {}}


@app.get("/api/detail/services")
def detail_services() -> dict[str, Any]:
    try:
        dashboard: dict[str, Any] | None = None
        dashboard_error: str | None = None
        try:
            dashboard = get_openconnector_dashboard()
        except Exception as exc:
            dashboard_error = str(exc)
        return detail_response("services", {
            "services": extended_services(),
            "openconnector_dashboard": dashboard,
            "openconnector_dashboard_error": dashboard_error,
        })
    except Exception as exc:
        return detail_response("services", {"error": str(exc)}, "error")


@app.get("/api/openconnector/dashboard")
def api_openconnector_dashboard() -> dict[str, Any]:
    try:
        return detail_response("openconnector", {"dashboard": get_openconnector_dashboard()})
    except Exception as exc:
        return detail_response("openconnector", {"error": str(exc)}, "error")


@app.get("/compact")
def compact_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/openclaw")
def openclaw_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/openclaw-control")
def openclaw_control_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/war-room")
def war_room_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "war-room.html")


@app.get("/fast-gateway")
def fast_gateway_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "fast-gateway.html")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    cpu = get_cpu()
    memory = get_memory()
    disk = get_disk()
    network = get_network()
    gpus = get_gpus()
    gpu = gpus[0] if gpus else {
        "uuid": None,
        "name": "Unknown",
        "usage_percent": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_usage_percent": None,
        "temperature_c": None,
        "power_draw_w": None,
        "power_limit_w": None,
        "fan_speed_percent": None,
        "pci_bus_id": None,
        "pcie_gen_current": None,
        "pcie_gen_max": None,
        "pcie_width_current": None,
        "pcie_width_max": None,
        "status": "unknown",
        "source": "none",
    }
    services = get_services()
    openclaw = collect_openclaw_status()
    openconnector_error = None
    try:
        openconnector = get_openconnector_dashboard()
    except Exception as exc:
        openconnector_error = _sanitize_error(exc)
        openconnector = {"summary": {"managed": 0, "healthy": 0, "warning": 0, "error": 0, "unverified": 0}}

    service_states = [item["state"] for item in services.values()]
    gateway_state = openclaw.get("summary", {}).get("gateway", "unknown")
    oc_summary = openconnector.get("summary", {})
    oc_healthy = oc_summary.get("healthy", 0)
    oc_total = oc_summary.get("managed", 0)
    oc_attention = oc_summary.get("attention", oc_summary.get("unverified", 0))
    oc_errors = oc_summary.get("error", 0)

    gpu_summary = gpu_risk_summary(gpus)
    gpu_risk = gpu_summary["risk"]
    gpu_headroom_gb = gpu_summary["headroom_gb"]
    gpu_temp_c = gpu_summary["temperature_c"]

    has_error = any(
        item.get("status") == "error" for item in [cpu, memory, disk, network]
    ) or "error" in service_states or gateway_state in {"down", "probe_failed"} or gpu_risk == "error"
    has_warning = any(item.get("status") in {"unknown", "warming"} for item in [network]) or any(
        state in {"stopped", "unknown"} for state in service_states
    ) or gateway_state == "degraded" or gpu_risk == "warning" or openconnector_error is not None or (
        oc_total > 0 and (oc_attention > 0 or oc_errors > 0)
    )

    overall = "error" if has_error else "warning" if has_warning else "normal"

    return {
        "timestamp": int(time.time()),
        "last_updated": time.strftime("%H:%M:%S"),
        "server": {
            "name": os.getenv("PLACHEM_MONITOR_SERVER_NAME", "PLACHEM-AI-01"),
            "os": f"{platform.system()} {platform.release()}",
            "uptime": get_uptime(),
        },
        "overall": overall,
        "overall_breakdown": {
            "cpu": cpu.get("status"),
            "memory": memory.get("status"),
            "disk": disk.get("status"),
            "network": network.get("status"),
            "gpu": gpu_risk,
            "services": {k: v.get("state") for k, v in services.items()},
            "gateway": gateway_state,
            "openconnector": {"healthy": oc_healthy, "total": oc_total, "attention": oc_attention, "error": oc_errors, "state": "unavailable" if openconnector_error else "available"},
        },
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "network": network,
        "gpu": gpu,
        "gpus": gpus,
        "gpu_risk": gpu_risk,
        "gpu_headroom_gb": gpu_headroom_gb,
        "gpu_temperature_c": gpu_temp_c,
        "services": services,
        "gateway": openclaw.get("gateway"),
        "openconnector_summary": oc_summary,
        "openconnector_error": openconnector_error,
        "gpu_risk_detail": gpu_summary,
    }


def _is_automated_session_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("heartbeat", "smoke", "test", "benchmark", ":explicit:"))


def _select_erpmanager_session(sessions: dict[str, Any]) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    candidates: list[tuple[tuple[int, int, int], str, dict[str, Any]]] = []
    for key, item in sessions.items():
        if not isinstance(item, dict) or item.get("status") == "failed":
            continue
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        is_webchat_direct = (
            origin.get("provider") == "webchat"
            and origin.get("surface") == "webchat"
            and (origin.get("chatType") == "direct" or item.get("chatType") == "direct")
        )
        is_dashboard = ":dashboard:" in str(key)
        if _is_automated_session_key(str(key)):
            continue
        status_active = item.get("status") in {"running", "working", "active"}
        priority = (
            2 if is_dashboard else 1 if is_webchat_direct else 0,
            1 if status_active else 0,
            int(item.get("updatedAt") or 0),
        )
        candidates.append((priority, str(key), item))
    if not candidates:
        return None, None
    _, key, item = max(candidates, key=lambda candidate: candidate[0])
    return key, item


def _runtime_agent_sessions(runtime_status: dict[str, Any], agent_id: str) -> dict[str, Any]:
    all_sessions = runtime_status.get("sessions")
    if not isinstance(all_sessions, list):
        raise RuntimeError("session status unavailable")
    sessions: dict[str, Any] = {}
    for item in all_sessions:
        if not isinstance(item, dict) or str(item.get("agentId")) != agent_id:
            continue
        key = str(item.get("key") or item.get("sessionId") or "")
        if key:
            sessions[key] = item
    return sessions


def _collect_agent_context(agent_id: str) -> dict[str, Any]:
    sessions = _runtime_agent_sessions(read_openclaw_runtime_status(), agent_id)
    best_key, best_item, best_updated = None, None, 0
    if agent_id == "erpmanager":
        best_key, best_item = _select_erpmanager_session(sessions)
    else:
        for key, item in sessions.items():
            if not isinstance(item, dict):
                continue
            updated = int(item.get("updatedAt") or 0)
            if updated >= best_updated:
                best_key, best_item, best_updated = key, item, updated
    if best_item is None:
        raise RuntimeError("no active session found")
    total_tokens = best_item.get("totalTokens")
    context_tokens = best_item.get("contextTokens")
    input_tokens = best_item.get("inputTokens")
    output_tokens = best_item.get("outputTokens")
    cache_read = best_item.get("cacheRead")
    cache_write = best_item.get("cacheWrite")
    budget = best_item.get("contextBudgetStatus")
    estimate_source = None
    if isinstance(budget, dict) and isinstance(budget.get("estimatedPromptTokens"), (int, float)):
        budget = dict(budget)
        estimate_source = str(budget.get("source") or "pre-prompt-estimate")
    else:
        budget = _read_prompt_budget_from_gateway_journal(
            agent_id=agent_id,
            session_key=str(best_key),
            session_id=str(best_item.get("sessionId") or ""),
            context_token_budget=context_tokens,
        )
        estimate_source = "gateway-journal" if budget else None

    estimated_prompt_tokens = budget.get("estimatedPromptTokens") if budget else None
    context_token_budget = budget.get("contextTokenBudget") if budget else context_tokens
    prompt_budget = budget.get("promptBudgetBeforeReserve") if budget else None
    reserve_tokens = budget.get("reserveTokens") if budget else None
    remaining_prompt_budget_tokens = budget.get("remainingPromptBudgetTokens") if budget else None
    overflow_tokens = budget.get("overflowTokens") if budget else None
    route = budget.get("route") if budget else None
    # Dashboard sessions do not always persist contextBudgetStatus. In that
    # case totalTokens is the best available live context usage counter.
    if estimated_prompt_tokens is None and isinstance(total_tokens, (int, float)):
        estimated_prompt_tokens = total_tokens
        estimate_source = "session-total-tokens"
    if prompt_budget is None and isinstance(context_tokens, (int, float)):
        prompt_budget = context_tokens
    if remaining_prompt_budget_tokens is None and isinstance(estimated_prompt_tokens, (int, float)) and isinstance(prompt_budget, (int, float)):
        remaining_prompt_budget_tokens = max(0, prompt_budget - estimated_prompt_tokens)
    prompt_percent = round(estimated_prompt_tokens / prompt_budget * 100) if isinstance(estimated_prompt_tokens, (int, float)) and isinstance(prompt_budget, (int, float)) and prompt_budget else None
    denom = (cache_read or 0) + (cache_write or 0) + (input_tokens or 0)
    cache_hit_percent = round(cache_read / denom * 100) if cache_read and denom else None
    if prompt_percent is None:
        health = None
    elif isinstance(overflow_tokens, (int, float)) and overflow_tokens > 0:
        health = "overflow"
    elif prompt_percent < 70:
        health = "normal"
    elif prompt_percent < 85:
        health = "warning"
    else:
        health = "critical"
    started = best_item.get("sessionStartedAt")
    now_ms = int(time.time() * 1000)
    duration_seconds = round((now_ms - started) / 1000) if isinstance(started, (int, float)) else None
    return {
        "agent_id": agent_id,
        "session_key": best_key,
        "session_id": best_item.get("sessionId"),
        "model": best_item.get("model"),
        "total_tokens": total_tokens,
        "context_tokens": context_tokens,
        "context_percent": prompt_percent,
        "prompt_percent": prompt_percent,
        "estimate_source": estimate_source,
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "context_token_budget": context_token_budget,
        "prompt_budget_before_reserve": prompt_budget,
        "reserve_tokens": reserve_tokens,
        "remaining_prompt_budget_tokens": remaining_prompt_budget_tokens,
        "overflow_tokens": overflow_tokens,
        "route": route,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cache_hit_percent": cache_hit_percent,
        "session_started_at": started,
        "updated_at": best_item.get("updatedAt"),
        "duration_seconds": duration_seconds,
        "health": health,
    }


_PROMPT_BUDGET_LOG_RE = re.compile(
    r"\[context-overflow-precheck\].*?sessionKey=(?P<session_key>\S+).*?"
    r"route=(?P<route>\S+).*?estimatedPromptTokens=(?P<estimated>\d+).*?"
    r"promptBudgetBeforeReserve=(?P<prompt_budget>\d+).*?overflowTokens=(?P<overflow>\d+).*?"
    r"reserveTokens=(?P<reserve>\d+)"
)


def _read_prompt_budget_from_gateway_journal(
    agent_id: str,
    session_key: str,
    session_id: str,
    context_token_budget: Any,
) -> dict[str, Any] | None:
    result = safe_run(
        ["journalctl", "--user", "-u", "openclaw-gateway", "--no-pager", "-o", "cat", "-n", "5000"],
        timeout=2.0,
    )
    if result is None or result.returncode != 0:
        return None
    if not session_key or not session_id:
        return None
    session_file_marker = f"/sessions/{session_id}.jsonl"
    latest = None
    for line in result.stdout.splitlines():
        if "[context-overflow-precheck]" not in line or session_file_marker not in line:
            continue
        match = _PROMPT_BUDGET_LOG_RE.search(line)
        if match and match.group("session_key") == session_key:
            latest = match
    if latest is None:
        return None
    estimated = int(latest.group("estimated"))
    prompt_budget = int(latest.group("prompt_budget"))
    overflow = int(latest.group("overflow"))
    reserve = int(latest.group("reserve"))
    resolved_context_budget = int(context_token_budget) if isinstance(context_token_budget, (int, float)) else prompt_budget + reserve
    return {
        "source": "gateway-journal",
        "estimatedPromptTokens": estimated,
        "contextTokenBudget": resolved_context_budget,
        "promptBudgetBeforeReserve": prompt_budget,
        "reserveTokens": reserve,
        "remainingPromptBudgetTokens": max(0, prompt_budget - estimated),
        "overflowTokens": overflow,
        "route": latest.group("route"),
    }


def _collect_erpmanager_context(timeout: float = 2.0) -> dict[str, Any]:
    return _collect_agent_context("erpmanager")


@app.get("/api/detail/erpmanager")
def detail_erpmanager() -> dict[str, Any]:
    try:
        return detail_response("erpmanager", _collect_erpmanager_context())
    except Exception as exc:
        return detail_response("erpmanager", {"error": _sanitize_error(exc)}, "error")


@app.get("/api/detail/secretary")
def detail_secretary() -> dict[str, Any]:
    try:
        return detail_response("secretary", _collect_agent_context("secretary"))
    except Exception as exc:
        return detail_response("secretary", {"error": _sanitize_error(exc)}, "error")


# ---------------------------------------------------------------------------
# Agent Watchdog (read-only consumer of agent-stall-detector results)
#
# Reads detection results already produced by the agent-stall-detector tool.
# Never runs its own detection logic, never writes to the detector DB,
# and never takes any action against agents (no kill/restart/recovery).
# ---------------------------------------------------------------------------
import sqlite3 as _sqlite3

WATCHDOG_DB_PATH = os.environ.get(
    "PLACHEM_STALL_DETECTIONS_DB",
    "/home/plachem-sever/.openclaw/workspace/projects/agent-stall-detector/data/detections.sqlite",
)
WATCHDOG_MAX_AGE_MS = 30 * 60 * 1000  # detector output older than 30 min -> UNAVAILABLE

# Map detector kinds to the 4 display buckets requested by the UI
_KIND_BUCKET = {
    "STALL": "STALL",
    "LOOP": "LOOP",
    "COMPACTION_STREAK": "CONTEXT",
    "CONTEXT_EVIDENCE": "CONTEXT",
    "FG_STALL": "STALL",
    "FG_FAIL_RECOVERABLE": "FG_FAIL",
}
_BUCKET_LABEL = {
    "STALL": "STALL",
    "LOOP": "LOOP",
    "CONTEXT": "CONTEXT/COMPACTION",
    "FG_FAIL": "FG_FAIL",
}

def _wd_now_ms() -> int:
    return int(time.time() * 1000)

def _fmt_ts(ms):
    if ms in (None, 0):
        return None
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(ms / 1000.0))
    except Exception:
        return None

def _wd_evidence_reason(kind: str, ev: dict) -> str:
    """One-line human reason from evidence dict; no secrets, no truncation surprises."""
    try:
        if kind == "STALL":
            return f"session idle {ev.get('idle_seconds', 'UNAVAILABLE')}s"
        if kind == "FG_STALL":
            secs = ev.get("running_seconds", ev.get("idle_seconds"))
            return f"FastGateway run running {secs if secs is not None else 'UNAVAILABLE'}s"
        if kind == "LOOP":
            n = ev.get("count_in_window", "UNAVAILABLE")
            lbl = str(ev.get("fingerprint_label") or ev.get("fingerprint") or "")[:60]
            return f"same call repeated {n}x ({lbl})"
        if kind == "COMPACTION_STREAK":
            return f"{ev.get('count', 'UNAVAILABLE')} compactions in session"
        if kind == "CONTEXT_EVIDENCE":
            return f"context at {ev.get('percent', 'UNAVAILABLE')}% of budget"
        if kind == "FG_FAIL_RECOVERABLE":
            return f"FastGateway FAIL ({ev.get('reason', 'UNAVAILABLE')})"
    except Exception:
        pass
    return "UNAVAILABLE"

# Authoritative state overlay for the front-page Agent Watchdog card.
# The card must show the same State as OpenClaw Control -> Agents
# (compute_agent_display_state).  Detector rows only remain auxiliary
# evidence when they verifiably belong to the agent's CURRENT active session.
_JEV_ABNORMAL_STATES = {"SALVAGE", "DEAD"}


def _authoritative_agent_states() -> tuple[dict[str, dict[str, Any]], str | None]:
    """Per-agent display_state from OpenClaw real sessions + current-session JEV."""
    try:
        home = openclaw_home()
        config = read_json_file(home / "openclaw.json")
        if not isinstance(config, dict):
            return {}, "authoritative_state_unavailable"
        states: dict[str, dict[str, Any]] = {}
        for row in collect_openclaw_agents(config, home):
            session = row.get("session") or {}
            states[str(row.get("id"))] = {
                "display_state": str(session.get("display_state") or "IDLE"),
                "session_id": session.get("session_id"),
                "basis": session.get("basis"),
                "jev_decision": session.get("jev_decision"),
                "jev_confidence": session.get("jev_confidence"),
                "jev_observed_at": session.get("jev_observed_at"),
                "session_age_seconds": session.get("session_age_seconds"),
                "token_telemetry": session.get("token_telemetry") or {},
            }
        return states, None
    except Exception:
        return {}, "authoritative_state_unavailable"


def _detection_is_current(r: _sqlite3.Row, states: dict[str, dict[str, Any]]) -> bool:
    """True only when the detection verifiably belongs to the agent's current session."""
    if r["core_run_id"]:
        return False  # FastGateway run records never define agent State
    agent_state = states.get(str(r["agent_id"] or ""))
    if not agent_state:
        return False
    current_sid = agent_state.get("session_id")
    detection_sid = str(r["session_id"] or "")
    return bool(current_sid) and bool(detection_sid) and detection_sid == current_sid


def _collect_agent_watchdog() -> dict[str, Any]:
    now = _wd_now_ms()
    agents: dict[str, dict[str, Any]] = {}
    scan_ts = None
    unavailable_reason = None
    try:
        con = _sqlite3.connect(f"file:{WATCHDOG_DB_PATH}?mode=ro", uri=True)
        con.row_factory = _sqlite3.Row
        try:
            try:
                row = con.execute(
                    "SELECT max(ts_ms) AS ts FROM scan_runs"
                ).fetchone()
                if row is not None and row["ts"] is not None:
                    scan_ts = int(row["ts"])
            except Exception:
                scan_ts = None
            rows = con.execute(
                """SELECT agent_id, kind, severity, status, label,
                          ts_detected_ms, first_seen_ms, last_seen_ms,
                          evidence_json, session_id, core_run_id
                   FROM detections
                   WHERE status IN ('NEW','CONFIRMED')
                   ORDER BY ts_detected_ms DESC"""
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        return {
            "available": False,
            "unavailable_reason": _sanitize_error(exc),
            "scan_ts": None,
            "stale": True,
            "agents": {"UNAVAILABLE": {"status": "UNAVAILABLE", "buckets": {}, "warnings": []}},
        }

    if scan_ts is not None and (now - scan_ts) > WATCHDOG_MAX_AGE_MS:
        unavailable_reason = "detector output stale (>30 min)"
    if scan_ts is None:
        unavailable_reason = "no scan data found"

    states, state_error = _authoritative_agent_states()
    for agent_id, st in states.items():
        agents[agent_id] = {
            "status": st["display_state"],
            "display_state": st["display_state"],
            "session_id": st.get("session_id"),
            "basis": st.get("basis"),
            "jev_decision": st.get("jev_decision"),
            "jev_confidence": st.get("jev_confidence"),
            "jev_observed_at": st.get("jev_observed_at"),
            "session_age_seconds": st.get("session_age_seconds"),
            "buckets": {},
            "warnings": [],
            "ignored_detection_count": 0,
            "token_telemetry": st.get("token_telemetry") or {},
            "token_status": (st.get("token_telemetry") or {}).get("status", "UNAVAILABLE"),
        }
    legacy_agents: dict[str, dict[str, Any]] = {}
    for r in rows:
        agent = r["agent_id"] or "UNAVAILABLE"
        kind = r["kind"] or "UNAVAILABLE"
        bucket = _KIND_BUCKET.get(kind, "OTHER")
        legacy = legacy_agents.setdefault(agent, {"status": "ABNORMAL", "buckets": {}, "warnings": []})
        lb = legacy["buckets"].setdefault(bucket, {"count": 0, "worst": "WARN", "latest_ts": None})
        lb["count"] += 1
        if (r["severity"] or "WARN") == "FAIL":
            lb["worst"] = "FAIL"
        if not _detection_is_current(r, states):
            # Past/other-session/FastGateway detection: kept in the detector DB
            # untouched but excluded from current State, summary and ALERT.
            info = agents.get(agent)
            if info is not None:
                info["ignored_detection_count"] = int(info.get("ignored_detection_count") or 0) + 1
            continue
        info = agents.setdefault(agent, {"status": "IDLE", "display_state": "IDLE", "buckets": {}, "warnings": [], "ignored_detection_count": 0})
        sev = r["severity"] or "WARN"
        try:
            ev = json.loads(r["evidence_json"]) if r["evidence_json"] else {}
            if not isinstance(ev, dict):
                ev = {}
        except Exception:
            ev = {}
        b = info["buckets"].setdefault(bucket, {"count": 0, "worst": "WARN", "latest_ts": None})
        b["count"] += 1
        if sev == "FAIL":
            b["worst"] = "FAIL"
        ts = r["ts_detected_ms"] or r["last_seen_ms"]
        if ts and (b["latest_ts"] is None or ts > b["latest_ts"]):
            b["latest_ts"] = ts
        if len(info["warnings"]) < 5:
            info["warnings"].append({
                "kind": kind,
                "bucket": _BUCKET_LABEL.get(bucket, bucket),
                "severity": sev,
                "ts": ts,
                "ts_text": _fmt_ts(ts) or "UNAVAILABLE",
                "reason": r["label"] or _wd_evidence_reason(kind, ev) or "UNAVAILABLE",
            })
    # cap warnings per agent, newest first
    for agent, info in agents.items():
        info["warnings"].sort(key=lambda w: w["ts"] or 0, reverse=True)
        info["warnings"] = info["warnings"][:5]

    # Agents the authoritative source cannot see keep an UNAVAILABLE card; they
    # are never promoted to ABNORMAL by stale detector rows.
    for agent_id, legacy in legacy_agents.items():
        if agent_id in states:
            continue
        info = agents.setdefault(agent_id, {"buckets": {}, "warnings": [], "ignored_detection_count": 0})
        info["status"] = "UNAVAILABLE"
        info["display_state"] = "UNAVAILABLE"
        info["buckets"] = legacy["buckets"]
        info["ignored_detection_count"] = sum(b["count"] for b in legacy["buckets"].values())

    abnormal = [a for a in agents.values() if str(a.get("display_state") or "") in _JEV_ABNORMAL_STATES]
    token_abnormal_agents = sorted(
        str(agent_id) for agent_id, info in agents.items()
        if str((info.get("token_telemetry") or {}).get("status") or "") == "ABNORMAL"
    )
    if state_error and not states:
        unavailable_reason = unavailable_reason or state_error

    return {
        "available": unavailable_reason is None,
        "unavailable_reason": unavailable_reason,
        "scan_ts": scan_ts,
        "scan_ts_text": _fmt_ts(scan_ts) or "UNAVAILABLE",
        "stale": unavailable_reason is not None,
        "agent_count": len(agents),
        "abnormal_count": len(abnormal),
        "abnormal_agents": sorted(str(k) for k, v in agents.items() if str(v.get("display_state") or "") in _JEV_ABNORMAL_STATES),
        "state_source": "openclaw_sessions+jev",
        "legacy_detector_abnormal_count": len(legacy_agents),
        "token_abnormal_count": len(token_abnormal_agents),
        "token_abnormal_agents": token_abnormal_agents,
        "agents": agents,
    }

@app.get("/api/detail/agent-watchdog")
def detail_agent_watchdog() -> dict[str, Any]:
    try:
        return detail_response("agent-watchdog", _collect_agent_watchdog())
    except Exception as exc:
        return detail_response(
            "agent-watchdog",
            {
                "available": False,
                "unavailable_reason": _sanitize_error(exc),
                "scan_ts": None,
                "scan_ts_text": "UNAVAILABLE",
                "stale": True,
                "agent_count": 0,
                "abnormal_count": 0,
                "agents": {"UNAVAILABLE": {"status": "UNAVAILABLE", "buckets": {}, "warnings": []}},
            },
        )
