from __future__ import annotations

from enum import Enum
from pathlib import Path
import re

import yaml

from gateway.models import Environment, ExecutionMode, RiskLevel, TaskSpec


class PolicyDecision(str, Enum):
    READY = "ready"
    NEEDS_INFO = "needs_info"
    NEEDS_APPROVAL = "needs_approval"
    DENIED = "denied"


class PolicyViolation(ValueError):
    pass


_REQUIRED_DENIALS = {"production", "merge", "deploy", "secrets_export"}
_REQUIRED_CRITICAL = {"destructive_delete", "permission_change"}
_FIXED_LOCK = "C:/ProgramData/PLACHEM-Agent-Control/rtx3090.lock"
_FIXED_MODEL = "E:/AI/models/Qwen3.8-27B-Uncensored-GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
_FIXED_HERMES = "C:/Users/egomine2/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe"
_FIXED_NVIDIA_SMI = "C:/Windows/System32/nvidia-smi.exe"
_MEDIUM_SCOPED_PERMISSIONS = {
    "workspace_read",
    "workspace_write_scoped",
    "local_test",
}
_FIXED_RUNTIME = "E:/PLACHEM-Agent-Control/repo/runtime"
_HERMES_SHA256 = "dc5357dc27045339c7748c96ba1690eeccdd72a903231027a0235a33cdd291c3"
_NVIDIA_SMI_SHA256 = "9b3da28a74c5bfbf33b147b4b73c105e55b1f74b474e63ec7762843e5f2b635d"
_MODEL_SHA256 = "4c5e2db039e9325ac7724c8846c71356a24ad1cdfa28002d73ecb6be645f9675"
_EVIDENCE_REFERENCE = re.compile(r"^(?P<path>[^:\r\n|]+):[1-9][0-9]*$")


