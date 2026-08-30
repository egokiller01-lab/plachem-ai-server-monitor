import json
import hashlib
import stat
from pathlib import Path

import pytest

from gateway.audit import AuditLog
from gateway.achilles_runner import RunnerParseError
from gateway.context_pack import MAX_CONTEXT_BYTES
from gateway.local_validation import LocalValidationAdapter
from gateway.models import TaskSpec, WorkerResult
from gateway.service import DelegationGateway
from gateway.verifier import VerificationStatus
from gateway.workspace import ScopedWorkspace


class FakeGuard:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeRunner:
    def run(self, task, context_pack):
        assert "Inspect README" in context_pack
        return WorkerResult.model_validate(
            {
                "task_id": task.task_id,
                "status": "completed",
                "summary": "README inspected",
                "checks": ["README.md:1|README is authoritative"],
                "evidence": ["README.md:1"],
                "permission_use": ["repo_read"],
                "production_changes": 0,
            }
        )


def test_gateway_runs_approved_medium_artifact_workflow(tmp_path: Path):
    project = tmp_path / "project"
    runtime = project / "runtime"
    project.mkdir()
    demo = project / "demo"
    demo.mkdir()
    (demo / "index.html").write_text("<h1>Old monitor</h1>", encoding="utf-8")
    (demo / "style.css").write_text("body { color: gray; }", encoding="utf-8")
    (demo / "app.js").write_text("console.log('old')", encoding="utf-8")
    (demo / "README.md").write_text("protected", encoding="utf-8")
    task = TaskSpec.model_validate({
        "task_id": "medium-e2e-001", "agent": "achilles", "objective": "Build static web",
        "risk": "medium", "execution": "bounded", "environment": "local",
        "scope": {
            "include": ["demo/index.html", "demo/style.css", "demo/app.js"],
            "exclude": ["demo/README.md", "gateway", "config", ".git"],
        },
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change", "external_network"],
        "limits": {"max_steps": 12, "max_retries": 2, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 5, "min_evidence": 3, "no_changes": False},
        "evidence": ["demo/index.html:1", "demo/style.css:1", "demo/app.js:1"],
    })

    class MediumRunner:
        def run(self, task, context_pack):
            assert "Return every requested file as an artifact" in context_pack
            assert "<h1>Old monitor</h1>" in context_pack
            assert "body { color: gray; }" in context_pack
            assert "console.log('old')" in context_pack
            assert "protected" not in context_pack
            return WorkerResult.model_validate({
                "task_id": task.task_id, "status": "completed", "summary": "Created files.",
                "artifacts": [
                    {"path": "demo/index.html", "content": '<!doctype html><link rel="stylesheet" href="style.css"><script src="app.js"></script>'},
                    {"path": "demo/style.css", "content": "body { color: black; }"},
                    {"path": "demo/app.js", "content": "console.log('ok')"},
                ],
                "changes": ["demo/index.html", "demo/style.css", "demo/app.js"],
                "checks": ["bundle self-check"],
                "evidence": ["demo/index.html:1", "demo/style.css:1", "demo/app.js:1"],
                "permission_use": task.permissions,
                "production_changes": 0,
            })

    validation_roots = []

    class TrackingValidator(LocalValidationAdapter):
        def validate(self, task, staged):
            validation_roots.append(staged.root)
            return super().validate(task, staged)

    gateway = DelegationGateway(
        runtime_dir=runtime,
        runner=MediumRunner(),
        guard=FakeGuard(),
        audit=AuditLog(runtime / "audit.jsonl"),
        workspace=ScopedWorkspace(project, runtime),
        validator=TrackingValidator(),
    )

    outcome = gateway.execute(task, authoritative_context="Approved user requirements", approved=True)

    assert outcome.verification.status is VerificationStatus.VERIFIED
    assert (project / "demo" / "index.html").exists()
    baseline = json.loads((runtime / "baselines" / "medium-e2e-001.json").read_text(encoding="utf-8"))
    assert {item["path"] for item in baseline["files"]} == {
        "demo/index.html", "demo/style.css", "demo/app.js", "demo/README.md"
    }
    assert all(item["size"] >= 0 and len(item["sha256"]) == 64 for item in baseline["files"])
    assert baseline["started_at"]
    assert (demo / "README.md").read_text(encoding="utf-8") == "protected"
    assert len(validation_roots) == 2
    assert validation_roots[1] == project.resolve()
    assert (runtime / "manifests" / "medium-e2e-001.json").exists()
    event = json.loads((runtime / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["status"] == "verified"


def test_gateway_blocks_apply_when_protected_baseline_file_changes(tmp_path: Path):
    project = tmp_path / "project"
    runtime = project / "runtime"
    demo = project / "demo"
    demo.mkdir(parents=True)
    originals = {
        "index.html": "<h1>old</h1>", "style.css": "body{}",
        "app.js": "console.log('old')", "README.md": "protected",
    }
    for name, content in originals.items():
        (demo / name).write_text(content, encoding="utf-8")
    task_payload = {
        "task_id": "protected-change-001", "agent": "achilles", "objective": "Update web",
        "risk": "medium", "execution": "bounded", "environment": "local",
        "scope": {
            "include": ["demo/index.html", "demo/style.css", "demo/app.js"],
            "exclude": ["demo/README.md"],
        },
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change", "external_network"],
        "limits": {"max_steps": 6, "max_retries": 0, "timeout_seconds": 120},
        "completion": {"max_summary_sentences": 3, "min_evidence": 3, "no_changes": False},
        "evidence": ["demo/index.html:1", "demo/style.css:1", "demo/app.js:1"],
    }
    task = TaskSpec.model_validate(task_payload)

    class InterferingRunner:
        def run(self, task, context_pack):
            (demo / "README.md").write_text("interfered", encoding="utf-8")
            return WorkerResult.model_validate({
                "task_id": task.task_id, "status": "completed", "summary": "updated",
                "artifacts": [
                    {"path": "demo/index.html", "content": "<h1>new</h1>"},
                    {"path": "demo/style.css", "content": "body{color:black}"},
                    {"path": "demo/app.js", "content": "console.log('new')"},
                ],
                "changes": task.scope.include, "checks": ["checked"],
                "evidence": task.evidence, "permission_use": task.permissions,
                "production_changes": 0,
            })

    gateway = DelegationGateway(
        runtime, InterferingRunner(), FakeGuard(), AuditLog(runtime / "audit.jsonl"),
        workspace=ScopedWorkspace(project, runtime), validator=LocalValidationAdapter(),
    )
    with pytest.raises(Exception, match="protected baseline file changed"):
        gateway.execute(task, approved=True)
    assert (demo / "index.html").read_text(encoding="utf-8") == originals["index.html"]


def test_gateway_runs_policy_context_worker_verification_and_audit(tmp_path: Path):
    task = TaskSpec.model_validate(
        {
            "task_id": "e2e-001",
            "agent": "achilles",
            "objective": "Inspect README",
            "risk": "low",
            "execution": "bounded",
            "environment": "local",
            "scope": {"include": ["README.md"], "exclude": ["production"]},
            "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
            "limits": {"max_steps": 7, "max_retries": 1, "timeout_seconds": 120},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["README.md:1"],
        }
    )
    gateway = DelegationGateway(
        runtime_dir=tmp_path,
        runner=FakeRunner(),
        guard=FakeGuard(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )

    with pytest.raises(ValueError, match="authoritative context is required"):
        gateway.execute(task, authoritative_context="  ")

    outcome = gateway.execute(task, authoritative_context="README is authoritative")

    assert outcome.verification.status is VerificationStatus.VERIFIED
    assert (tmp_path / "tasks" / "e2e-001.json").exists()
    assert (tmp_path / "results" / "e2e-001.json").exists()
    assert (tmp_path / "audit.jsonl").exists()
    task_path = tmp_path / "tasks" / "e2e-001.json"
    result_path = tmp_path / "results" / "e2e-001.json"
    assert task_path.stat().st_mode & stat.S_IWUSR == 0
    assert result_path.stat().st_mode & stat.S_IWUSR == 0
    event = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    canonical_task = json.dumps(json.loads(task_path.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_result = json.dumps(json.loads(result_path.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert event["task_sha256"] == hashlib.sha256(canonical_task.encode()).hexdigest()
    assert event["result_sha256"] == hashlib.sha256(canonical_result.encode()).hexdigest()

    with pytest.raises(FileExistsError, match="task_id already exists"):
        gateway.execute(task, authoritative_context="README is authoritative")


def test_gateway_audits_runner_failure_before_reraising(tmp_path: Path):
    task = TaskSpec.model_validate(
        {
            "task_id": "e2e-fail",
            "agent": "achilles",
            "objective": "Inspect README",
            "risk": "low",
            "execution": "bounded",
            "environment": "local",
            "scope": {"include": ["README.md"], "exclude": ["production"]},
            "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
            "limits": {"max_steps": 7, "max_retries": 1, "timeout_seconds": 120},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["README.md:1"],
        }
    )

    class FailingRunner:
        def run(self, task, context_pack):
            raise RuntimeError("invalid worker output")

    gateway = DelegationGateway(
        runtime_dir=tmp_path,
        runner=FailingRunner(),
        guard=FakeGuard(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )

    with pytest.raises(RuntimeError, match="invalid worker output"):
        gateway.execute(task, authoritative_context="README is authoritative")

    event = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert event["status"] == "runner_failure"


def test_gateway_preflights_all_task_artifacts_before_guard_or_worker(tmp_path: Path):
    task = TaskSpec.model_validate(
        {
            "task_id": "preflight-001", "agent": "achilles", "objective": "Inspect README",
            "risk": "low", "execution": "bounded", "environment": "local",
            "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
            "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["README.md:1"],
        }
    )
    entered = []

    class TrackingGuard(FakeGuard):
        def __enter__(self):
            entered.append("guard")
            return self

    class TrackingRunner(FakeRunner):
        def run(self, task, context_pack):
            entered.append("runner")
            return super().run(task, context_pack)

    result_path = tmp_path / "results" / "preflight-001.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text("occupied", encoding="utf-8")
    gateway = DelegationGateway(
        tmp_path, TrackingRunner(), TrackingGuard(), AuditLog(tmp_path / "audit.jsonl")
    )

    with pytest.raises(FileExistsError, match="task_id already exists"):
        gateway.execute(task, "README is authoritative")

    assert entered == []
    event = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert event["status"] == "preflight_failure"


@pytest.mark.parametrize("folder, extension", [("tasks", ".json"), ("tasks", ".md"), ("results", ".json")])
def test_gateway_rejects_each_preexisting_artifact_path(tmp_path: Path, folder: str, extension: str):
    task = TaskSpec.model_validate(
        {
            "task_id": "occupied-001", "agent": "achilles", "objective": "Inspect README",
            "risk": "low", "execution": "bounded", "environment": "local",
            "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
            "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["README.md:1"],
        }
    )
    occupied = tmp_path / folder / f"occupied-001{extension}"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.mkdir()
    gateway = DelegationGateway(tmp_path, FakeRunner(), FakeGuard(), AuditLog(tmp_path / "audit.jsonl"))
    with pytest.raises(FileExistsError):
        gateway.execute(task, "README is authoritative")


def test_gateway_audits_parse_failure_distinctly(tmp_path: Path):
    task = TaskSpec.model_validate(
        {
            "task_id": "parse-001", "agent": "achilles", "objective": "Inspect README",
            "risk": "low", "execution": "bounded", "environment": "local",
            "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
            "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
            "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
            "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
            "evidence": ["README.md:1"],
        }
    )

    class ParseFailRunner:
        def run(self, task, context_pack):
            raise RunnerParseError("bad result")

    gateway = DelegationGateway(tmp_path, ParseFailRunner(), FakeGuard(), AuditLog(tmp_path / "audit.jsonl"))
    with pytest.raises(RunnerParseError):
        gateway.execute(task, "README is authoritative")
    event = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert event["status"] == "parse_failure"


def test_gateway_rejects_runtime_artifact_parent_symlink(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    try:
        (runtime / "tasks").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    gateway = DelegationGateway(runtime, FakeRunner(), FakeGuard(), AuditLog(runtime / "audit.jsonl"))
    task = TaskSpec.model_validate({
        "task_id": "escape-001", "agent": "achilles", "objective": "Inspect README",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["README.md:1"],
    })
    with pytest.raises(ValueError, match="reparse"):
        gateway.execute(task, "README is authoritative")
    assert list(outside.iterdir()) == []


def test_gpu_guard_remains_held_through_result_persistence_and_audit(tmp_path: Path):
    active = False

    class Guard:
        def __enter__(self):
            nonlocal active
            active = True
            return self

        def __exit__(self, *args):
            nonlocal active
            active = False

    class CheckingAudit(AuditLog):
        def append(self, *args, **kwargs):
            assert active is True
            assert (tmp_path / "results" / "guard-001.json").exists()
            return super().append(*args, **kwargs)

    task = TaskSpec.model_validate({
        "task_id": "guard-001", "agent": "achilles", "objective": "Inspect README",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["README.md:1"],
    })
    DelegationGateway(tmp_path, FakeRunner(), Guard(), CheckingAudit(tmp_path / "audit.jsonl")).execute(
        task, "README is authoritative"
    )
    assert active is False


def test_oversized_context_fails_before_guard_or_worker_and_audits_no_raw_content(tmp_path: Path):
    task = TaskSpec.model_validate({
        "task_id": "context-limit-1", "agent": "achilles", "objective": "Inspect",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["README.md:1"],
    })
    entered = []

    class Guard(FakeGuard):
        def __enter__(self):
            entered.append("guard")
            return self

    class Runner(FakeRunner):
        def run(self, task, context_pack):
            entered.append("runner")
            return super().run(task, context_pack)

    raw = "S" * (MAX_CONTEXT_BYTES + 1)
    gateway = DelegationGateway(tmp_path, Runner(), Guard(), AuditLog(tmp_path / "audit.jsonl"))
    with pytest.raises(ValueError, match="context exceeds"):
        gateway.execute(task, raw)
    assert entered == []
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert raw[:100] not in audit
