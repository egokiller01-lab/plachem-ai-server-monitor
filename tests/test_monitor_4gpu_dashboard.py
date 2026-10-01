"""4-GPU AI Server dashboard contract tests (2+4 plan, placeholders, stale, UI anchors)."""
import importlib
import os
import re
import sys
import time
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

app = importlib.import_module("app")


def _detected(index, **overrides):
    gpu = {
        "index": index,
        "uuid": f"GPU-{index}",
        "name": "NVIDIA CMP 170HX" if index < 2 else "NVIDIA GeForce RTX 3090",
        "usage_percent": 42.0,
        "vram_used_gb": 20.0,
        "vram_total_gb": 24.0,
        "vram_usage_percent": 83.3,
        "vram_headroom_gb": 4.0,
        "vram_headroom_state": "ok",
        "temperature_c": 61.0,
        "power_draw_w": 210.0,
        "power_limit_w": 320.0,
        "fan_speed_percent": 55.0,
        "pci_bus_id": f"0000:0{index}:00.0",
        "pcie_gen_current": 4,
        "pcie_gen_max": 4,
        "pcie_width_current": 16,
        "pcie_width_max": 16,
        "status": "ok",
        "source": "nvidia-smi",
    }
    gpu.update(overrides)
    return gpu


PLACEHOLDER_TELEMETRY_KEYS = [
    "usage_percent",
    "vram_used_gb",
    "vram_total_gb",
    "vram_usage_percent",
    "temperature_c",
    "power_draw_w",
    "power_limit_w",
    "fan_speed_percent",
    "pci_bus_id",
    "pcie_gen_current",
    "pcie_gen_max",
    "pcie_width_current",
    "pcie_width_max",
]


class DashboardServerPlanTests(unittest.TestCase):
    def test_two_plus_four_slot_structure(self):
        plan = app.build_dashboard_servers([_detected(0), _detected(1)], now=1_700_000_000.0)
        servers = {item["id"]: item for item in plan["servers"]}
        self.assertEqual(set(servers), {"main", "ai"})
        self.assertEqual(servers["main"]["planned_slots"], 2)
        self.assertEqual(servers["main"]["model"], "CMP170HX")
        self.assertEqual(servers["ai"]["planned_slots"], 4)
        self.assertEqual(servers["ai"]["model"], "RTX 3090")
        self.assertEqual(len(servers["main"]["slots"]), 2)
        self.assertEqual(len(servers["ai"]["slots"]), 4)
        # Main detects both, AI detects none with only 2 GPUs total.
        self.assertEqual(servers["main"]["detected_slots"], 2)
        self.assertEqual(servers["ai"]["detected_slots"], 0)
        self.assertEqual(servers["ai"]["missing_slots"], 4)
        for slot in servers["main"]["slots"]:
            self.assertTrue(slot["detected"])
            self.assertIsNone(slot.get("state"))

    def test_three_detected_leaves_ai_slot_four_offline_install_pending(self):
        plan = app.build_dashboard_servers(
            [_detected(0), _detected(1), _detected(2)], now=1_700_000_000.0
        )
        servers = {item["id"]: item for item in plan["servers"]}
        ai = servers["ai"]
        self.assertEqual(ai["detected_slots"], 1)
        self.assertEqual(ai["missing_slots"], 3)
        detected = [s for s in ai["slots"] if s["detected"]]
        placeholders = [s for s in ai["slots"] if not s["detected"]]
        self.assertEqual([s["slot"] for s in detected], [1])
        self.assertEqual([s["slot"] for s in placeholders], [2, 3, 4])
        fourth = ai["slots"][3]
        self.assertEqual(fourth["slot"], 4)
        self.assertEqual(fourth["state"], "offline")
        self.assertEqual(fourth["install_reason"], "install_pending")
        self.assertFalse(fourth["detected"])
        # Virtual telemetry forbidden: every telemetry field stays null.
        for key in PLACEHOLDER_TELEMETRY_KEYS:
            self.assertIsNone(fourth.get(key), f"placeholder telemetry must be null: {key}")

    def test_no_gpus_all_slots_placeholder(self):
        plan = app.build_dashboard_servers([], now=1_700_000_000.0)
        servers = {item["id"]: item for item in plan["servers"]}
        self.assertEqual(servers["main"]["detected_slots"], 0)
        self.assertEqual(servers["ai"]["detected_slots"], 0)
        for server in plan["servers"]:
            for slot in server["slots"]:
                self.assertEqual(slot["state"], "offline")
                self.assertEqual(slot["install_reason"], "install_pending")
                for key in PLACEHOLDER_TELEMETRY_KEYS:
                    self.assertIsNone(slot.get(key))

    def test_power_targets_320w_per_slot_1280w_total(self):
        plan = app.build_dashboard_servers([_detected(0), _detected(1), _detected(2)], now=1_700_000_000.0)
        servers = {item["id"]: item for item in plan["servers"]}
        ai = servers["ai"]
        self.assertEqual(ai["power_target_slot_w"], 320)
        self.assertEqual(ai["power_target_total_w"], 1280)
        self.assertEqual(320 * ai["planned_slots"], ai["power_target_total_w"])
        for slot in ai["slots"]:
            self.assertEqual(slot["power_target_w"], 320)
        main = servers["main"]
        self.assertIsNone(main["power_target_slot_w"])
        self.assertIsNone(main["power_target_total_w"])


