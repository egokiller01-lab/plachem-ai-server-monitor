import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jev_recovery_live as live


def _write_valid_handoff(path: Path) -> None:
    sections = []
    for name in live.HandoffWriter.REQUIRED:
        value = "recorded failed approach" if name == "Failed Methods" else f"value for {name}"
        sections.append(f"## {name}\n{value}\n")
    path.write_text("\n".join(sections), encoding="utf-8")


def test_phase_a_writes_durable_handoff_without_agent_session(monkeypatch, tmp_path):
    handoff_dir = tmp_path / ".jev-recovery" / "source"
    handoff_dir.mkdir(parents=True)
    handoff = handoff_dir / "RECOVERY_HANDOFF.md"
    rows = []

    monkeypatch.setattr(
        live,
        "_session_context",
        lambda agent_id, session_id: {
            "latest_user": "finish the current task",
            "recent_assistant_texts": ["implemented module A", "tests started"],
            "command_texts": ["pytest -q", "pytest -q"],
            "latest_run": {
                "available": True, "active": True, "terminal": False,
                "control_only": False, "run_id": "run-1", "status": None,
            },
            "run_id": "run-1",
        },
    )
    monkeypatch.setattr(
        live.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Phase A must not spawn a helper session")),
    )
    monkeypatch.setattr(
        live,
        "_agent_command",
        lambda **kw: (_ for _ in ()).throw(AssertionError("Phase A must not build an agent command")),
    )
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))

    result = live._phase_a_prepare_handoff(
        agent_id="erpmanager",
        session_id="source",
        session_key="agent:erpmanager:task",
        detection={"kind": "LOOP", "severity": "WARN"},
        handoff=handoff,
        handoff_dir=handoff_dir,
        timeout=10,
    )

    assert result is None
    assert live.HandoffWriter(handoff_dir).validate(handoff) is True
    text = handoff.read_text(encoding="utf-8")
    assert "finish the current task" in text
    assert "pytest -q" in text
    assert "writer: deterministic-code" in text
    assert ":jev-handoff:" not in text
    assert any(
        row.get("phase") == "A_HANDOFF"
        and row.get("status") == "COMPLETE"
        and row.get("writer") == "code"
        for row in rows
    )


def test_phase_a_reuses_valid_handoff_after_prior_timeout(monkeypatch, tmp_path):
    handoff_dir = tmp_path / ".jev-recovery" / "source"
    handoff_dir.mkdir(parents=True)
    handoff = handoff_dir / "RECOVERY_HANDOFF.md"
    _write_valid_handoff(handoff)
    rows = []

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("existing valid handoff must not launch another salvage turn")

    monkeypatch.setattr(live.subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))

    result = live._phase_a_prepare_handoff(
        agent_id="erpmanager",
        session_id="source",
        session_key="agent:erpmanager:dashboard:x",
        detection={"kind": "CONTEXT_EVIDENCE"},
        handoff=handoff,
        handoff_dir=handoff_dir,
        timeout=10,
    )

    assert result is None
    assert any(row.get("phase") == "A_HANDOFF" and row.get("status") == "REUSED" for row in rows)


def test_recover_live_runs_code_handoff_then_one_recovery_turn(monkeypatch, tmp_path):
    rows = []
    order = []

    class Snapshot:
        pass

    snapshot = Snapshot()
    ctx = {
        "agent_id": "erpmanager",
        "session_id": "source-session",
        "session_key": "agent:erpmanager:dashboard:x",
        "status": "running",
        "run_id": "source-run",
        "latest_run": {
            "available": True, "active": True, "terminal": False,
            "control_only": False, "status": None,
        },
    }

    monkeypatch.setattr(live, "LIVE_HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(live, "_workspace_for", lambda agent_id: tmp_path)
    monkeypatch.setattr(live, "snapshot_state_digest", lambda value: "same")
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, object(), snapshot))
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))

    def phase_a(**kwargs):
        order.append("A")
        _write_valid_handoff(kwargs["handoff"])

    def phase_b(**kwargs):
        order.append("B")
        return True, True, True

    def phase_ce(**kwargs):
        order.append("CE")
        assert kwargs["handoff"].exists()
        return {
            "session_id": "target-session",
            "text": "RECOVERY_COMPLETE done",
            "recovery_status": "RECOVERY_COMPLETE",
            "handoff_ack": True,
        }

    monkeypatch.setattr(live, "_phase_a_prepare_handoff", phase_a)
    monkeypatch.setattr(live, "_phase_b_terminate_source", phase_b)
    monkeypatch.setattr(live, "_phase_c_restart_and_resume", phase_ce)

    with live._LOCK:
        live._INFLIGHT.add("source-session")
    live._recover_live(
        ctx=ctx,
        snapshot=snapshot,
        detection={"kind": "CONTEXT_EVIDENCE"},
        decision_choice="SALVAGE",
        confidence=0.85,
    )

    assert order == ["A", "B", "CE"]
    outcome = [r for r in rows if r.get("type") == "live_recovery_outcome"][-1]
    assert outcome["status"] == "RECOVERY_COMPLETE"
    assert outcome["handoff_ack"] is True
    assert outcome["target_session_id"] == "target-session"
    assert "source-session" not in live._INFLIGHT


