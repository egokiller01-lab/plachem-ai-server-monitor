from pathlib import Path

import pytest

from gateway.local_validation import LocalValidationAdapter, LocalValidationError
from gateway.models import TaskSpec, WorkerResult
from gateway.workspace import ScopedWorkspace


def task() -> TaskSpec:
    return TaskSpec.model_validate({
        "task_id": "validate-001", "agent": "achilles", "objective": "Build static web",
        "risk": "medium", "execution": "bounded", "environment": "local",
        "scope": {"include": ["demo/index.html", "demo/style.css", "demo/app.js"], "exclude": []},
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change", "external_network"],
        "limits": {"max_steps": 12, "max_retries": 2, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 5, "min_evidence": 3, "no_changes": False},
        "evidence": ["demo/index.html:1", "demo/style.css:1", "demo/app.js:1"],
    })


def stage(tmp_path: Path, html: str, js: str = "console.log('ok')"):
    result = WorkerResult.model_validate({
        "task_id": "validate-001", "status": "completed", "summary": "done",
        "artifacts": [
            {"path": "demo/index.html", "content": html},
            {"path": "demo/style.css", "content": "body { color: black; }"},
            {"path": "demo/app.js", "content": js},
        ],
        "changes": ["demo/index.html", "demo/style.css", "demo/app.js"],
        "checks": ["self-check"],
        "evidence": ["demo/index.html:1", "demo/style.css:1", "demo/app.js:1"],
        "permission_use": ["workspace_read", "workspace_write_scoped", "local_test"],
        "production_changes": 0,
    })
    project = tmp_path / "project"
    project.mkdir()
    workspace = ScopedWorkspace(project, project / "runtime")
    return workspace.stage(task(), result)


def test_local_validator_accepts_self_contained_static_web_bundle(tmp_path: Path):
    staged = stage(
        tmp_path,
        '<!doctype html><html><head><link rel="stylesheet" href="style.css"></head>'
        '<body><script src="app.js"></script></body></html>',
    )

    report = LocalValidationAdapter().validate(task(), staged)

    assert "3 non-empty UTF-8 artifacts" in report.checks
    assert "static web references are scoped and local" in report.checks


@pytest.mark.parametrize(
    "html, js, reason",
    [
        ('<script src="missing.js"></script>', "ok", "missing local asset"),
        ('<script src="https://example.com/app.js"></script>', "ok", "external network"),
        ('<script src="app.js"></script>', "fetch('https://api.example.com')", "external network"),
    ],
)
def test_local_validator_rejects_missing_assets_and_external_network(tmp_path: Path, html, js, reason):
    staged = stage(tmp_path, html, js)
    with pytest.raises(LocalValidationError, match=reason):
        LocalValidationAdapter().validate(task(), staged)


def test_local_validator_enforces_taskspec_text_regex_and_javascript_syntax(tmp_path: Path):
    payload = task().model_dump(mode="json")
    payload["validation"] = {
        "required_text": [
            "Hermes Agent Monitor", "Odyssey", "Achilles", "Start Demo",
            "Complete Task", "Reset", "Last Update",
        ],
        "required_regex": [r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"],
        "javascript_syntax": True,
    }
    validated_task = TaskSpec.model_validate(payload)
    html = (
        '<!doctype html><html><head><title>Hermes Agent Monitor</title>'
        '<link rel="stylesheet" href="style.css"></head><body>'
        '<article>Odyssey Last Update 00:00:00</article><article>Achilles</article>'
        '<button>Start Demo</button><button>Complete Task</button><button>Reset</button>'
        '<script src="app.js"></script></body></html>'
    )
    staged = stage(tmp_path, html, "const ok = true;")

    report = LocalValidationAdapter().validate(validated_task, staged)

    assert "TaskSpec required text validated" in report.checks
    assert "TaskSpec required regex validated" in report.checks
    assert "JavaScript syntax validated" in report.checks


def test_local_validator_rejects_invalid_javascript_when_requested(tmp_path: Path):
    payload = task().model_dump(mode="json")
    payload["validation"] = {
        "required_text": [], "required_regex": [], "javascript_syntax": True,
    }
    validated_task = TaskSpec.model_validate(payload)
    staged = stage(
        tmp_path,
        '<!doctype html><link rel="stylesheet" href="style.css"><script src="app.js"></script>',
        "function broken( {",
    )

    with pytest.raises(LocalValidationError, match="JavaScript syntax"):
        LocalValidationAdapter().validate(validated_task, staged)
