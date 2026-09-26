"""Shared in-process Fast Gateway Core construction."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from plachem_fast_gateway import (
    AgentRegistry,
    CoreEngine,
    EnvironmentSecretRef,
    ModelRegistry,
    OpenClawAdapter,
    RunRegistry,
    SQLiteRunBindingStore,
    production_result_validator,
    recover_production_result_format,
)
from plachem_fast_gateway.auth_broker import create_production_auth_broker

ROOT = Path(__file__).resolve().parent

_TERMINAL_STATUSES = {"PASS", "FAIL", "BLOCKED", "TIMEOUT", "CANCELLED"}


def fastgateway_watchdog_recovery_enabled() -> bool:
    return str(
        os.environ.get("PLACHEM_FAST_GATEWAY_WATCHDOG_RECOVERY_ENABLED", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}


class ActiveRunController:
    """Process-local lease for one run's dispatch-owner connection."""

    def __init__(self, core_run_id: str, engine: CoreEngine, timeout_seconds: float = 1.0) -> None:
        self.core_run_id = core_run_id
        self.engine = engine
        self.owner_connection = getattr(getattr(engine, "adapter", None), "rpc", None)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.released = False
        self.release_count = 0
        self.cancel_requested = False
        self.observer_started = False
        self.stop_event = threading.Event()
        self.observer: threading.Thread | None = None

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.release_count += 1
            self.stop_event.set()


class _HarnessEngineProxy:
    """CoreEngine-shaped view that routes lifecycle calls through the harness."""

    def __init__(self, harness: "PersistentExecutionHarness") -> None:
        self._harness = harness

    def __getattr__(self, name: str) -> Any:
        return getattr(self._harness.core_engine, name)

    def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        return self._harness.dispatch(**kwargs)

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        return self._harness.wait(core_run_id, timeout_seconds=timeout_seconds)

    def cancel(self, core_run_id: str) -> dict[str, Any]:
        return self._harness.cancel(core_run_id)

    def fresh_context(self, core_run_id: str) -> dict[str, Any]:
        return self._harness.fresh_context(core_run_id)

    def request_user_cancel(self, core_run_id: str) -> None:
        self._harness.request_user_cancel(core_run_id)

    def is_cancel_requested(self, core_run_id: str) -> bool:
        return self._harness.is_cancel_requested(core_run_id)


