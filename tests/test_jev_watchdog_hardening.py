import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jev_agent_watchdog_supervisor as supervisor
import jev_recovery_live as live


def _active_ctx():
    return {
        "status": "running",
        "session_key": "agent:erpmanager:task",
        "latest_run": {
            "available": True,
            "active": True,
            "terminal": False,
            "control_only": False,
            "status": None,
        },
    }


def test_supervisor_grace_uses_session_start_not_last_interaction(monkeypatch):
    now = 1_000_000.0
    row = {
        "status": "running",
        "sessionId": "s1",
        "key": "agent:erpmanager:task",
        "agentId": "erpmanager",
        "sessionStartedAt": int((now - 301) * 1000),
        "lastInteractionAt": int((now - 10) * 1000),
    }
    calls = []
    supervisor._last.clear()
    monkeypatch.setattr(supervisor, "sessions", lambda: [row])
    monkeypatch.setattr(supervisor, "_session_context", lambda *a, **k: _active_ctx())
    monkeypatch.setattr(
        supervisor,
        "post",
        lambda item: calls.append(item) or {"choice": "CONTINUE", "action": "OBSERVE_ONLY"},
    )

    result = supervisor.tick(now=now)

    assert len(calls) == 1
    assert result[0]["age"] == 301.0


def test_supervisor_never_posts_handoff_session(monkeypatch):
    now = 1_000_000.0
    row = {
        "status": "running",
        "sessionId": "handoff",
        "key": "agent:erpmanager:jev-handoff:source",
        "agentId": "erpmanager",
        "sessionStartedAt": int((now - 900) * 1000),
        "lastInteractionAt": int((now - 900) * 1000),
    }
    supervisor._last.clear()
    monkeypatch.setattr(supervisor, "sessions", lambda: [row])
    monkeypatch.setattr(
        supervisor,
        "post",
        lambda item: (_ for _ in ()).throw(AssertionError("handoff must not be posted")),
    )

    assert supervisor.tick(now=now) == []


def test_supervisor_still_observes_recovery_session(monkeypatch):
    now = 1_000_000.0
    row = {
        "status": "running",
        "sessionId": "recovery",
        "key": "agent:erpmanager:jev-recovery:source",
        "agentId": "erpmanager",
        "sessionStartedAt": int((now - 301) * 1000),
        "lastInteractionAt": int((now - 5) * 1000),
    }
    calls = []
    supervisor._last.clear()
    monkeypatch.setattr(supervisor, "sessions", lambda: [row])
    monkeypatch.setattr(supervisor, "_session_context", lambda *a, **k: _active_ctx())
    monkeypatch.setattr(
        supervisor,
        "post",
        lambda item: calls.append(item) or {
            "choice": "SALVAGE",
            "action": "RECOVERY_GENERATION_LIMIT",
        },
    )

    result = supervisor.tick(now=now)

    assert len(calls) == 1
    assert result[0]["session_class"] == "recovery"
    assert result[0]["action"] == "RECOVERY_GENERATION_LIMIT"


class _FakeActivity:
    scope = live.Availability(True, {"auto_recovery_allowed": True})


class _FakeDecision:
    choice = "SALVAGE"
    confidence = 0.95
    probabilities = {"SALVAGE": 0.95, "WATCH": 0.03, "CONTINUE": 0.02, "DEAD": 0.0}


class _FakeJudge:
    def decide(self, **kwargs):
        return _FakeDecision()


class _FakeFeatures:
    features = {"state_digest": "feature"}
    state_digest = "feature"


def _install_salvage_fakes(monkeypatch, *, ctx=None, stable_digest="stable"):
    if ctx is None:
        ctx = _active_ctx()
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, _FakeActivity(), object()))
    monkeypatch.setattr(
        live,
        "FeatureBuilder",
        lambda: type("FB", (), {"build": lambda self, snap: _FakeFeatures()})(),
    )
    monkeypatch.setattr(live, "ExistingOpenConnectorHealthJudge", lambda: _FakeJudge())
    monkeypatch.setattr(live, "snapshot_state_digest", lambda snap: stable_digest)
    monkeypatch.setattr(live, "_recovery_attempt_exists", lambda session_id: False)


def _clear_live_state():
    with live._LOCK:
        live._INFLIGHT.clear()
        live._SALVAGE_CONFIRMATIONS.clear()




