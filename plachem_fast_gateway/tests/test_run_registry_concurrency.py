from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from plachem_fast_gateway.core_engine import RunRegistry, RunRegistryCorruptionError
from plachem_fast_gateway.runtime_policy import GoalContract


GOAL = GoalContract("TEST", "test objective", ("TEST",), ("NONE",), "test result", ("DONE",))
POLICY = {"runtime_class": "LOCAL", "policy_profile": "TEST"}


def _create_batch(path: str, worker: int, count: int, ready, start) -> None:
    registry = RunRegistry(path)
    ready.set()
    start.wait()
    for index in range(count):
        registry.create(
            core_run_id=f"run-{worker}-{index}",
            agent_id="test-agent",
            idempotency_key=f"idem-{worker}-{index}",
            request_hash=f"hash-{worker}-{index}",
            policy=POLICY,
            goal_contract=GOAL,
        )


def _read_during_writes(path: str, started, failures) -> None:
    registry = RunRegistry(path)
    started.wait()
    try:
        while True:
            records = registry.recent(200)
            if len(records) >= 80:
                return
    except Exception as exc:  # pragma: no cover - reported by parent assertion
        failures.put(repr(exc))


class RunRegistryConcurrencyTests(unittest.TestCase):
    def test_fresh_instance_preserves_idempotent_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            first, created = RunRegistry(path).create(
                core_run_id="same-run", agent_id="agent", idempotency_key="same-idem",
                request_hash="same-hash", policy=POLICY, goal_contract=GOAL,
            )
            replay, replayed = RunRegistry(path).create(
                core_run_id="same-run", agent_id="agent", idempotency_key="same-idem",
                request_hash="same-hash", policy=POLICY, goal_contract=GOAL,
            )
            self.assertTrue(created)
            self.assertFalse(replayed)
            self.assertEqual(first, replay)
            self.assertEqual(1, len(pathlib_lines(str(path))))

    def test_concurrent_process_writers_are_atomic_and_fresh_instance_sees_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "runs.jsonl")
            context = multiprocessing.get_context("fork")
            start = context.Event()
            ready = [context.Event() for _ in range(4)]
            writers = [
                context.Process(target=_create_batch, args=(path, worker, 20, ready[worker], start))
                for worker in range(4)
            ]
            for process in writers:
                process.start()
            for event in ready:
                self.assertTrue(event.wait(5))
            start.set()
            for process in writers:
                process.join(10)
                self.assertEqual(0, process.exitcode)

            fresh = RunRegistry(path)
            records = fresh.recent(200)
            self.assertEqual(80, len(records))
            self.assertEqual(80, len({item["core_run_id"] for item in records}))
            self.assertEqual(80, len(pathlib_lines(path)))

    def test_reader_during_concurrent_writes_never_observes_partial_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "runs.jsonl")
            context = multiprocessing.get_context("fork")
            start = context.Event()
            ready = [context.Event() for _ in range(4)]
            failures = context.Queue()
            reader = context.Process(target=_read_during_writes, args=(path, start, failures))
            writers = [
                context.Process(target=_create_batch, args=(path, worker, 20, ready[worker], start))
                for worker in range(4)
            ]
            reader.start()
            for process in writers:
                process.start()
            for event in ready:
                self.assertTrue(event.wait(5))
            start.set()
            for process in writers:
                process.join(10)
                self.assertEqual(0, process.exitcode)
            reader.join(10)
            self.assertEqual(0, reader.exitcode)
            self.assertTrue(failures.empty(), failures.get() if not failures.empty() else "")

    def test_truncated_tail_reports_distinctly_and_preserves_valid_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            registry = RunRegistry(path)
            registry.create(core_run_id="valid", agent_id="agent", idempotency_key="idem", request_hash="hash", policy=POLICY, goal_contract=GOAL)
            with path.open("ab") as handle:
                handle.write(b'{"core_run_id":"truncated"')
            with self.assertRaises(RunRegistryCorruptionError) as raised:
                RunRegistry(path).recent(10)
            self.assertEqual("TRUNCATED_RUN_REGISTRY_TAIL", raised.exception.code)
            self.assertEqual(["valid"], [item["core_run_id"] for item in raised.exception.records])

    def test_malformed_middle_record_is_not_ignored_and_valid_prefix_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            valid = {"core_run_id": "valid"}
            path.write_text(json.dumps(valid) + "\nnot-json\n" + json.dumps({"core_run_id": "later"}) + "\n", encoding="utf-8")
            with self.assertRaises(RunRegistryCorruptionError) as raised:
                RunRegistry(path).get("valid")
            self.assertEqual("MALFORMED_RUN_REGISTRY", raised.exception.code)
            self.assertEqual([valid], raised.exception.records)


def pathlib_lines(path: str) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


if __name__ == "__main__":
    unittest.main()
