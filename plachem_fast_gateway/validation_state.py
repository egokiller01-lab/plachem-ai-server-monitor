"""Immutable server-side inputs for restart-safe result verification.

No permissions are inferred from worker output. Replay never submits an agent.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


class ValidationStateStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._memory: dict[tuple[str, str], tuple[str, str]] = {}
        self._lock = threading.RLock()
        if self.path is not None:
            with sqlite3.connect(self.path) as con:
                con.execute("CREATE TABLE IF NOT EXISTS gateway_validation_state (core_run_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(core_run_id,kind))")

    def put(self, run_id: str, kind: str, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self._lock:
            if self.path is None:
                previous = self._memory.setdefault((run_id, kind), (raw, digest))
            else:
                with sqlite3.connect(self.path) as con:
                    con.execute("INSERT OR IGNORE INTO gateway_validation_state VALUES (?,?,?,?)", (run_id, kind, raw, digest))
                    previous = con.execute("SELECT payload,sha256 FROM gateway_validation_state WHERE core_run_id=? AND kind=?", (run_id, kind)).fetchone()
            if previous != (raw, digest):
                raise ValueError("VALIDATION_STATE_CONFLICT")

    def get(self, run_id: str, kind: str) -> dict[str, Any] | None:
        with self._lock:
            if self.path is None:
                row = self._memory.get((run_id, kind))
            else:
                with sqlite3.connect(self.path) as con:
                    row = con.execute("SELECT payload,sha256 FROM gateway_validation_state WHERE core_run_id=? AND kind=?", (run_id, kind)).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("VALIDATION_STATE_TAMPERED")
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValueError("VALIDATION_STATE_INVALID")
        return deepcopy(value)
