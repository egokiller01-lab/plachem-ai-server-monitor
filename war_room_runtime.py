"""Long-lived War Room worker runtime.

The same adapter instance owns delivery and stop calls. A replacement runtime
may poll durable run results, but it cannot inherit the prior Gateway
connection's abort ownership.
"""
from __future__ import annotations

import threading
import time
import sqlite3
from pathlib import Path
from typing import Any

from war_room_adapter import OpenClawSessionAdapter, SessionAdapter
from war_room_worker import process_due_deliveries, recover_received_deliveries, request_project_stop
import war_room


def provision_disposable_sessions(*, db_path: str | Path, adapter: Any, project_id: str, agent_ids: list[str]) -> list[dict[str, Any]]:
    """Provision and bind explicit test-only sessions without touching work sessions."""
    if (not agent_ids or len(agent_ids) != len(set(agent_ids))
            or any(agent not in war_room.ALLOWED_AGENT_IDS or agent == "main" for agent in agent_ids)):
        raise ValueError("unique non-main allowlisted agent_ids required")
    with sqlite3.connect(Path(db_path)) as con:
        placeholders = ",".join("?" for _ in agent_ids)
        if not con.execute("SELECT 1 FROM war_projects WHERE id=?", (project_id,)).fetchone():
            raise ValueError("project not found")
        participants = {row[0] for row in con.execute(f"SELECT principal_id FROM war_participants WHERE project_id=? AND active=1 AND principal_id IN ({placeholders})", (project_id, *agent_ids))}
        if participants != set(agent_ids):
            raise ValueError("all agents must be active project participants")
        unsafe = con.execute(f"SELECT 1 FROM war_project_sessions WHERE project_id=? AND enabled=1 AND agent_id IN ({placeholders}) AND (purpose!='test' OR disposable!=1) LIMIT 1", (project_id, *agent_ids)).fetchone()
        if unsafe:
            raise RuntimeError("existing work session binding must not be replaced")
    created = []
    for agent_id in agent_ids:
        binding = adapter.create_disposable_session(agent_id=agent_id, project_id=project_id)
        if binding.get("purpose") != "test" or binding.get("disposable") is not True:
            raise RuntimeError("adapter returned unsafe session binding")
        created.append({"agent_id": agent_id, **binding})
    with sqlite3.connect(Path(db_path)) as con:
        con.execute("BEGIN IMMEDIATE")
        for binding in created:
            con.execute("UPDATE war_project_sessions SET enabled=0 WHERE project_id=? AND agent_id=? AND purpose='test' AND disposable=1", (project_id, binding["agent_id"]))
            con.execute("INSERT INTO war_project_sessions(project_id,agent_id,session_key,session_id,enabled,purpose,disposable) VALUES (?,?,?,?,1,'test',1)", (project_id, binding["agent_id"], binding["session_key"], binding["session_id"]))
        con.commit()
    return created


class WarRoomRuntime:
    def __init__(self, adapter: SessionAdapter | None = None) -> None:
        self.adapter = adapter or OpenClawSessionAdapter()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, *, db_path: str | Path, now: int | None = None) -> list[dict[str, Any]]:
        delivered = process_due_deliveries(db_path=db_path, adapter=self.adapter, now=now)
        recovered = recover_received_deliveries(db_path=db_path, gateway=self.adapter, now=now)
        return delivered + recovered

    def stop_project(self, *, db_path: str | Path, project_id: str, actor_id: str, now: int | None = None) -> dict[str, Any]:
        return request_project_stop(db_path=db_path, project_id=project_id, actor_id=actor_id, adapter=self.adapter, now=now)

    def start(self, *, db_path: str | Path, interval_seconds: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.tick(db_path=db_path)
                except Exception:
                    # Individual delivery failures are persisted by the worker;
                    # an unexpected loop error must not kill the service owner.
                    continue
        self._thread = threading.Thread(target=loop, name="war-room-runtime", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        closer = getattr(self.adapter, "close", None)
        if closer:
            closer()


_RUNTIME: WarRoomRuntime | None = None


def get_runtime() -> WarRoomRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = WarRoomRuntime()
    return _RUNTIME
