from __future__ import annotations

import unittest
import time
from threading import Event, Thread
from unittest.mock import patch

from fast_gateway_service import PersistentExecutionHarness


class FakeAdapter:
    def __init__(self):
        self.rpc = object()
        self.closed = False

    def close(self):
        self.closed = True


class FakeCore:
    def __init__(self):
        self.adapter = FakeAdapter()
        self.records = {}
        self.completed = set()
        self.cancel_calls = 0

    def dispatch(self, **kwargs):
        record = {"core_run_id": kwargs["core_run_id"], "status": "RUNNING"}
        self.records[record["core_run_id"]] = record
        return record

    def wait(self, core_run_id, *, timeout_seconds):
        if core_run_id in self.completed:
            self.records[core_run_id]["status"] = "PASS"
        return self.records[core_run_id]

    def status(self, core_run_id):
        if core_run_id not in self.records:
            raise ValueError(f"UNKNOWN_CORE_RUN:{core_run_id}")
        return self.records[core_run_id]

    def cancel(self, core_run_id):
        self.cancel_calls += 1
        self.records[core_run_id]["status"] = "CANCELLED"
        return self.records[core_run_id]

    def fresh_context(self, core_run_id):
        return self.records[core_run_id]


class CancelRaceCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.wait_entered = Event()
        self.allow_wait = Event()
        self.terminal_writes = 0
        self._guard = None

    def set_terminal_transition_guard(self, guard):
        self._guard = guard

    def wait(self, core_run_id, *, timeout_seconds):
        self.wait_entered.set()
        self.allow_wait.wait(1.0)
        if self._guard is not None and self._guard(core_run_id):
            return self.records[core_run_id]
        self.records[core_run_id]["status"] = "PASS"
        self.terminal_writes += 1
        return self.records[core_run_id]

    def cancel(self, core_run_id):
        self.records[core_run_id]["status"] = "CANCELLED"
        self.terminal_writes += 1
        self.allow_wait.set()
        return self.records[core_run_id]

    def mark_user_cancelled_after_abort(self, core_run_id):
        self.records[core_run_id]["status"] = "CANCELLED"
        self.terminal_writes += 1
        self.allow_wait.set()
        return self.records[core_run_id]