def test_recovery_error_records_failed_phase_and_detail(monkeypatch, tmp_path):
    rows = []

    class Snapshot:
        pass

    snapshot = Snapshot()
    ctx = {
        "agent_id": "erpmanager",
        "session_id": "source-session",
        "session_key": "agent:erpmanager:dashboard:x",
        "status": "running",
        "run_id": "source-run",
    }

    monkeypatch.setattr(live, "LIVE_HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(live, "_workspace_for", lambda agent_id: tmp_path)
    monkeypatch.setattr(live, "snapshot_state_digest", lambda value: "same")
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, object(), snapshot))
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(
        live,
        "_phase_a_prepare_handoff",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("HANDOFF_WRITE_TIMEOUT")),
    )

    live._recover_live(
        ctx=ctx,
        snapshot=snapshot,
        detection={"kind": "CONTEXT_EVIDENCE"},
        decision_choice="SALVAGE",
        confidence=0.85,
    )

    outcome = [r for r in rows if r.get("type") == "live_recovery_outcome"][-1]
    assert outcome["status"] == "RECOVERY_ERROR"
    assert outcome["phase"] == "A_HANDOFF"
    assert "HANDOFF_WRITE_TIMEOUT" in outcome["error_detail"]

def test_source_task_active_rejects_terminal_and_control_runs():
    terminal_ctx = {
        "status": "running",
        "latest_run": {
            "available": True, "active": False, "terminal": True,
            "control_only": False, "status": "success",
        },
    }
    assert live._source_task_active(terminal_ctx) == (False, "latest_run_terminal:success")

    control_ctx = {
        "status": "running",
        "latest_run": {
            "available": True, "active": True, "terminal": False,
            "control_only": True, "status": None,
        },
    }
    assert live._source_task_active(control_ctx) == (False, "control_only_run")

    active_ctx = {
        "status": "running",
        "latest_run": {
            "available": True, "active": True, "terminal": False,
            "control_only": False, "status": None,
        },
    }
    assert live._source_task_active(active_ctx) == (True, "active_task_run")


def test_low_margin_salvage_is_deferred_not_recovered(monkeypatch):
    rows = []
    started = []

    class FakeActivity:
        scope = live.Availability(True, {"auto_recovery_allowed": True})

    class FakeDecision:
        choice = "SALVAGE"
        confidence = 0.53
        probabilities = {"SALVAGE": 0.53, "WATCH": 0.42, "CONTINUE": 0.05, "DEAD": 0.0}
        state_digest = "d"

    class FakeJudge:
        def decide(self, **kwargs):
            return FakeDecision()

    class FakeFeatures:
        features = {"state_digest": "d"}
        state_digest = "d"

    ctx = {
        "status": "running",
        "latest_run": {
            "available": True, "active": True, "terminal": False,
            "control_only": False, "status": None,
        },
    }

    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, FakeActivity(), object()))
    monkeypatch.setattr(live, "FeatureBuilder", lambda: type("FB", (), {"build": lambda self, snap: FakeFeatures()})())
    monkeypatch.setattr(live, "ExistingOpenConnectorHealthJudge", lambda: FakeJudge())
    monkeypatch.setattr(live, "snapshot_state_digest", lambda snap: "stable")
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(live.threading, "Thread", lambda *a, **kw: started.append((a, kw)))

    result = live.evaluate_live_candidate({
        "agent_id": "erpmanager",
        "session_id": "source",
        "session_key": "agent:erpmanager:main",
        "kind": "STALL",
        "severity": "WARN",
    })

    assert result["choice"] == "SALVAGE"
    assert result["action"] == "SALVAGE_DEFERRED_RECHECK"
    assert result["auto_recovery_deferred"] is True
    assert result["defer_reason"] == "low_confidence"
    assert started == []
    assert "source" not in live._INFLIGHT


def test_candidate_skips_control_only_run_before_jev(monkeypatch):
    rows = []

    class FakeActivity:
        scope = live.Availability(True, {"auto_recovery_allowed": True})

    ctx = {
        "status": "running",
        "latest_run": {
            "available": True,
            "run_id": "announce-run",
            "active": True,
            "terminal": False,
            "control_only": True,
            "prompt": "Agent-to-agent announce step.",
            "status": None,
        },
    }
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, FakeActivity(), object()))
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))

    class ForbiddenJudge:
        def __init__(self, *a, **kw):
            raise AssertionError("JEV must not run for control-only announce turns")

    monkeypatch.setattr(live, "ExistingOpenConnectorHealthJudge", ForbiddenJudge)

    result = live.evaluate_live_candidate({
        "agent_id": "erpmanager",
        "session_id": "source",
        "session_key": "agent:erpmanager:main",
        "kind": "STALL",
    })
    assert result["status"] == "NO_ACTIVE_TASK"
    assert result["reason"] == "control_only_run"
    assert result["action"] == "OBSERVE_ONLY"


