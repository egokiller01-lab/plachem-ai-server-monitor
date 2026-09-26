from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from command_center_client import CommandCenterAPIError, CommandCenterClient
from war_room_execution import execute_war_room_task


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self):
        return json.dumps(self.payload).encode()


class CommandCenterClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        def opener(request, timeout):
            self.requests.append((request, timeout))
            return FakeResponse({"ok": True})
        self.client = CommandCenterClient("http://command-center.test", "integration-secret", "war-user-1", opener=opener)

    def test_all_operations_use_contract_paths_and_server_side_auth_headers(self):
        self.client.submit({"war_project_id": "p", "war_task_id": "t", "scope": "inspect", "requested_agents": ["Achilles"], "workspace_id": "w"}, "submit-key")
        self.client.status("p", "t")
        self.client.candidates("p", "t")
        self.client.dispatch("child-1", "dispatch-key")
        self.client.next_ready("p", "t")
        self.client.summary("p", "t")
        paths = [request.full_url for request, _ in self.requests]
        self.assertEqual(paths, [
            "http://command-center.test/api/command-center/war-room/tasks",
            "http://command-center.test/api/command-center/war-room/projects/p/tasks/t",
            "http://command-center.test/api/command-center/war-room/projects/p/tasks/t/candidates",
            "http://command-center.test/api/command-center/tasks/child-1/dispatch",
            "http://command-center.test/api/command-center/war-room/projects/p/tasks/t/next-ready",
            "http://command-center.test/api/command-center/war-room/projects/p/tasks/t/summary",
        ])
        headers = dict(self.requests[0][0].header_items())
        self.assertEqual(headers["X-war-room-integration-secret"], "integration-secret")
        self.assertEqual(headers["X-war-room-actor-id"], "war-user-1")
        self.assertEqual(headers["Idempotency-key"], "submit-key")
        static_text = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in Path("static").rglob("*.*"))
        self.assertNotIn("X-War-Room-Integration-Secret", static_text)
        self.assertNotIn("integration-secret", static_text)

    def test_client_rejects_runtime_fields_before_network(self):
        with self.assertRaises(ValueError):
            self.client.submit({"war_project_id": "p", "model": "private-model"}, "key")
        self.assertEqual(self.requests, [])

    def test_submit_and_dispatch_require_idempotency_keys(self):
        with self.assertRaises(ValueError):
            self.client.submit({}, "")
        with self.assertRaises(ValueError):
            self.client.dispatch("child-1", "")
        self.assertEqual(self.requests, [])

    def test_http_error_is_normalized_without_secret_or_internal_details(self):
        def opener(request, timeout):
            raise CommandCenterAPIError(409, {"error": "IDEMPOTENCY_CONFLICT"})
        client = CommandCenterClient("http://command-center.test", "integration-secret", "war-user-1", opener=opener)
        with self.assertRaisesRegex(CommandCenterAPIError, "IDEMPOTENCY_CONFLICT"):
            client.dispatch("child-1", "key")

    def test_execution_mode_keeps_legacy_and_never_falls_back(self):
        legacy = mock.Mock(return_value={"mode": "legacy"})
        command_center = mock.Mock(side_effect=RuntimeError("command center unavailable"))
        with self.assertRaises(RuntimeError):
            execute_war_room_task("command_center", command_center=command_center, legacy=legacy)
        command_center.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(execute_war_room_task("legacy", command_center=command_center, legacy=legacy), {"mode": "legacy"})
        legacy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
