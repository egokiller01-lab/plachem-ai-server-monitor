import json
import os
import tempfile
import unittest
from pathlib import Path


class WarRoomAgentCatalogTests(unittest.TestCase):
    def test_openclaw_93_entries_are_intersected_with_gateway_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "openclaw.json").write_text(json.dumps({
                "agents": {
                    "list": [{"id": "legacy-only"}],
                    "entries": {
                        "ERPmanager": {"workspace": "/ignored"},
                        "RegisteredOnly": {},
                        "bad id;echo pwned": {},
                    },
                },
            }), encoding="utf-8")
            (root / "gateway.json").write_text(json.dumps({
                "erpmanager": {"enabled": True, "capabilities": ["erp-management"]},
                "registeredonly": {"enabled": False, "capabilities": ["test-only"]},
                "gateway-only": {"enabled": True},
            }), encoding="utf-8")
            old = {
                "PLACHEM_OPENCLAW_CONFIG": os.environ.get("PLACHEM_OPENCLAW_CONFIG"),
                "PLACHEM_FAST_GATEWAY_AGENTS": os.environ.get("PLACHEM_FAST_GATEWAY_AGENTS"),
                "PLACHEM_WAR_ROOM_TEST_ADAPTER": os.environ.get("PLACHEM_WAR_ROOM_TEST_ADAPTER"),
            }
            try:
                os.environ["PLACHEM_OPENCLAW_CONFIG"] = str(root / "openclaw.json")
                os.environ["PLACHEM_FAST_GATEWAY_AGENTS"] = str(root / "gateway.json")
                os.environ.pop("PLACHEM_WAR_ROOM_TEST_ADAPTER", None)
                from war_room_agents import load_agent_catalog

                catalog = load_agent_catalog()
                self.assertEqual({"ERPmanager", "RegisteredOnly"}, set(catalog))
                self.assertTrue(catalog["ERPmanager"].execution_eligible)
                self.assertTrue(catalog["RegisteredOnly"].gateway_allowed)
                self.assertFalse(catalog["RegisteredOnly"].execution_eligible)
                self.assertNotIn("legacy-only", catalog)
                self.assertNotIn("gateway-only", catalog)
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class ApprovedPathsContractTests(unittest.TestCase):
    """War Room task contract -> grounding packet approved_paths link.

    The grounding packet is the immutable, server-supplied trust boundary.
    Only absolute paths supplied by the task contract (prepare request)
    become approved_paths; worker responses and arbitrary additions never
    contribute to this list.
    """

    def setUp(self):
        import tempfile
        from war_room_actions import _grounding_packet

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._grounding_packet = _grounding_packet
        self.base_grounding = {
            "worktree": "/home/plachem-sever/codex-workspaces/cha-secretary/plachem-ai-server-monitor",
            "branch": "feature/x",
            "revision": "abc1234",
            "api_base": "http://127.0.0.1:8123",
            "db_label": "war-room-scratch",
            "forbidden": ["production data", "deployment", "commit", "push", "existing work sessions"],
            "required_evidence": ["test", "artifact"],
        }

    def _packet(self, grounding: dict | None) -> dict:
        if grounding is None:
            return self._grounding_packet({"grounding": self.base_grounding}, "p1", "v1")
        return self._grounding_packet({"grounding": grounding}, "p1", "v1")

    def test_contract_approved_paths_are_stored_in_packet(self):
        paths = [
            "/home/plachem-sever/.openclaw/agents/ERPmanager/01_ACTIVE/war-room-approved-paths-link-20260913",
            "/home/plachem-sever/.openclaw/agents/ERPmanager/01_ACTIVE/war-room-approved-paths-link-20260913/05_QA_EVIDENCE",
        ]
        packet = self._packet({**self.base_grounding, "approved_paths": paths})
        self.assertEqual(paths, packet["approved_paths"])

    def test_relative_or_malformed_contract_paths_are_dropped(self):
        packet = self._packet({**self.base_grounding, "approved_paths": [
            "relative/path", "/abs/ok", "", 42, "not-a-string",
        ]})
        self.assertEqual(["/abs/ok"], packet["approved_paths"])

    def test_non_list_approved_paths_become_empty(self):
        packet = self._packet({**self.base_grounding, "approved_paths": "/abs/only"})
        self.assertEqual([], packet["approved_paths"])
        packet = self._packet(None)
        self.assertEqual([], packet["approved_paths"])


if __name__ == "__main__":
    unittest.main()
