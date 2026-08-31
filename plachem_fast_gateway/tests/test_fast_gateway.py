import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast_gateway as g


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


if __name__ == "__main__":
    unittest.main()
