import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway.achilles_runner import AchillesRunner, RunnerError
from gateway.models import TaskSpec


def task() -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "task_id": "runner-001",
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
            "evidence": ["lines"],
        }
    )


def test_runner_uses_achilles_profile_and_parses_json(tmp_path: Path):
    calls = []
    payload = {
        "task_id": "runner-001",
        "status": "completed",
        "summary": "README inspected",
        "changes": [],
        "checks": ["read README"],
        "evidence": ["README.md:1"],
        "permission_use": ["repo_read"],
        "production_changes": 0,
        "remaining_risks": [],
        "next_action": "review",
    }

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    result = AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "bounded context")

    command, kwargs = calls[0]
    assert command[:4] == [
        "C:/Users/egomine2/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe",
        "-p", "achilles", "chat",
    ]
    assert "--query-file" in command
    assert command[command.index("--toolsets") + 1] == "todo"
    assert "--ignore-rules" in command
    assert command[command.index("--max-turns") + 1] == "7"
    assert kwargs["timeout"] == 120
    assert result.summary == "README inspected"


def test_runner_rejects_non_json_result(tmp_path: Path):
    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")

    with pytest.raises(RunnerError, match="valid WorkerResult"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "bounded context")


def test_runner_scrubs_secrets_and_never_grants_worker_tools(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("LOCALAPPDATA", "safe-local-app-data")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    payload = {
        "task_id": "runner-001",
        "status": "completed",
        "summary": "README inspected",
        "checks": ["README.md:1 contains heading"],
        "evidence": ["README.md:1"],
        "permission_use": ["repo_read"],
        "production_changes": 0,
    }

    def execute(command, **kwargs):
        assert command[command.index("--toolsets") + 1] == "todo"
        assert kwargs["env"]["PATH"] == "C:/Windows/System32;C:/Windows"
        assert kwargs["env"]["LOCALAPPDATA"] == "safe-local-app-data"
        assert "OPENAI_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "bounded context")


def test_runner_rejects_worker_result_with_extra_fields(tmp_path: Path):
    payload = {
        "task_id": "runner-001",
        "status": "completed",
        "summary": "README inspected",
        "checks": ["README.md:1 contains heading"],
        "evidence": ["README.md:1"],
        "permission_use": ["repo_read"],
        "production_changes": 0,
        "unexpected": "model-controlled field",
    }

    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    with pytest.raises(RunnerError, match="valid WorkerResult"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "bounded context")


@pytest.mark.parametrize("suffix", [" trailing text", "\n{}", "\n{\"task_id\":\"other\"}"])
def test_runner_rejects_trailing_or_multiple_output_documents(tmp_path: Path, suffix: str):
    payload = {
        "task_id": "runner-001",
        "status": "completed",
        "summary": "README inspected",
        "checks": ["README.md:1 contains heading"],
        "evidence": ["README.md:1"],
        "permission_use": ["repo_read"],
        "production_changes": 0,
    }

    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + suffix, stderr="")

    with pytest.raises(RunnerError, match="exactly one WorkerResult"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "bounded context")


def test_runner_accepts_optional_safe_session_id_before_json(tmp_path: Path):
    payload = {
        "task_id": "runner-001", "status": "completed", "summary": "ok",
        "checks": ["README.md:1|title"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    }

    def execute(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout="session_id: safe-session_01\n" + json.dumps(payload), stderr=""
        )

    assert AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "context").summary == "ok"


@pytest.mark.parametrize(
    "prefix",
    [
        "Warning: Unknown toolsets: todo\n",
        "wrapper noise\n",
        "session_id: ../unsafe\n",
        "\nsession_id: safe-session\n",
    ],
)
def test_runner_rejects_unexpected_wrapper_output(tmp_path: Path, prefix: str):
    payload = {
        "task_id": "runner-001", "status": "completed", "summary": "ok",
        "checks": ["README.md:1|title"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    }

    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=prefix + json.dumps(payload), stderr="")

    with pytest.raises(RunnerError, match="exactly one WorkerResult"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "context")


def test_runner_rejects_duplicate_json_keys(tmp_path: Path):
    output = (
        '{"task_id":"runner-001","task_id":"runner-002","status":"completed",'
        '"summary":"ok","checks":["README.md:1|title"],'
        '"evidence":["README.md:1"],"permission_use":["repo_read"],"production_changes":0}'
    )

    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(RunnerError, match="valid WorkerResult"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "context")


