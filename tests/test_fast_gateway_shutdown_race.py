"""A late observation cannot resurrect a controller after shutdown."""
from threading import Event
from fast_gateway_service import PersistentExecutionHarness


def test_close_joins_observer_and_discards_late_running_snapshot():
    entered, released = Event(), Event()
    class Core:
        def __init__(self):
            self.adapter, self.closed = self, 0
        def dispatch(self, **kwargs):
            return {"core_run_id": "shutdown-only-test", "status": "RUNNING"}
        def wait(self, run_id, **kwargs):
            entered.set()
            released.wait(2)
            return {"core_run_id": run_id, "status": "RUNNING"}
        def close(self):
            self.closed += 1
            released.set()
    core = Core()
    harness = PersistentExecutionHarness(core)
    harness.dispatch(timeout_seconds=1)
    assert entered.wait(1)
    observers = tuple(harness._observers)
    harness.close()
    harness._record({"core_run_id": "shutdown-only-test", "status": "RUNNING"})
    assert harness.active_controllers == {}
    assert not any(thread.is_alive() for thread in observers)
    harness.close()
    assert core.closed == 1
