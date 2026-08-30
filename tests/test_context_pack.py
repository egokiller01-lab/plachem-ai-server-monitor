from gateway.context_pack import build_context_pack
from gateway.models import TaskSpec


def test_context_pack_contains_contract_and_result_only_marker():
    task = TaskSpec.model_validate(
        {
            "task_id": "context-001",
            "agent": "achilles",
            "objective": "Inspect README and cite lines",
            "risk": "low",
            "execution": "bounded",
            "environment": "local",
            "scope": {"include": ["README.md"], "exclude": ["production", "secrets"]},
            "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export"],
            "limits": {"max_steps": 8, "max_retries": 1, "timeout_seconds": 600},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["file and line references"],
        }
    )

    text = build_context_pack(task, authoritative_context="Project root contains README.md")

    assert "Inspect README and cite lines" in text
    assert "Only the profile-local todo planning tool is permitted" in text
    assert "No filesystem, terminal, network, or external tools" in text
    assert "All required source content is already included" in text
    assert "production" in text
    assert "Return exactly one JSON object" in text
    assert "exact file:line strings" in text
    assert "Project root contains README.md" in text


def test_medium_context_pack_requests_artifact_bundle_without_worker_filesystem_access():
    task = TaskSpec.model_validate({
        "task_id": "context-medium-001", "agent": "achilles", "objective": "Build files",
        "risk": "medium", "execution": "bounded", "environment": "local",
        "scope": {"include": ["demo/index.html", "demo/app.js"], "exclude": ["gateway"]},
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export"],
        "limits": {"max_steps": 12, "max_retries": 2, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 5, "min_evidence": 2, "no_changes": False},
        "evidence": ["demo/index.html:1", "demo/app.js:1"],
    })

    packed = build_context_pack(task, "User requirements")

    assert '"artifacts"' in packed
    assert '"path": "demo/index.html"' in packed
    assert "Return every requested file as an artifact" in packed
    assert "Do not write files directly" in packed


def test_context_pack_preserves_authoritative_context_byte_for_byte():
    task = TaskSpec.model_validate(
        {
            "task_id": "context-bytes-001", "agent": "achilles", "objective": "Inspect",
            "risk": "low", "execution": "bounded", "environment": "local",
            "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export"],
            "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["lines"],
        }
    )
    authoritative = "\n  first line  \r\nsecond line\t\r\n"
    packed = build_context_pack(task, authoritative)
    start = packed.index("AUTHORITATIVE CONTEXT\n") + len("AUTHORITATIVE CONTEXT\n")
    end = packed.index("\nEND AUTHORITATIVE CONTEXT", start)
    assert packed[start:end] == authoritative
