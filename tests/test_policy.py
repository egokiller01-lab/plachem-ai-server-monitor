import pytest

from gateway.models import TaskSpec
from gateway.policy import PolicyDecision, PolicyEngine, PolicyViolation


def task(**overrides):
    data = {
        "task_id": "policy-001",
        "agent": "achilles",
        "objective": "Inspect a local file",
        "risk": "low",
        "execution": "bounded",
        "environment": "local",
        "scope": {"include": ["README.md"], "exclude": ["production"]},
        "permissions": ["repo_read"],
        "deny": [
            "production",
            "merge",
            "deploy",
            "secrets_export",
            "destructive_delete",
            "permission_change",
        ],
        "limits": {"max_steps": 12, "max_retries": 1, "timeout_seconds": 900},
        "completion": {"max_summary_sentences": 2, "min_evidence": 1, "no_changes": True},
        "evidence": ["README.md:1"],
    }
    data.update(overrides)
    return TaskSpec.model_validate(data)


def test_allows_low_risk_local_read_task():
    engine = PolicyEngine.default()
    decision = engine.evaluate(task())
    assert decision is PolicyDecision.READY
    assert engine.hermes_executable.endswith("/hermes.exe")
    assert engine.nvidia_smi_executable == "C:/Windows/System32/nvidia-smi.exe"
    assert engine.runtime_root == "C:/Users/egomine2/PLACHEM-Agent-Control/runtime"
    assert engine.hermes_sha256 == "dc5357dc27045339c7748c96ba1690eeccdd72a903231027a0235a33cdd291c3"
    assert engine.nvidia_smi_sha256 == "9b3da28a74c5bfbf33b147b4b73c105e55b1f74b474e63ec7762843e5f2b635d"
    assert engine.model_sha256 == "4c5e2db039e9325ac7724c8846c71356a24ad1cdfa28002d73ecb6be645f9675"


def test_rejects_production_for_achilles():
    with pytest.raises(PolicyViolation, match="production"):
        PolicyEngine.default().evaluate(
            task(risk="critical", environment="production", permissions=["production"])
        )


def test_high_risk_task_requires_approval_instead_of_execution():
    decision = PolicyEngine.default().evaluate(
        task(risk="high", execution="supervised", environment="staging", permissions=["draft_only"])
    )
    assert decision is PolicyDecision.NEEDS_APPROVAL


def test_medium_risk_read_task_requires_approval_in_v1():
    decision = PolicyEngine.default().evaluate(task(risk="medium"))
    assert decision is PolicyDecision.NEEDS_APPROVAL


def test_approved_medium_scoped_workspace_task_is_ready():
    medium = task(
        risk="medium",
        scope={
            "include": [
                "delegation-demo/index.html",
                "delegation-demo/style.css",
                "delegation-demo/app.js",
                "delegation-demo/README.md",
            ],
            "exclude": ["gateway", "config", "tests", ".git"],
        },
        permissions=["workspace_read", "workspace_write_scoped", "local_test"],
        completion={"max_summary_sentences": 5, "min_evidence": 4, "no_changes": False},
        evidence=[
            "delegation-demo/index.html:1",
            "delegation-demo/style.css:1",
            "delegation-demo/app.js:1",
            "delegation-demo/README.md:1",
        ],
    )
    engine = PolicyEngine.default()

    assert engine.evaluate(medium) is PolicyDecision.NEEDS_APPROVAL
    assert engine.evaluate(medium, approved=True) is PolicyDecision.READY


def test_approved_medium_task_rejects_permissions_outside_scoped_package():
    medium = task(
        risk="medium",
        permissions=["workspace_write_scoped", "git_push"],
        completion={"max_summary_sentences": 5, "min_evidence": 1, "no_changes": False},
    )

    with pytest.raises(PolicyViolation, match="MEDIUM permissions"):
        PolicyEngine.default().evaluate(medium, approved=True)


def test_rejects_contract_missing_mandatory_denials():
    with pytest.raises(PolicyViolation, match="mandatory denials"):
        PolicyEngine.default().evaluate(task(deny=["production"]))


@pytest.mark.parametrize("missing", ["destructive_delete", "permission_change"])
def test_rejects_contract_missing_critical_denial(missing):
    deny = [
        "production",
        "merge",
        "deploy",
        "secrets_export",
        "destructive_delete",
        "permission_change",
    ]
    deny.remove(missing)

    with pytest.raises(PolicyViolation, match="critical denials"):
        PolicyEngine.default().evaluate(task(deny=deny))


def test_v1_rejects_any_permission_other_than_exact_repo_read():
    with pytest.raises(PolicyViolation, match="V1 permissions must be exactly"):
        PolicyEngine.default().evaluate(task(permissions=["repo_read", "file_write"]))


def test_explicit_deny_overrides_matching_permission():
    with pytest.raises(PolicyViolation, match="explicitly denied"):
        PolicyEngine.default().evaluate(task(deny=task().deny + ["repo_read"]))