class PolicyEngine:
    def __init__(
        self,
        critical_denies: set[str],
        mandatory_denials: set[str],
        worker: str,
        allowed_permissions: list[str],
        profile: str = "achilles",
        model_endpoint: str = "http://127.0.0.1:8080/v1",
        model: str = _FIXED_MODEL,
        toolset: str = "todo",
        minimum_free_vram_mib: int = 512,
        lock_path: str = _FIXED_LOCK,
        hermes_executable: str = _FIXED_HERMES,
        nvidia_smi_executable: str = _FIXED_NVIDIA_SMI,
        runtime_root: str = _FIXED_RUNTIME,
        hermes_sha256: str = _HERMES_SHA256,
        nvidia_smi_sha256: str = _NVIDIA_SMI_SHA256,
        model_sha256: str = _MODEL_SHA256,
    ) -> None:
        self.critical_denies = critical_denies
        self.mandatory_denials = mandatory_denials
        self.worker = worker
        self.allowed_permissions = allowed_permissions
        self.profile = profile
        self.model_endpoint = model_endpoint
        self.model = model
        self.toolset = toolset
        self.minimum_free_vram_mib = minimum_free_vram_mib
        self.lock_path = lock_path
        self.hermes_executable = hermes_executable
        self.nvidia_smi_executable = nvidia_smi_executable
        self.runtime_root = runtime_root
        self.hermes_sha256 = hermes_sha256
        self.nvidia_smi_sha256 = nvidia_smi_sha256
        self.model_sha256 = model_sha256

    @classmethod
    def default(cls) -> "PolicyEngine":
        config_dir = Path(__file__).resolve().parent.parent / "config"
        return cls.from_config(config_dir / "project-policy.yaml", config_dir / "agents.yaml")

    @classmethod
    def from_config(cls, policy_path: Path, agents_path: Path) -> "PolicyEngine":
        try:
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            agents = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
            if not isinstance(policy, dict) or not isinstance(agents, dict):
                raise TypeError("configuration roots must be mappings")
            if policy.get("default") != "deny":
                raise ValueError("default must be exactly deny")
            mandatory = set(policy["mandatory_denials"])
            critical = set(policy["critical_denials"])
            if not _REQUIRED_DENIALS.issubset(mandatory):
                raise ValueError("mandatory_denials omits a required denial")
            if not _REQUIRED_CRITICAL.issubset(critical):
                raise ValueError("critical_denials omits a required denial")

            v1 = policy["v1"]
            if not isinstance(v1, dict):
                raise TypeError("v1 must be a mapping")
            worker = v1["worker"]
            if worker != "achilles":
                raise ValueError("V1 worker must be achilles")
            if v1["risk"] != "low" or v1["environment"] != "local" or v1["execution"] != "bounded":
                raise ValueError("V1 config must remain low/local/bounded")
            permissions = v1["permissions"]
            if permissions != ["repo_read"]:
                raise ValueError("V1 config permissions must be exactly [repo_read]")

            agent = agents["agents"][worker]
            profile = agent["profile"]
            endpoint = agent["model_endpoint"]
            model = agent["model"]
            toolset = agent["toolset"]
            hermes_executable = agent["hermes_executable"]
            nvidia_smi_executable = agent["nvidia_smi_executable"]
            hermes_sha256 = agent["hermes_sha256"]
            nvidia_smi_sha256 = agent["nvidia_smi_sha256"]
            model_sha256 = agent["model_sha256"]
            if profile != "achilles":
                raise ValueError("V1 profile must be achilles")
            if endpoint != "http://127.0.0.1:8080/v1":
                raise ValueError("V1 endpoint must be the fixed localhost endpoint")
            if model != _FIXED_MODEL:
                raise ValueError("V1 model must be the fixed Achilles model")
            if toolset != "todo":
                raise ValueError("V1 toolset must be todo")
            if hermes_executable != _FIXED_HERMES or nvidia_smi_executable != _FIXED_NVIDIA_SMI:
                raise ValueError("V1 executable identities must be fixed absolute paths")
            if (hermes_sha256, nvidia_smi_sha256, model_sha256) != (
                _HERMES_SHA256, _NVIDIA_SMI_SHA256, _MODEL_SHA256
            ):
                raise ValueError("V1 trusted file hashes must be policy-pinned")

            resource = policy["resource_policy"]
            minimum = resource["minimum_free_vram_mib"]
            if resource["gpu"] != "RTX 3090" or resource["block_when_comfyui_busy"] is not True:
                raise ValueError("V1 resource policy must retain RTX 3090 and ComfyUI exclusion")
            if type(minimum) is not int or minimum < 1:
                raise TypeError("minimum_free_vram_mib must be a positive integer")
            lock_path = resource["lock_path"]
            if lock_path != _FIXED_LOCK:
                raise ValueError("V1 lock path must be the fixed machine-wide path")
            runtime_root = policy["runtime_root"]
            if runtime_root != _FIXED_RUNTIME:
                raise ValueError("V1 runtime root must be the fixed dedicated path")
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise PolicyViolation(f"invalid authoritative configuration: {exc}") from exc
        return cls(
            critical_denies=critical,
            mandatory_denials=mandatory,
            worker=worker,
            allowed_permissions=permissions,
            profile=profile,
            model_endpoint=endpoint,
            model=model,
            toolset=toolset,
            minimum_free_vram_mib=minimum,
            lock_path=lock_path,
            hermes_executable=hermes_executable,
            nvidia_smi_executable=nvidia_smi_executable,
            runtime_root=runtime_root,
            hermes_sha256=hermes_sha256,
            nvidia_smi_sha256=nvidia_smi_sha256,
            model_sha256=model_sha256,
        )

    def evaluate(self, task: TaskSpec, *, approved: bool = False) -> PolicyDecision:
        requested = set(task.permissions)
        denied = set(task.deny)
        explicitly_denied = requested & denied
        if explicitly_denied:
            raise PolicyViolation(f"permissions explicitly denied: {sorted(explicitly_denied)}")
        included = {item.replace("\\", "/") for item in task.scope.include}
        for requirement in task.evidence:
            match = _EVIDENCE_REFERENCE.fullmatch(requirement)
            if not match or match.group("path").replace("\\", "/") not in included:
                raise PolicyViolation(
                    "unsupported or out-of-scope evidence requirement; expected exact <path>:<line>"
                )
        excluded = {item.replace("\\", "/") for item in task.scope.exclude}
        if included & excluded:
            return PolicyDecision.NEEDS_INFO
        missing_denials = self.mandatory_denials - denied
        if missing_denials:
            raise PolicyViolation(f"mandatory denials missing: {sorted(missing_denials)}")
        missing_critical = self.critical_denies - set(task.deny)
        if missing_critical:
            raise PolicyViolation(f"critical denials missing: {sorted(missing_critical)}")
        if task.agent != self.worker:
            raise PolicyViolation(f"unknown worker: {task.agent}")
        if task.environment is Environment.PRODUCTION or "production" in requested:
            raise PolicyViolation("production is denied for achilles")
        if task.risk is RiskLevel.MEDIUM:
            if not approved:
                return PolicyDecision.NEEDS_APPROVAL
            unsupported = requested - _MEDIUM_SCOPED_PERMISSIONS
            if unsupported or "workspace_write_scoped" not in requested:
                raise PolicyViolation(
                    "MEDIUM permissions must be a scoped subset of "
                    f"{sorted(_MEDIUM_SCOPED_PERMISSIONS)} and include workspace_write_scoped"
                )
            if task.environment is not Environment.LOCAL:
                raise PolicyViolation("MEDIUM scoped workspace execution must be local")
            if task.execution is not ExecutionMode.BOUNDED:
                raise PolicyViolation("MEDIUM scoped workspace execution must be bounded")
            if task.completion.no_changes:
                raise PolicyViolation("MEDIUM scoped workspace completion must allow declared changes")
            return PolicyDecision.READY
        if task.risk is not RiskLevel.LOW:
            return PolicyDecision.NEEDS_APPROVAL
        if len(task.scope.include) != 1:
            return PolicyDecision.NEEDS_APPROVAL
        if task.environment is not Environment.LOCAL:
            raise PolicyViolation("V1 environment must be local")
        if task.execution is not ExecutionMode.BOUNDED:
            raise PolicyViolation("V1 execution must be bounded")
        if task.permissions != self.allowed_permissions:
            raise PolicyViolation("V1 permissions must be exactly ['repo_read']")
        if not task.completion.no_changes:
            raise PolicyViolation("V1 completion must require no changes")
        forbidden = requested & self.critical_denies
        if forbidden:
            raise PolicyViolation(f"denied permissions: {sorted(forbidden)}")
        return PolicyDecision.READY