class PersistentExecutionHarness:
    """One process-lifetime Core/adapter owner shared by HTTP consumers."""

    def __init__(self, core_engine: CoreEngine) -> None:
        self.core_engine = core_engine
        self.engine = _HarnessEngineProxy(self)
        self._controllers: dict[str, ActiveRunController] = {}
        self._cancel_intents: set[str] = set()
        self._lock = threading.RLock()
        self._terminal_completion_subscriber: Any | None = None
        self._recovery_layer: Any | None = None
        self._session_watchdog: Any | None = None
        setter = getattr(core_engine, "set_terminal_transition_guard", None)
        if callable(setter):
            setter(self.is_cancel_requested)

    def set_recovery_layer(self, layer: Any) -> None:
        """Install the post-terminal Recovery Layer (v0.2 auto-wiring).

        Only terminal FAIL records classified as recoverable by the layer
        trigger a bounded one-shot recovery.  PASS, BLOCKED, TIMEOUT,
        CANCELLED and policy/auth-blocked runs never reach recovery.
        """
        with self._lock:
            self._recovery_layer = layer

    def set_session_watchdog(self, watchdog: Any | None) -> None:
        """Enable the advisory JEV session watchdog explicitly.

        Production remains disabled until a caller installs a watchdog; the
        watchdog itself also requires the persisted watchdog-managed flag.
        """
        with self._lock:
            self._session_watchdog = watchdog

    def _handle_recovery(self, record: dict[str, Any]) -> None:
        """Post-terminal recovery hook (v0.2).

        Never mutates the original record: the parent run stays immutable.
        All recovery accounting is appended by the RecoveryLayer to the
        child run record it creates.
        """
        status = str(record.get("status") or "")
        if status != "FAIL":
            return  # PASS / BLOCKED / TIMEOUT / CANCELLED: no recovery
        core_run_id = str(record.get("core_run_id") or "")
        if str(record.get("escalation_reason") or "") == "WATCHDOG_REQUEUE_REQUIRED":
            return
        # A recovery child run reaching terminal state is the end of the
        # chain; never recover a recovery child (max 1, no chaining).
        goal_state = (record.get("policy_state") or {}).get("goal") or {}
        if goal_state.get("recovery_source_run_id"):
            return
        with self._lock:
            layer = self._recovery_layer
        if layer is None:
            return
        try:
            layer.recover(core_run_id)
        except Exception:
            # Recovery is best-effort post-processing.  The original FAIL
            # record is immutable and authoritative; a layer error must not
            # change the run's terminal status or raise into the observer.
            pass

    @property
    def active_controllers(self) -> dict[str, ActiveRunController]:
        with self._lock:
            return dict(self._controllers)

    def _controller(self, core_run_id: str, timeout_seconds: float = 1.0) -> ActiveRunController:
        controller = self._controllers.get(core_run_id)
        if controller is None:
            controller = ActiveRunController(core_run_id, self.core_engine, timeout_seconds)
            self._controllers[core_run_id] = controller
        return controller

    def is_cancel_requested(self, core_run_id: str) -> bool:
        with self._lock:
            return core_run_id in self._cancel_intents

    def set_terminal_completion_subscriber(self, subscriber: Any | None) -> None:
        """Install the War Room terminal completion callback."""
        with self._lock:
            self._terminal_completion_subscriber = subscriber

    def request_user_cancel(self, core_run_id: str) -> None:
        with self._lock:
            self._cancel_intents.add(core_run_id)
            self._controller(core_run_id).cancel_requested = True

    def _start_observer(self, controller: ActiveRunController) -> None:
        if controller.observer_started or controller.released:
            return
        controller.observer_started = True

        def observe() -> None:
            try:
                while not controller.stop_event.is_set():
                    try:
                        record = self.core_engine.wait(
                            controller.core_run_id,
                            timeout_seconds=min(controller.timeout_seconds, 1.0),
                        )
                    except Exception:
                        return
                    if str(record.get("status") or "") in _TERMINAL_STATUSES:
                        with self._lock:
                            self._record(record)
                            subscriber = self._terminal_completion_subscriber
                        if callable(subscriber):
                            try:
                                subscriber(record)
                            except Exception:
                                # Completion observation must not kill the
                                # lifecycle observer or alter Core truth.
                                pass
                        return
                    self._record(record)
                    controller.stop_event.wait(0.01)
            finally:
                with self._lock:
                    self._cancel_intents.discard(controller.core_run_id)

        controller.observer = threading.Thread(
            target=observe,
            name=f"fast-gateway-observer-{controller.core_run_id}",
            daemon=True,
        )
        controller.observer.start()

    def _record(self, record: dict[str, Any]) -> dict[str, Any]:
        core_run_id = record.get("core_run_id")
        if isinstance(core_run_id, str) and core_run_id:
            status = str(record.get("status") or "")
            if status in _TERMINAL_STATUSES:
                controller = self._controllers.pop(core_run_id, None)
                if controller is not None:
                    controller.release()
                if controller is not None and not self._controllers:
                    close = getattr(self.core_engine.adapter, "close", None)
                    if callable(close):
                        close()
                if status == "FAIL":
                    self._handle_recovery(record)
            else:
                controller = self._controller(core_run_id)
                self._start_observer(controller)
                watchdog = self._session_watchdog
                if watchdog is not None:
                    try:
                        watchdog.observe(core_run_id)
                    except Exception:
                        # Advisory observation cannot alter lifecycle truth.
                        pass
        return record

    def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        record = self.core_engine.dispatch(**kwargs)
        with self._lock:
            core_run_id = record.get("core_run_id")
            if str(record.get("status") or "") not in _TERMINAL_STATUSES and isinstance(core_run_id, str):
                controller = self._controller(core_run_id, kwargs.get("timeout_seconds", 1.0))
                self._start_observer(controller)
            return self._record(record)

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._controller(core_run_id, timeout_seconds)
        record = self.core_engine.wait(core_run_id, timeout_seconds=timeout_seconds)
        with self._lock:
            return self._record(record)

    def cancel(self, core_run_id: str) -> dict[str, Any]:
        self.request_user_cancel(core_run_id)
        with self._lock:
            self._controller(core_run_id)
        record = self.core_engine.cancel(core_run_id)
        with self._lock:
            return self._record(record)

    def fresh_context(self, core_run_id: str) -> dict[str, Any]:
        with self._lock:
            self._controller(core_run_id)
        record = self.core_engine.fresh_context(core_run_id)
        with self._lock:
            return self._record(record)

    def close(self) -> None:
        with self._lock:
            for controller in self._controllers.values():
                controller.release()
            self._controllers.clear()
            self._cancel_intents.clear()
            close = getattr(self.core_engine.adapter, "close", None)
            if callable(close):
                close()


