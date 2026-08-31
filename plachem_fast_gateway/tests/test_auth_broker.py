import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mock_auth_broker import LocalTestStore, TaskAuthBroker


class TaskAuthBrokerTests(unittest.TestCase):
    def test_existing_store_rejects_signing_key_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            LocalTestStore(path, signing_key="original-key")

            with self.assertRaisesRegex(ValueError, "signing key mismatch"):
                LocalTestStore(path, signing_key="replacement-key")

    def test_issued_authorization_allows_matching_task_worker_and_actions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify", "git_commit"],
                deny=["production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

            result = broker.authorize(
                authorization_id=auth_id,
                task_id="task-a",
                worker="achilles",
                requested_actions=["workspace_modify", "git_commit"],
            )

            self.assertEqual(result["task_id"], "task-a")
            self.assertEqual(result["worker"], "achilles")
            self.assertEqual(result["allow"], ["git_commit", "workspace_modify"])
            self.assertNotIn("signing_key", result)

    def test_signed_authorization_carries_gateway_execution_constraints(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify", "git_commit", "git_push"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                git_push_target="runtime/mock-remotes/auth.git",
                git_push_ref="refs/heads/test10-auth-v2",
            )

            result = broker.authorize(
                authorization_id=auth_id,
                task_id="task-a",
                worker="achilles",
                requested_actions=["git_commit", "git_push"],
            )

            self.assertEqual(result["git_push_target"], "runtime/mock-remotes/auth.git")
            self.assertEqual(result["git_push_ref"], "refs/heads/test10-auth-v2")

    def test_authorization_rejects_different_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

            with self.assertRaisesRegex(ValueError, "task mismatch"):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-b",
                    worker="achilles",
                    requested_actions=["workspace_modify"],
                )

    def test_authorization_rejects_different_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

            with self.assertRaisesRegex(ValueError, "worker mismatch"):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-a",
                    worker="other-worker",
                    requested_actions=["workspace_modify"],
                )

    def test_authorization_rejects_denied_or_unlisted_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=["production_deploy"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

            for action in ("production_deploy", "git_push"):
                with self.subTest(action=action):
                    with self.assertRaisesRegex(ValueError, f"action not authorized: {action}"):
                        broker.authorize(
                            authorization_id=auth_id,
                            task_id="task-a",
                            worker="achilles",
                            requested_actions=[action],
                        )

    def test_authorization_rejects_expired_credential(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=now - timedelta(seconds=1),
            )

            with self.assertRaisesRegex(ValueError, "authorization expired"):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-a",
                    worker="achilles",
                    requested_actions=["workspace_modify"],
                    now=now,
                )

    def test_revoked_authorization_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            broker.revoke(auth_id)
            revoke_event = json.loads((root / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(revoke_event["event"], "revoked")

            with self.assertRaisesRegex(ValueError, "authorization revoked"):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-a",
                    worker="achilles",
                    requested_actions=["workspace_modify"],
                )

    def test_tampered_authorization_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            store.data["authorizations"][auth_id]["allow"].append("production_deploy")
            store.save()

            with self.assertRaisesRegex(ValueError, "signature mismatch"):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-a",
                    worker="achilles",
                    requested_actions=["workspace_modify"],
                )

    def test_authorization_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, root / "audit.jsonl")
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            broker.authorize(
                authorization_id=auth_id,
                task_id="task-a",
                worker="achilles",
                requested_actions=["workspace_modify"],
            )

            with self.assertRaisesRegex(ValueError, "authorization already used"):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-a",
                    worker="achilles",
                    requested_actions=["workspace_modify"],
                )

    def test_audit_log_records_issue_denial_and_successful_use(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audit_path = root / "audit.jsonl"
            store = LocalTestStore(root / "auth.json", signing_key="test-signing-key")
            broker = TaskAuthBroker(store, audit_path)
            auth_id = broker.issue(
                task_id="task-a",
                worker="achilles",
                allow=["workspace_modify"],
                deny=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            with self.assertRaises(ValueError):
                broker.authorize(
                    authorization_id=auth_id,
                    task_id="task-a",
                    worker="other-worker",
                    requested_actions=["workspace_modify"],
                )
            broker.authorize(
                authorization_id=auth_id,
                task_id="task-a",
                worker="achilles",
                requested_actions=["workspace_modify"],
            )

            events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [event["event"] for event in events],
                ["issued", "authorization_requested", "authorization_denied", "authorization_requested", "authorization_used"],
            )
            self.assertEqual(events[2]["reason"], "authorization worker mismatch")
            self.assertEqual(events[4]["result"], "ALLOW")

    def test_missing_authorization_is_rejected_and_audited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audit_path = root / "audit.jsonl"
            broker = TaskAuthBroker(
                LocalTestStore(root / "auth.json", signing_key="test-signing-key"),
                audit_path,
            )

            with self.assertRaisesRegex(ValueError, "authorization not found"):
                broker.authorize(
                    authorization_id="missing-auth",
                    task_id="task-a",
                    worker="achilles",
                    requested_actions=["workspace_modify"],
                )

            event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["event"], "authorization_denied")
            self.assertEqual(event["reason"], "authorization not found")


if __name__ == "__main__":
    unittest.main()
