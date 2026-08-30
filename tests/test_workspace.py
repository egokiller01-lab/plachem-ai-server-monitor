import json
from pathlib import Path

import pytest

from gateway.models import TaskSpec, WorkerResult
from gateway.workspace import ScopedWorkspace, WorkspaceViolation


def task() -> TaskSpec:
    return TaskSpec.model_validate({
        "task_id": "workspace-001", "agent": "achilles", "objective": "Build files",
        "risk": "medium", "execution": "bounded", "environment": "local",
        "scope": {"include": ["demo/index.html", "demo/app.js"], "exclude": ["gateway", ".git"]},
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 12, "max_retries": 2, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 5, "min_evidence": 2, "no_changes": False},
        "evidence": ["demo/index.html:1", "demo/app.js:1"],
    })


def result(artifacts=None) -> WorkerResult:
    artifacts = artifacts or [
        {"path": "demo/index.html", "content": "<h1>Hello</h1>"},
        {"path": "demo/app.js", "content": "console.log('ok')"},
    ]
    return WorkerResult.model_validate({
        "task_id": "workspace-001", "status": "completed", "summary": "created",
        "artifacts": artifacts,
        "changes": [item["path"] for item in artifacts],
        "checks": ["bundle built"],
        "evidence": ["demo/index.html:1", "demo/app.js:1"],
        "permission_use": ["workspace_read", "workspace_write_scoped", "local_test"],
        "production_changes": 0,
    })


def test_workspace_stages_and_promotes_only_declared_artifacts(tmp_path: Path):
    project = tmp_path / "project"
    runtime = project / "runtime"
    project.mkdir()
    existing = project / "demo" / "app.js"
    existing.parent.mkdir()
    existing.write_text("old", encoding="utf-8")
    workspace = ScopedWorkspace(project, runtime)

    staged = workspace.stage(task(), result())
    outcome = workspace.promote(task(), staged)

    assert (project / "demo" / "index.html").read_text(encoding="utf-8") == "<h1>Hello</h1>"
    assert existing.read_text(encoding="utf-8") == "console.log('ok')"
    assert (runtime / "backups" / "workspace-001" / "demo" / "app.js").read_text(encoding="utf-8") == "old"
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert [item["path"] for item in manifest["artifacts"]] == ["demo/app.js", "demo/index.html"]
    assert outcome.promoted_paths == [project / "demo" / "app.js", project / "demo" / "index.html"]


def test_workspace_rolls_back_partial_promotion(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    runtime = project / "runtime"
    existing = project / "demo" / "app.js"
    existing.parent.mkdir(parents=True)
    existing.write_text("old-app", encoding="utf-8")
    index = project / "demo" / "index.html"
    index.write_text("old-index", encoding="utf-8")
    workspace = ScopedWorkspace(project, runtime)
    staged = workspace.stage(task(), result())

    import gateway.workspace as workspace_module
    real_replace = workspace_module.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated promotion failure")
        return real_replace(source, target)

    monkeypatch.setattr(workspace_module.os, "replace", fail_second)
    with pytest.raises(WorkspaceViolation, match="promotion failed"):
        workspace.promote(task(), staged)

    assert existing.read_text(encoding="utf-8") == "old-app"
    assert index.read_text(encoding="utf-8") == "old-index"


def test_workspace_can_rollback_completed_promotion(tmp_path: Path):
    project = tmp_path / "project"
    runtime = project / "runtime"
    existing = project / "demo" / "app.js"
    existing.parent.mkdir(parents=True)
    existing.write_text("old-app", encoding="utf-8")
    index = project / "demo" / "index.html"
    index.write_text("old-index", encoding="utf-8")
    workspace = ScopedWorkspace(project, runtime)
    staged = workspace.stage(task(), result())
    promotion = workspace.promote(task(), staged)

    workspace.rollback(task(), promotion)

    assert existing.read_text(encoding="utf-8") == "old-app"
    assert index.read_text(encoding="utf-8") == "old-index"


def test_workspace_rejects_missing_or_out_of_scope_artifacts(tmp_path: Path):
    workspace = ScopedWorkspace(tmp_path / "project", tmp_path / "project" / "runtime")
    (tmp_path / "project").mkdir()

    with pytest.raises(WorkspaceViolation, match="exactly match"):
        workspace.stage(task(), result([
            {"path": "demo/index.html", "content": "ok"},
        ]))
