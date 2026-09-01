import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace_registry


OFFICIAL_ROOT = Path(r"E:\PLACHEM-Agent-Control\repo")
OFFICIAL_PROJECT_ID = "plachem-agent-control"
OFFICIAL_BRANCH = "phase2-worker-identity"


class WorkspaceRegistryTests(unittest.TestCase):
    def write_registry(self, directory: Path, root: Path, branch=OFFICIAL_BRANCH) -> Path:
        path = directory / "workspaces.json"
        path.write_text(
            json.dumps(
                {
                    "workspaces": {
                        OFFICIAL_PROJECT_ID: {
                            "root": str(root),
                            "branch": branch,
                            "status": "ACTIVE",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_official_workspace_resolves_and_validates_current_path_and_branch(self):
        registry = workspace_registry.WorkspaceRegistry.load(
            Path(__file__).resolve().parents[1] / "workspaces.json"
        )

        entry = registry.validate(OFFICIAL_PROJECT_ID, OFFICIAL_ROOT)

        self.assertEqual(entry.project_id, OFFICIAL_PROJECT_ID)
        self.assertEqual(entry.canonical_root, OFFICIAL_ROOT.resolve())
        self.assertEqual(entry.branch, OFFICIAL_BRANCH)
        self.assertEqual(entry.status, "ACTIVE")

    def test_old_c_repo_is_blocked(self):
        registry = workspace_registry.WorkspaceRegistry.load(
            Path(__file__).resolve().parents[1] / "workspaces.json"
        )

        with self.assertRaisesRegex(ValueError, "WORKSPACE_PATH_MISMATCH"):
            registry.validate(
                OFFICIAL_PROJECT_ID,
                Path(r"C:\Users\egomine2\PLACHEM-Agent-Control"),
            )

    def test_appdata_temp_worktree_is_blocked(self):
        registry = workspace_registry.WorkspaceRegistry.load(
            Path(__file__).resolve().parents[1] / "workspaces.json"
        )

        with self.assertRaisesRegex(ValueError, "WORKSPACE_PATH_MISMATCH"):
            registry.validate(
                OFFICIAL_PROJECT_ID,
                Path(r"C:\Users\egomine2\AppData\Local\Temp\PLACHEM-CORE4-Agent-Registry"),
            )

    def test_wrong_branch_is_blocked(self):
        registry = workspace_registry.WorkspaceRegistry.load(
            Path(__file__).resolve().parents[1] / "workspaces.json"
        )

        with (
            mock.patch.object(workspace_registry, "current_git_branch", return_value="wrong-branch"),
            self.assertRaisesRegex(ValueError, "WORKSPACE_BRANCH_MISMATCH"),
        ):
            registry.validate(OFFICIAL_PROJECT_ID, OFFICIAL_ROOT)

    def test_unknown_project_id_is_blocked(self):
        registry = workspace_registry.WorkspaceRegistry.load(
            Path(__file__).resolve().parents[1] / "workspaces.json"
        )

        with self.assertRaisesRegex(ValueError, "UNKNOWN_WORKSPACE:missing"):
            registry.validate("missing", OFFICIAL_ROOT)

    def test_validation_does_not_modify_registry_or_workspace_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            root.mkdir()
            marker = root / "marker.txt"
            marker.write_text("unchanged\n", encoding="utf-8")
            registry_path = self.write_registry(Path(td), root)
            before_registry = registry_path.read_bytes()
            before_files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

            with mock.patch.object(
                workspace_registry,
                "current_git_branch",
                return_value=OFFICIAL_BRANCH,
            ):
                workspace_registry.WorkspaceRegistry.load(registry_path).validate(
                    OFFICIAL_PROJECT_ID,
                    root,
                )

            self.assertEqual(registry_path.read_bytes(), before_registry)
            self.assertEqual(
                {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before_files,
            )


if __name__ == "__main__":
    unittest.main()
