from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import yaml

from gateway.models import TaskSpec, WorkerResult
from gateway.context_pack import MAX_QUERY_BYTES
from gateway.path_security import reject_reparse_points


class RunnerError(RuntimeError):
    pass


class RunnerParseError(RunnerError):
    pass


_WINDOWS_LAUNCH_ENV = {
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
_FIXED_LAUNCH_PATH = "C:/Windows/System32;C:/Windows"


def _scrubbed_environment() -> dict[str, str]:
    result = {key: value for key, value in os.environ.items() if key.upper() in _WINDOWS_LAUNCH_ENV}
    result["PATH"] = _FIXED_LAUNCH_PATH
    return result


_SESSION_LINE = re.compile(r"^session_id: [A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIXED_PROVIDER = "custom"
_FIXED_BASE_URL = "http://127.0.0.1:8080/v1"
_FIXED_MODEL = "E:/AI/models/Qwen3.8-27B-Uncensored-GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
_HERMES_EXECUTABLE = "C:/Users/egomine2/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe"
_MAX_CAPTURE_BYTES = 1024 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _extract_worker_result(text: str) -> WorkerResult:
    document = text.strip()
    if text.startswith("session_id:"):
        first, separator, remainder = text.rstrip().partition("\n")
        if not separator or not _SESSION_LINE.fullmatch(first):
            raise RunnerParseError(
                "Achilles did not return a valid WorkerResult; output must contain exactly one WorkerResult JSON document"
            )
        document = remainder.strip()
    try:
        value = json.loads(document, object_pairs_hook=_reject_duplicate_keys)
        return WorkerResult.model_validate(value)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise RunnerParseError(
            "Achilles did not return a valid WorkerResult; output must contain exactly one WorkerResult JSON document"
        ) from exc


class AchillesRunner:
    def __init__(
        self,
        runtime_dir: Path,
        executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        profile: str = "achilles",
        toolset: str = "todo",
        model: str = _FIXED_MODEL,
        attestor: Callable[[], None] | None = None,
        executable: str = _HERMES_EXECUTABLE,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.executor = executor
        self.profile = profile
        self.toolset = toolset
        self.model = model
        self.attestor = attestor or (
            lambda: None
        )
        if executable != _HERMES_EXECUTABLE or not Path(executable).is_absolute():
            raise RunnerError("Hermes executable identity is not trusted")
        self.executable = executable

    def _snapshot_home(self, task_id: str) -> Path:
        home = self.runtime_dir / "hermes-homes" / task_id
        profile_dir = home / "profiles" / self.profile
        profile_dir.mkdir(parents=True, exist_ok=False)
        config = {
            "model": {
                "provider": _FIXED_PROVIDER,
                "base_url": _FIXED_BASE_URL,
                "default": self.model,
            }
        }
        config_path = profile_dir / "config.yaml"
        with config_path.open("x", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=True)
        config_path.chmod(stat.S_IREAD)
        return home

    def _terminate_process_tree(self, process: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            try:
                killed = subprocess.run(
                    ["C:/Windows/System32/taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
                )
            except subprocess.TimeoutExpired as exc:
                raise RunnerError("process-tree termination timed out; descendants may remain") from exc
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RunnerError("process tree did not terminate; descendants may remain") from exc
            if killed.returncode != 0:
                raise RunnerError("process-tree termination could not be confirmed; descendants may remain")
        else:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise RunnerError("process did not terminate") from exc

    def _execute_once(self, command: list[str], launch_env: dict[str, str], timeout: float):
        if self.executor is not subprocess.run:
            return self.executor(
                command, cwd=str(self.runtime_dir.parent), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                env=launch_env,
            )
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command, cwd=str(self.runtime_dir.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=launch_env, creationflags=creationflags,
        )
        captures = {"stdout": bytearray(), "stderr": bytearray()}
        exceeded = threading.Event()

        def read_bounded(name: str, stream) -> None:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                captures[name].extend(chunk)
                if len(captures[name]) > _MAX_CAPTURE_BYTES:
                    exceeded.set()
                    return

        readers = [
            threading.Thread(target=read_bounded, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=read_bounded, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        while process.poll() is None and not exceeded.is_set():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)
        if exceeded.is_set() or timed_out:
            self._terminate_process_tree(process)
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            raise RunnerError("worker output readers did not terminate")
        if exceeded.is_set():
            raise RunnerError("Achilles output exceeded the configured capture limit")
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(
            command, process.returncode,
            bytes(captures["stdout"]).decode("utf-8", errors="replace"),
            bytes(captures["stderr"]).decode("utf-8", errors="replace"),
        )

    def run(self, task: TaskSpec, context_pack: str) -> WorkerResult:
        task_dir = self.runtime_dir / "tasks"
        reject_reparse_points(self.runtime_dir)
        reject_reparse_points(task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        query_file = task_dir / f"{task.task_id}.md"
        encoded_context = context_pack.encode("utf-8")
        if len(encoded_context) > MAX_QUERY_BYTES:
            raise RunnerError("worker query exceeds the configured byte limit")
        try:
            with query_file.open("xb") as handle:
                handle.write(encoded_context)
            query_file.chmod(stat.S_IREAD)
        except (FileExistsError, IsADirectoryError, PermissionError) as exc:
            raise RunnerError(f"query file already exists: {query_file}") from exc
        command = [
            self.executable,
            "-p",
            self.profile,
            "chat",
            "--provider",
            _FIXED_PROVIDER,
            "--model",
            self.model,
            "--query-file",
            str(query_file),
            "--source",
            "tool",
            "--toolsets",
            self.toolset,
            "--max-turns",
            str(task.limits.max_steps),
            "--run-budget",
            str(task.limits.timeout_seconds),
            "--ignore-rules",
            "--quiet",
        ]
        expected_query_hash = hashlib.sha256(encoded_context).digest()
        self.attestor()
        reject_reparse_points(query_file)
        try:
            actual_query = query_file.read_bytes()
        except OSError as exc:
            raise RunnerError("query artifact integrity verification failed") from exc
        if hashlib.sha256(actual_query).digest() != expected_query_hash:
            raise RunnerError("query artifact integrity verification failed")
        snapshot_home = self._snapshot_home(task.task_id)
        launch_env = _scrubbed_environment()
        launch_env["HERMES_SAFE_MODE"] = "1"
        launch_env["HERMES_HOME"] = str(snapshot_home)
        completed = None
        last_timeout: subprocess.TimeoutExpired | None = None
        deadline = time.monotonic() + task.limits.timeout_seconds
        for _attempt in range(task.limits.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_timeout = subprocess.TimeoutExpired(command, task.limits.timeout_seconds)
                break
            try:
                completed = self._execute_once(command, launch_env, remaining)
            except subprocess.TimeoutExpired as exc:
                last_timeout = exc
                continue
            except OSError as exc:
                raise RunnerError("could not start Achilles") from exc
            if len(completed.stdout.encode("utf-8")) > _MAX_CAPTURE_BYTES or len(completed.stderr.encode("utf-8")) > _MAX_CAPTURE_BYTES:
                raise RunnerError("Achilles output exceeded the configured capture limit")
            if completed.returncode == 0:
                break
        if completed is None or completed.returncode != 0:
            if completed is None and last_timeout is not None:
                raise RunnerError(f"Achilles timed out after {task.limits.timeout_seconds}s") from last_timeout
            if completed is None:
                raise RunnerError("Achilles process failed without a result")
            raise RunnerError(f"Achilles process failed with exit code {completed.returncode}")
        return _extract_worker_result(completed.stdout)