_HARNESSES: dict[tuple[str, str], PersistentExecutionHarness] = {}
_HARNESSES_LOCK = threading.RLock()


def close_persistent_harnesses() -> None:
    """Release shared Gateway owners when the application shuts down."""
    with _HARNESSES_LOCK:
        harnesses = list(_HARNESSES.values())
    for harness in harnesses:
        harness.close()


def create_core_engine(*, run_path: str | Path | None = None,
                       bindings_path: str | Path | None = None,
                       agents_path: str | Path | None = None,
                       models_path: str | Path | None = None,
                       adapter: OpenClawAdapter | None = None) -> CoreEngine:
    """Build the trusted Core service for both HTTP and War Room callers."""
    run_file = Path(run_path or os.environ.get("PLACHEM_FAST_GATEWAY_RUNS", ROOT / "runtime" / "fast-gateway-runs.jsonl"))
    binding_file = Path(bindings_path or os.environ.get("PLACHEM_FAST_GATEWAY_BINDINGS", ROOT / "runtime" / "fast-gateway-bindings.sqlite3"))
    agent_file = Path(agents_path or os.environ.get("PLACHEM_FAST_GATEWAY_AGENTS", ROOT / "plachem_fast_gateway" / "agents.json"))
    agent_registry = AgentRegistry.load(agent_file)
    # models_path remains accepted for call compatibility. Model metadata is
    # not a startup dependency or an execution policy source for this service.
    model_registry = ModelRegistry({})
    # Recovery eligibility is an explicit service-level capability, not a
    # consequence of an Agent's descriptive runtime_model_id.  Keep the
    # configured candidate set neutral; the adapter's validator still gates
    # whether a recovery result is accepted.
    local_recovery_agents = set(agent_registry.candidate_agent_ids())
    gateway_adapter = adapter or OpenClawAdapter(
        EnvironmentSecretRef(), SQLiteRunBindingStore(binding_file),
        result_validator=production_result_validator(),
        result_recovery=recover_production_result_format,
        result_recovery_agent_ids=local_recovery_agents,
    )
    from direct_gateway_authorization import CompositeGrantAuthorizer, DirectIngressGrantAuthorizer
    from war_room_authorization import WarRoomGrantAuthorizer
    import war_room
    broker = (create_production_auth_broker()
              if os.environ.get("PLACHEM_AUTH_BROKER_DB") and os.environ.get("PLACHEM_AUTH_BROKER_KEY_ID")
              else None)
    return CoreEngine(
        RunRegistry(run_file), agent_registry, model_registry, gateway_adapter,
        auth_broker=broker, auth_required=True,
        grant_authorizer=CompositeGrantAuthorizer(
            WarRoomGrantAuthorizer(war_room._db_path()),
            DirectIngressGrantAuthorizer(),
        ),
    )