def test_risk_scope_treats_explicit_no_production_as_boundary():
    ctx = {
        "session_key": "agent:qwentest:test",
        "channel": "",
        "status": "running",
        "latest_user": "No network, no production, no git, no service/config changes.",
        "command_texts": [],
    }
    scope = live._risk_scope(ctx)
    assert scope["production"] is False
    assert scope["auto_recovery_allowed"] is True


def test_risk_scope_still_blocks_affirmative_production_action():
    ctx = {
        "session_key": "agent:qwentest:test",
        "channel": "",
        "status": "running",
        "latest_user": "Deploy to production now.",
        "command_texts": [],
    }
    scope = live._risk_scope(ctx)
    assert scope["production"] is True
    assert scope["auto_recovery_allowed"] is False


def test_event_candidate_under_five_minutes_is_observe_only(monkeypatch):
    _clear_live_state()
    ctx = {
        "status": "running",
        "started_at": 1_000_000_000_000,
        "session_key": "agent:erpmanager:task",
        "latest_run": {
            "available": True,
            "active": True,
            "terminal": False,
            "control_only": False,
            "status": None,
        },
    }
    monkeypatch.setattr(live.time, "time", lambda: 1_000_000_100.0)
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, _FakeActivity(), object()))
    rows = []
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(
        live,
        "ExistingOpenConnectorHealthJudge",
        lambda: (_ for _ in ()).throw(AssertionError("JEV must not run inside grace period")),
    )

    result = live.evaluate_live_candidate(
        {
            "agent_id": "erpmanager",
            "session_id": "source",
            "session_key": "agent:erpmanager:task",
            "kind": "LOOP",
        }
    )

    assert result["status"] == "GRACE_PERIOD"
    assert result["action"] == "OBSERVE_ONLY"
    assert result["reason"] == "session_under_5m"


def test_recovery_session_can_be_judged_but_cannot_spawn_recovery(monkeypatch):
    _clear_live_state()
    _install_salvage_fakes(monkeypatch)
    rows = []
    started = []
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(live.threading, "Thread", lambda *a, **kw: started.append((a, kw)))

    result = live.evaluate_live_candidate(
        {
            "agent_id": "erpmanager",
            "session_id": "recovery-session",
            "session_key": "agent:erpmanager:jev-recovery:source",
            "kind": "PERIODIC_HEALTH",
        }
    )

    assert result["choice"] == "SALVAGE"
    assert result["action"] == "RECOVERY_GENERATION_LIMIT"
    assert result["recovery_generation_limit"] == 1
    assert started == []


def test_handoff_session_skips_before_snapshot_or_jev(monkeypatch):
    _clear_live_state()
    rows = []
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(
        live,
        "_make_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("handoff snapshot forbidden")),
    )

    result = live.evaluate_live_candidate(
        {
            "agent_id": "erpmanager",
            "session_id": "handoff-session",
            "session_key": "agent:erpmanager:jev-handoff:source",
            "kind": "PERIODIC_HEALTH",
        }
    )

    assert result["status"] == "CONTROL_SESSION_SKIP"
    assert result["action"] == "OBSERVE_ONLY"


def test_periodic_salvage_requires_two_same_state_confirmations(monkeypatch):
    _clear_live_state()
    monkeypatch.setenv("PLACHEM_JEV_AUTO_RECOVERY_ENABLED", "1")
    _install_salvage_fakes(monkeypatch, stable_digest="same-state")
    monkeypatch.setattr(live, "_append", lambda row: None)

    class FakeThread:
        starts = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            type(self).starts += 1

    monkeypatch.setattr(live.threading, "Thread", FakeThread)

    payload = {
        "agent_id": "erpmanager",
        "session_id": "normal-session",
        "session_key": "agent:erpmanager:task",
        "kind": "PERIODIC_HEALTH",
        "evidence": {"source": "periodic_supervisor"},
    }
    first = live.evaluate_live_candidate(payload)
    with live._LOCK:
        count, last_at, digest = live._SALVAGE_CONFIRMATIONS["normal-session"]
        live._SALVAGE_CONFIRMATIONS["normal-session"] = (
            count,
            last_at - live.AUTO_RECOVERY_CONFIRMATION_GAP_SECONDS - 1,
            digest,
        )
    second = live.evaluate_live_candidate(payload)

    assert first["action"] == "SALVAGE_DEFERRED_RECHECK"
    assert first["defer_reason"] == "confirmation_required"
    assert first["salvage_confirmation_count"] == 1
    assert second["action"] == "RECOVERY_STARTED"
    assert second["salvage_confirmation_count"] == 2
    assert FakeThread.starts == 1


