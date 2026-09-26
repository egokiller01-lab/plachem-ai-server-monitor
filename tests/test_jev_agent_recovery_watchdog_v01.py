import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_agent_recovery_watchdog_v01 import *


class Collector:
    def collect(self, activity):
        return ObserverSnapshot(
            "snap-1", 1.0, activity,
            Availability(True, {"step": "compile"}), Availability(True, ["report.md"]),
            Availability.unsupported("git source unavailable"), Availability(True, {"checkpoint": 2}),
            Availability(True, {"passed": 3}), Availability.unsupported("repetition detector unavailable"),
        )


class JEV:
    def decide(self, *, feature_state, idempotency_key):
        return JEVDecision("SALVAGE", {"CONTINUE": 0.1, "WATCH": 0.1, "SALVAGE": 0.7, "DEAD": 0.1},
                           state_digest(feature_state["snapshot"]))


class Sessions:
    def __init__(self): self.calls = []
    def create(self, *, agent_id, handoff, attempt):
        self.calls.append((agent_id, attempt, handoff.handoff_id)); return f"new-{attempt}"


class RejectingDirectSessionPort(InMemoryDirectSessionPort):
    def await_handoff_ack(self, *, session_id, handoff_path):
        return False


def activity(status="RUNNING"):
    u = Availability.unsupported("not exposed")
    return AgentActivity("ERPcoder", "old-session", status, Availability(True, "compile"), u, u,
                         Availability(True, {"production": False, "destructive": False}), u)


def test_digest_and_unsupported_are_canonical(tmp_path):
    h = AppendOnlyHistory(tmp_path / "history.jsonl")
    sessions = Sessions(); controller = RecoveryController(session_factory=sessions, history=h)
    snapshot = Collector().collect(activity())
    digest = state_digest(snapshot.facts())
    decision = JEV().decide(feature_state={"snapshot": snapshot.facts()}, idempotency_key="x")
    assert decision.state_digest == state_digest(snapshot.facts())
    assert snapshot.git_diff.available is False
    assert snapshot.git_diff.reason and "unavailable" in snapshot.git_diff.reason


def test_event_driven_salvage_handoff_and_no_old_session_close(tmp_path):
    h = AppendOnlyHistory(tmp_path / "history.jsonl")
    sessions = Sessions(); c = RecoveryController(session_factory=sessions, history=h)
    w = WatchdogV01(collector=Collector(), decision_port=JEV(), recovery=c, history=h)
    result = w.observe(activity(), remaining_work=["finish test"])
    assert result["status"] == "OBSERVED"
    assert result["recovery_outcome"]["kind"] == "SALVAGE_HANDOFF"
    assert sessions.calls == [("ERPcoder", 1, sessions.calls[0][2])]
    assert result["recovery_outcome"]["source_session_id"] == "old-session"


def test_stale_digest_and_terminal_and_overlap_guards(tmp_path):
    h = AppendOnlyHistory(tmp_path / "history.jsonl")
    c = RecoveryController(session_factory=Sessions(), history=h)
    w = WatchdogV01(collector=Collector(), decision_port=JEV(), recovery=c, history=h)
    stale = c.apply(decision=JEVDecision("SALVAGE", {}, "wrong"), activity=activity(),
                    snapshot=Collector().collect(activity()), remaining_work=["x"])
    assert stale.kind == "STALE_REJECTED"
    assert w.observe(activity("COMPLETED"), remaining_work=[]) == {"status": "TERMINAL", "polled": False}
    w._inflight = True
    assert w.observe(activity(), remaining_work=[]) == {"status": "OVERLAP_BLOCKED", "polled": False}


def test_dead_is_main_escalation_only_and_salvage_is_idempotent(tmp_path):
    h = AppendOnlyHistory(tmp_path / "history.jsonl")
    sessions = Sessions(); c = RecoveryController(session_factory=sessions, history=h)
    snapshot = Collector().collect(activity())
    dead = c.apply(decision=JEVDecision("DEAD", {}, state_digest(snapshot.facts())), activity=activity(),
                   snapshot=snapshot, remaining_work=["x"])
    assert dead.status == "ESCALATE_MAIN" and not sessions.calls
    first = c.apply(decision=JEVDecision("SALVAGE", {}, state_digest(snapshot.facts())), activity=activity(),
                    snapshot=snapshot, remaining_work=["x"])
    second = c.apply(decision=JEVDecision("SALVAGE", {}, state_digest(snapshot.facts())), activity=activity(),
                     snapshot=snapshot, remaining_work=["x"])
    assert first.kind == "SALVAGE_HANDOFF" and second.kind == "SALVAGE_DUPLICATE"


def test_final_validation_combines_actual_and_advisory(tmp_path):
    result = FinalValidation().validate(actual_result={"completed": True},
        tests=Availability(True, {"passed": 4}), files=Availability(True, ["x"]),
        verifier_advisory=Availability(True, {"choice": "PASS"}), remaining_work=[])
    assert result["status"] == "PASS"