def get_persistent_harness(*, run_path: str | Path | None = None,
                           bindings_path: str | Path | None = None,
                           agents_path: str | Path | None = None,
                           models_path: str | Path | None = None,
                           adapter: OpenClawAdapter | None = None) -> PersistentExecutionHarness:
    """Return the one process-local harness for a Core run/binding pair."""
    run_file = Path(run_path or os.environ.get("PLACHEM_FAST_GATEWAY_RUNS", ROOT / "runtime" / "fast-gateway-runs.jsonl")).resolve()
    binding_file = Path(bindings_path or os.environ.get("PLACHEM_FAST_GATEWAY_BINDINGS", ROOT / "runtime" / "fast-gateway-bindings.sqlite3")).resolve()
    key = (str(run_file), str(binding_file))
    with _HARNESSES_LOCK:
        existing = _HARNESSES.get(key)
        if existing is not None:
            return existing
        harness = PersistentExecutionHarness(create_core_engine(
            run_path=run_file, bindings_path=binding_file,
            agents_path=agents_path, models_path=models_path, adapter=adapter,
        ))
        # v0.2: wire the post-terminal Recovery Layer.  Terminal FAIL runs
        # that classify as recoverable get one bounded recovery attempt;
        # PASS and policy/auth-blocked runs are never touched.  The layer
        # reuses the engine's existing auth/policy/validator paths.
        recovery_layer = None
        try:
            from plachem_fast_gateway.recovery_layer import RecoveryLayer
            recovery_layer = RecoveryLayer(
                harness.core_engine,
                harness.core_engine.registry,
                dispatcher=harness.engine,
            )
            harness.set_recovery_layer(recovery_layer)
        except Exception:
            # Recovery wiring is additive; a failure here must not break
            # the primary dispatch path.  The service continues without
            # auto-recovery.
            recovery_layer = None

        # Opt in exactly once per persistent harness when the existing JEV
        # transport configuration is complete.
        if (os.environ.get("JEV_OPENCONNECTOR_ENDPOINT")
                and os.environ.get("JEV_OPENCONNECTOR_CONNECTION")
                and os.environ.get("JEV_OPENCONNECTOR_TOKEN_FILE")):
            try:
                from jev_session_watchdog import JEVSessionRecoveryWatchdog

                def watchdog_recovery(core_run_id: str, snapshot: Any) -> dict[str, Any]:
                    if not fastgateway_watchdog_recovery_enabled():
                        return {
                            "status": "REJECTED",
                            "reason": "WATCHDOG_RECOVERY_OBSERVE_ONLY",
                        }
                    if recovery_layer is None:
                        return {"status": "REJECTED", "reason": "RECOVERY_LAYER_UNAVAILABLE"}
                    current = harness.core_engine.registry.get(core_run_id)
                    if current is None:
                        return {"status": "REJECTED", "reason": "UNKNOWN_CORE_RUN"}

                    auto_agents = {
                        value.strip().casefold()
                        for value in os.environ.get(
                            "PLACHEM_FAST_GATEWAY_WATCHDOG_AUTO_RECOVERY_AGENTS", ""
                        ).split(",")
                        if value.strip()
                    }
                    agent_id = str(current.get("agent_id") or "").casefold()

                    # Production default: do not duplicate a protected side
                    # effect automatically. Mark the run for Main/Process Board
                    # stop + controlled-lane re-approval instead.
                    if (
                        not core_run_id.startswith("direct-")
                        or agent_id not in auto_agents
                    ):
                        return recovery_layer.mark_watchdog_requeue(
                            core_run_id,
                            prior_evidence=snapshot,
                        )

                    # Explicitly allowlisted safe Direct agents may be
                    # cancelled and restarted through a fresh Auth Broker grant.
                    if str(current.get("status") or "") not in _TERMINAL_STATUSES:
                        cancelled = harness.cancel(core_run_id)
                    else:
                        cancelled = current
                    if str(cancelled.get("status") or "") != "CANCELLED":
                        return {
                            "status": "REJECTED",
                            "reason": "SOURCE_CANCEL_UNCONFIRMED",
                            "source_status": cancelled.get("status"),
                        }
                    return recovery_layer.recover_watchdog_cancelled(
                        core_run_id,
                        prior_evidence=snapshot,
                    )

                harness.set_session_watchdog(JEVSessionRecoveryWatchdog(
                    harness.core_engine.registry,
                    history_path=run_file.parent / "jev-watchdog-history.jsonl",
                    recovery=watchdog_recovery,
                ))
            except Exception:
                # Missing/invalid optional wiring must leave auto recovery
                # disabled; no alternate auth or secret path is invented.
                pass
        _HARNESSES[key] = harness
        return harness
