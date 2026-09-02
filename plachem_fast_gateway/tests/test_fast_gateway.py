import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast_gateway as g
from mock_auth_broker import LocalTestStore, TaskAuthBroker


class FastGatewayTests(unittest.TestCase):
    def test_workspace_accepts_delegation_demo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = root / "delegation-demo"
            expected.mkdir()
            self.assertEqual(g.normalize_workspace(root, "delegation-demo"), expected)

    def test_workspace_accepts_python_demo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = root / "python-demo"
            expected.mkdir()
            self.assertEqual(g.normalize_workspace(root, "python-demo"), expected)

    def test_workspace_accepts_dot_relative_python_demo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = root / "python-demo"
            expected.mkdir()
            self.assertEqual(g.normalize_workspace(root, "./python-demo"), expected)

    def test_workspace_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "workspace escapes project root"):
                g.normalize_workspace(root, "../outside")

    def test_workspace_rejects_absolute_path_outside_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            outside = Path(td) / "outside"
            root.mkdir()
            outside.mkdir()
            with self.assertRaisesRegex(ValueError, "workspace escapes project root"):
                g.normalize_workspace(root, str(outside))

    def test_blocked_action_respects_negation(self):
        blocked = ["git push", "production", "deploy", "배포"]
        self.assertEqual(g.detect_explicit_blocked_action("Production에 배포해", blocked), "production")
        self.assertIsNone(g.detect_explicit_blocked_action("Production 작업 금지", blocked))
        self.assertIsNone(g.detect_explicit_blocked_action("Production은 건드리지 마", blocked))
        self.assertEqual(g.detect_explicit_blocked_action("git push 해", blocked), "git push")
        self.assertIsNone(g.detect_explicit_blocked_action("git push 금지", blocked))
        self.assertIsNone(g.detect_explicit_blocked_action("배포하지 마", blocked))

    def test_result_rejects_scope_escape(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            policy = dict(g.DEFAULT_POLICY)
            ok, failures, arts = g.validate_result(ws, {
                "status": "completed",
                "summary": "x",
                "reason": "",
                "artifacts": [{"path": "../outside.txt", "content": "bad"}],
            }, policy)
            self.assertFalse(ok)
            self.assertIn("invalid_artifact_path", failures)
            self.assertEqual(arts, [])

    def test_result_accepts_new_file_inside_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            policy = dict(g.DEFAULT_POLICY)
            ok, failures, arts = g.validate_result(ws, {
                "status": "completed",
                "summary": "x",
                "reason": "",
                "artifacts": [{"path": "new.py", "content": "print('ok')\n"}],
            }, policy)
            self.assertTrue(ok, failures)
            self.assertEqual(arts[0]["path"], "new.py")

    def test_atomic_apply_updates_and_creates(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "a.txt").write_text("old", encoding="utf-8")
            changed = g.atomic_apply(ws, [
                {"path": "a.txt", "content": "new"},
                {"path": "b.txt", "content": "created"},
            ])
            self.assertEqual((ws / "a.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual((ws / "b.txt").read_text(encoding="utf-8"), "created")
            self.assertEqual(len(changed), 2)

    def test_collect_context_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            for i in range(5):
                (ws / f"{i}.txt").write_text("x" * 100, encoding="utf-8")
            policy = dict(g.DEFAULT_POLICY)
            policy["max_context_files"] = 2
            items = g.collect_context(ws, policy)
            self.assertEqual(len(items), 2)

    def test_agents_registry_contains_athena_hermes_profile(self):
        agents = g.load_json(g.ROOT / "agents.json")
        self.assertEqual(
            agents["athena"],
            {
                "enabled": True,
                "priority": 30,
                "capabilities": ["coding", "review", "research", "documentation", "testing", "general"],
                "runtime_profile": "athena",
            },
        )
        for agent in agents.values():
            self.assertNotIn("model", agent)
            self.assertNotIn("provider", agent)
            self.assertNotIn("base_url", agent)

    def test_load_hermes_session_evidence_verifies_actual_model_and_provider(self):
        with tempfile.TemporaryDirectory() as td:
            state_db = Path(td) / "state.db"
            conn = sqlite3.connect(state_db)
            try:
                conn.execute(
                    """CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        model TEXT,
                        billing_provider TEXT,
                        profile_name TEXT,
                        ended_at REAL,
                        end_reason TEXT,
                        api_call_count INTEGER,
                        tool_call_count INTEGER,
                        input_tokens INTEGER,
                        output_tokens INTEGER
                    )"""
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "session-123",
                        "gpt-5.6-luna",
                        "openai-codex",
                        "athena",
                        123.0,
                        "cli_close",
                        1,
                        0,
                        100,
                        20,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            evidence = g.load_hermes_session_evidence(
                state_db,
                "session-123",
                expected_profile="athena",
                expected_model="gpt-5.6-luna",
                expected_provider="openai-codex",
            )

            self.assertEqual(evidence["source"], "hermes_state_db")
            self.assertEqual(evidence["session_id"], "session-123")
            self.assertEqual(evidence["model"], "gpt-5.6-luna")
            self.assertEqual(evidence["provider"], "openai-codex")
            self.assertEqual(evidence["profile"], "athena")
            self.assertEqual(evidence["api_calls"], 1)
            self.assertEqual(evidence["tool_calls"], 0)
            self.assertEqual(evidence["input_tokens"], 100)
            self.assertEqual(evidence["output_tokens"], 20)
            self.assertTrue(evidence["completed"])

    def test_call_worker_uses_hermes_profile_adapter_without_tools(self):
        agent = {
            "provider": "hermes-profile",
            "profile": "athena",
            "inference_provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "max_tokens": 8000,
        }
        worker_result = {
            "result_type": "read_only",
            "status": "completed",
            "summary": "reviewed",
            "reason": "",
            "review_result": "PASS",
            "findings": [],
            "artifacts": [],
        }
        captured: dict[str, str] = {}

        with tempfile.TemporaryDirectory() as td:
            state_db = Path(td) / "state.db"
            conn = sqlite3.connect(state_db)
            try:
                conn.execute(
                    """CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        model TEXT,
                        billing_provider TEXT,
                        profile_name TEXT,
                        ended_at REAL,
                        end_reason TEXT,
                        api_call_count INTEGER,
                        tool_call_count INTEGER,
                        input_tokens INTEGER,
                        output_tokens INTEGER
                    )"""
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "session-123",
                        "gpt-5.6-luna",
                        "openai-codex",
                        "athena",
                        123.0,
                        "cli_close",
                        1,
                        0,
                        100,
                        20,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            def fake_run(command, **kwargs):
                query_path = Path(command[command.index("--query-file") + 1])
                captured["prompt"] = query_path.read_text(encoding="utf-8")
                kwargs["stdout"].write(json.dumps(worker_result))
                kwargs["stderr"].write(chr(10) + "session_id: session-123" + chr(10))
                return subprocess.CompletedProcess(args=command, returncode=0)

            with (
                mock.patch.object(g.subprocess, "run", side_effect=fake_run) as called,
                mock.patch.object(
                    g,
                    "resolve_hermes_profile_state_db",
                    return_value=state_db,
                ),
            ):
                result = g.call_worker(agent, "bounded prompt", 30)

        self.assertEqual(result, worker_result)
        self.assertEqual(result.execution_evidence["source"], "hermes_state_db")
        self.assertEqual(result.execution_evidence["model"], "gpt-5.6-luna")
        self.assertEqual(result.execution_evidence["provider"], "openai-codex")
        self.assertEqual(result.execution_evidence["tool_calls"], 0)
        command = called.call_args.args[0]
        self.assertEqual(command[0:3], ["hermes", "-p", "athena"])
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("--provider", command)
        self.assertIn("openai-codex", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--toolsets", command)
        self.assertIn("context_engine", command)
        self.assertIn("--query-file", command)
        self.assertNotIn("--usage-file", command)
        self.assertNotIn("--oneshot", command)
        self.assertIn("-Q", command)
        self.assertIn("--source", command)
        self.assertIn("tool", command)
        self.assertNotIn("bounded prompt", command)
        self.assertEqual(captured["prompt"], "bounded prompt")
        self.assertEqual(called.call_args.kwargs["timeout"], 30)
        self.assertIs(called.call_args.kwargs["stdin"], subprocess.DEVNULL)
        child_env = called.call_args.kwargs["env"]
        self.assertIn("PATH", child_env)
        self.assertIn("SystemDrive", child_env)
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("ANTHROPIC_API_KEY", child_env)

    def test_worker_prompt_uses_current_request_agent_identity(self):
        policy = dict(g.DEFAULT_POLICY)

        for worker_id in ("worker-a", "worker-b", "achilles"):
            with self.subTest(worker_id=worker_id):
                prompt = g.build_worker_prompt(
                    task="Perform a bounded review",
                    workspace_name="command_center",
                    context=[],
                    policy=policy,
                    worker_id=worker_id,
                )
                self.assertIn(f"You are {worker_id}, a bounded implementation worker.", prompt)

    def test_mock_broker_authorizes_only_matching_task_actions(self):
        with tempfile.TemporaryDirectory() as td:
            broker_path = Path(td) / "broker.json"
            broker_path.write_text(json.dumps({
                "tasks": {
                    "auth-001": {
                        "allow": ["workspace_modify", "git_commit", "git_push"],
                        "deny": ["production_deploy", "production_migration", "business_data_change"],
                        "git_push_target": "runtime/mock-remotes/test10.git",
                        "git_push_ref": "refs/heads/test10-auth",
                    }
                }
            }), encoding="utf-8")
            auth = g.load_mock_authorization(broker_path, "auth-001")
            decision = g.authorize_requested_actions(
                "파일을 수정하고 git commit 후 git push를 실행해",
                list(g.DEFAULT_POLICY["blocked_actions"]),
                auth,
            )
            self.assertTrue(decision["allowed"])
            self.assertEqual(decision["requested"], ["git_commit", "git_push"])
            with self.assertRaisesRegex(ValueError, "authorization not found"):
                g.load_mock_authorization(broker_path, "other-task")

    def test_no_broker_keeps_git_push_blocked(self):
        decision = g.authorize_requested_actions(
            "파일을 수정하고 git push를 실행해",
            list(g.DEFAULT_POLICY["blocked_actions"]),
            None,
        )
            
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "BLOCKED_ACTION:git push")

    def test_git_authorization_does_not_allow_production(self):
        auth = {
            "allow": ["workspace_modify", "git_commit", "git_push"],
            "deny": ["production_deploy", "production_migration", "business_data_change"],
        }
        decision = g.authorize_requested_actions(
            "파일을 수정하고 git push한 뒤 Production deploy를 실행해",
            list(g.DEFAULT_POLICY["blocked_actions"]),
            auth,
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "UNAUTHORIZED_ACTION:production_deploy")

    def test_execute_authorized_git_actions_commits_and_pushes_changed_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            remote = Path(td) / "remote.git"
            root.mkdir()
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            target = workspace / "app.js"
            target.write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "delegation-demo/app.js"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "baseline"], check=True, capture_output=True)
            target.write_text("new\n", encoding="utf-8")
            result = g.execute_authorized_git_actions(
                root,
                "delegation-demo",
                [{"path": "app.js"}],
                "auth-001",
                ["git_commit", "git_push"],
                {
                    "allow": ["workspace_modify", "git_commit", "git_push"],
                    "git_push_target": str(remote),
                    "git_push_ref": "refs/heads/test10-auth",
                },
            )
            self.assertTrue(result["commit"])
            self.assertTrue(result["push"])
            shown = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "refs/heads/test10-auth:delegation-demo/app.js"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(shown, "new\n")

    def test_run_uses_broker_then_applies_commits_and_pushes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            remote = Path(td) / "remote.git"
            root.mkdir()
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "delegation-demo/app.js"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "baseline"], check=True, capture_output=True)
            broker_path = root / "broker.json"
            broker_path.write_text(json.dumps({
                "tasks": {
                    "auth-run-001": {
                        "allow": ["workspace_modify", "git_commit", "git_push"],
                        "deny": ["production_deploy", "production_migration", "business_data_change"],
                        "git_push_target": str(remote),
                        "git_push_ref": "refs/heads/test10-run",
                    }
                }
            }), encoding="utf-8")
            worker_result = {
                "status": "completed",
                "summary": "updated",
                "reason": "",
                "artifacts": [{"path": "app.js", "content": "new\n"}],
            }
            with mock.patch.object(g, "call_worker", return_value=worker_result) as called:
                record = g.run(
                    {
                        "task_id": "auth-run-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "app.js를 수정하고 git commit 후 git push를 실행해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    broker_path,
                )
            self.assertEqual(called.call_count, 1)
            self.assertEqual(record["status"], "PASS")
            self.assertTrue(record["auth"]["broker_called"])
            self.assertTrue(record["git"]["commit"])
            self.assertTrue(record["git"]["push"])
            self.assertEqual((workspace / "app.js").read_text(encoding="utf-8"), "new\n")

    def test_run_uses_signed_task_and_worker_bound_authorization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("old\n", encoding="utf-8")
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            broker.issue(
                task_id="signed-task-001",
                worker="achilles",
                allow=["workspace_modify"],
                deny=["production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            worker_result = {
                "status": "completed",
                "summary": "updated",
                "reason": "",
                "artifacts": [{"path": "app.js", "content": "new\n"}],
            }

            with mock.patch.object(g, "call_worker", return_value=worker_result) as called:
                record = g.run(
                    {
                        "task_id": "signed-task-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "app.js를 수정해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(called.call_count, 1)
            self.assertEqual(record["status"], "PASS")
            self.assertEqual(record["auth"]["worker"], "achilles")
            self.assertTrue(record["auth"]["authorization_id"])

    def test_authorized_read_only_review_accepts_zero_artifacts_and_findings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            source = workspace / "app.js"
            source.write_text("unchanged\n", encoding="utf-8")
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            auth_id = broker.issue(
                task_id="read-only-review-001",
                worker="achilles",
                allow=["read_only_review"],
                deny=["workspace_modify", "git_commit", "git_push", "production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            review_result = {
                "result_type": "read_only",
                "status": "completed",
                "summary": "review completed",
                "reason": "",
                "review_result": "PASS",
                "findings": ["No blocking findings"],
                "artifacts": [],
            }

            with mock.patch.object(g, "call_worker", return_value=review_result) as called:
                record = g.run(
                    {
                        "task_id": "read-only-review-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "READ-ONLY code review를 수행해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(called.call_count, 1)
            self.assertEqual(record["status"], "PASS")
            self.assertEqual(record["auth"]["requested"], ["read_only_review"])
            self.assertEqual(record["result"]["review_result"], "PASS")
            self.assertEqual(record["result"]["findings"], ["No blocking findings"])
            self.assertEqual(record["result"]["changes"], [])
            self.assertEqual(source.read_text(encoding="utf-8"), "unchanged\n")
            self.assertFalse(record["git"]["commit"])
            self.assertFalse(record["git"]["push"])
            self.assertTrue(LocalTestStore(auth_path).is_used(auth_id))

    def test_athena_adapter_returns_authorized_read_only_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            source = workspace / "app.js"
            source.write_text("unchanged\n", encoding="utf-8")
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            auth_id = broker.issue(
                task_id="athena-review-001",
                worker="athena",
                allow=["read_only_review"],
                deny=["workspace_modify", "git_commit", "git_push", "production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            review_result = {
                "result_type": "read_only",
                "status": "completed",
                "summary": "Athena review completed",
                "reason": "",
                "review_result": "PASS",
                "findings": [],
                "artifacts": [],
            }
            response = g.WorkerResponse(review_result)
            response.execution_evidence = {
                "source": "hermes_state_db",
                "session_id": "session-123",
                "profile": "athena",
                "model": "gpt-5.6-luna",
                "provider": "openai-codex",
                "api_calls": 1,
                "tool_calls": 0,
                "input_tokens": 100,
                "output_tokens": 20,
                "completed": True,
            }
            agents = {
                "athena": {
                    "provider": "hermes-profile",
                    "profile": "athena",
                    "inference_provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "max_tokens": 8000,
                }
            }

            with mock.patch.object(g, "call_worker", return_value=response) as called:
                record = g.run(
                    {
                        "task_id": "athena-review-001",
                        "agent": "athena",
                        "workspace": "delegation-demo",
                        "task": "READ-ONLY code review를 수행해",
                    },
                    agents,
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(called.call_count, 1)
            self.assertIn(
                "You are athena, a bounded implementation worker.",
                called.call_args.args[1],
            )
            self.assertEqual(record["status"], "PASS")
            self.assertEqual(record["auth"]["worker"], "athena")
            self.assertEqual(record["result_type"], "read_only")
            self.assertEqual(record["result"]["review_result"], "PASS")
            self.assertEqual(record["result"]["changes"], [])
            self.assertEqual(record["execution_evidence"]["session_id"], "session-123")
            self.assertEqual(record["execution_evidence"]["model"], "gpt-5.6-luna")
            self.assertEqual(record["execution_evidence"]["provider"], "openai-codex")
            self.assertEqual(record["attempts"][0]["execution_evidence"]["api_calls"], 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "unchanged\n")
            self.assertFalse(record["git"]["commit"])
            self.assertFalse(record["git"]["push"])
            self.assertTrue(LocalTestStore(auth_path).is_used(auth_id))

    def test_read_only_review_without_authorization_is_blocked_before_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("unchanged\n", encoding="utf-8")

            with mock.patch.object(g, "call_worker") as called:
                record = g.run(
                    {
                        "task_id": "unauthorized-review-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "READ-ONLY code review를 수행해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                )

            self.assertEqual(called.call_count, 0)
            self.assertEqual(record["status"], "BLOCKED")
            self.assertEqual(record["result"]["reason"], "AUTH_REQUIRED_ACTION:read_only_review")

    def test_read_only_review_rejects_worker_artifacts_without_applying_them(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            source = workspace / "app.js"
            source.write_text("unchanged\n", encoding="utf-8")
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            auth_id = broker.issue(
                task_id="review-write-attempt-001",
                worker="achilles",
                allow=["read_only_review"],
                deny=["workspace_modify", "git_commit", "git_push"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            invalid_result = {
                "result_type": "read_only",
                "status": "completed",
                "summary": "attempted write",
                "reason": "",
                "review_result": "PASS",
                "findings": [],
                "artifacts": [{"path": "app.js", "content": "modified\n"}],
            }

            with mock.patch.object(g, "call_worker", return_value=invalid_result):
                record = g.run(
                    {
                        "task_id": "review-write-attempt-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "READ-ONLY code review를 수행해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(record["status"], "FAIL")
            self.assertEqual(source.read_text(encoding="utf-8"), "unchanged\n")
            self.assertFalse(LocalTestStore(auth_path).is_used(auth_id))
            self.assertIn("read_only_with_artifacts", record["attempts"][0]["failures"])

    def test_read_only_review_commit_and_push_request_is_blocked_before_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("unchanged\n", encoding="utf-8")
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            auth_id = broker.issue(
                task_id="review-git-attempt-001",
                worker="achilles",
                allow=["read_only_review"],
                deny=["workspace_modify", "git_commit", "git_push"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

            with mock.patch.object(g, "call_worker") as called:
                record = g.run(
                    {
                        "task_id": "review-git-attempt-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "READ-ONLY code review 후 git commit하고 git push해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(called.call_count, 0)
            self.assertEqual(record["status"], "BLOCKED")
            self.assertIn("action not authorized: git_commit", record["result"]["reason"])
            self.assertFalse(LocalTestStore(auth_path).is_used(auth_id))

    def test_push_only_action_pushes_authorized_commit_without_artifacts_or_new_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            remote = Path(td) / "remote.git"
            root.mkdir()
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("ready\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "delegation-demo/app.js"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "target"], check=True, capture_output=True)
            target_commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            auth_id = broker.issue(
                task_id="push-only-001",
                worker="achilles",
                allow=["git_push"],
                deny=["workspace_modify", "git_commit", "production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                git_push_target=str(remote),
                git_push_ref="refs/heads/test10-push-only",
                git_push_commit=target_commit,
            )
            action_result = {
                "result_type": "action_only",
                "status": "completed",
                "summary": "ready to push",
                "reason": "",
                "artifacts": [],
            }

            with mock.patch.object(g, "call_worker", return_value=action_result) as called:
                record = g.run(
                    {
                        "task_id": "push-only-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "지정 commit을 git push해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(called.call_count, 1)
            self.assertEqual(record["status"], "PASS")
            self.assertEqual(record["result"]["changes"], [])
            self.assertFalse(record["git"]["commit"])
            self.assertTrue(record["git"]["push"])
            self.assertEqual(record["git"]["commit_sha"], target_commit)
            self.assertEqual(
                subprocess.run(
                    ["git", f"--git-dir={remote}", "rev-parse", "refs/heads/test10-push-only"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                target_commit,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                target_commit,
            )
            self.assertTrue(LocalTestStore(auth_path).is_used(auth_id))

    def test_signed_authorization_is_not_consumed_when_worker_validation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("old\n", encoding="utf-8")
            auth_path = root / "auth-v2.json"
            broker = TaskAuthBroker(
                LocalTestStore(auth_path, signing_key="gateway-test-key"),
                root / "auth-audit.jsonl",
            )
            auth_id = broker.issue(
                task_id="validation-failure-001",
                worker="achilles",
                allow=["workspace_modify"],
                deny=["production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            invalid_result = {
                "status": "completed",
                "summary": "invalid empty artifact result",
                "reason": "",
                "artifacts": [],
            }

            with mock.patch.object(g, "call_worker", return_value=invalid_result):
                record = g.run(
                    {
                        "task_id": "validation-failure-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "app.js를 수정해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                    auth_path,
                )

            self.assertEqual(record["status"], "FAIL")
            self.assertFalse(LocalTestStore(auth_path).is_used(auth_id))

    def test_action_only_result_requires_an_authorized_requested_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "delegation-demo"
            workspace.mkdir()
            (workspace / "app.js").write_text("old\n", encoding="utf-8")
            action_result = {
                "result_type": "action_only",
                "status": "completed",
                "summary": "nothing to execute",
                "reason": "",
                "artifacts": [],
            }

            with mock.patch.object(g, "call_worker", return_value=action_result):
                record = g.run(
                    {
                        "task_id": "empty-action-only-001",
                        "agent": "achilles",
                        "workspace": "delegation-demo",
                        "task": "현재 상태를 확인해",
                    },
                    {"achilles": {"base_url": "unused", "model": "unused"}},
                    dict(g.DEFAULT_POLICY),
                    root,
                    root / "runs.jsonl",
                )

            self.assertEqual(record["status"], "FAIL")
            failure_text = "|".join(
                failure
                for attempt in record["attempts"]
                for failure in attempt["failures"]
            )
            self.assertIn(
                "action-only result requires an implemented authorized action",
                failure_text,
            )


    def test_explicit_read_only_action_survives_when_task_detection_is_empty(self):
        self.assertEqual(
            g.resolve_requested_actions("Read the supplied context only.", ["read_only_review"]),
            ["read_only_review"],
        )

    def test_missing_explicit_actions_uses_legacy_task_detection(self):
        self.assertEqual(
            g.resolve_requested_actions("파일을 수정하고 git push를 실행해", None),
            ["git_push"],
        )

    def test_detected_risk_action_is_added_to_explicit_actions(self):
        self.assertEqual(
            g.resolve_requested_actions(
                "Read-only review and then git push를 실행해",
                ["read_only_review"],
            ),
            ["read_only_review", "git_push"],
        )

    def test_authorize_uses_resolved_actions_for_validator_contract(self):
        auth = {"allow": ["read_only_review"], "deny": []}
        decision = g.authorize_requested_actions(
            "Read the supplied context only.",
            list(g.DEFAULT_POLICY["blocked_actions"]),
            auth,
            ["read_only_review"],
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["requested"], ["read_only_review"])

    def test_authorize_keeps_resolved_risk_actions(self):
        auth = {"allow": ["read_only_review", "git_push"], "deny": []}
        decision = g.authorize_requested_actions(
            "Read the supplied context only.",
            list(g.DEFAULT_POLICY["blocked_actions"]),
            auth,
            ["read_only_review", "git_push"],
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["requested"], ["read_only_review", "git_push"])


if __name__ == "__main__":
    unittest.main()
