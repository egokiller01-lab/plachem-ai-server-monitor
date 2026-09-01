import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_query import RunQuery


class RunQueryTests(unittest.TestCase):
    def record(
        self,
        task_id,
        status,
        worker="achilles",
        *,
        started_at=None,
        completed_at=None,
        failure_reason="",
    ):
        return {
            "task_id": task_id,
            "project_id": "plachem-agent-control",
            "worker": worker,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "gateway_result": None,
            "failure_reason": failure_reason,
        }

    def write(self, path: Path, records) -> bytes:
        data = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode("utf-8")
        path.write_bytes(data)
        return data

    def test_task_id_returns_latest_run_without_modifying_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            before = self.write(
                path,
                [
                    self.record("task-a", "CREATED"),
                    self.record("task-a", "RUNNING", started_at="2026-09-01T01:00:00+00:00"),
                ],
            )

            result = RunQuery(path).get("task-a")

            self.assertEqual(result["status"], "RUNNING")
            self.assertEqual(result["started_at"], "2026-09-01T01:00:00+00:00")
            self.assertEqual(path.read_bytes(), before)

    def test_recent_n_returns_latest_runs_in_most_recent_record_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            self.write(
                path,
                [
                    self.record("task-a", "CREATED"),
                    self.record("task-b", "PASS"),
                    self.record("task-a", "RUNNING"),
                    self.record("task-c", "FAIL"),
                ],
            )

            recent = RunQuery(path).recent(2)

        self.assertEqual([record["task_id"] for record in recent], ["task-c", "task-a"])
        self.assertEqual([record["status"] for record in recent], ["FAIL", "RUNNING"])

    def test_active_returns_only_current_created_dispatching_and_running(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            self.write(
                path,
                [
                    self.record("task-created", "CREATED"),
                    self.record("task-pass", "PASS"),
                    self.record("task-dispatch", "DISPATCHING"),
                    self.record("task-running", "RUNNING"),
                    self.record("task-blocked", "BLOCKED"),
                ],
            )

            active = RunQuery(path).active()

        self.assertEqual(
            [(record["task_id"], record["status"]) for record in active],
            [
                ("task-running", "RUNNING"),
                ("task-dispatch", "DISPATCHING"),
                ("task-created", "CREATED"),
            ],
        )

    def test_recent_terminal_returns_only_pass_fail_and_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            self.write(
                path,
                [
                    self.record("task-pass", "PASS"),
                    self.record("task-running", "RUNNING"),
                    self.record("task-fail", "FAIL"),
                    self.record("task-blocked", "BLOCKED"),
                ],
            )

            terminal = RunQuery(path).recent_terminal(2)

        self.assertEqual(
            [(record["task_id"], record["status"]) for record in terminal],
            [("task-blocked", "BLOCKED"), ("task-fail", "FAIL")],
        )

    def test_worker_statuses_use_each_workers_latest_current_run(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            self.write(
                path,
                [
                    self.record("task-old", "PASS", worker="achilles"),
                    self.record("task-athena", "BLOCKED", worker="athena"),
                    self.record("task-new", "RUNNING", worker="achilles"),
                ],
            )

            workers = RunQuery(path).worker_statuses()

        self.assertEqual(workers, {"achilles": "RUNNING", "athena": "BLOCKED"})

    def test_counts_summarize_current_latest_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            self.write(
                path,
                [
                    self.record("task-a", "RUNNING"),
                    self.record("task-a", "PASS"),
                    self.record("task-b", "FAIL"),
                    self.record("task-c", "BLOCKED"),
                    self.record("task-d", "RUNNING"),
                ],
            )

            counts = RunQuery(path).counts()

        self.assertEqual(counts, {"PASS": 1, "FAIL": 1, "BLOCKED": 1, "RUNNING": 1})

    def test_failure_reason_and_timestamps_are_preserved_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            expected = self.record(
                "task-fail",
                "FAIL",
                started_at="2026-09-01T02:00:00+00:00",
                completed_at="2026-09-01T02:01:00+00:00",
                failure_reason="원본 Gateway 실패 사유",
            )
            self.write(path, [expected])

            result = RunQuery(path).get("task-fail")

        self.assertEqual(result, expected)

    def test_missing_or_empty_registry_returns_empty_results(self):
        with tempfile.TemporaryDirectory() as td:
            missing = RunQuery(Path(td) / "missing.jsonl")
            empty_path = Path(td) / "empty.jsonl"
            empty_path.write_text("", encoding="utf-8")
            empty = RunQuery(empty_path)

            for query in (missing, empty):
                self.assertIsNone(query.get("missing"))
                self.assertEqual(query.recent(10), [])
                self.assertEqual(query.active(), [])
                self.assertEqual(query.recent_terminal(10), [])
                self.assertEqual(query.worker_statuses(), {})
                self.assertEqual(query.counts(), {})

    def test_malformed_jsonl_raises_explicit_error_with_line_number(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            path.write_text(
                json.dumps(self.record("task-a", "PASS")) + "\n{not-json}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "MALFORMED_RUN_JSONL:line=2"):
                RunQuery(path).recent(10)

    def test_summary_returns_active_recent_workers_and_counts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.jsonl"
            self.write(
                path,
                [
                    self.record("task-pass", "PASS", worker="achilles"),
                    self.record("task-run", "RUNNING", worker="achilles"),
                    self.record("task-block", "BLOCKED", worker="athena"),
                ],
            )

            summary = RunQuery(path).summary(recent_limit=2)

        self.assertEqual([record["task_id"] for record in summary["active"]], ["task-run"])
        self.assertEqual([record["task_id"] for record in summary["recent"]], ["task-block", "task-run"])
        self.assertEqual(summary["workers"], {"achilles": "RUNNING", "athena": "BLOCKED"})
        self.assertEqual(summary["counts"], {"PASS": 1, "RUNNING": 1, "BLOCKED": 1})


if __name__ == "__main__":
    unittest.main()
