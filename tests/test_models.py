from pydantic import ValidationError
import pytest

from gateway.models import MAX_ITEM_BYTES, MAX_ITEMS, MAX_STRING_BYTES, RiskLevel, TaskSpec, WorkerResult


def test_task_spec_requires_explicit_completion_and_denials():
    task = TaskSpec.model_validate(
        {
            "task_id": "pilot-001",
            "agent": "achilles",
            "objective": "Inspect one file and cite evidence",
            "risk": "low",
            "execution": "bounded",
            "environment": "local",
            "scope": {"include": ["README.md"], "exclude": ["production"]},
            "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export"],
            "limits": {"max_steps": 12, "max_retries": 1, "timeout_seconds": 900},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["file and line references"],
        }
    )

    assert task.risk is RiskLevel.LOW
    assert task.completion.no_changes is True


def test_task_id_cannot_escape_runtime_directory():
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "task_id": "../escape",
                "agent": "achilles",
                "objective": "Inspect one file",
                "risk": "low",
                "execution": "bounded",
                "environment": "local",
                "scope": {"include": ["README.md"], "exclude": ["production"]},
                "permissions": ["repo_read"],
                "deny": ["production", "merge", "deploy", "secrets_export"],
                "limits": {"max_steps": 8, "max_retries": 1, "timeout_seconds": 600},
                "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
                "evidence": ["line references"],
            }
        )


@pytest.mark.parametrize("include", [["../README.md"], ["/README.md"], ["C:/README.md"]])
def test_scope_requires_relative_non_traversing_document_paths(include):
    payload = {
        "task_id": "scope-001", "agent": "achilles", "objective": "Inspect",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": include, "exclude": []}, "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["exact lines"],
    }
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(payload)


def test_universal_intake_accepts_multiple_valid_documents():
    payload = {
        "task_id": "multi-001", "agent": "achilles", "objective": "Compare documents",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md", "docs/a.md"], "exclude": []},
        "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["exact lines"],
    }
    assert TaskSpec.model_validate(payload).scope.include == ["README.md", "docs/a.md"]


def test_all_nested_models_forbid_extras_and_strict_integers():
    payload = {
        "task_id": "strict-001", "agent": "achilles", "objective": "Inspect",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md"], "exclude": [], "extra": True},
        "permissions": ["repo_read"], "deny": ["production", "merge", "deploy", "secrets_export"],
        "limits": {"max_steps": "3", "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["exact lines"],
    }
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(payload)


def test_json_enum_values_still_parse():
    raw = '{"task_id":"json-001","agent":"achilles","objective":"Inspect","risk":"low",' \
        '"execution":"bounded","environment":"local","scope":{"include":["README.md"],"exclude":[]},' \
        '"permissions":["repo_read"],"deny":["production","merge","deploy","secrets_export"],' \
        '"limits":{"max_steps":3,"max_retries":0,"timeout_seconds":30},' \
        '"completion":{"max_summary_sentences":2,"min_evidence":1,"no_changes":true},"evidence":["lines"]}'
    assert TaskSpec.model_validate_json(raw).risk is RiskLevel.LOW


def test_worker_result_uses_strict_types():
    with pytest.raises(ValidationError):
        WorkerResult.model_validate({
            "task_id": "strict-001", "status": "completed", "summary": "done",
            "production_changes": "0",
        })


def test_worker_result_accepts_bounded_artifact_bundle():
    result = WorkerResult.model_validate({
        "task_id": "artifact-001",
        "status": "completed",
        "summary": "created",
        "artifacts": [
            {"path": "demo/index.html", "content": "<h1>Hello</h1>"},
            {"path": "demo/app.js", "content": "console.log('ok')"},
        ],
    })
    assert [artifact.path for artifact in result.artifacts] == ["demo/index.html", "demo/app.js"]


@pytest.mark.parametrize(
    "artifacts",
    [
        [{"path": "../escape.txt", "content": "bad"}],
        [
            {"path": "demo/a.txt", "content": "one"},
            {"path": "demo/a.txt", "content": "two"},
        ],
    ],
)
def test_worker_result_rejects_unsafe_or_duplicate_artifact_paths(artifacts):
    with pytest.raises(ValidationError):
        WorkerResult.model_validate({
            "task_id": "artifact-001",
            "status": "completed",
            "summary": "created",
            "artifacts": artifacts,
        })


def test_taskspec_rejects_oversized_strings_items_and_arrays():
    base = {
        "task_id": "bounded-1", "agent": "achilles", "objective": "Inspect",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md"], "exclude": []},
        "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["README.md:1"],
    }
    for update in (
        {"objective": "x" * (MAX_STRING_BYTES + 1)},
        {"evidence": ["x" * (MAX_ITEM_BYTES + 1)]},
        {"permissions": ["repo_read"] * (MAX_ITEMS + 1)},
    ):
        with pytest.raises(ValidationError):
            TaskSpec.model_validate(base | update)


def test_worker_result_rejects_oversized_summary_items_and_arrays():
    base = {"task_id": "bounded-1", "status": "completed", "summary": "done"}
    for update in (
        {"summary": "x" * (MAX_STRING_BYTES + 1)},
        {"checks": ["x" * (MAX_ITEM_BYTES + 1)]},
        {"evidence": ["x"] * (MAX_ITEMS + 1)},
    ):
        with pytest.raises(ValidationError):
            WorkerResult.model_validate(base | update)
