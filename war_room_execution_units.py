"""War Room Phase 2 execution units persistence.

Provides minimal restart-safe persistence for execution units.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS war_execution_units (
    execution_id TEXT PRIMARY KEY,
    war_project_id TEXT NOT NULL REFERENCES war_projects(id),
    war_task_id TEXT NOT NULL REFERENCES war_tasks(id),
    correlation_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    workflow_role TEXT NOT NULL DEFAULT 'implementation',
    depends_on_execution_ids TEXT NOT NULL DEFAULT '[]',
    core_run_id TEXT,
    dispatch_message TEXT,
    dispatch_timeout_seconds REAL,
    dispatch_goal_contract TEXT,
    dispatch_claimed_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_war_execution_units_project
    ON war_execution_units(war_project_id, war_task_id, correlation_id);
CREATE INDEX IF NOT EXISTS idx_war_execution_units_agent
    ON war_execution_units(agent_id, core_run_id);
"""


class ExecutionUnitStore:
    """Restart-safe persistence for execution units."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=2000")
        return con

    def ensure_schema(self) -> None:
        """Ensure the schema is created."""
        with self._lock, self._connect() as con:
            parent_tables = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"war_projects", "war_tasks"}.issubset(parent_tables):
                raise RuntimeError("EXECUTION_UNIT_PARENT_SCHEMA_REQUIRED")
            con.executescript(_CREATE_TABLE_SQL)
            columns = {row[1] for row in con.execute("PRAGMA table_info(war_execution_units)")}
            migrations = {
                "workflow_role": (
                    "ALTER TABLE war_execution_units ADD COLUMN workflow_role "
                    "TEXT NOT NULL DEFAULT 'implementation'"
                ),
                "dispatch_message": "ALTER TABLE war_execution_units ADD COLUMN dispatch_message TEXT",
                "dispatch_timeout_seconds": "ALTER TABLE war_execution_units ADD COLUMN dispatch_timeout_seconds REAL",
                "dispatch_goal_contract": "ALTER TABLE war_execution_units ADD COLUMN dispatch_goal_contract TEXT",
                "dispatch_claimed_at": "ALTER TABLE war_execution_units ADD COLUMN dispatch_claimed_at INTEGER",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    con.execute(statement)
            task_columns = {row[1] for row in con.execute("PRAGMA table_info(war_tasks)")}
            task_migrations = {
                "workflow_status": "ALTER TABLE war_tasks ADD COLUMN workflow_status TEXT",
                "workflow_finalized_at": "ALTER TABLE war_tasks ADD COLUMN workflow_finalized_at INTEGER",
                "workflow_finalize_count": (
                    "ALTER TABLE war_tasks ADD COLUMN workflow_finalize_count "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in task_migrations.items():
                if column not in task_columns:
                    con.execute(statement)

    def create_unit(
        self,
        *,
        execution_id: str,
        war_project_id: str,
        war_task_id: str,
        correlation_id: str,
        agent_id: str,
        workflow_role: str = "implementation",
        depends_on_execution_ids: list[str] | None = None,
        core_run_id: str | None = None,
        dispatch_message: str | None = None,
        dispatch_timeout_seconds: float | None = None,
        dispatch_goal_contract: dict[str, Any] | None = None,
        created_at: int | None = None,
    ) -> dict[str, Any]:
        """Create a single execution unit.

        Raises:
            ValueError: If execution_id already exists.
        """
        now = int(time.time()) if created_at is None else int(created_at)
        deps_json = json.dumps(depends_on_execution_ids or [], ensure_ascii=False)

        with self._lock, self._connect() as con:
            try:
                con.execute(
                    """INSERT INTO war_execution_units
                       (execution_id, war_project_id, war_task_id, correlation_id,
                        agent_id, workflow_role, depends_on_execution_ids, core_run_id,
                        dispatch_message, dispatch_timeout_seconds, dispatch_goal_contract,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (execution_id, war_project_id, war_task_id, correlation_id,
                     agent_id, workflow_role, deps_json, core_run_id, dispatch_message,
                     dispatch_timeout_seconds,
                     json.dumps(dispatch_goal_contract, ensure_ascii=False) if dispatch_goal_contract is not None else None,
                     now),
                )
            except sqlite3.IntegrityError as exc:
                if self._is_duplicate_error(exc):
                    raise ValueError(f"EXECUTION_UNIT_EXISTS:{execution_id}") from exc
                raise

            return {
                "execution_id": execution_id,
                "war_project_id": war_project_id,
                "war_task_id": war_task_id,
                "correlation_id": correlation_id,
                "agent_id": agent_id,
                "workflow_role": workflow_role,
                "depends_on_execution_ids": depends_on_execution_ids or [],
                "core_run_id": core_run_id,
                "dispatch_message": dispatch_message,
                "dispatch_timeout_seconds": dispatch_timeout_seconds,
                "dispatch_goal_contract": dispatch_goal_contract,
                "dispatch_claimed_at": None,
                "created_at": now,
            }

    def create_units(self, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create multiple execution units atomically.

        Raises:
            ValueError: If any execution_id already exists.
        """
        if not units:
            return []
        with self._lock, self._connect() as con:
            try:
                for unit in units:
                    deps_json = json.dumps(unit.get("depends_on_execution_ids") or [], ensure_ascii=False)
                    con.execute(
                        """INSERT INTO war_execution_units
                           (execution_id, war_project_id, war_task_id, correlation_id,
                            agent_id, workflow_role, depends_on_execution_ids, core_run_id,
                            dispatch_message, dispatch_timeout_seconds, dispatch_goal_contract,
                            created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (unit["execution_id"], unit["war_project_id"], unit["war_task_id"],
                         unit["correlation_id"], unit["agent_id"],
                         unit.get("workflow_role", "implementation"), deps_json,
                         unit.get("core_run_id"), unit.get("dispatch_message"),
                         unit.get("dispatch_timeout_seconds"),
                         json.dumps(unit.get("dispatch_goal_contract"), ensure_ascii=False)
                         if unit.get("dispatch_goal_contract") is not None else None,
                         int(unit["created_at"])),
                    )
            except sqlite3.IntegrityError as exc:
                if self._is_duplicate_error(exc):
                    raise ValueError("EXECUTION_UNIT_EXISTS") from exc
                raise
            return units

    @staticmethod
    def _is_duplicate_error(exc: sqlite3.IntegrityError) -> bool:
        message = str(exc).lower()
        return "unique constraint failed: war_execution_units.execution_id" in message or "primary key" in message

    def get_unit(self, execution_id: str) -> dict[str, Any] | None:
        """Get a single execution unit by ID."""
        with self._lock, self._connect() as con:
            row = con.execute(
                """SELECT execution_id, war_project_id, war_task_id, correlation_id,
                          agent_id, workflow_role, depends_on_execution_ids, core_run_id,
                          dispatch_message, dispatch_timeout_seconds, dispatch_goal_contract,
                          dispatch_claimed_at, created_at
                   FROM war_execution_units WHERE execution_id=?""",
                (execution_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_unit(row)

    def get_unit_by_core_run_id(self, core_run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM war_execution_units WHERE core_run_id=?", (core_run_id,)).fetchone()
        return self._row_to_unit(row) if row is not None else None

    def claim_for_dispatch(self, execution_id: str, *, claimed_at: int | None = None) -> dict[str, Any] | None:
        """Atomically claim an undispatched unit; returns None for a lost race."""
        now = int(time.time()) if claimed_at is None else int(claimed_at)
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE war_execution_units SET dispatch_claimed_at=? "
                "WHERE execution_id=? AND core_run_id IS NULL AND dispatch_claimed_at IS NULL",
                (now, execution_id),
            )
            if cur.rowcount != 1:
                return None
            row = con.execute("SELECT * FROM war_execution_units WHERE execution_id=?", (execution_id,)).fetchone()
        return self._row_to_unit(row)

    def update_dispatch_input(self, execution_id: str, *, message: str,
                              timeout_seconds: float,
                              goal_contract: dict[str, Any] | None = None) -> None:
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE war_execution_units SET dispatch_message=?, dispatch_timeout_seconds=?, dispatch_goal_contract=? "
                "WHERE execution_id=?",
                (message, float(timeout_seconds), json.dumps(goal_contract, ensure_ascii=False)
                 if goal_contract is not None else None, execution_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"UNKNOWN_EXECUTION_UNIT:{execution_id}")

    def get_units_by_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        """Get all execution units for a correlation."""
        with self._lock, self._connect() as con:
            rows = con.execute(
                """SELECT execution_id, war_project_id, war_task_id, correlation_id,
                          agent_id, workflow_role, depends_on_execution_ids, core_run_id,
                          dispatch_message, dispatch_timeout_seconds, dispatch_goal_contract,
                          dispatch_claimed_at, created_at
                   FROM war_execution_units WHERE correlation_id=?
                   ORDER BY created_at ASC, execution_id ASC""",
                (correlation_id,),
            ).fetchall()
        return [self._row_to_unit(row) for row in rows]

    def get_units_by_workflow(
        self, war_project_id: str, war_task_id: str, correlation_id: str
    ) -> list[dict[str, Any]]:
        """Get units scoped to the exact persisted workflow identity."""
        with self._lock, self._connect() as con:
            rows = con.execute(
                """SELECT execution_id, war_project_id, war_task_id, correlation_id,
                          agent_id, workflow_role, depends_on_execution_ids, core_run_id,
                          dispatch_message, dispatch_timeout_seconds, dispatch_goal_contract,
                          dispatch_claimed_at, created_at
                   FROM war_execution_units
                   WHERE war_project_id=? AND war_task_id=? AND correlation_id=?
                   ORDER BY created_at ASC, execution_id ASC""",
                (war_project_id, war_task_id, correlation_id),
            ).fetchall()
        return [self._row_to_unit(row) for row in rows]

    def get_units_by_task(self, war_project_id: str, war_task_id: str) -> list[dict[str, Any]]:
        """Get all execution units for a task."""
        with self._lock, self._connect() as con:
            rows = con.execute(
                """SELECT execution_id, war_project_id, war_task_id, correlation_id,
                          agent_id, workflow_role, depends_on_execution_ids, core_run_id,
                          dispatch_message, dispatch_timeout_seconds, dispatch_goal_contract,
                          dispatch_claimed_at, created_at
                   FROM war_execution_units WHERE war_project_id=? AND war_task_id=?
                   ORDER BY created_at ASC, execution_id ASC""",
                (war_project_id, war_task_id),
            ).fetchall()
        return [self._row_to_unit(row) for row in rows]

    def update_core_run_id(self, execution_id: str, core_run_id: str) -> None:
        """Update the core_run_id for an execution unit."""
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE war_execution_units SET core_run_id=? WHERE execution_id=?",
                (core_run_id, execution_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"UNKNOWN_EXECUTION_UNIT:{execution_id}")

    def get_workflow_projection(self, war_task_id: str) -> dict[str, Any] | None:
        """Read the durable terminal projection stored on the existing parent."""
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT workflow_status, workflow_finalized_at, workflow_finalize_count "
                "FROM war_tasks WHERE id=?",
                (war_task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row["workflow_status"],
            "finalized_at": row["workflow_finalized_at"],
            "finalize_count": int(row["workflow_finalize_count"] or 0),
        }

    def finalize_workflow_projection(
        self, war_task_id: str, status: str, *, finalized_at: int | None = None
    ) -> bool:
        """Atomically finalize the existing parent once; return whether won."""
        if status not in {"COMPLETED", "FAILED", "BLOCKED"}:
            raise ValueError(f"WORKFLOW_STATUS_NOT_TERMINAL:{status}")
        now = int(time.time()) if finalized_at is None else int(finalized_at)
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE war_tasks SET workflow_status=?, workflow_finalized_at=?, "
                "workflow_finalize_count=workflow_finalize_count+1 "
                "WHERE id=? AND workflow_finalized_at IS NULL",
                (status, now, war_task_id),
            )
            return cur.rowcount == 1

    def _row_to_unit(self, row: sqlite3.Row) -> dict[str, Any]:
        deps_raw = row["depends_on_execution_ids"]
        deps = json.loads(deps_raw) if deps_raw else []
        return {
            "execution_id": row["execution_id"],
            "war_project_id": row["war_project_id"],
            "war_task_id": row["war_task_id"],
            "correlation_id": row["correlation_id"],
            "agent_id": row["agent_id"],
            "workflow_role": row["workflow_role"],
            "depends_on_execution_ids": deps,
            "core_run_id": row["core_run_id"],
            "dispatch_message": row["dispatch_message"],
            "dispatch_timeout_seconds": row["dispatch_timeout_seconds"],
            "dispatch_goal_contract": json.loads(row["dispatch_goal_contract"]) if row["dispatch_goal_contract"] else None,
            "dispatch_claimed_at": row["dispatch_claimed_at"],
            "created_at": row["created_at"],
        }
