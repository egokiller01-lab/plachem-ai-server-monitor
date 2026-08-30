import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
MODULE_SPEC = importlib.util.spec_from_file_location("delegation_gateway_v1", ROOT / "gateway.py")
gateway = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
MODULE_SPEC.loader.exec_module(gateway)


def policy():
    return json.loads((ROOT / "policy.yaml").read_text(encoding="utf-8"))


def request(**overrides):
    value = {
        "task_id": "policy-test-001",
        "objective": "Create one bounded artifact",
        "scope": {"include": ["demo/out.txt"], "exclude": ["all other files"]},
        "permission_package": ["artifact.create", "artifact.write"],
        "max_steps": 3,
        "max_retries": 1,
        "max_files": 1,
        "timeout_seconds": 30,
        "completion": ["Create the requested artifact"],
    }
    value.update(overrides)
    return value


def generic_result(content="ALPHA"):
    return {
        "task_id": "policy-test-001",
        "status": "completed",
        "steps_used": 1,
        "summary": "Created artifact.",
        "artifacts": [{"path": "demo/out.txt", "content": content}],
        "files_created": ["demo/out.txt"],
        "files_modified": [],
        "functional_test": {"artifact": "PASS"},
        "scope_violation": "NO",
        "permission_violation": "NO",
        "repeated_failure": "NO",
        "remaining_issue": "None",
        "gateway_used": "YES",
        "direct_delegation_attempted": "NO",
        "policy_applied": "PASS",
        "final_result": "PASS",
    }


def test_result_parser_accepts_exactly_one_bounded_json_object():
    encoded = json.dumps(generic_result())
    assert gateway.parse_result_text(encoded, max_bytes=10000) == generic_result()


@pytest.mark.parametrize("text", [
    json.dumps(generic_result()) + " trailing",
    '{"task_id":"first","task_id":"second"}',
])
def test_result_parser_rejects_trailing_text_and_duplicate_keys(text):
    with pytest.raises(ValueError):
        gateway.parse_result_text(text, max_bytes=10000)


def test_result_parser_rejects_oversized_output():
    with pytest.raises(ValueError, match="byte limit"):
        gateway.parse_result_text(json.dumps(generic_result()), max_bytes=10)


def test_verifier_rejects_missing_and_extra_result_fields():
    compiled = gateway.build_taskspec(
        request(validation={"required_text": ["ALPHA"], "forbidden_text": []}),
        policy(),
    )
    missing = generic_result()
    missing.pop("gateway_used")
    extra = generic_result()
    extra["unexpected"] = True

    assert "result_fields" in gateway.verify(compiled, missing)[1]
    assert "result_fields" in gateway.verify(compiled, extra)[1]


def test_verifier_uses_taskspec_validation_instead_of_dashboard_literals():
    compiled = gateway.build_taskspec(
        request(validation={"required_text": ["ALPHA"], "forbidden_text": ["FORBIDDEN"]}),
        policy(),
    )

    passed, failures = gateway.verify(compiled, generic_result())

    assert passed is True
    assert failures == []


def test_verifier_rejects_taskspec_forbidden_text():
    compiled = gateway.build_taskspec(
        request(validation={"required_text": ["ALPHA"], "forbidden_text": ["FORBIDDEN"]}),
        policy(),
    )

    passed, failures = gateway.verify(compiled, generic_result("ALPHA FORBIDDEN"))

    assert passed is False
    assert "forbidden_text" in failures


def test_gateway_retries_inside_one_run_and_records_attempts():
    responses = iter(["not-json", json.dumps(generic_result())])
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return gateway.parse_result_text(next(responses), max_bytes=10000)

    result, attempts = gateway.execute_with_retries(operation, max_retries=2)

    assert result == generic_result()
    assert calls == 2
    assert [item["status"] for item in attempts] == ["FAIL", "PASS"]
    assert attempts[0]["error_code"] == "INVALID_JSON"
    assert attempts[0]["retry"] is True
    assert attempts[0]["started_at"]
    assert attempts[0]["ended_at"]
    assert attempts[1]["final_success"] is True


def test_gateway_stops_after_two_identical_failures():
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return gateway.parse_result_text("not-json", max_bytes=10000)

    with pytest.raises(gateway.RetryExhausted) as caught:
        gateway.execute_with_retries(operation, max_retries=5)

    assert calls == 2
    assert caught.value.stop_reason == "REPEATED_FAILURE"
    assert [item["status"] for item in caught.value.attempts] == ["FAIL", "FAIL"]
    assert caught.value.attempts[-1]["retry"] is False


