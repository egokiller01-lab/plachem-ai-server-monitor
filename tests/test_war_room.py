from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import war_room


class WarRoomReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.previous_home = os.environ.get("OPENCLAW_HOME")
        self.previous_db = os.environ.get("PLACHEM_WAR_ROOM_DB")
        os.environ["OPENCLAW_HOME"] = str(root / "openclaw")
        os.environ["PLACHEM_WAR_ROOM_DB"] = str(root / "war-room.sqlite3")

    def tearDown(self) -> None:
        if self.previous_home is None:
            os.environ.pop("OPENCLAW_HOME", None)
        else:
            os.environ["OPENCLAW_HOME"] = self.previous_home
        if self.previous_db is None:
            os.environ.pop("PLACHEM_WAR_ROOM_DB", None)
        else:
            os.environ["PLACHEM_WAR_ROOM_DB"] = self.previous_db
        self.temp_dir.cleanup()

    def test_baseline_project_and_participants(self) -> None:
        war_room.provision_database()
        projects = war_room.list_projects()
        self.assertEqual("readonly", projects["mode"])
        self.assertEqual(1, len(projects["items"]))
        project_id = projects["items"][0]["id"]
        project = war_room.get_project(project_id)
        self.assertFalse(project["write_actions_enabled"])
        participants = war_room.get_participants(project_id)["items"]
        self.assertEqual(["ERPcoder", "ERPqa", "main"], [row["principal_id"] for row in participants])

    def test_timeline_and_manyfast_baseline(self) -> None:
        war_room.provision_database()
        timeline = war_room.get_timeline(war_room.PROJECT_ID)
        self.assertEqual("decision", timeline["items"][0]["message_type"])
        baseline = war_room.get_manyfast_baseline(war_room.PROJECT_ID)
        self.assertEqual(9, baseline["counts"]["requirements"])
        self.assertEqual(6, baseline["counts"]["wireframe_pages"])
        self.assertEqual(3, len(baseline["known_gaps"]))

    def test_missing_openclaw_sessions_are_isolated(self) -> None:
        war_room.provision_database()
        operations = war_room.get_operations(war_room.PROJECT_ID)
        self.assertEqual(3, len(operations["agents"]))
        self.assertTrue(all(row["state"] == "unmapped" and row["session_count"] == 0 for row in operations["agents"]))

    def test_get_does_not_create_database(self) -> None:
        with self.assertRaises(Exception):
            war_room.list_projects()
        self.assertFalse(Path(os.environ["PLACHEM_WAR_ROOM_DB"]).exists())

    def test_project_scope_and_secret_value_redaction(self) -> None:
        war_room.provision_database()
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        with sqlite3.connect(db) as connection:
            connection.execute("INSERT INTO war_projects VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("other-project", "Other", "active", "other", "v1", 1, 1))
            connection.execute("INSERT INTO war_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("other-message", "other-project", "note", "x", "x", "token=synthetic-secret-value", None, None, 2, None, "clean"))
            connection.commit()
        self.assertEqual([war_room.PROJECT_ID], [row["id"] for row in war_room.list_projects()["items"]])
        timeline = war_room.get_timeline(war_room.PROJECT_ID)
        self.assertNotIn("synthetic-secret-value", json.dumps(timeline))

    def test_same_timestamp_cursor_is_lossless(self) -> None:
        war_room.provision_database()
        db = Path(os.environ["PLACHEM_WAR_ROOM_DB"])
        with sqlite3.connect(db) as connection:
            for message_id in ("tie-a", "tie-b", "tie-c"):
                connection.execute("INSERT INTO war_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (message_id, war_room.PROJECT_ID, "note", "test", "tester", message_id, None, None, 777, None, "clean"))
            connection.commit()
        first = war_room.get_timeline(war_room.PROJECT_ID, limit=2)
        second = war_room.get_timeline(war_room.PROJECT_ID, limit=2, before=first["next_cursor"])
        ids = [item["id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("tie-a", ids)

    def test_corrupt_and_invalid_session_states_are_explicit(self) -> None:
        war_room.provision_database()
        session_dir = Path(os.environ["OPENCLAW_HOME"]) / "agents" / "main" / "sessions"
        session_dir.mkdir(parents=True)
        (session_dir / "sessions.json").write_text("{broken", encoding="utf-8")
        with sqlite3.connect(Path(os.environ["PLACHEM_WAR_ROOM_DB"])) as connection:
            connection.execute("INSERT INTO war_project_sessions VALUES (?, ?, ?, ?, 1)", (war_room.PROJECT_ID, "main", "s", "s"))
            connection.commit()
        corrupt = war_room.get_operations(war_room.PROJECT_ID)
        self.assertEqual("corrupt", next(row for row in corrupt["agents"] if row["agent_id"] == "main")["state"])
        (session_dir / "sessions.json").write_text(json.dumps({"s": {"sessionId": "s", "updatedAt": "bad"}}), encoding="utf-8")
        invalid = war_room.get_operations(war_room.PROJECT_ID)
        self.assertEqual("invalid", next(row for row in invalid["agents"] if row["agent_id"] == "main")["state"])


if __name__ == "__main__":
    unittest.main()