class PersistentExecutionHarnessTests(unittest.TestCase):
    @staticmethod
    def wait_until(predicate):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition was not reached")

    def test_background_observer_collects_completion_without_external_wait(self):
        core = FakeCore()
        harness = PersistentExecutionHarness(core)

        harness.dispatch(core_run_id="run-no-wait")
        core.completed.add("run-no-wait")
        self.wait_until(lambda: not harness.active_controllers)

        self.assertEqual("PASS", core.records["run-no-wait"]["status"])

    def test_background_observer_invokes_terminal_subscriber_exactly_once(self):
        core = FakeCore()
        harness = PersistentExecutionHarness(core)
        received = []
        delivered = Event()

        def subscriber(record):
            received.append(dict(record))
            delivered.set()

        harness.set_terminal_completion_subscriber(subscriber)
        harness.dispatch(core_run_id="run-terminal-subscriber")
        core.completed.add("run-terminal-subscriber")

        self.assertTrue(delivered.wait(1.0))
        self.wait_until(lambda: not harness.active_controllers)
        time.sleep(0.05)

        self.assertEqual(1, len(received))
        self.assertEqual("run-terminal-subscriber", received[0]["core_run_id"])
        self.assertEqual("PASS", received[0]["status"])

    def test_poll_race_releases_controller_once(self):
        core = FakeCore()
        harness = PersistentExecutionHarness(core)

        harness.dispatch(core_run_id="run-poll-race")
        controller = harness.active_controllers["run-poll-race"]
        core.completed.add("run-poll-race")
        result = harness.wait("run-poll-race", timeout_seconds=1)
        self.wait_until(lambda: not harness.active_controllers)

        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, controller.release_count)

    def test_cancel_race_does_not_overwrite_cancelled_terminal_state(self):
        core = FakeCore()
        harness = PersistentExecutionHarness(core)

        harness.dispatch(core_run_id="run-cancel-race")
        controller = harness.active_controllers["run-cancel-race"]
        result = harness.cancel("run-cancel-race")
        self.wait_until(lambda: not harness.active_controllers)

        self.assertEqual("CANCELLED", result["status"])
        self.assertEqual("CANCELLED", core.records["run-cancel-race"]["status"])
        self.assertEqual(1, controller.release_count)
        self.assertEqual(1, core.cancel_calls)

    def test_cancel_race_has_one_terminal_write_not_just_one_release(self):
        core = CancelRaceCore()
        harness = PersistentExecutionHarness(core)
        harness.dispatch(core_run_id="run-one-write")
        self.assertTrue(core.wait_entered.wait(1.0))

        cancel_thread = Thread(target=lambda: harness.cancel("run-one-write"))
        cancel_thread.start()
        cancel_thread.join(1.0)
        self.assertFalse(cancel_thread.is_alive())
        self.wait_until(lambda: not harness.active_controllers)

        self.assertEqual("CANCELLED", core.records["run-one-write"]["status"])
        self.assertEqual(1, core.terminal_writes)

    def test_proxy_stop_path_sets_intent_before_abort_for_one_terminal_write(self):
        from war_room_fast_gateway import FastGatewayWarRoomAdapter

        class Control:
            def __init__(self, race_core):
                self.race_core = race_core
                self.calls = []

            def abort(self, *, session_key):
                self.calls.append(session_key)
                self.race_core.allow_wait.set()
                return "aborted"

        core = CancelRaceCore()
        harness = PersistentExecutionHarness(core)
        harness.engine.dispatch(core_run_id="run-proxy-stop")
        core.records["run-proxy-stop"]["openclaw_binding"] = {
            "session_key": "agent:qwentest:main",
            "openclaw_run_id": "openclaw-run-proxy-stop",
        }
        self.assertTrue(core.wait_entered.wait(1.0))
        control = Control(core)
        receipt = FastGatewayWarRoomAdapter(
            harness.engine, "/tmp/unused-war-room-bindings.sqlite3", control_rpc=control,
        ).stop_core_run(core_run_id="run-proxy-stop")
        self.wait_until(lambda: not harness.active_controllers)

        self.assertEqual("stopped", receipt.status)
        self.assertEqual(["agent:qwentest:main"], control.calls)
        self.assertEqual("CANCELLED", core.records["run-proxy-stop"]["status"])
        self.assertEqual(1, core.terminal_writes)

    def test_one_controller_per_run_is_retained_until_terminal(self):
        core = FakeCore()
        harness = PersistentExecutionHarness(core)

        harness.dispatch(core_run_id="run-1")
        harness.dispatch(core_run_id="run-2")
        controller = harness.active_controllers["run-1"]
        harness.dispatch(core_run_id="run-1")
        self.assertIs(controller, harness.active_controllers["run-1"])
        self.assertFalse(controller.released)

        core.completed.add("run-1")
        harness.wait("run-1", timeout_seconds=1)
        self.assertIn("run-2", harness.active_controllers)
        self.assertTrue(controller.released)
        self.assertFalse(core.adapter.closed)
        core.completed.add("run-2")
        harness.wait("run-2", timeout_seconds=1)
        self.assertTrue(core.adapter.closed)

    def test_engine_proxy_routes_war_room_lifecycle_to_same_harness(self):
        core = FakeCore()
        harness = PersistentExecutionHarness(core)

        harness.engine.dispatch(core_run_id="run-2")
        self.assertIn("run-2", harness.active_controllers)
        harness.engine.cancel("run-2")
        self.assertEqual({}, harness.active_controllers)
        self.assertEqual("CANCELLED", core.records["run-2"]["status"])


    def test_fastgateway_watchdog_recovery_defaults_off(self):
        import os
        import fast_gateway_service

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLACHEM_FAST_GATEWAY_WATCHDOG_RECOVERY_ENABLED", None)
            self.assertFalse(fast_gateway_service.fastgateway_watchdog_recovery_enabled())
            os.environ["PLACHEM_FAST_GATEWAY_WATCHDOG_RECOVERY_ENABLED"] = "1"
            self.assertTrue(fast_gateway_service.fastgateway_watchdog_recovery_enabled())

    def test_process_harness_factory_reuses_one_core_owner(self):
        import fast_gateway_service

        fast_gateway_service._HARNESSES.clear()
        first_core = FakeCore()
        with patch.object(fast_gateway_service, "create_core_engine", return_value=first_core) as factory:
            first = fast_gateway_service.get_persistent_harness(
                run_path="/tmp/phase2-harness-runs.jsonl",
                bindings_path="/tmp/phase2-harness-bindings.sqlite3",
            )
            second = fast_gateway_service.get_persistent_harness(
                run_path="/tmp/phase2-harness-runs.jsonl",
                bindings_path="/tmp/phase2-harness-bindings.sqlite3",
            )
        self.assertIs(first, second)
        self.assertIs(first.core_engine, first_core)
        factory.assert_called_once()
        fast_gateway_service._HARNESSES.clear()


if __name__ == "__main__":
    unittest.main()