def test_auto_recovery_defaults_to_observe_only(monkeypatch):
    _clear_live_state()
    monkeypatch.delenv("PLACHEM_JEV_AUTO_RECOVERY_ENABLED", raising=False)
    _install_salvage_fakes(monkeypatch, stable_digest="same-state")
    monkeypatch.setattr(live, "_append", lambda row: None)
    monkeypatch.setattr(
        live.threading,
        "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("observe-only mode must not create recovery")),
    )

    payload = {
        "agent_id": "erpmanager",
        "session_id": "observe-only-session",
        "session_key": "agent:erpmanager:task",
        "kind": "PERIODIC_HEALTH",
        "evidence": {"source": "periodic_supervisor"},
    }
    first = live.evaluate_live_candidate(payload)
    with live._LOCK:
        count, last_at, digest = live._SALVAGE_CONFIRMATIONS["observe-only-session"]
        live._SALVAGE_CONFIRMATIONS["observe-only-session"] = (
            count,
            last_at - live.AUTO_RECOVERY_CONFIRMATION_GAP_SECONDS - 1,
            digest,
        )
    second = live.evaluate_live_candidate(payload)

    assert first["action"] == "SALVAGE_DEFERRED_RECHECK"
    assert second["action"] == "RECOVERY_READY_OBSERVE_ONLY"
    assert second["recovery_ready"] is True
    assert second["auto_recovery_execution_enabled"] is False


def test_event_detection_is_evidence_only_and_does_not_confirm_salvage(monkeypatch):
    _clear_live_state()
    _install_salvage_fakes(monkeypatch)
    monkeypatch.setattr(live, "_append", lambda row: None)
    monkeypatch.setattr(
        live.threading,
        "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("event evidence must not recover")),
    )

    result = live.evaluate_live_candidate({
        "agent_id": "erpmanager",
        "session_id": "normal-session",
        "session_key": "agent:erpmanager:task",
        "kind": "LOOP",
        "evidence": {"source": "stall_detector"},
    })

    assert result["action"] == "EVENT_EVIDENCE_ONLY"
    assert result["defer_reason"] == "single_entry_periodic_supervisor_only"
    assert "normal-session" not in live._SALVAGE_CONFIRMATIONS




def test_salvage_confirmation_gap_helper():
    _clear_live_state()
    assert live._confirm_salvage("s", stable_digest="a", now=100.0) == 1
    assert live._confirm_salvage("s", stable_digest="a", now=120.0) == 1
    assert live._confirm_salvage(
        "s",
        stable_digest="a",
        now=100.0 + live.AUTO_RECOVERY_CONFIRMATION_GAP_SECONDS + 1,
    ) == 2


def test_salvage_confirmation_resets_when_state_changes():
    _clear_live_state()
    assert live._confirm_salvage("s", stable_digest="state-a", now=100.0) == 1
    assert live._confirm_salvage(
        "s",
        stable_digest="state-a",
        now=100.0 + live.AUTO_RECOVERY_CONFIRMATION_GAP_SECONDS + 1,
    ) == 2
    assert live._confirm_salvage(
        "s",
        stable_digest="state-b",
        now=200.0,
    ) == 1




def test_runtime_safe_uses_trace_fallback_when_trajectory_missing():
    activity = live.AgentActivity(
        agent_id="qwentest",
        session_id="s",
        status="RUNNING",
        last_meaningful_activity=live.Availability(True, {}),
        active_process=live.Availability(
            True,
            {"session_nonterminal": True, "inflight_tool_calls": 0},
        ),
        context=live.Availability(True, {}),
        scope=live.Availability(True, {"auto_recovery_allowed": True}),
        recent_events=live.Availability(True, []),
    )
    snapshot = live.ObserverSnapshot(
        snapshot_id="x",
        captured_at=0,
        activity=activity,
        progress=live.Availability(True, {}),
        artifacts=live.Availability.unsupported("x"),
        git_diff=live.Availability.unsupported("x"),
        checkpoint=live.Availability.unsupported("x"),
        test_result=live.Availability.unsupported("x"),
        repetition=live.Availability(True, {}),
    )
    ctx = {
        "status": "running",
        "run_id": "run-from-transcript",
        "latest_run": {"available": False},
    }
    assert live._runtime_recovery_safe(ctx, snapshot) is True