def test_include_exclude_intersection_is_not_ready():
    decision = PolicyEngine.default().evaluate(
        task(
            scope={"include": ["docs/README.md"], "exclude": ["docs\\README.md"]},
            evidence=["docs/README.md:1"],
        )
    )
    assert decision is not PolicyDecision.READY


def test_multiple_documents_are_valid_intake_but_need_approval_for_v1_execution():
    decision = PolicyEngine.default().evaluate(
        task(scope={"include": ["README.md", "docs/a.md"], "exclude": []})
    )
    assert decision is PolicyDecision.NEEDS_APPROVAL


@pytest.mark.parametrize("requirement", ["lines", "line references", "README.md", "other.md:1"])
def test_policy_rejects_unsupported_or_out_of_scope_evidence_requirements(requirement):
    with pytest.raises(PolicyViolation, match="evidence requirement"):
        PolicyEngine.default().evaluate(task(evidence=[requirement]))


def test_v1_rejects_low_risk_non_local_execution():
    with pytest.raises(PolicyViolation, match="V1 environment must be local"):
        PolicyEngine.default().evaluate(task(environment="development"))


def test_v1_rejects_non_bounded_execution_mode():
    with pytest.raises(PolicyViolation, match="V1 execution must be bounded"):
        PolicyEngine.default().evaluate(task(execution="autonomous"))


def test_policy_engine_loads_authoritative_yaml_configuration(tmp_path):
    policy_path = tmp_path / "project-policy.yaml"
    agents_path = tmp_path / "agents.yaml"
    policy_path.write_text(
        "default: deny\n"
        "runtime_root: C:/Users/egomine2/PLACHEM-Agent-Control/runtime\n"
        "mandatory_denials: [production, merge, deploy, secrets_export, custom_deny]\n"
        "critical_denials: [destructive_delete, permission_change]\n"
        "v1:\n"
        "  worker: achilles\n"
        "  risk: low\n"
        "  environment: local\n"
        "  execution: bounded\n"
        "  permissions: [repo_read]\n"
        "resource_policy:\n"
        "  gpu: RTX 3090\n"
        "  minimum_free_vram_mib: 512\n"
        "  block_when_comfyui_busy: true\n"
        "  lock_path: C:/ProgramData/PLACHEM-Agent-Control/rtx3090.lock\n",
        encoding="utf-8",
    )
    agents_path.write_text(
        "agents:\n"
        "  achilles:\n"
        "    profile: achilles\n"
        "    model_endpoint: http://127.0.0.1:8080/v1\n"
        "    model: E:/AI/models/Qwen3.8-27B-Uncensored-GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf\n"
        "    toolset: todo\n"
        "    hermes_executable: C:/Users/egomine2/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe\n"
        "    hermes_sha256: dc5357dc27045339c7748c96ba1690eeccdd72a903231027a0235a33cdd291c3\n"
        "    nvidia_smi_executable: C:/Windows/System32/nvidia-smi.exe\n"
        "    nvidia_smi_sha256: 9b3da28a74c5bfbf33b147b4b73c105e55b1f74b474e63ec7762843e5f2b635d\n"
        "    model_sha256: 4c5e2db039e9325ac7724c8846c71356a24ad1cdfa28002d73ecb6be645f9675\n",
        encoding="utf-8",
    )

    engine = PolicyEngine.from_config(policy_path, agents_path)

    assert engine.profile == "achilles"
    assert engine.model_endpoint == "http://127.0.0.1:8080/v1"
    assert engine.model.endswith("Qwen3.8-27B-Uncensored-Q4_K_M.gguf")
    assert engine.toolset == "todo"
    assert engine.minimum_free_vram_mib == 512
    with pytest.raises(PolicyViolation, match="custom_deny"):
        engine.evaluate(task())


@pytest.mark.parametrize(
    "policy_text, agents_text",
    [
        (
            "default: allow\nmandatory_denials: [production, merge, deploy, secrets_export]\n"
            "critical_denials: [destructive_delete, permission_change]\n",
            "agents: {}\n",
        ),
        (
            "default: deny\nmandatory_denials: [production]\ncritical_denials: [destructive_delete, permission_change]\n",
            "agents: {}\n",
        ),
        (
            "default: deny\nmandatory_denials: [production, merge, deploy, secrets_export]\n"
            "critical_denials: [destructive_delete]\n",
            "agents: {}\n",
        ),
    ],
)
def test_config_rejects_weakened_global_invariants(tmp_path, policy_text, agents_text):
    policy_path = tmp_path / "policy.yaml"
    agents_path = tmp_path / "agents.yaml"
    policy_path.write_text(policy_text, encoding="utf-8")
    agents_path.write_text(agents_text, encoding="utf-8")
    with pytest.raises(PolicyViolation, match="invalid authoritative configuration"):
        PolicyEngine.from_config(policy_path, agents_path)
