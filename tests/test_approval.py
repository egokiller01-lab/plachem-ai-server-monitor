import json
from pathlib import Path

from gateway.approval import ApprovalStore
from gateway.models import TaskSpec


def medium_task(objective: str = "Build a scoped web app") -> TaskSpec:
    return TaskSpec.model_validate({
        "task_id": "approval-001",
        "agent": "achilles",
        "objective": objective,
        "risk": "medium",
        "execution": "bounded",
        "environment": "local",
        "scope": {"include": ["demo/index.html"], "exclude": ["gateway", ".git"]},
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 12, "max_retries": 2, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 5, "min_evidence": 1, "no_changes": False},
        "evidence": ["demo/index.html:1"],
    })


def test_approval_receipt_is_bound_to_exact_taskspec(tmp_path: Path):
    store = ApprovalStore(tmp_path)
    task = medium_task()

    receipt = store.approve(task, approved_by="kim")

    assert store.is_approved(task) is True
    assert store.is_approved(medium_task("Changed objective")) is False
    saved = json.loads((tmp_path / "approvals" / "approval-001.json").read_text(encoding="utf-8"))
    assert saved["task_sha256"] == receipt.task_sha256
    assert saved["approved_by"] == "kim"


def test_approval_receipt_cannot_be_overwritten(tmp_path: Path):
    store = ApprovalStore(tmp_path)
    task = medium_task()
    store.approve(task, approved_by="kim")

    try:
        store.approve(task, approved_by="other")
    except FileExistsError:
        pass
    else:
        raise AssertionError("approval receipt overwrite must fail")