class DashboardMetadataTests(unittest.TestCase):
    def test_collected_at_and_stale_metadata(self):
        now = 1_700_000_000.0
        plan = app.build_dashboard_servers([_detected(0)], now=now)
        self.assertEqual(plan["collected_ts"], int(now))
        self.assertEqual(plan["stale_after_seconds"], 60)
        self.assertEqual(plan["age_seconds"], 0)
        self.assertFalse(plan["stale"])
        self.assertRegex(plan["collected_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        for server in plan["servers"]:
            for slot in server["slots"]:
                self.assertEqual(slot["stale_after_seconds"], 60)
                self.assertIn("collected_ts", slot)
                self.assertIn("age_seconds", slot)
                self.assertIn("stale", slot)

    def test_stale_detected_slot_flagged_and_severity_warning(self):
        old_ts = 1_700_000_000 - 300
        plan = app.build_dashboard_servers(
            [_detected(0, collected_ts=old_ts), _detected(1)], now=1_700_000_000.0
        )
        servers = {item["id"]: item for item in plan["servers"]}
        first = servers["main"]["slots"][0]
        self.assertTrue(first["stale"])
        self.assertGreaterEqual(first["age_seconds"], 299)
        self.assertEqual(first["stale_after_seconds"], 60)
        # install_pending placeholders never escalate; a stale detected slot does.
        self.assertEqual(plan["severity"], "warning")
        self.assertEqual(plan["worst_slot"]["slot"], 1)

    def test_install_pending_placeholders_do_not_escalate_severity(self):
        plan = app.build_dashboard_servers([_detected(0), _detected(1)], now=1_700_000_000.0)
        self.assertEqual(plan["severity"], "ok")
        self.assertIsNone(plan["worst_slot"])

    def test_error_slot_escalates_severity(self):
        plan = app.build_dashboard_servers(
            [_detected(0), _detected(1, status="error")], now=1_700_000_000.0
        )
        self.assertEqual(plan["severity"], "warning")
        self.assertEqual(plan["worst_slot"]["slot"], 2)


class ApiStatusIntegrationTests(unittest.TestCase):
    def _healthy(self):
        return {"status": "ok"}

    def test_api_status_contains_dashboard_servers_and_keeps_gpu_keys(self):
        gpus = [_detected(0), _detected(1), _detected(2)]
        with patch.multiple(
            app,
            get_cpu=lambda: self._healthy(),
            get_memory=lambda: self._healthy(),
            get_disk=lambda: self._healthy(),
            get_network=lambda: self._healthy(),
            get_gpus=lambda: gpus,
            get_services=lambda: {},
            collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}},
            get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "attention": 0, "error": 0}},
            _collect_agent_watchdog=lambda: {"available": True, "stale": False, "agent_count": 0, "abnormal_count": 0, "agents": {}},
            collect_model_probes=lambda: {"models": [], "all_ok": True, "checked_at": int(time.time())},
        ):
            payload = app.api_status()
        # Legacy compatibility keys stay untouched.
        self.assertIn("gpu", payload)
        self.assertIn("gpus", payload)
        self.assertEqual(len(payload["gpus"]), 3)
        self.assertEqual(payload["gpus"], gpus)
        self.assertIn("dashboard_servers", payload)
        servers = {item["id"]: item for item in payload["dashboard_servers"]["servers"]}
        self.assertEqual(servers["main"]["planned_slots"], 2)
        self.assertEqual(servers["ai"]["planned_slots"], 4)
        self.assertEqual(servers["ai"]["power_target_total_w"], 1280)
        fourth = servers["ai"]["slots"][3]
        self.assertEqual(fourth["state"], "offline")
        self.assertEqual(fourth["install_reason"], "install_pending")
        for key in PLACEHOLDER_TELEMETRY_KEYS:
            self.assertIsNone(fourth[key])

    def test_dashboard_servers_build_failure_degrades_gracefully(self):
        def boom(*args, **kwargs):
            raise RuntimeError("plan build failed")

        with patch.multiple(
            app,
            get_cpu=lambda: self._healthy(),
            get_memory=lambda: self._healthy(),
            get_disk=lambda: self._healthy(),
            get_network=lambda: self._healthy(),
            get_gpus=lambda: [_detected(0), _detected(1)],
            get_services=lambda: {},
            collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}},
            get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "attention": 0, "error": 0}},
            _collect_agent_watchdog=lambda: {"available": True, "stale": False, "agent_count": 0, "abnormal_count": 0, "agents": {}},
            collect_model_probes=lambda: {"models": [], "all_ok": True, "checked_at": int(time.time())},
        ):
            with patch.object(app, "build_dashboard_servers", boom):
                payload = app.api_status()
        self.assertIn("dashboard_servers", payload)
        self.assertTrue(payload["dashboard_servers"]["stale"])
        self.assertEqual(payload["dashboard_servers"]["severity"], "warning")
        # Overall banner reflects the degraded plan builder but keeps serving.
        self.assertEqual(payload["overall"], "warning")


class UiAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, "static", "index.html")
        with open(path, encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_server_sections_exist(self):
        self.assertIn('id="gpu-server-main"', self.html)
        self.assertIn('id="gpu-server-ai"', self.html)
        self.assertIn('id="gpu-grid"', self.html)
        self.assertIn('id="ai-gpu-grid"', self.html)
        self.assertIn("Main Server", self.html)
        self.assertIn("AI Server", self.html)

    def test_ai_grid_two_by_two_then_single_column_under_620(self):
        rule = re.search(r"\.ai-gpu-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);[^}]*\}", self.html)
        self.assertIsNotNone(rule, "AI grid must be 2x2 on desktop")
        media = re.search(r"@media\s*\(max-width:\s*620px\)\s*\{\s*\.ai-gpu-grid\s*\{\s*grid-template-columns:\s*1fr;", self.html)
        self.assertIsNotNone(media, "AI grid must collapse to one column at 620px")

    def test_badges_and_null_placeholders_in_render(self):
        self.assertIn("OFFLINE", self.html)
        self.assertIn("INSTALL PENDING", self.html)
        self.assertIn("STALE", self.html)
        self.assertIn("renderGpuServerSection", self.html)
        self.assertIn("gpuOverallState", self.html)
        self.assertIn("data.dashboard_servers", self.html)
        # null-safe formatter keeps using '--' placeholders
        self.assertIn('formatGpuValue(value, suffix = "")', self.html)
        self.assertIn('"--"', self.html)

    def test_power_target_and_pcie_anchors(self):
        self.assertIn("power_target_w", self.html)
        self.assertIn("power_target_total_w", self.html)
        self.assertIn("pcie_gen_max", self.html)
        self.assertIn("pcie_width_max", self.html)

    def test_reference_layout_wins_over_legacy_compact_theme(self):
        self.assertIn("Final main-monitor layout aligned to the supplied 1122px reference", self.html)
        self.assertRegex(
            self.html,
            r"body:not\(\.auth-control-open\) \.shell\s*\{[^}]*width:\s*min\(1080px,",
        )
        self.assertRegex(
            self.html,
            r"body:not\(\.auth-control-open\) \.gpu-grid,[\s\S]*?"
            r"grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)",
        )
        self.assertRegex(
            self.html,
            r"body:not\(\.auth-control-open\) \.ai-system-panel\s*\{[^}]*"
            r"grid-template-columns:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\)",
        )

    def test_ai_system_panel_id_is_unique(self):
        self.assertEqual(self.html.count('id="ai-system-panel"'), 1)

    def test_fallback_render_path_keeps_legacy_gpus(self):
        self.assertIn("data.gpus || []", self.html)
        self.assertIn("renderGpuCard(index, item)", self.html)


if __name__ == "__main__":
    unittest.main()
