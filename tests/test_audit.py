import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gateway.audit import AuditLog


def test_audit_log_appends_hashes_without_raw_payload(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.append("task-1", "completed", {"objective": "inspect"}, {"summary": "done"})

    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["task_id"] == "task-1"
    assert len(event["task_sha256"]) == 64
    assert len(event["result_sha256"]) == 64
    assert "objective" not in event
    assert "summary" not in event


def test_audit_log_is_hash_chained_and_detects_tampering(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.append("task-1", "started", {"task_id": "task-1"}, {})
    audit.append("task-1", "completed", {"task_id": "task-1"}, {"summary": "done"})

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["previous_sha256"] == "0" * 64
    assert events[1]["previous_sha256"] == events[0]["event_sha256"]
    assert audit.verify() is True

    events[0]["status"] = "forged"
    path.chmod(0o600)
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="audit integrity"):
        audit.verify()


def test_append_migrates_fully_legacy_log_without_losing_records(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    legacy_events = [
        {
            "timestamp": "2026-08-30T00:00:00+00:00",
            "task_id": "legacy-1",
            "status": "verified",
            "task_sha256": "1" * 64,
            "result_sha256": "2" * 64,
        },
        {
            "timestamp": "2026-08-30T00:01:00+00:00",
            "task_id": "legacy-2",
            "status": "verified",
            "task_sha256": "3" * 64,
            "result_sha256": "4" * 64,
        },
    ]
    original = "\n".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) for event in legacy_events
    ) + "\n"
    path.write_text(original, encoding="utf-8")

    audit = AuditLog(path)
    audit.append("task-3", "verified", {"task_id": "task-3"}, {"status": "completed"})

    backup = path.with_suffix(path.suffix + ".legacy")
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert backup.read_text(encoding="utf-8") == original
    assert len(events) == 3
    assert events[0]["previous_sha256"] == "0" * 64
    assert events[1]["previous_sha256"] == events[0]["event_sha256"]
    assert events[2]["previous_sha256"] == events[1]["event_sha256"]
    assert audit.verify() is True


def test_audit_rejects_reparse_point_parent(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="reparse"):
        AuditLog(link / "audit.jsonl").append("task", "failed", {}, {})
    assert list(outside.iterdir()) == []


def test_legacy_migration_rejects_invalid_schema_and_hashes(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({
        "timestamp": "not-a-time", "task_id": "legacy", "status": "verified",
        "task_sha256": "short", "result_sha256": "2" * 64,
        "unexpected": True,
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="audit integrity"):
        AuditLog(path).prepare()
    assert not path.with_suffix(".jsonl.legacy").exists()


def test_concurrent_appends_preserve_a_valid_chain(tmp_path: Path):
    path = tmp_path / "audit.jsonl"

    def append(index: int):
        AuditLog(path).append(f"task-{index}", "verified", {"i": index}, {"ok": True})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(32)))

    events = path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 32
    assert AuditLog(path).verify() is True


@pytest.mark.parametrize(
    "text",
    [
        "\n",
        '{"timestamp":"x","timestamp":"y"}\n',
        '{"z":1, "a":2}\n',
        '{}',
    ],
)
def test_audit_rejects_blank_duplicate_malformed_or_noncanonical_jsonl(tmp_path: Path, text: str):
    path = tmp_path / "audit.jsonl"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="audit integrity"):
        AuditLog(path).prepare()


def test_audit_writer_emits_only_canonical_jsonl(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.append("canonical-1", "verified", {}, {})
    event = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    assert path.read_text(encoding="utf-8") == canonical

    path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="audit integrity"):
        audit.verify()
