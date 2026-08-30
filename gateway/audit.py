from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from gateway.path_security import reject_reparse_points

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_KEYS = {"timestamp", "task_id", "status", "task_sha256", "result_sha256"}
_CHAIN_KEYS = _LEGACY_KEYS | {"previous_sha256", "event_sha256"}
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("audit integrity check failed")
        value[key] = item
    return value


def _event_digest(event: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
    return _digest(unsigned)


def _validate_event(event: Any, *, chained: bool) -> dict[str, Any]:
    keys = _CHAIN_KEYS if chained else _LEGACY_KEYS
    if not isinstance(event, dict) or set(event) != keys:
        raise ValueError("audit integrity check failed")
    if not all(isinstance(event[key], str) and event[key] for key in ("timestamp", "task_id", "status")):
        raise ValueError("audit integrity check failed")
    try:
        timestamp = datetime.fromisoformat(event["timestamp"])
    except ValueError as exc:
        raise ValueError("audit integrity check failed") from exc
    if timestamp.tzinfo is None:
        raise ValueError("audit integrity check failed")
    hash_keys = ["task_sha256", "result_sha256"]
    if chained:
        hash_keys += ["previous_sha256", "event_sha256"]
    if not all(isinstance(event[key], str) and _HEX64.fullmatch(event[key]) for key in hash_keys):
        raise ValueError("audit integrity check failed")
    return event


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        reject_reparse_points(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_points(self.path.parent)
        reject_reparse_points(self.path)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        reject_reparse_points(lock_path)
        key = str(lock_path.absolute()).casefold()
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
        with thread_lock:
            with lock_path.open("a+b") as handle:
                reject_reparse_points(lock_path)
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _atomic_write(self, path: Path, text: str, *, exclusive: bool = False) -> None:
        reject_reparse_points(path.parent)
        reject_reparse_points(path)
        if exclusive and path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise ValueError("audit integrity check failed")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive and path.exists():
                if path.read_text(encoding="utf-8") != text:
                    raise ValueError("audit integrity check failed")
                temporary.unlink()
            else:
                reject_reparse_points(path.parent)
                reject_reparse_points(path)
                os.replace(temporary, path)
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            temporary.unlink(missing_ok=True)

    def prepare(self) -> None:
        with self._locked():
            self._migrate_legacy_if_needed()
            self._verify_unlocked()

    def _read_events(self) -> list[Any]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
            if text and not text.endswith("\n"):
                raise ValueError("audit integrity check failed")
            lines = text[:-1].split("\n") if text else []
            if any(not line for line in lines):
                raise ValueError("audit integrity check failed")
            events = []
            for line in lines:
                event = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                if line != _canonical_json(event):
                    raise ValueError("audit integrity check failed")
                events.append(event)
            return events
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("audit integrity check failed") from exc

    def _migrate_legacy_if_needed(self) -> None:
        if not self.path.exists():
            return
        original = self.path.read_text(encoding="utf-8")
        events = self._read_events()
        if not events:
            return
        legacy_flags = [isinstance(event, dict) and not ({"previous_sha256", "event_sha256"} & set(event)) for event in events]
        if not any(legacy_flags):
            return
        if not all(legacy_flags):
            raise ValueError("audit integrity check failed")
        validated = [_validate_event(event, chained=False) for event in events]
        backup = self.path.with_suffix(self.path.suffix + ".legacy")
        self._atomic_write(backup, original, exclusive=True)
        previous = "0" * 64
        migrated: list[str] = []
        for event in validated:
            chained = dict(event, previous_sha256=previous)
            chained["event_sha256"] = _event_digest(chained)
            previous = chained["event_sha256"]
            migrated.append(_canonical_json(chained))
        self._atomic_write(self.path, "\n".join(migrated) + "\n")

    def append(self, task_id: str, status: str, task_payload: dict[str, Any], result_payload: dict[str, Any]) -> None:
        with self._locked():
            self._migrate_legacy_if_needed()
            self._verify_unlocked()
            events = self._read_events()
            previous = events[-1]["event_sha256"] if events else "0" * 64
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "task_id": task_id,
                "status": status,
                "task_sha256": _digest(task_payload),
                "result_sha256": _digest(result_payload),
                "previous_sha256": previous,
            }
            event["event_sha256"] = _event_digest(event)
            lines = [_canonical_json(item) for item in events]
            lines.append(_canonical_json(event))
            self._atomic_write(self.path, "\n".join(lines) + "\n")
            self._verify_unlocked()

    def _verify_unlocked(self) -> bool:
        previous = "0" * 64
        for raw in self._read_events():
            event = _validate_event(raw, chained=True)
            if event["previous_sha256"] != previous or event["event_sha256"] != _event_digest(event):
                raise ValueError("audit integrity check failed")
            previous = event["event_sha256"]
        return True

    def verify(self) -> bool:
        with self._locked():
            return self._verify_unlocked()
