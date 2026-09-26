from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProcessBoardUiTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "static/war-room.html").read_text()
        self.js = (ROOT / "static/war-room-ui.js").read_text()

    def test_process_board_is_read_only_and_has_required_columns(self):
        self.assertIn('data-screen="process-board"', self.html)
        self.assertIn('id="screen-process-board"', self.html)
        self.assertIn("process-board", self.js)
        for label in ("WAITING", "READY", "RUNNING", "PASS", "FAIL", "REWORK", "BLOCKED", "DONE"):
            self.assertIn(label, self.js)
        for label in ("Task / Step", "Agent", "Status", "Predecessor", "Session", "Rework"):
            self.assertIn(label, self.html + self.js)

    def test_readiness_banner_contract_is_read_only_and_fail_closed(self):
        self.assertIn('id="process-board-readiness"', self.html)
        self.assertIn('role="status"', self.html)
        self.assertIn("/readiness", self.js)
        self.assertIn("currentReadiness", self.js)
        self.assertIn("ready_for_representative_completion", self.js)
        self.assertIn("blocking_task_ids", self.js)
        self.assertIn("blocking_reasons", self.js)
        self.assertIn("CURRENT_EVIDENCE", self.js)
        self.assertIn("SESSION_INTEGRITY", self.js)
        self.assertIn("GROUNDING_PACKET_INVALID", self.js)
        self.assertIn("QA_SIGNATURE_UNAVAILABLE", self.js)
        self.assertIn("mode:\"unavailable\"", self.js)
        self.assertIn("SUPERSEDED", self.js)

    def test_process_board_emphasizes_attention_states_and_preserves_existing_load(self):
        self.assertIn("emphasis-", self.js)
        self.assertIn('"RUNNING","FAIL","REWORK","BLOCKED"', self.js)
        self.assertIn("process-board", self.js)
        self.assertIn("processBoard", self.js)
        self.assertIn("renderTasks()", self.js)
        self.assertIn("renderProcessBoard()", self.js)


if __name__ == "__main__":
    unittest.main()
