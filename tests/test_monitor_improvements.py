import json
import unittest
from unittest.mock import patch

import app


class _HealthResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"ok": True}).encode()


class MonitorImprovementTests(unittest.TestCase):
    def test_gateway_accepts_only_verified_health(self):
        config = {"gateway": {"port": 18789}}
        with patch.object(app.urllib.request, "urlopen", return_value=_HealthResponse()) as urlopen:
            result = app.probe_openclaw_gateway(config)
        self.assertEqual("healthy", result["state"])
        self.assertEqual("/health", result["checked_path"])
        self.assertIn("/health", urlopen.call_args.args[0].full_url)

    def test_gateway_reports_invalid_health_as_probe_failure(self):
        response = _HealthResponse()
        response.read = lambda: b'{"status":"running"}'
        with patch.object(app.urllib.request, "urlopen", return_value=response):
            result = app.probe_openclaw_gateway({"gateway": {"port": 18789}})
        self.assertEqual("probe_failed", result["state"])
        self.assertFalse(result["ok"])

    def test_gpu_risk_aggregates_worst_adapter_and_preserves_headroom(self):
        gpus = [
            {"status": "ok", "vram_used_gb": 28.0, "vram_total_gb": 32.0, "vram_usage_percent": 87.5, "vram_headroom_gb": 4.0, "vram_headroom_state": "ok", "temperature_c": 65},
            {"status": "ok", "vram_used_gb": 30.0, "vram_total_gb": 32.0, "vram_usage_percent": 93.8, "vram_headroom_gb": 2.0, "vram_headroom_state": "headroom_low", "temperature_c": 70},
        ]
        result = app.gpu_risk_summary(gpus)
        self.assertEqual("warning", result["risk"])
        self.assertEqual(1, result["worst_index"])
        self.assertEqual(2.0, result["headroom_gb"])

    def test_high_vram_qwen_style_load_is_normal_with_headroom(self):
        result = app.gpu_risk_summary([{
            "status": "ok", "vram_used_gb": 70.0, "vram_total_gb": 80.0,
            "vram_usage_percent": 87.5, "vram_headroom_gb": 10.0,
            "vram_headroom_state": "ok", "temperature_c": 72,
        }])
        self.assertEqual("ok", result["risk"])

    def test_api_status_uses_top_level_openconnector_summary(self):
        healthy = {"status": "ok", "state": "ok"}
        with patch.multiple(app, get_cpu=lambda: healthy, get_memory=lambda: healthy, get_disk=lambda: healthy, get_network=lambda: healthy, get_gpus=lambda: [{"status": "ok", "vram_headroom_state": "ok", "vram_headroom_gb": 10.0, "temperature_c": 60}], get_services=lambda: {}, collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}}, get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "unverified": 0}}):
            result = app.api_status()
        self.assertEqual(2, result["overall_breakdown"]["openconnector"]["total"])
        self.assertEqual("normal", result["overall"])

    def test_api_status_survives_openconnector_probe_failure(self):
        healthy = {"status": "ok", "state": "ok"}
        with patch.multiple(app, get_cpu=lambda: healthy, get_memory=lambda: healthy, get_disk=lambda: healthy, get_network=lambda: healthy, get_gpus=lambda: [{"status": "ok", "vram_headroom_state": "ok", "vram_headroom_gb": 10.0, "temperature_c": 60}], get_services=lambda: {}, collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}}, get_openconnector_dashboard=lambda: (_ for _ in ()).throw(RuntimeError("admin API unavailable"))):
            result = app.api_status()
        self.assertEqual("warning", result["overall"])
        self.assertEqual("unavailable", result["overall_breakdown"]["openconnector"]["state"])
        self.assertIn("admin API unavailable", result["openconnector_error"])

    def test_ui_exposes_all_gpu_rows_and_independent_connector_columns(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("gpus.forEach", html)
        self.assertIn("연결 관리", html)
        self.assertIn("최근 실제 성공", html)
        self.assertIn("Provider 만료정보 등록 필요", html)
        self.assertIn("vram_headroom_gb", html)

    def test_no_unprotected_monitor_write_routes_are_added(self):
        write_paths = {route.path for route in app.app.routes if getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}}
        self.assertFalse({"/api/alerts", "/api/alerts/ack"} & write_paths)


if __name__ == "__main__":
    unittest.main()
