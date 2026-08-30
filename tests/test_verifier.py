import pytest

from gateway.models import TaskSpec, WorkerResult
from gateway.verifier import VerificationStatus, _sentence_count, verify_result


def make_task() -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "task_id": "verify-001",
            "agent": "achilles",
            "objective": "Inspect README",
            "risk": "low",
            "execution": "bounded",
            "environment": "local",
            "scope": {"include": ["README.md"], "exclude": ["production"]},
            "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export"],
            "limits": {"max_steps": 7, "max_retries": 1, "timeout_seconds": 120},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["README.md:1"],
        }
    )


def test_completed_result_requires_evidence():
    result = WorkerResult.model_validate(
        {
            "task_id": "verify-001",
            "status": "completed",
            "summary": "done",
            "evidence": [],
            "production_changes": 0,
        }
    )
    verification = verify_result(make_task(), result)
    assert verification.status is VerificationStatus.REJECTED
    assert "evidence" in verification.reasons[0]


def test_valid_result_is_ready_for_main_review():
    result = WorkerResult.model_validate(
        {
            "task_id": "verify-001",
            "status": "completed",
            "summary": "README inspected",
            "checks": ["README.md:1|README heading"],
            "evidence": ["README.md:1"],
            "permission_use": ["repo_read"],
            "production_changes": 0,
        }
    )
    verification = verify_result(make_task(), result, authoritative_context="README heading")
    assert verification.status is VerificationStatus.VERIFIED


def test_verifier_rejects_evidence_from_outside_scope():
    result = WorkerResult.model_validate(
        {
            "task_id": "verify-001",
            "status": "completed",
            "summary": "done",
            "checks": ["read"],
            "evidence": ["secret.txt:1"],
            "permission_use": ["repo_read"],
            "production_changes": 0,
        }
    )

    verification = verify_result(make_task(), result)

    assert verification.status is VerificationStatus.REJECTED
    assert "outside scope" in " ".join(verification.reasons)


def test_verifier_rejects_line_reference_not_in_authoritative_context():
    result = WorkerResult.model_validate(
        {
            "task_id": "verify-001",
            "status": "completed",
            "summary": "done",
            "checks": ["README.md:99 checked"],
            "evidence": ["README.md:99"],
            "permission_use": ["repo_read"],
            "production_changes": 0,
        }
    )

    verification = verify_result(make_task(), result, authoritative_context="only one line")

    assert verification.status is VerificationStatus.REJECTED
    assert "authoritative context" in " ".join(verification.reasons)


def test_verifier_rejects_check_not_grounded_in_authoritative_context():
    result = WorkerResult.model_validate(
        {
            "task_id": "verify-001",
            "status": "completed",
            "summary": "done",
            "checks": ["claimed check without a file line"],
            "evidence": ["README.md:1"],
            "permission_use": ["repo_read"],
            "production_changes": 0,
        }
    )

    verification = verify_result(make_task(), result, authoritative_context="one line")

    assert verification.status is VerificationStatus.REJECTED
    assert "check is not grounded" in " ".join(verification.reasons)


def test_verifier_rejects_check_whose_line_text_is_not_exact():
    result = WorkerResult.model_validate(
        {
            "task_id": "verify-001", "status": "completed", "summary": "done",
            "changes": [], "checks": ["README.md:1|wrong text"],
            "evidence": ["README.md:1"], "permission_use": ["repo_read"],
            "production_changes": 0,
        }
    )
    verification = verify_result(make_task(), result, authoritative_context="exact text")
    assert verification.status is VerificationStatus.REJECTED
    assert "exact line text" in " ".join(verification.reasons)


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"changes": ["README.md"]}, "changes must be empty"),
        ({"permission_use": []}, "permission_use must be exactly"),
        ({"permission_use": ["repo_read", "other"]}, "permission_use must be exactly"),
        ({"summary": "One. Two. Three."}, "summary exceeds"),
    ],
)
def test_verifier_enforces_completed_v1_contract(overrides, reason):
    payload = {
        "task_id": "verify-001", "status": "completed", "summary": "done",
        "changes": [], "checks": ["README.md:1|exact text"],
        "evidence": ["README.md:1"], "permission_use": ["repo_read"],
        "production_changes": 0,
    }
    payload.update(overrides)
    verification = verify_result(
        make_task(), WorkerResult.model_validate(payload), authoritative_context="exact text"
    )
    assert verification.status is VerificationStatus.REJECTED
    assert reason in " ".join(verification.reasons)


def test_newline_separated_summary_sentences_cannot_bypass_limit():
    payload = {
        "task_id": "verify-001", "status": "completed", "summary": "One\nTwo\nThree",
        "changes": [], "checks": ["README.md:1|exact text"],
        "evidence": ["README.md:1"], "permission_use": ["repo_read"],
        "production_changes": 0,
    }
    verification = verify_result(
        make_task(), WorkerResult.model_validate(payload), authoritative_context="exact text"
    )
    assert verification.status is VerificationStatus.REJECTED
    assert "summary exceeds" in " ".join(verification.reasons)


def test_summary_urls_and_file_names_do_not_count_as_extra_sentences():
    payload = {
        "task_id": "verify-001",
        "status": "completed",
        "summary": (
            "Local API 127.0.0.1 responds (README.md:36). "
            "The profile is checked before launch (README.md:40)."
        ),
        "changes": [],
        "checks": ["README.md:1|exact text"],
        "evidence": ["README.md:1"],
        "permission_use": ["repo_read"],
        "production_changes": 0,
    }
    verification = verify_result(
        make_task(), WorkerResult.model_validate(payload), authoritative_context="exact text"
    )
    assert verification.status is VerificationStatus.VERIFIED