def test_scope_violation_result_never_reaches_artifact_apply():
    compiled = gateway.build_taskspec(
        request(
            task_id="runtime-scope-009",
            scope={"include": ["runtime-test/safe.txt"], "exclude": ["all other paths"]},
            max_files=1,
            permission_package=["artifact.create", "artifact.write"],
        ),
        policy(),
    )
    result = generic_result()
    result.update({
        "task_id": "runtime-scope-009",
        "artifacts": [
            {"path": "runtime-test/safe.txt", "content": "safe"},
            {"path": "runtime-test/extra.txt", "content": "outside scope"},
        ],
        "files_created": ["runtime-test/safe.txt", "runtime-test/extra.txt"],
    })
    apply_calls = 0

    def forbidden_apply(spec, result):
        nonlocal apply_calls
        apply_calls += 1
        return ["should-not-run"]

    passed, failures, written = gateway.validate_and_apply(
        compiled, result, apply_fn=forbidden_apply
    )

    assert passed is False
    assert "artifact_scope" in failures
    assert apply_calls == 0
    assert written == []


def test_apply_failure_rolls_back_all_original_hashes(tmp_path):
    runtime_test = tmp_path / "runtime-test"
    runtime_test.mkdir()
    originals = {
        "runtime-test/file1.txt": b"original-one",
        "runtime-test/file2.txt": b"original-two",
        "runtime-test/file3.txt": b"original-three",
    }
    for relative, content in originals.items():
        (tmp_path / relative).write_bytes(content)
    before = {
        relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        for relative in originals
    }
    compiled = gateway.build_taskspec(
        request(
            task_id="runtime-rollback-010",
            scope={"include": list(originals), "exclude": ["all other paths"]},
            max_files=3,
            permission_package=["artifact.read", "artifact.write"],
        ),
        policy(),
    )
    result = generic_result()
    result.update({
        "task_id": "runtime-rollback-010",
        "artifacts": [
            {"path": relative, "content": f"new-{number}"}
            for number, relative in enumerate(originals, 1)
        ],
        "files_created": [],
        "files_modified": list(originals),
    })
    replace_calls = 0

    def fail_second_replace(source, target):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected apply failure at file 2")
        os.replace(source, target)

    with pytest.raises(gateway.ArtifactApplyError) as caught:
        gateway.apply_artifacts(
            compiled,
            result,
            project_root=tmp_path,
            replace_file=fail_second_replace,
        )

    after = {
        relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        for relative in originals
    }
    assert caught.value.rollback_started is True
    assert caught.value.rollback_success is True
    assert after == before
    assert not list(tmp_path.rglob("*.gateway-tmp"))


def test_policy_accepts_registered_scoped_permissions():
    compiled = gateway.build_taskspec(
        request(permission_package=["delegation-demo.create", "delegation-demo.write"]),
        policy(),
    )
    assert compiled["permission_package"] == [
        "delegation-demo.create", "delegation-demo.write"
    ]


def test_policy_rejects_scope_larger_than_max_files_before_worker():
    includes = [f"delegation-demo/test{number}.txt" for number in range(1, 6)]
    with pytest.raises(ValueError, match="scope exceeds max_files"):
        gateway.build_taskspec(
            request(
                scope={"include": includes, "exclude": []},
                permission_package=["delegation-demo.create", "delegation-demo.write"],
                max_files=4,
            ),
            policy(),
        )


def test_policy_rejects_path_traversal_requested_in_objective_before_worker():
    with pytest.raises(ValueError, match="path traversal requested outside scope"):
        gateway.build_taskspec(
            request(
                objective="Create safe.txt and ../outside.txt as backup",
                scope={"include": ["delegation-demo/safe.txt"], "exclude": ["all other paths"]},
                permission_package=["delegation-demo.create", "delegation-demo.write"],
                completion=["Create safe.txt", "Create backup outside delegation-demo"],
            ),
            policy(),
        )


def test_policy_rejects_limits_above_configured_maximum():
    with pytest.raises(ValueError, match="max_steps exceeds policy maximum"):
        gateway.build_taskspec(request(max_steps=999), policy())
    with pytest.raises(ValueError, match="timeout_seconds exceeds policy maximum"):
        gateway.build_taskspec(request(timeout_seconds=99999), policy())


def test_policy_rejects_unknown_permission():
    with pytest.raises(ValueError, match="permission is not allowed"):
        gateway.build_taskspec(
            request(permission_package=["artifact.create", "unknown.capability"]),
            policy(),
        )


def test_policy_rejects_permission_that_conflicts_with_deny():
    with pytest.raises(ValueError, match="permission conflicts with deny"):
        gateway.build_taskspec(
            request(
                permission_package=["artifact.create", "git.push"],
                deny=["git_push"],
            ),
            policy(),
        )