def test_phase_a_loop_failed_methods_are_code_derived(monkeypatch, tmp_path):
    handoff_dir = tmp_path / ".jev-recovery" / "source"
    handoff_dir.mkdir(parents=True)
    handoff = handoff_dir / "RECOVERY_HANDOFF.md"

    monkeypatch.setattr(
        live,
        "_session_context",
        lambda agent_id, session_id: {
            "latest_user": "process the input",
            "recent_assistant_texts": ["still trying"],
            "command_texts": ["grep -n OMEGA input.txt .", "grep -n OMEGA input.txt ."],
            "latest_run": {
                "available": True, "active": True, "terminal": False,
                "control_only": False, "run_id": "run-loop", "status": None,
            },
        },
    )
    monkeypatch.setattr(live, "_append", lambda row: None)
    monkeypatch.setattr(
        live.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no helper process allowed")),
    )

    live._phase_a_prepare_handoff(
        agent_id="erpmanager",
        session_id="source",
        session_key="agent:erpmanager:task",
        detection={"kind": "LOOP"},
        handoff=handoff,
        handoff_dir=handoff_dir,
        timeout=10,
    )
    text = handoff.read_text(encoding="utf-8")
    failed = text.split("## Failed Methods\n", 1)[1].split("\n\n", 1)[0]
    assert "grep -n OMEGA input.txt ." in failed
    assert "None" not in failed


def test_recover_live_does_not_restart_if_source_finishes_during_handoff(monkeypatch, tmp_path):
    rows = []
    order = []

    class Snapshot:
        pass

    snapshot = Snapshot()
    initial_ctx = {
        "agent_id": "erpmanager",
        "session_id": "source-session",
        "session_key": "agent:erpmanager:task",
        "status": "running",
        "run_id": "source-run",
        "latest_run": {
            "available": True, "active": True, "terminal": False,
            "control_only": False, "status": None,
        },
    }
    finished_ctx = {
        **initial_ctx,
        "latest_run": {
            "available": True, "active": False, "terminal": True,
            "control_only": False, "status": "success",
        },
    }

    monkeypatch.setattr(live, "LIVE_HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(live, "_workspace_for", lambda agent_id: tmp_path)
    monkeypatch.setattr(live, "snapshot_state_digest", lambda value: "same")
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (initial_ctx, object(), snapshot))
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))

    def phase_a(**kwargs):
        order.append("A")
        _write_valid_handoff(kwargs["handoff"])
        return None

    monkeypatch.setattr(live, "_phase_a_prepare_handoff", phase_a)
    monkeypatch.setattr(live, "_session_context", lambda *a, **k: finished_ctx)
    monkeypatch.setattr(live, "_phase_c_restart_and_resume", lambda **kwargs: order.append("CE"))

    live._recover_live(
        ctx=initial_ctx,
        snapshot=snapshot,
        detection={"kind": "PERIODIC_HEALTH"},
        decision_choice="SALVAGE",
        confidence=0.9,
    )

    assert order == ["A"]
    outcomes = [r for r in rows if r.get("type") == "live_recovery_outcome"]
    assert outcomes[-1]["status"] == "STALE_REEVALUATED_NO_ACTION"
    assert outcomes[-1]["reason"] == "source_finished_before_confirmed_abort"


def test_recover_live_does_not_start_handoff_after_task_run_finished(monkeypatch, tmp_path):
    rows = []
    called = []

    class Snapshot:
        pass

    snapshot = Snapshot()
    ctx = {
        "agent_id": "erpmanager",
        "session_id": "source-session",
        "session_key": "agent:erpmanager:main",
        "status": "running",
        "latest_run": {
            "available": True, "active": False, "terminal": True,
            "control_only": False, "status": "success",
        },
    }
    monkeypatch.setattr(live, "LIVE_HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(live, "snapshot_state_digest", lambda value: "same")
    monkeypatch.setattr(live, "_make_snapshot", lambda **kwargs: (ctx, object(), snapshot))
    monkeypatch.setattr(live, "_append", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(live, "_phase_a_prepare_handoff", lambda **kwargs: called.append(True))

    live._recover_live(
        ctx=ctx,
        snapshot=snapshot,
        detection={"kind": "STALL"},
        decision_choice="SALVAGE",
        confidence=0.9,
    )

    assert called == []
    outcome = [r for r in rows if r.get("type") == "live_recovery_outcome"][-1]
    assert outcome["status"] == "STALE_REEVALUATED_NO_ACTION"
    assert outcome["reason"] == "latest_run_terminal:success"
