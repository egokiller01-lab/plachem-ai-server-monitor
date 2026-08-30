import json
from pathlib import Path

import pytest
import gateway.cli as cli_module

from gateway.cli import _build_gateway, _load_authoritative_context, _validated_runtime, main
from gateway.models import TaskSpec
from gateway.policy import PolicyEngine


def test_cli_dry_run_validates_any_taskspec_without_starting_worker(tmp_path: Path, capsys):
    task = {
        "task_id": "cli-001",
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
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")

    exit_code = main(["dry-run", "--task", str(path)])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"decision": "ready", "task_id": "cli-001"}


def test_cli_medium_task_requires_matching_approval_receipt(tmp_path: Path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    policy = PolicyEngine.default()
    policy.runtime_root = str(runtime)
    monkeypatch.setattr(cli_module.PolicyEngine, "default", classmethod(lambda cls: policy))
    task_path = tmp_path / "medium.json"
    task_path.write_text(json.dumps({
        "task_id": "medium-cli-001", "agent": "achilles", "objective": "Build web files",
        "risk": "medium", "execution": "bounded", "environment": "local",
        "scope": {"include": ["demo/index.html"], "exclude": ["gateway", ".git"]},
        "permissions": ["workspace_read", "workspace_write_scoped", "local_test"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 12, "max_retries": 2, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 5, "min_evidence": 1, "no_changes": False},
        "evidence": ["demo/index.html:1"],
    }), encoding="utf-8")

    assert main(["dry-run", "--task", str(task_path), "--runtime", str(runtime)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "needs_approval"

    assert main([
        "approve", "--task", str(task_path), "--runtime", str(runtime),
        "--approved-by", "kim",
    ]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"

    assert main(["dry-run", "--task", str(task_path), "--runtime", str(runtime)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "ready"


def test_context_file_must_be_named_in_taskspec_scope(tmp_path: Path):
    context = tmp_path / "secret.txt"
    context.write_text("not approved", encoding="utf-8")
    task = TaskSpec.model_validate(
        {
            "task_id": "cli-002",
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

    with pytest.raises(ValueError, match="not included in TaskSpec scope"):
        _load_authoritative_context(task, context, tmp_path)


def test_cli_wires_all_validated_v1_configuration(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runtime = project / "runtime"
    monkeypatch.chdir(project)
    policy = PolicyEngine.default()
    gateway = _build_gateway(runtime, policy)
    assert gateway.policy is policy
    assert gateway.runner.profile == policy.profile
    assert gateway.runner.toolset == policy.toolset
    assert gateway.runner.model == policy.model
    assert gateway.guard.minimum_free_vram_mib == policy.minimum_free_vram_mib
    assert gateway.guard.lock_path == Path(policy.lock_path)
    assert gateway.guard.llm_reachable.args == (policy.model_endpoint + "/models",)
    assert gateway.workspace is not None
    assert gateway.workspace.project_root == project.resolve()
    assert gateway.validator is not None


def test_runtime_must_resolve_to_policy_pinned_dedicated_root(tmp_path: Path):
    policy = PolicyEngine.default()
    with pytest.raises(ValueError, match="trusted runtime root"):
        _validated_runtime(tmp_path, policy)
    assert _validated_runtime(Path(policy.runtime_root), policy) == Path(policy.runtime_root)


def test_build_gateway_prepares_audit_before_worker_launch(tmp_path: Path, monkeypatch):
    prepared = []
    monkeypatch.setattr(cli_module.AuditLog, "prepare", lambda self: prepared.append(self.path))
    project = tmp_path / "project"
    project.mkdir()
    runtime = project / "runtime"
    monkeypatch.chdir(project)

    _build_gateway(runtime, PolicyEngine.default())

    assert prepared == [runtime / "audit.jsonl"]


def test_cli_rejects_duplicate_task_json_keys(tmp_path: Path, capsys):
    task_path = tmp_path / "duplicate.json"
    task_path.write_text(
        '{"task_id":"first","task_id":"second","agent":"achilles","objective":"Inspect",'
        '"risk":"low","execution":"bounded","environment":"local",'
        '"scope":{"include":["README.md"],"exclude":[]},"permissions":["repo_read"],'
        '"deny":["production","merge","deploy","secrets_export"],'
        '"limits":{"max_steps":3,"max_retries":0,"timeout_seconds":30},'
        '"completion":{"max_summary_sentences":2,"min_evidence":1,"no_changes":true},'
        '"evidence":["lines"]}',
        encoding="utf-8",
    )
    assert main(["dry-run", "--task", str(task_path)]) == 1
    assert "duplicate JSON key" in capsys.readouterr().err


def _write_ready_task(path: Path, task_id: str) -> None:
    path.write_text(json.dumps({
        "task_id": task_id, "agent": "achilles", "objective": "Inspect",
        "risk": "low", "execution": "bounded", "environment": "local",
        "scope": {"include": ["README.md"], "exclude": []}, "permissions": ["repo_read"],
        "deny": ["production", "merge", "deploy", "secrets_export", "destructive_delete", "permission_change"],
        "limits": {"max_steps": 3, "max_retries": 0, "timeout_seconds": 30},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["README.md:1"],
    }), encoding="utf-8")


def test_cli_does_not_write_untrusted_runtime_when_config_cannot_be_loaded(tmp_path: Path, monkeypatch):
    task_path = tmp_path / "task.json"
    runtime = tmp_path / "runtime"
    _write_ready_task(task_path, "config-fail-001")
    monkeypatch.setattr(cli_module.PolicyEngine, "default", classmethod(lambda cls: (_ for _ in ()).throw(ValueError("bad config SECRET"))))

    assert main(["run", "--task", str(task_path), "--runtime", str(runtime)]) == 1
    assert not runtime.exists()


def test_cli_audits_context_failure_without_raw_context(tmp_path: Path, monkeypatch):
    task_path = tmp_path / "task.json"
    runtime = tmp_path / "runtime"
    context = tmp_path / "secret.txt"
    _write_ready_task(task_path, "context-fail-001")
    context.write_text("RAW-SUPER-SECRET", encoding="utf-8")
    policy = PolicyEngine.default()
    policy.runtime_root = str(runtime)
    monkeypatch.setattr(cli_module.PolicyEngine, "default", classmethod(lambda cls: policy))

    assert main([
        "run", "--task", str(task_path), "--context", str(context),
        "--runtime", str(runtime),
    ]) == 1
    audit_text = (runtime / "audit.jsonl").read_text(encoding="utf-8")
    event = json.loads(audit_text)
    assert event["status"] == "context_failure"
    assert "RAW-SUPER-SECRET" not in audit_text
