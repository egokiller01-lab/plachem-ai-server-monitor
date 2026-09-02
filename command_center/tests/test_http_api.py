import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_registry import AgentRegistry
from http_api import create_app, current_actor
from war_room_orchestrator import WarRoomOrchestrator
from workspace_registry import WorkspaceEntry, WorkspaceRegistry


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project = root / "project"
        project.mkdir()
        self.orchestrator = mock.Mock(spec=WarRoomOrchestrator)
        self.orchestrator.submit.return_value = {
            "war_project_id": "project-1", "war_task_id": "task-1", "correlation_id": "corr-1",
            "command_tasks": [], "selection_mode": "strict",
        }
        self.orchestrator.status.return_value = {"war_project_id": "project-1", "war_task_id": "task-1", "correlation_id": "corr-1", "tasks": []}
        self.orchestrator.candidates.return_value = {"candidates": [], "excluded": []}
        self.orchestrator.next_ready.return_value = []
        self.orchestrator.summary.return_value = {"war_project_id": "project-1", "war_task_id": "task-1", "correlation_id": "corr-1", "overall": "READY", "tasks": []}
        self.orchestrator.dispatch.return_value = {"task_id": "child-1", "status": "PASS"}
        self.app = create_app(self.orchestrator, idempotency_path=root / "idempotency.json", integration_secret="integration-secret", enabled=True)

    def request(self, method, path, body=None, **headers):
        raw = b"" if body is None else json.dumps(body).encode()
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), "wsgi.url_scheme": "http", "SERVER_NAME": "test", "SERVER_PORT": "80"}
        environ.update({"HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items()})
        captured = {}
        def start(status, response_headers):
            captured["status"] = status
            captured["headers"] = dict(response_headers)
        result = b"".join(self.app(environ, start))
        return int(captured["status"].split()[0]), json.loads(result)

    def auth(self, **extra):
        return {"X-War-Room-Integration-Secret": "integration-secret", "X-War-Room-Actor-ID": "war-user-1", **extra}

    def payload(self, scope="inspect"):
        return {"war_project_id": "project-1", "war_task_id": "task-1", "scope": scope, "requested_agents": ["Achilles"], "workspace_id": "workspace-1"}

    def test_feature_gate_off_blocks_write_and_never_dispatches(self):
        app = create_app(self.orchestrator, idempotency_path=Path(self.temp.name) / "off.json", integration_secret="integration-secret", enabled=False)
        old = self.app
        self.app = app
        status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **self.auth(**{"Idempotency-Key": "k1"}))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "INTEGRATION_DISABLED")
        self.orchestrator.submit.assert_not_called()
        status, body = self.request("POST", "/api/command-center/tasks/child-1/dispatch", {}, **self.auth(**{"Idempotency-Key": "k2"}))
        self.assertEqual(status, 404)
        self.orchestrator.dispatch.assert_not_called()
        self.app = old

    def test_submit_requires_auth_and_idempotency_and_calls_once(self):
        status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **{"Idempotency-Key": "k1"})
        self.assertEqual(status, 401)
        status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **self.auth())
        self.assertEqual(status, 400)
        self.orchestrator.submit.assert_not_called()
        status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **self.auth(**{"Idempotency-Key": "k1"}))
        self.assertEqual(status, 200)
        self.orchestrator.submit.assert_called_once_with(self.payload())
        self.assertEqual(body["correlation_id"], "corr-1")

    def test_authenticated_principal_is_available_at_transport_boundary(self):
        observed = []
        def submit(payload):
            observed.append(current_actor())
            return {"war_project_id": "project-1", "war_task_id": "task-1", "correlation_id": "corr-actor", "command_tasks": []}
        self.orchestrator.submit.side_effect = submit
        status, _ = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **self.auth(**{"Idempotency-Key": "actor"}))
        self.assertEqual(status, 200)
        self.assertEqual(observed, ["war-user-1"])

    def test_submit_idempotency_replays_and_conflict_is_409(self):
        headers = self.auth(**{"Idempotency-Key": "same"})
        first = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **headers)
        second = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **headers)
        self.assertEqual(first, second)
        self.orchestrator.submit.assert_called_once()
        status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(scope="different"), **headers)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "IDEMPOTENCY_CONFLICT")

    def test_read_apis_delegate_without_dispatch(self):
        headers = self.auth()
        for path in [
            "/api/command-center/war-room/projects/project-1/tasks/task-1",
            "/api/command-center/war-room/projects/project-1/tasks/task-1/candidates",
            "/api/command-center/war-room/projects/project-1/tasks/task-1/next-ready",
            "/api/command-center/war-room/projects/project-1/tasks/task-1/summary",
        ]:
            status, _ = self.request("GET", path, **headers)
            self.assertEqual(status, 200)
        self.orchestrator.dispatch.assert_not_called()
        self.orchestrator.status.assert_called_once_with("project-1", "task-1")
        self.orchestrator.candidates.assert_called_once_with("project-1", "task-1")
        self.orchestrator.next_ready.assert_called_once_with("project-1", "task-1")
        self.orchestrator.summary.assert_called_once_with("project-1", "task-1")

    def test_dispatch_is_explicit_single_task_and_idempotent(self):
        headers = self.auth(**{"Idempotency-Key": "dispatch-1"})
        status, body = self.request("POST", "/api/command-center/tasks/child-1/dispatch", {}, **headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "PASS")
        self.orchestrator.dispatch.assert_called_once_with("child-1")
        self.request("POST", "/api/command-center/tasks/child-1/dispatch", {}, **headers)
        self.orchestrator.dispatch.assert_called_once()

    def test_forbidden_runtime_fields_and_spoofed_actor_are_rejected(self):
        status, body = self.request("POST", "/api/command-center/war-room/tasks", {**self.payload(), "model": "secret-model"}, **self.auth(**{"Idempotency-Key": "k"}))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_REQUEST")
        status, body = self.request("GET", "/api/command-center/war-room/projects/project-1/tasks/task-1", **{"X-War-Room-Actor-ID": "spoof"})
        self.assertEqual(status, 401)
        self.assertNotIn("secret", json.dumps(body).lower())
        self.assertNotIn("traceback", json.dumps(body).lower())

    def test_error_mapping_and_no_legacy_fallback(self):
        self.orchestrator.submit.side_effect = ValueError("UNKNOWN_AGENT:missing")
        status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(), **self.auth(**{"Idempotency-Key": "err"}))
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "UNKNOWN_AGENT")
        self.assertNotIn("legacy", json.dumps(body).lower())

    def test_required_domain_errors_are_mapped_without_internal_details(self):
        for message, expected in [("UNKNOWN_WORKSPACE:missing", 422), ("TASK_NOT_READY", 409), ("TASK_ALREADY_ACTIVE", 409), ("NO_AVAILABLE_WORKER", 409)]:
            self.orchestrator.submit.side_effect = ValueError(message)
            status, body = self.request("POST", "/api/command-center/war-room/tasks", self.payload(scope=message), **self.auth(**{"Idempotency-Key": "err-" + str(expected)}))
            self.assertEqual(status, expected)
            self.assertNotIn("missing", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