def test_e2e_one_safe_recovery_cycle(tmp_path):
    """Synthetic approved-safe task: abnormal -> salvage -> new session -> finish -> validate."""
    history_path = tmp_path / "e2e-history.jsonl"
    sessions = Sessions()
    history = AppendOnlyHistory(history_path)
    controller = RecoveryController(session_factory=sessions, history=history)
    watchdog = WatchdogV01(collector=Collector(), decision_port=JEV(), recovery=controller, history=history)
    first = watchdog.observe(activity(), remaining_work=["finish test"])
    assert first["recovery_outcome"]["status"] == "HANDOFF_CREATED"
    new_session = first["recovery_outcome"]["target_session_id"]
    final = FinalValidation().validate(
        actual_result={"session_id": new_session, "completed": True},
        tests=Availability(True, {"passed": 4}), files=Availability(True, ["report.md"]),
        verifier_advisory=Availability(True, {"choice": "PASS"}), remaining_work=[])
    history.append({"type": "final_validation", "session_id": new_session, **final})
    rows = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert final["status"] == "PASS"
    assert any(row.get("type") == "jev_decision" for row in rows)
    assert any(row.get("type") == "recovery_outcome" for row in rows)
    assert any(row.get("type") == "final_validation" and row["status"] == "PASS" for row in rows)


def test_fastgateway_and_legacy_watchdog_are_not_imported():
    source = Path(__file__).resolve().parents[1] / "jev_agent_recovery_watchdog_v01.py"
    text = source.read_text(encoding="utf-8")
    assert "import fast_gateway" not in text.lower()
    assert "import jev_session_watchdog" not in text


def test_concrete_collector_features_and_four_physical_streams(tmp_path):
    work = tmp_path / "work"
    work.mkdir(); (work / "checkpoint.json").write_text("{}")
    events = [{"type": "loop", "at": 1}]
    a = activity(); a = AgentActivity(a.agent_id, a.session_id, a.status, a.last_meaningful_activity,
                                      a.active_process, a.context, a.scope, a.recent_events,
                                      task_id="task-1", recovery_generation=2)
    snapshot = ConcreteEvidenceCollector(work, events).collect(a)
    features = FeatureBuilder().build(snapshot).features
    assert features["checkpoint_available"] is True
    assert features["repetition_count"] == 1
    assert features["artifact_count"] >= 1
    assert features["state_digest"]
    store = EvidenceStore(tmp_path / "evidence")
    for kind in store.KINDS:
        store.append(kind, task_id="task-1", session_id="old-session", recovery_generation=2,
                     state_digest_value=features["state_digest"], observed_at=1.0, payload={})
        row = json.loads(store.paths[kind].read_text().splitlines()[-1])
        assert all(row[key] is not None for key in ("task_id", "session_id", "recovery_generation", "state_digest", "observed_at"))


def test_handoff_file_ack_before_cleanup_and_existing_adapter():
    class FakeClient:
        def choice(self, **kwargs):
            return {"choice": "RESTART_SESSION", "probabilities": {"RESTART_SESSION": .8}, "confidence": .8}
    decision = ExistingOpenConnectorHealthJudge(FakeClient()).decide(
        feature_state={"state_digest": "d", "x": 1}, idempotency_key="k")
    assert decision.choice == "SALVAGE"
    root = Path(__file__).parent / "_handoff_test_tmp"
    root.mkdir(exist_ok=True)
    try:
        sessions = Sessions(); direct = InMemoryDirectSessionPort(); closed = []
        controller = RecoveryController(session_factory=sessions, history=AppendOnlyHistory(root / "h.jsonl"),
            handoff_writer=HandoffWriter(root), direct_session=direct,
            cleanup_callback=closed.append)
        snap = Collector().collect(activity())
        outcome = controller.apply(decision=JEVDecision("SALVAGE", {}, state_digest(snap.facts())),
            activity=activity(), snapshot=snap, remaining_work=["finish test"])
        assert outcome.status == "HANDOFF_CREATED"
        assert sessions.calls == []
        assert len(direct.created) == 1
        assert HandoffWriter(root).validate()
        assert closed == ["old-session"]
    finally:
        for p in root.glob("*"): p.unlink()
        root.rmdir()


def test_direct_ack_failure_never_cleans_source_and_never_falls_back(tmp_path):
    sessions = Sessions(); closed = []
    controller = RecoveryController(
        session_factory=sessions, history=AppendOnlyHistory(tmp_path / "h.jsonl"),
        handoff_writer=HandoffWriter(tmp_path), direct_session=RejectingDirectSessionPort(),
        cleanup_callback=closed.append)
    snap = Collector().collect(activity())
    outcome = controller.apply(
        decision=JEVDecision("SALVAGE", {}, state_digest(snap.facts())),
        activity=activity(), snapshot=snap, remaining_work=["finish test"])
    assert outcome.kind == "SALVAGE_BLOCKED"
    assert outcome.reason == "HANDOFF_ACK_REQUIRED"
    assert sessions.calls == []
    assert closed == []


def test_handoff_has_all_required_non_empty_fields(tmp_path):
    sessions = Sessions()
    controller = RecoveryController(session_factory=sessions, history=AppendOnlyHistory(tmp_path / "h.jsonl"),
                                     handoff_writer=HandoffWriter(tmp_path))
    snap = Collector().collect(activity())
    outcome = controller.apply(
        decision=JEVDecision("SALVAGE", {}, state_digest(snap.facts())),
        activity=activity(), snapshot=snap, remaining_work=["finish test"])
    text = (tmp_path / "RECOVERY_HANDOFF.md").read_text(encoding="utf-8")
    for section in HandoffWriter.REQUIRED:
        assert f"## {section}\n" in text
    assert outcome.status == "HANDOFF_CREATED"
