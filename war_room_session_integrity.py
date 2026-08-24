"""Exact, private-safe snapshot helpers for existing business sessions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def snapshot(stores: dict[str, Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for agent, root in stores.items():
        index = json.loads((root / "sessions.json").read_text(encoding="utf-8"))
        for key, meta in index.items():
            if "war-room-test" in key:
                continue
            path = root / f"{meta.get('sessionId')}.jsonl"
            raw = path.read_bytes() if path.exists() else b""
            stat = path.stat() if path.exists() else None
            rows.append({
                "agent": agent, "session_key": key, "session_id": meta.get("sessionId"),
                "transcript_path": str(path), "exists": path.exists(),
                "message_count": raw.count(b"\n"), "size": len(raw),
                "mtime_ns": str(stat.st_mtime_ns) if stat else None,
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
    rows.sort(key=lambda row: (row["agent"], row["session_key"]))
    return {"schema": 2, "mtime_encoding": "decimal_string", "sessions": rows}


def compare(pre: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    if pre.get("mtime_encoding") != "decimal_string" or post.get("mtime_encoding") != "decimal_string":
        return {"pre_count": len(pre.get("sessions", [])), "post_count": len(post.get("sessions", [])), "changed_count": 0, "deleted_count": 0, "uncertain_count": 1, "changes": []}
    before = {(row["agent"], row["session_key"]): row for row in pre["sessions"]}
    after = {(row["agent"], row["session_key"]): row for row in post["sessions"]}
    changes = []
    fields = ("exists", "message_count", "size", "mtime_ns", "sha256")
    for key, old in before.items():
        new = after.get(key)
        changed = fields if new is None else tuple(field for field in fields if old.get(field) != new.get(field))
        if changed:
            changes.append({"agent": old["agent"], "session_key": old["session_key"], "fields": list(changed)})
    return {"pre_count": len(before), "post_count": len(after), "changed_count": len(changes), "deleted_count": sum(1 for key, old in before.items() if old.get("exists") and (key not in after or not after[key].get("exists"))), "uncertain_count": 0, "changes": changes}


def write_private_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
