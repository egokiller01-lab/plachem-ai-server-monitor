import json
import tempfile
import unittest
from pathlib import Path

from jev_session_watchdog import (
    CHOICE_VALUES,
    HistoricalJEVClient,
    JEVSessionRecoveryWatchdog,
    deterministic_interlock,
    recovery_snapshot,
)
from jev_task_router import parse_boolean_answers


def response(choice="CONTINUE"):
    return {
        "boolean_probabilities": {name: {"true": 1.0, "false": 0.0} for name in (
            "meaningful_progress", "productive_activity", "loop_suspected", "stalled",
            "continue_current_session", "restart_recommended")},
        "choice": choice,
        "probabilities": {name: (1.0 if name == choice else 0.0) for name in CHOICE_VALUES},
        "confidence": 1.0,
    }


class Registry:
    def __init__(self, record):
        self.record = record

    def get(self, _run_id):
        return dict(self.record)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def record(**overrides):
    value = {
        "core_run_id": "run-1", "agent_id": "erpcoder", "status": "RUNNING",
        "watchdog_managed": True, "policy_state": {"watchdog_started_at": 0.0},
        "goal_contract": {"primary_objective": "TEST", "completion_conditions": ["DONE"]},
        "verified_progress": {"completed_conditions": [], "artifacts": [], "evidence": []},
        "progress_checkpoint": {"current_step": "WORK", "remaining_conditions": ["DONE"]},
        "policy_events": [], "openclaw_binding": {"session_id": "session-1"},
    }
    value.update(overrides)
    return value


class WatchdogTests(unittest.TestCase):
    def test_openconnector_boolean_shape_preserves_six_numeric_probabilities(self):
        names = ("meaningful_progress", "productive_activity", "loop_suspected", "stalled", "continue_current_session", "restart_recommended")
        answers = {name: {"type": "boolean", "probability": index / 10} for index, name in enumerate(names, 1)}
        parsed = parse_boolean_answers(answers, names)
        self.assertEqual([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], list(parsed.values()))
    def make(self, client, clock, recovery=None):
        self.temp = tempfile.TemporaryDirectory()
        return JEVSessionRecoveryWatchdog(Registry(record()), history_path=Path(self.temp.name) / "history.jsonl", client=client, clock=clock, recovery=recovery)

    def tearDown(self):
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def test_no_jev_before_five_minutes_and_sixty_second_cadence(self):
        clock = Clock(0)
        client = HistoricalJEVClient(response())
        watchdog = self.make(client, clock)
        self.assertEqual(watchdog.observe("run-1")["status"], "WAITING")
        clock.value = 300
        self.assertTrue(watchdog.observe("run-1")["polled"])
        self.assertEqual(watchdog.observe("run-1")["status"], "CADENCE_WAIT")
        clock.value = 360
        self.assertTrue(watchdog.observe("run-1")["polled"])
        self.assertEqual(len(client.calls), 2)

    def test_terminal_stops_polling_and_legacy_is_unchanged(self):
        clock = Clock(600)
        client = HistoricalJEVClient(response())
        watchdog = self.make(client, clock)
        watchdog.registry.record["status"] = "PASS"
        self.assertEqual(watchdog.observe("run-1")["status"], "TERMINAL")
        watchdog.registry.record.update(status="RUNNING", watchdog_managed=False)
        self.assertEqual(watchdog.observe("run-1")["status"], "LEGACY")
        self.assertEqual(client.calls, [])

    def test_normal_activity_continue_loop_restart_and_interlock(self):
        clock = Clock(300)
        client = HistoricalJEVClient(response("CONTINUE"))
        watchdog = self.make(client, clock)
        self.assertEqual(watchdog.observe("run-1")["choice"], "CONTINUE")
        client.response = response("RECHECK")
        clock.value = 360
        self.assertEqual(watchdog.observe("run-1")["choice"], "RECHECK")
        client.response = response("RESTART_SESSION")
        children = []
        def recover(run_id, snapshot):
            children.append((run_id, snapshot))
            return {"status": "DISPATCHED", "recovery_run_id": "direct-child"}
        watchdog.recovery = recover
        clock.value = 420
        first_restart = watchdog.observe("run-1")
        self.assertFalse(first_restart["restart"])
        self.assertEqual(first_restart["follow_up"], "RESTART_CONFIRMATION_REQUIRED")
        self.assertEqual(children, [])

        clock.value = 480
        result = watchdog.observe("run-1")
        self.assertTrue(result["restart"])
        self.assertEqual(result["follow_up"], "FRESH_CHILD_CREATED")
        self.assertEqual(children[0][0], "run-1")
        self.assertIn("original_goal", children[0][1])

        unsafe = record(policy_events=[{"details": "production DB write"}])
        allowed, reason = deterministic_interlock(unsafe)
        self.assertFalse(allowed)
        self.assertIn("IRREVERSIBLE", reason)

    def test_restart_can_require_controlled_requeue_without_child(self):
        clock = Clock(300)
        client = HistoricalJEVClient(response("RESTART_SESSION"))
        watchdog = self.make(
            client,
            clock,
            recovery=lambda run_id, snapshot: {
                "status": "REQUEUE_REQUIRED",
                "reason": "WATCHDOG_REQUEUE_REQUIRED",
            },
        )
        first = watchdog.observe("run-1")
        self.assertFalse(first["restart"])
        self.assertEqual(first["follow_up"], "RESTART_CONFIRMATION_REQUIRED")
        self.assertIsNone(first["history"]["recovery_result"])

        clock.value = 360
        result = watchdog.observe("run-1")
        self.assertFalse(result["restart"])
        self.assertEqual(result["follow_up"], "CONTROLLED_REQUEUE_REQUIRED")
        self.assertEqual(
            result["history"]["recovery_result"]["status"],
            "REQUEUE_REQUIRED",
        )

    def test_restart_confirmation_resets_after_non_restart(self):
        clock = Clock(300)
        client = HistoricalJEVClient(response("RESTART_SESSION"))
        children = []
        watchdog = self.make(
            client,
            clock,
            recovery=lambda run_id, snapshot: children.append(run_id) or {
                "status": "DISPATCHED",
                "recovery_run_id": "child",
            },
        )
        first = watchdog.observe("run-1")
        self.assertEqual(first["follow_up"], "RESTART_CONFIRMATION_REQUIRED")

        client.response = response("CONTINUE")
        clock.value = 360
        self.assertEqual(watchdog.observe("run-1")["choice"], "CONTINUE")

        client.response = response("RESTART_SESSION")
        clock.value = 420
        third = watchdog.observe("run-1")
        self.assertEqual(third["follow_up"], "RESTART_CONFIRMATION_REQUIRED")
        self.assertEqual(children, [])

    def test_history_append_only_and_secrets_redacted(self):
        clock = Clock(300)
        client = HistoricalJEVClient(response("CONTINUE"))
        watchdog = self.make(client, clock)
        watchdog.registry.record["policy_state"]["token"] = "Bearer supersecret"
        watchdog.observe("run-1")
        path = watchdog.history_path
        first = path.read_text()
        watchdog.observe("run-1", now=360)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["run_id"], "run-1")
        self.assertNotIn("supersecret", first)

    def test_snapshot_is_compact_and_sanitized(self):
        snap = recovery_snapshot(record(policy_state={"tried_approaches": ["x"], "secret": "bad"}), {"x": "Bearer badsecret"})
        self.assertEqual(snap["original_goal"], "TEST")
        self.assertNotIn("badsecret", json.dumps(snap))


if __name__ == "__main__":
    unittest.main()