def test_existing_recovery_attempt_blocks_duplicate_automatic_attempt(monkeypatch):
    _clear_live_state()
    _install_salvage_fakes(monkeypatch)
    monkeypatch.setattr(live, "_recovery_attempt_exists", lambda session_id: True)
    monkeypatch.setattr(live, "_append", lambda row: None)
    monkeypatch.setattr(
        live.threading,
        "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("duplicate recovery forbidden")),
    )

    result = live.evaluate_live_candidate(
        {
            "agent_id": "erpmanager",
            "session_id": "source",
            "session_key": "agent:erpmanager:task",
            "kind": "STALL",
        }
    )

    assert result["action"] == "RECOVERY_ATTEMPT_EXISTS"


def test_missing_runtime_evidence_blocks_automatic_salvage(monkeypatch):
    _clear_live_state()
    ctx = {
        "status": "running",
        "session_key": "agent:erpmanager:task",
        "latest_run": {
            "available": False,
            "active": False,
            "terminal": False,
            "control_only": False,
            "status": None,
        },
    }
    _install_salvage_fakes(monkeypatch, ctx=ctx)
    monkeypatch.setattr(live, "_append", lambda row: None)
    monkeypatch.setattr(
        live.threading,
        "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unsafe recovery forbidden")),
    )

    result = live.evaluate_live_candidate(
        {
            "agent_id": "erpmanager",
            "session_id": "source",
            "session_key": "agent:erpmanager:task",
            "kind": "STALL",
        }
    )

    assert result["action"] == "SALVAGE_BLOCKED_NO_RUNTIME_EVIDENCE"


def test_recovery_target_key_is_deterministic_per_source(monkeypatch, tmp_path):
    rows = []
    captured = {}

    class Snapshot:
        pass

    snapshot = Snapshot()
    ctx = {
        "agent_id": "erpmanager",
        "session_id": "source-session",
        "session_key": "agent:erpmanager:task",
        "status": "running",
        "run_id": "source-run",
        "latest_run": {
            "available": True,
            "active": True,
            "terminal": False,
            "control_only": False,
            "status": None,
        },
    }
    monkeypatch.setattr(live, "LIVE_HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(live, "_workspace_for", lambda agent_id: tmp_path)
    monkeypatch.setattr(live, "snapshot_state_digest", lambda value: "same")
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, object(), snapshot))
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(live, "_phase_a_prepare_handoff", lambda **kwargs: None)
    monkeypatch.setattr(live, "_phase_b_terminate_source", lambda **kwargs: (True, True, True))

    def phase_ce(**kwargs):
        captured["target_key"] = kwargs["target_key"]
        return {
            "session_id": "target",
            "text": "RECOVERY_COMPLETE",
            "recovery_status": "RECOVERY_COMPLETE",
            "handoff_ack": True,
        }

    monkeypatch.setattr(live, "_phase_c_restart_and_resume", phase_ce)

    live._recover_live(
        ctx=ctx,
        snapshot=snapshot,
        detection={"kind": "STALL"},
        decision_choice="SALVAGE",
        confidence=0.9,
    )

    assert captured["target_key"] == "agent:erpmanager:jev-recovery:source-session"


def test_single_recovery_turn_prompt_is_bounded_to_handoff_and_task_workspace(monkeypatch, tmp_path):
    captured = {}
    phases = []

    def fake_turn(**kwargs):
        captured["message"] = kwargs["message"]
        captured["session_key"] = kwargs["session_key"]
        captured["calls"] = captured.get("calls", 0) + 1
        return {"session_id": "target", "text": "RECOVERY_BLOCKED"}

    monkeypatch.setattr(live, "_agent_turn", fake_turn)
    monkeypatch.setattr(live, "_append_recovery_phase", lambda **kwargs: phases.append(dict(kwargs)))

    result = live._phase_c_restart_and_resume(
        agent_id="qwentest",
        session_id="source",
        handoff=tmp_path / "RECOVERY_HANDOFF.md",
        target_key="agent:qwentest:jev-recovery:source",
        timeout=10,
    )

    message = captured["message"]
    assert captured["calls"] == 1
    assert captured["session_key"] == "agent:qwentest:jev-recovery:source"
    assert "Do not search global OpenClaw history" in message
    assert "unrelated sessions" in message
    assert "wider ~/.openclaw tree" in message
    assert "## Failed Methods" in message
    assert result["recovery_status"] == "RECOVERY_BLOCKED"
    assert result["handoff_ack"] is True
    assert any(p.get("phase") == "D_ACK" and p.get("status") == "IMPLICIT_BY_RECOVERY_TURN" for p in phases)