def test_verifier_accepts_exact_medium_artifact_bundle():
    task = make_task().model_copy(update={
        "risk": "medium",
        "scope": make_task().scope.model_copy(update={"include": ["demo/index.html", "demo/app.js"]}),
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "completion": make_task().completion.model_copy(update={"min_evidence": 2, "no_changes": False}),
        "evidence": ["demo/index.html:1", "demo/app.js:1"],
    })
    result = WorkerResult.model_validate({
        "task_id": "verify-001", "status": "completed", "summary": "Created files.",
        "artifacts": [
            {"path": "demo/index.html", "content": "<h1>Demo</h1>"},
            {"path": "demo/app.js", "content": "console.log('ok')"},
        ],
        "changes": ["demo/index.html", "demo/app.js"],
        "checks": ["artifact bundle self-check"],
        "evidence": ["demo/index.html:1", "demo/app.js:1"],
        "permission_use": ["workspace_read", "workspace_write_scoped", "local_test"],
        "production_changes": 0,
    })

    assert verify_result(task, result).status is VerificationStatus.VERIFIED


def test_verifier_rejects_medium_bundle_with_scope_or_permission_drift():
    task = make_task().model_copy(update={
        "risk": "medium",
        "scope": make_task().scope.model_copy(update={"include": ["demo/index.html"]}),
        "permissions": ["workspace_write_scoped"],
        "completion": make_task().completion.model_copy(update={"no_changes": False}),
        "evidence": ["demo/index.html:1"],
    })
    result = WorkerResult.model_validate({
        "task_id": "verify-001", "status": "completed", "summary": "Created files.",
        "artifacts": [{"path": "demo/index.html", "content": "ok"}],
        "changes": ["demo/index.html"], "checks": ["checked"],
        "evidence": ["demo/index.html:1"],
        "permission_use": ["workspace_write_scoped", "git_push"],
        "production_changes": 0,
    })

    verification = verify_result(task, result)
    assert verification.status is VerificationStatus.REJECTED
    assert "permission_use" in " ".join(verification.reasons)


def test_duplicate_evidence_does_not_satisfy_minimum_unique_grounded_evidence():
    task = make_task().model_copy(
        update={"completion": make_task().completion.model_copy(update={"min_evidence": 2})}
    )
    payload = {
        "task_id": "verify-001", "status": "completed", "summary": "Done",
        "changes": [], "checks": ["README.md:1|first line"],
        "evidence": ["README.md:1", "README.md:1"], "permission_use": ["repo_read"],
        "production_changes": 0,
    }
    verification = verify_result(
        task, WorkerResult.model_validate(payload), authoritative_context="first line\nsecond line"
    )
    assert verification.status is VerificationStatus.REJECTED
    assert "unique grounded evidence" in " ".join(verification.reasons)


def test_sentence_limit_counts_boundaries_before_closing_quotes_and_parentheses():
    result = WorkerResult.model_validate({
        "task_id": "verify-001", "status": "completed",
        "summary": '"One.)" "Two!" (Three?)', "changes": [],
        "checks": ["README.md:1|exact text"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    })
    verification = verify_result(make_task(), result, authoritative_context="exact text")
    assert verification.status is VerificationStatus.REJECTED
    assert "summary exceeds" in " ".join(verification.reasons)


def test_common_abbreviations_do_not_overcount_sentences():
    result = WorkerResult.model_validate({
        "task_id": "verify-001", "status": "completed",
        "summary": "Dr. Kim checked the U.S. file. It is ready.", "changes": [],
        "checks": ["README.md:1|exact text"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    })
    assert verify_result(make_task(), result, "exact text").status is VerificationStatus.VERIFIED


def test_each_evidence_reference_requires_a_matching_exact_grounded_check():
    result = WorkerResult.model_validate({
        "task_id": "verify-001", "status": "completed", "summary": "Done",
        "checks": ["README.md:1|first"],
        "evidence": ["README.md:2"], "permission_use": ["repo_read"],
        "production_changes": 0,
    })
    verification = verify_result(make_task(), result, "first\nunrelated")
    assert verification.status is VerificationStatus.REJECTED
    assert "matching grounded check" in " ".join(verification.reasons)


def test_exact_taskspec_evidence_requirement_must_be_satisfied():
    task = make_task().model_copy(update={"evidence": ["README.md:2"]})
    result = WorkerResult.model_validate({
        "task_id": "verify-001", "status": "completed", "summary": "Done",
        "checks": ["README.md:1|first"],
        "evidence": ["README.md:1"], "permission_use": ["repo_read"],
        "production_changes": 0,
    })
    verification = verify_result(task, result, "first\nrequired")
    assert verification.status is VerificationStatus.REJECTED
    assert "TaskSpec evidence requirement" in " ".join(verification.reasons)


@pytest.mark.parametrize(
    "summary, expected",
    [
        ("One.Two!Three?", 3),
        ("第一句。第二句！第三句？", 3),
        ("Dr. Kim checked https://example.com/a.b. It is ready.", 2),
        ("The U.S. file is ready.Next check passed.", 2),
    ],
)
def test_sentence_count_handles_no_space_unicode_abbreviations_and_urls(summary: str, expected: int):
    assert _sentence_count(summary) == expected
