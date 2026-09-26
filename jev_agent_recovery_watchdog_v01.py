from __future__ import annotations

"""JEV Agent Recovery Watchdog v0.1.

This module is deliberately independent of the previous watchdog module.  It only
coordinates evidence, a supplied JEV decision port, and a supplied session
factory.  It never calls a gateway orchestrator, creates a session by itself, or mutates
production/lifecycle state.
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

CHOICES = ("CONTINUE", "WATCH", "SALVAGE", "DEAD")
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "DEAD"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def state_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_VOLATILE_STATE_KEYS = {
    "snapshot_id", "captured_at", "observed_at", "last_message_age_sec",
    "idle_seconds", "running_seconds", "observation_delta",
    "no_forward_change", "no_forward_change_sec",
    # Token telemetry is JEV evidence, but its per-minute counters are volatile
    # and must not reset the two-confirmation SALVAGE stable-state guard.
    "token_telemetry",
}


def _stable_state_material(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_state_material(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_STATE_KEYS
            and not str(key).endswith("_age_sec")
        }
    if isinstance(value, (list, tuple)):
        return [_stable_state_material(item) for item in value]
    return value


def snapshot_state_digest(snapshot: "ObserverSnapshot") -> str:
    return state_digest(_stable_state_material(snapshot.facts()))


@dataclass(frozen=True)
class Availability:
    available: bool
    value: Any = None
    reason: str | None = None

    @classmethod
    def unsupported(cls, reason: str) -> "Availability":
        return cls(False, None, reason)


@dataclass(frozen=True)
class AgentActivity:
    agent_id: str
    session_id: str
    status: str
    last_meaningful_activity: Availability
    active_process: Availability
    context: Availability
    scope: Availability
    recent_events: Availability
    task_id: str = "unknown-task"
    recovery_generation: int = 0


@dataclass(frozen=True)
class ObserverSnapshot:
    snapshot_id: str
    captured_at: float
    activity: AgentActivity
    progress: Availability
    artifacts: Availability
    git_diff: Availability
    checkpoint: Availability
    test_result: Availability
    repetition: Availability

    def facts(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class FeatureVector:
    state_digest: str
    features: Mapping[str, Any]


@dataclass(frozen=True)
class JEVDecision:
    choice: str
    probabilities: Mapping[str, float]
    state_digest: str
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    confidence: float | None = None


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    task: str
    previous_agent: str
    session_id: str
    recovery_generation: int
    completed_work: tuple[str, ...]
    changed_files: tuple[str, ...]
    test_results: tuple[str, ...]
    current_problem: str
    failed_methods: tuple[str, ...]
    remaining_work: tuple[str, ...]
    next_agent_first_action: str
    attempt: int
    target_agent_id: str
    evidence_digest: str
    created_at: float

    @property
    def source_session_id(self) -> str:
        return self.session_id


@dataclass(frozen=True)
class RecoveryOutcome:
    outcome_id: str
    kind: str
    status: str
    source_session_id: str
    target_session_id: str | None
    handoff_id: str | None
    reason: str
    state_digest: str
    task_id: str = "unknown-task"
    recovery_generation: int = 0
    observed_at: float = field(default_factory=time.time)


class EvidenceCollector(Protocol):
    def collect(self, activity: AgentActivity) -> ObserverSnapshot: ...


class EvidenceStore:
    """Four physically separate append-only JSONL streams with a common envelope."""
    KINDS = ("agent_activity", "observer_snapshot", "jev_decision", "recovery_outcome")

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = {kind: self.root / f"{kind}.jsonl" for kind in self.KINDS}

    def append(self, kind: str, *, task_id: str, session_id: str,
               recovery_generation: int, state_digest_value: str,
               observed_at: float, payload: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in self.paths:
            raise ValueError(f"unsupported evidence stream: {kind}")
        record = {"task_id": task_id, "session_id": session_id,
                  "recovery_generation": recovery_generation,
                  "state_digest": state_digest_value, "observed_at": observed_at,
                  "type": kind, **dict(payload)}
        with self.paths[kind].open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
        return record


class ConcreteEvidenceCollector:
    """Read-only collector for evidence sources available in a work root."""
    def __init__(self, work_root: str | Path, activity_events: Sequence[Mapping[str, Any]] = ()):
        self.work_root = Path(work_root)
        self.activity_events = tuple(activity_events)

    def _git_diff(self) -> Availability:
        try:
            result = subprocess.run(["git", "-C", str(self.work_root), "diff", "--stat"],
                                    capture_output=True, text=True, timeout=3, check=False)
            return Availability(True, {"exit_code": result.returncode, "stat": result.stdout.strip()})
        except (OSError, subprocess.SubprocessError) as exc:
            return Availability.unsupported(f"git diff unavailable: {type(exc).__name__}")

    def collect(self, activity: AgentActivity) -> ObserverSnapshot:
        files = list(self.work_root.rglob("*")) if self.work_root.exists() else []
        artifacts = [str(p.relative_to(self.work_root)) for p in files if p.is_file()][:200]
        checkpoints = [str(p.relative_to(self.work_root)) for p in files
                       if p.is_file() and "checkpoint" in p.name.lower()]
        tests = [str(p.relative_to(self.work_root)) for p in files
                 if p.is_file() and ("test" in p.name.lower() or "result" in p.name.lower())]
        repetitions = [e for e in self.activity_events
                       if e.get("type") in {"repeat", "loop", "repetition"}]
        return ObserverSnapshot(
            snapshot_id=str(uuid.uuid4()), captured_at=time.time(), activity=activity,
            progress=activity.last_meaningful_activity,
            artifacts=Availability(True, artifacts) if artifacts else Availability.unsupported("artifacts unavailable"),
            git_diff=self._git_diff(),
            checkpoint=Availability(True, checkpoints) if checkpoints else Availability.unsupported("checkpoint unavailable"),
            test_result=Availability(True, tests) if tests else Availability.unsupported("test result unavailable"),
            repetition=Availability(True, {"events": repetitions, "count": len(repetitions)})
                       if repetitions else Availability.unsupported("repetition evidence unavailable"),
        )


class ExistingOpenConnectorHealthJudge:
    """Adapter from existing OpenConnectorJEVClient.choice() to v0.1 choices."""
    CRITERIA = {
        "CONTINUE": (
            "Keep the current session when evidence shows meaningful forward progress, "
            "changing results, or a healthy active run without strong repetition/stall evidence. "
            "An expected artifact still being missing is normal early in a task when a successful "
            "tool result happened very recently, the run is active, and there are no repeated "
            "results, tool errors, or other no-progress signals."
        ),
        "WATCH": (
            "Observe without mutation when evidence is incomplete or ambiguous, the idle/repetition "
            "signal is weak or isolated, or there is not enough evidence yet to justify handoff."
        ),
        "SALVAGE": (
            "Prepare a validated handoff when the session is still responsive but evidence shows "
            "sustained repetition of the same output/tool/error/step with little or no meaningful "
            "progress, clear runaway work expansion/context growth, an active in-flight tool remains "
            "unresolved while expected task artifacts stay missing and no forward state change appears, "
            "or the session is idle after a task attempt while required artifacts/results are still "
            "missing even though the session remains recently responsive."
        ),
        "DEAD": (
            "Escalate to Main only when the session appears unresponsive or unrecoverable; "
            "do not use DEAD merely because telemetry is unsupported."
        ),
    }
    NORMALIZE = {"RECHECK": "WATCH", "RESTART_SESSION": "SALVAGE", "ESCALATE_MAIN": "DEAD"}

    def __init__(self, client: Any | None = None):
        if client is None:
            from jev_task_router import OpenConnectorJEVClient
            client = OpenConnectorJEVClient()
        self.client = client

    def decide(self, *, feature_state: Mapping[str, Any], idempotency_key: str) -> JEVDecision:
        result = self.client.choice(
            question="Choose exactly one v0.1 recovery action from CONTINUE, WATCH, SALVAGE, DEAD.",
            state=dict(feature_state), criteria=self.CRITERIA, idempotency_key=idempotency_key)
        raw_choice = str(result.get("choice", "DEAD")).upper()
        choice = self.NORMALIZE.get(raw_choice, raw_choice)
        if choice not in CHOICES:
            choice = "DEAD"
        probabilities = result.get("probabilities") if isinstance(result.get("probabilities"), Mapping) else {}
        return JEVDecision(choice, dict(probabilities), str(feature_state["state_digest"]),
                           confidence=result.get("confidence"))


class HandoffWriter:
    REQUIRED = (
        "Task", "Previous Agent", "Session ID", "Recovery Generation",
        "Completed Work", "Changed Files", "Test Results", "Current Problem",
        "Failed Methods", "Remaining Work", "Next Agent First Action",
    )

    def __init__(self, work_root: str | Path):
        self.work_root = Path(work_root)

    def write(self, handoff: Handoff) -> Path:
        path = self.work_root / "RECOVERY_HANDOFF.md"
        if path.exists():
            raise FileExistsError("RECOVERY_HANDOFF.md already exists")
        self.work_root.mkdir(parents=True, exist_ok=True)
        def bullets(values: Sequence[str]) -> str:
            return "\n".join(f"- {value}" for value in values)

        content = ("# RECOVERY_HANDOFF\n\n"
                   f"## Task\n{handoff.task}\n\n"
                   f"## Previous Agent\n{handoff.previous_agent}\n\n"
                   f"## Session ID\n{handoff.session_id}\n\n"
                   f"## Recovery Generation\n{handoff.recovery_generation}\n\n"
                   f"## Completed Work\n{bullets(handoff.completed_work)}\n\n"
                   f"## Changed Files\n{bullets(handoff.changed_files)}\n\n"
                   f"## Test Results\n{bullets(handoff.test_results)}\n\n"
                   f"## Current Problem\n{handoff.current_problem}\n\n"
                   f"## Failed Methods\n{bullets(handoff.failed_methods)}\n\n"
                   f"## Remaining Work\n{bullets(handoff.remaining_work)}\n\n"
                   f"## Next Agent First Action\n{handoff.next_agent_first_action}\n\n"
                   "## Recovery Metadata\n"
                   f"- handoff_id: {handoff.handoff_id}\n"
                   f"- attempt: {handoff.attempt}\n"
                   f"- target_agent: {handoff.target_agent_id}\n"
                   f"- evidence_digest: {handoff.evidence_digest}\n"
                   f"- created_at: {handoff.created_at}\n\n"
                   "## Safety\n- Old-session cleanup is forbidden until target acknowledgement.\n")
        path.write_text(content, encoding="utf-8")
        self.validate(path)
        return path

    def validate(self, path: str | Path | None = None) -> bool:
        target = Path(path or (self.work_root / "RECOVERY_HANDOFF.md"))
        text = target.read_text(encoding="utf-8")
        if not text.strip() or any(f"## {section}" not in text for section in self.REQUIRED):
            raise ValueError("RECOVERY_HANDOFF.md missing required non-empty sections")
        for section in self.REQUIRED:
            marker = f"## {section}\n"
            value = text.split(marker, 1)[1].split("\n\n", 1)[0].strip()
            if not value:
                raise ValueError(f"RECOVERY_HANDOFF.md has empty section: {section}")
        return True


class DirectSessionPort(Protocol):
    def create_direct_session(self, *, agent_id: str, handoff_path: str, attempt: int) -> str: ...
    def await_handoff_ack(self, *, session_id: str, handoff_path: str) -> bool: ...


class InMemoryDirectSessionPort:
    def __init__(self):
        self.created: list[tuple[str, str, int]] = []
        self.acks: set[str] = set()

    def create_direct_session(self, *, agent_id: str, handoff_path: str, attempt: int) -> str:
        sid = f"direct-{attempt}-{uuid.uuid4()}"
        self.created.append((sid, handoff_path, attempt))
        return sid

    def await_handoff_ack(self, *, session_id: str, handoff_path: str) -> bool:
        self.acks.add(session_id)
        return True


class JEVDecisionPort(Protocol):
    """The Direct Worker supplies this port; this watchdog never invokes JEV itself."""

    def decide(self, *, feature_state: Mapping[str, Any], idempotency_key: str) -> JEVDecision: ...


class SessionFactory(Protocol):
    def create(self, *, agent_id: str, handoff: Handoff, attempt: int) -> str: ...


class EventTrigger(Protocol):
    def on_event(self, event: Mapping[str, Any]) -> None: ...


class BackstopTimer(Protocol):
    def due(self, now: float) -> bool: ...


class AppendOnlyHistory:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(dict(record)) + "\n")


class RecoveryController:
    def __init__(self, *, session_factory: SessionFactory | None, history: AppendOnlyHistory,
                 main_agent_id: str = "main", handoff_writer: HandoffWriter | None = None,
                 direct_session: DirectSessionPort | None = None,
                 cleanup_callback: Callable[[str], None] | None = None):
        self.session_factory = session_factory
        self.history = history
        self.main_agent_id = main_agent_id
        self._generated: set[str] = set()
        self.handoff_writer = handoff_writer
        self.direct_session = direct_session
        self.cleanup_callback = cleanup_callback

    @staticmethod
    def _handoff_from_activity(*, activity: AgentActivity, attempt: int,
                               target_agent: str, digest: str,
                               remaining_work: Sequence[str],
                               snapshot: ObserverSnapshot) -> Handoff:
        artifacts = snapshot.artifacts.value if snapshot.artifacts.available else []
        tests = snapshot.test_result.value if snapshot.test_result.available else []
        changed_files = tuple(str(item) for item in artifacts) or ("No changed files observed",)
        test_results = (canonical_json(tests),) if tests else ("Test result unavailable",)
        return Handoff(
            handoff_id=str(uuid.uuid4()),
            task=activity.task_id,
            previous_agent=activity.agent_id,
            session_id=activity.session_id,
            recovery_generation=activity.recovery_generation,
            completed_work=("Evidence collection and JEV decision completed",),
            changed_files=changed_files,
            test_results=test_results,
            current_problem=f"Source session status requires {activity.status.lower()} recovery handling",
            failed_methods=("No failed method recorded in current evidence",),
            remaining_work=tuple(remaining_work),
            next_agent_first_action="Read and acknowledge RECOVERY_HANDOFF.md before continuing the remaining work",
            attempt=attempt,
            target_agent_id=target_agent,
            evidence_digest=digest,
            created_at=time.time(),
        )

    def apply(self, *, decision: JEVDecision, activity: AgentActivity,
              snapshot: ObserverSnapshot, remaining_work: Sequence[str],
              deterministic_safe: bool = True) -> RecoveryOutcome:
        digest = state_digest(snapshot.facts())
        if digest != decision.state_digest:
            return self._record("STALE_REJECTED", "BLOCKED", activity.session_id, None, None,
                                "DIGEST_MISMATCH", digest)
        if decision.choice == "DEAD":
            return self._record("MAIN_ESCALATION", "ESCALATE_MAIN", activity.session_id, None, None,
                                "DEAD_MAIN_ONLY", digest)
        if decision.choice != "SALVAGE":
            kind = "NO_ACTION" if decision.choice in {"CONTINUE", "WATCH"} else "BLOCKED"
            return self._record(kind, decision.choice, activity.session_id, None, None,
                                "OBSERVE_ONLY", digest)
        if not deterministic_safe:
            return self._record("SALVAGE_BLOCKED", "ESCALATE_MAIN", activity.session_id, None, None,
                                "SAFETY_INTERLOCK", digest)
        if not remaining_work:
            return self._record("SALVAGE_BLOCKED", "ESCALATE_MAIN", activity.session_id, None, None,
                                "NO_REMAINING_WORK", digest)
        base = canonical_json({"session": activity.session_id, "digest": digest, "remaining": list(remaining_work)})
        if base in self._generated:
            return self._record("SALVAGE_DUPLICATE", "NO_ACTION", activity.session_id, None, None,
                                "HANDOFF_ALREADY_GENERATED", digest)
        self._generated.add(base)
        for attempt, target in ((1, activity.agent_id), (2, "alternate"), (3, self.main_agent_id)):
            handoff = self._handoff_from_activity(activity=activity, attempt=attempt,
                                                   target_agent=target, digest=digest,
                                                   remaining_work=remaining_work, snapshot=snapshot)
            if not self._valid_handoff(handoff, activity.session_id, digest):
                continue
            handoff_path = str(self.handoff_writer.write(handoff)) if self.handoff_writer else None
            target_session = None
            if attempt < 3 and self.direct_session is not None:
                if not handoff_path:
                    return self._record("SALVAGE_BLOCKED", "ESCALATE_MAIN", activity.session_id,
                                        None, handoff.handoff_id, "HANDOFF_WRITER_REQUIRED", digest)
                target_session = self.direct_session.create_direct_session(
                    agent_id=target, handoff_path=handoff_path, attempt=attempt)
                if not self.direct_session.await_handoff_ack(session_id=target_session,
                                                             handoff_path=handoff_path):
                    return self._record("SALVAGE_BLOCKED", "ESCALATE_MAIN", activity.session_id,
                                        None, handoff.handoff_id, "HANDOFF_ACK_REQUIRED", digest)
                if self.cleanup_callback is not None:
                    self.cleanup_callback(activity.session_id)
            elif attempt < 3:
                if self.session_factory is None:
                    return self._record("SALVAGE_BLOCKED", "ESCALATE_MAIN", activity.session_id,
                                        None, handoff.handoff_id, "SESSION_FACTORY_UNAVAILABLE", digest)
                target_session = self.session_factory.create(
                    agent_id=target, handoff=handoff, attempt=attempt)
            return self._record("SALVAGE_HANDOFF", "HANDOFF_CREATED", activity.session_id,
                                target_session, handoff.handoff_id, f"ATTEMPT_{attempt}", digest)
        return self._record("SALVAGE_BLOCKED", "ESCALATE_MAIN", activity.session_id, None, None,
                            "HANDOFF_VALIDATION_FAILED", digest)

    @staticmethod
    def _valid_handoff(handoff: Handoff, source_session_id: str, digest: str) -> bool:
        return bool(handoff.handoff_id and handoff.source_session_id == source_session_id
                    and handoff.evidence_digest == digest and handoff.remaining_work)

    def _record(self, kind: str, status: str, source: str, target: str | None,
                handoff: str | None, reason: str, digest: str) -> RecoveryOutcome:
        outcome = RecoveryOutcome(str(uuid.uuid4()), kind, status, source, target, handoff, reason, digest)
        self.history.append({"type": "recovery_outcome", **asdict(outcome)})
        return outcome


class WatchdogV01:
    def __init__(self, *, collector: EvidenceCollector, decision_port: JEVDecisionPort,
                 recovery: RecoveryController, history: AppendOnlyHistory,
                 main_agent_id: str = "main", evidence_store: EvidenceStore | None = None):
        self.collector = collector
        self.decision_port = decision_port
        self.recovery = recovery
        self.history = history
        self.main_agent_id = main_agent_id
        self._inflight = False
        self._last_digest: str | None = None
        self.evidence_store = evidence_store

    def observe(self, activity: AgentActivity, *, remaining_work: Sequence[str],
                now: float | None = None, trigger: str = "event") -> dict[str, Any]:
        if self._inflight:
            return {"status": "OVERLAP_BLOCKED", "polled": False}
        if activity.status in TERMINAL:
            return {"status": "TERMINAL", "polled": False}
        self._inflight = True
        try:
            snapshot = self.collector.collect(activity)
            features = FeatureBuilder().build(snapshot)
            digest = features.state_digest
            self._last_digest = digest
            self.history.append({"type": "agent_activity", "activity": _jsonable(asdict(activity)),
                                 "captured_at": now or time.time()})
            self.history.append({"type": "observer_snapshot", "snapshot": snapshot.facts(),
                                 "state_digest": digest})
            observed_at = now or time.time()
            if self.evidence_store:
                common = {"task_id": activity.task_id, "session_id": activity.session_id,
                          "recovery_generation": activity.recovery_generation,
                          "state_digest_value": digest, "observed_at": observed_at}
                self.evidence_store.append("agent_activity", payload={"activity": _jsonable(asdict(activity))}, **common)
                self.evidence_store.append("observer_snapshot", payload={"snapshot": snapshot.facts()}, **common)
            decision = self.decision_port.decide(feature_state=features.features,
                                                 idempotency_key=f"jev-recovery-v01:{activity.session_id}:{digest}")
            if decision.state_digest != digest or decision.choice not in CHOICES:
                decision = JEVDecision("DEAD", {x: 1.0 if x == "DEAD" else 0.0 for x in CHOICES}, digest)
            safe, _ = deterministic_safety_interlock(snapshot)
            outcome = self.recovery.apply(decision=decision, activity=activity, snapshot=snapshot,
                                          remaining_work=remaining_work, deterministic_safe=safe)
            record = {"type": "jev_decision", "trigger": trigger, "captured_at": now or time.time(),
                      "activity": _jsonable(asdict(activity)), "observer_snapshot": snapshot.facts(),
                      "feature_state": features.features, "state_digest": digest,
                      "jev_decision": asdict(decision), "recovery_outcome": asdict(outcome)}
            self.history.append(record)
            if self.evidence_store:
                self.evidence_store.append("jev_decision", payload={"trigger": trigger,
                    "feature_state": features.features, "decision": asdict(decision)}, **common)
                self.evidence_store.append("recovery_outcome", payload={"outcome": asdict(outcome)}, **common)
            return {"status": "OBSERVED", **record}
        finally:
            self._inflight = False


class FeatureBuilder:
    def build(self, snapshot: ObserverSnapshot) -> FeatureVector:
        facts = snapshot.facts()
        def avail(name: str) -> bool:
            return bool(facts.get(name, {}).get("available"))
        repetition = facts.get("repetition", {}).get("value") if avail("repetition") else None
        recent = facts.get("activity", {}).get("recent_events", {})
        features = {
            "schema": "jev-agent-recovery-watchdog-v0.1",
            "state_digest": state_digest(facts),
            "progress_observed": avail("progress"),
            "progress_value": facts.get("progress", {}).get("value") if avail("progress") else None,
            "artifact_count": len(facts.get("artifacts", {}).get("value", [])) if avail("artifacts") else None,
            "git_diff_available": avail("git_diff"),
            "checkpoint_available": avail("checkpoint"),
            "test_result_available": avail("test_result"),
            "repetition_count": repetition.get("count") if isinstance(repetition, Mapping) else None,
            "recent_event_count": len(recent.get("value", [])) if recent.get("available") else None,
            "session_response_available": bool(facts.get("activity", {}).get("context", {}).get("available")),
            "active_process_available": bool(facts.get("activity", {}).get("active_process", {}).get("available")),
            "scope_available": bool(facts.get("activity", {}).get("scope", {}).get("available")),
            "availability_policy": "unsupported_is_not_zero",
            "choices": list(CHOICES),
        }
        features["snapshot"] = facts
        return FeatureVector(features["state_digest"], features)


class EventDrivenObserver:
    """Event-first trigger; timer is only a backstop and never the primary loop."""
    def __init__(self, watchdog: WatchdogV01, timer: BackstopTimer):
        self.watchdog, self.timer = watchdog, timer

    def on_event(self, activity: AgentActivity, event: Mapping[str, Any], *, remaining_work: Sequence[str]) -> dict[str, Any]:
        return self.watchdog.observe(activity, remaining_work=remaining_work, trigger=str(event.get("type", "event")))

    def on_backstop(self, activity: AgentActivity, now: float, *, remaining_work: Sequence[str]) -> dict[str, Any] | None:
        if not self.timer.due(now):
            return None
        return self.watchdog.observe(activity, remaining_work=remaining_work, now=now, trigger="backstop_timer")


class FinalValidation:
    def validate(self, *, actual_result: Mapping[str, Any], tests: Availability,
                 files: Availability, verifier_advisory: Availability,
                 remaining_work: Sequence[str]) -> dict[str, Any]:
        checks = {"actual_result": bool(actual_result), "tests": tests.available,
                  "files": files.available, "result_verifier_advisory": verifier_advisory.available,
                  "remaining_work_empty": not remaining_work}
        return {"status": "PASS" if all(checks.values()) else "BLOCKED", "checks": checks,
                "evidence": {"actual_result": dict(actual_result), "tests": asdict(tests),
                             "files": asdict(files), "result_verifier_advisory": asdict(verifier_advisory)}}


def deterministic_safety_interlock(snapshot: ObserverSnapshot) -> tuple[bool, str]:
    """Fail closed on explicit risky scope or unavailable scope facts."""
    if not snapshot.activity.scope.available:
        return False, "SCOPE_UNAVAILABLE"
    scope = snapshot.activity.scope.value
    if not isinstance(scope, Mapping):
        return False, "SCOPE_INVALID"
    if (scope.get("production") is True or scope.get("destructive") is True
            or scope.get("external_send") is True or scope.get("unknown") is True):
        return False, "PROTECTED_SCOPE"
    return True, "SAFE_SCOPE"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Availability):
        return {"available": value.available, "value": _jsonable(value.value), "reason": value.reason}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