def test_runner_uses_supplied_validated_profile_and_toolset(tmp_path: Path):
    payload = {
        "task_id": "runner-001", "status": "completed", "summary": "ok",
        "checks": ["README.md:1|title"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    }

    def execute(command, **kwargs):
        assert command[command.index("-p") + 1] == "validated-profile"
        assert command[command.index("--toolsets") + 1] == "todo"
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    AchillesRunner(
        tmp_path, executor=execute, profile="validated-profile", toolset="todo",
        attestor=lambda: None,
    ).run(task(), "context")


def test_runner_launches_from_sanitized_config_snapshot(tmp_path: Path):
    payload = {
        "task_id": "runner-001", "status": "completed", "summary": "ok",
        "checks": ["README.md:1|title"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    }

    def execute(command, **kwargs):
        assert command[command.index("--toolsets") + 1] == "todo"
        assert "--safe-mode" not in command
        assert "--ignore-rules" in command
        assert command[command.index("--provider") + 1] == "custom"
        assert kwargs["env"]["HERMES_SAFE_MODE"] == "1"
        home = Path(kwargs["env"]["HERMES_HOME"])
        config = (home / "profiles" / "achilles" / "config.yaml").read_text(encoding="utf-8")
        assert "provider: custom" in config
        assert "base_url: http://127.0.0.1:8080/v1" in config
        assert "Qwen3.8-27B-Uncensored-Q4_K_M.gguf" in config
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    AchillesRunner(tmp_path, executor=execute).run(task(), "context")


def test_runner_preserves_context_bytes_across_windows_newlines(tmp_path: Path):
    context = "first line\nsecond line\n"
    payload = {
        "task_id": "runner-001", "status": "completed", "summary": "ok",
        "checks": ["README.md:1|title"], "evidence": ["README.md:1"],
        "permission_use": ["repo_read"], "production_changes": 0,
    }

    def execute(command, **kwargs):
        query = Path(command[command.index("--query-file") + 1])
        assert query.read_bytes() == context.encode("utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    result = AchillesRunner(tmp_path, executor=execute).run(task(), context)
    assert result.task_id == "runner-001"


def test_runner_re_reads_and_hash_verifies_query_before_launch(tmp_path: Path):
    launched = False

    def tamper():
        query = tmp_path / "tasks" / "runner-001.md"
        query.chmod(0o600)
        query.write_text("tampered", encoding="utf-8")

    def execute(command, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("must not launch")

    with pytest.raises(RunnerError, match="query artifact integrity"):
        AchillesRunner(tmp_path, executor=execute, attestor=tamper).run(task(), "trusted")
    assert launched is False


def test_runner_honors_max_retries_and_sanitizes_nonzero_output(tmp_path: Path):
    calls = 0

    def execute(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 9, stdout="", stderr="SECRET_TOKEN=leak")

    with pytest.raises(RunnerError) as caught:
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "trusted")
    assert calls == 2
    assert "SECRET_TOKEN" not in str(caught.value)
    assert "exit code 9" in str(caught.value)


def test_runner_rejects_oversized_worker_output(tmp_path: Path):
    def execute(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="x" * (1024 * 1024 + 1), stderr="")

    with pytest.raises(RunnerError, match="output exceeded"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "trusted")


def test_runner_enforces_output_limit_while_process_is_running(tmp_path: Path):
    runner = AchillesRunner(tmp_path)
    command = [
        sys.executable,
        "-c",
        "import sys,time;sys.stdout.write('x'*(1024*1024+1));sys.stdout.flush();time.sleep(10)",
    ]
    started = time.monotonic()
    with pytest.raises(RunnerError, match="output exceeded"):
        runner._execute_once(command, {}, 8)
    assert time.monotonic() - started < 5


def test_retries_share_one_monotonic_total_deadline(tmp_path: Path):
    timeouts = []

    def execute(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        time.sleep(0.02)
        return subprocess.CompletedProcess(command, 9, stdout="", stderr="")

    with pytest.raises(RunnerError, match="exit code"):
        AchillesRunner(tmp_path, executor=execute, attestor=lambda: None).run(task(), "trusted")
    assert len(timeouts) == 2
    assert timeouts[1] < timeouts[0]
