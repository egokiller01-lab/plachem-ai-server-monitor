from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import war_room
import war_room_actions
from fastapi import HTTPException


class WindowsCompatibilityTests(unittest.TestCase):
    def test_absolute_paths_are_platform_independent_and_relative_is_rejected(self):
        for path in ("/tmp/war-room", "E:/PLACHEM-Agent-Control/war-room"):
            packet = war_room_actions._grounding_packet({"grounding": {"worktree": path}}, "project", "version")
            self.assertEqual(packet["worktree"], path)
        with self.assertRaises(HTTPException):
            war_room_actions._grounding_packet({"grounding": {"worktree": "relative/worktree"}}, "project", "version")

    def test_rw_connection_context_closes_before_temporary_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "war-room.sqlite3"
            war_room.provision_database(db)
            old_db = os.environ.get("PLACHEM_WAR_ROOM_DB")
            os.environ["PLACHEM_WAR_ROOM_DB"] = str(db)
            try:
                with war_room_actions._connect_rw() as connection:
                    connection.execute("SELECT 1")
                db.unlink()
                self.assertFalse(db.exists())
            finally:
                if old_db is None:
                    os.environ.pop("PLACHEM_WAR_ROOM_DB", None)
                else:
                    os.environ["PLACHEM_WAR_ROOM_DB"] = old_db

    def test_readonly_connection_context_closes_before_temporary_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "war-room.sqlite3"
            war_room.provision_database(db)
            old_db = os.environ.get("PLACHEM_WAR_ROOM_DB")
            os.environ["PLACHEM_WAR_ROOM_DB"] = str(db)
            try:
                with war_room._connect_readonly() as connection:
                    connection.execute("SELECT 1")
                db.unlink()
                self.assertFalse(db.exists())
            finally:
                if old_db is None:
                    os.environ.pop("PLACHEM_WAR_ROOM_DB", None)
                else:
                    os.environ["PLACHEM_WAR_ROOM_DB"] = old_db


if __name__ == "__main__":
    unittest.main()
