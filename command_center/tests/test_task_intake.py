import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_intake import create_task_package


class TaskIntakeTests(unittest.TestCase):
    def test_preserves_original_instruction_and_sha256(self):
        instruction = "한글 작업지시 원문입니다.\n두 번째 줄도 그대로 보존합니다."
        package = create_task_package(
            instruction,
            requested_worker="achilles",
            requested_actions=["workspace_modify"],
        )
        self.assertEqual(package["original_instruction"], instruction)
        self.assertEqual(
            package["instruction_sha256"],
            hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(package["status"], "CREATED")

    def test_generates_unique_task_ids(self):
        first = create_task_package("첫 번째 작업")
        second = create_task_package("두 번째 작업")
        self.assertNotEqual(first["task_id"], second["task_id"])

    def test_preserves_worker_and_actions_without_approval(self):
        package = create_task_package(
            "원문",
            requested_worker="athena",
            requested_actions=["read_only_review", "result_write"],
        )
        self.assertEqual(package["requested_worker"], "athena")
        self.assertEqual(package["requested_actions"], ["read_only_review", "result_write"])
        self.assertEqual(package["status"], "CREATED")


if __name__ == "__main__":
    unittest.main()
