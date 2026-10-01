import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
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
    def setUp(self):
        app._runtime_status_cache = {}
        app._runtime_status_cache_at = 0.0

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

    def test_api_status_includes_agent_watchdog_payload(self):
        healthy = {"status": "ok", "state": "ok"}
        watchdog = {
            "available": True,
            "unavailable_reason": None,
            "scan_ts": int(app.time.time() * 1000),
            "scan_ts_text": "2026-09-30 21:00:00",
            "stale": False,
            "agent_count": 2,
            "abnormal_count": 0,
            "agents": {"main": {"status": "IDLE", "display_state": "IDLE", "buckets": {}, "warnings": []}},
        }
        with patch.multiple(app, get_cpu=lambda: healthy, get_memory=lambda: healthy, get_disk=lambda: healthy, get_network=lambda: healthy, get_gpus=lambda: [{"status": "ok", "vram_headroom_state": "ok", "vram_headroom_gb": 10.0, "temperature_c": 60}], get_services=lambda: {}, collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}}, get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "unverified": 0}}, _collect_agent_watchdog=lambda: watchdog):
            result = app.api_status()
        self.assertTrue(result["stall_detector"]["available"])
        self.assertFalse(result["stall_detector"]["stale"])
        self.assertEqual(2, result["stall_detector"]["agent_count"])
        self.assertEqual("fresh", result["overall_breakdown"]["stall_detector"])

    def test_api_status_isolates_agent_watchdog_failure(self):
        healthy = {"status": "ok", "state": "ok"}
        with patch.multiple(app, get_cpu=lambda: healthy, get_memory=lambda: healthy, get_disk=lambda: healthy, get_network=lambda: healthy, get_gpus=lambda: [{"status": "ok", "vram_headroom_state": "ok", "vram_headroom_gb": 10.0, "temperature_c": 60}], get_services=lambda: {}, collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}}, get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "unverified": 0}}, _collect_agent_watchdog=lambda: (_ for _ in ()).throw(RuntimeError("watchdog db unreadable"))):
            result = app.api_status()
        self.assertFalse(result["stall_detector"]["available"])
        self.assertTrue(result["stall_detector"]["stale"])
        self.assertIn("watchdog db unreadable", result["stall_detector"]["unavailable_reason"])
        self.assertEqual("unavailable", result["overall_breakdown"]["stall_detector"])
        self.assertEqual("normal", result["overall"])

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

    def test_agent_state_uses_only_working_idle_offline(self):
        now_ms = int(app.time.time() * 1000)
        runtime = {
            "sessions": [
                {"agentId": "main", "key": "agent:main:test", "updatedAt": now_ms - 5 * 60 * 1000,
                 "totalTokens": 1, "contextTokens": 1, "systemSent": True}
            ]
        }
        result = app.read_agent_sessions(runtime, "main")
        self.assertEqual("idle", result["state"])

    def test_runtime_session_status_is_cached_across_monitor_consumers(self):
        payload = {"sessions": [{"agentId": "main", "key": "agent:main:main"}]}
        completed = app.subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.object(app, "safe_run", return_value=completed) as run:
            first = app.read_openclaw_runtime_status()
            second = app.read_openclaw_runtime_status()
        self.assertIs(first, second)
        self.assertEqual(1, run.call_count)

    def test_runtime_session_status_coalesces_concurrent_refreshes(self):
        payload = {"sessions": [{"agentId": "secretary", "key": "agent:secretary:main"}]}
        completed = app.subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        entered = threading.Event()
        release = threading.Event()

        def slow_run(*args, **kwargs):
            entered.set()
            release.wait(2)
            return completed

        results = []
        with patch.object(app, "safe_run", side_effect=slow_run) as run:
            first = threading.Thread(target=lambda: results.append(app.read_openclaw_runtime_status()))
            second = threading.Thread(target=lambda: results.append(app.read_openclaw_runtime_status()))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            release.set()
            first.join(2)
            second.join(2)

        self.assertEqual(2, len(results))
        self.assertEqual(1, run.call_count)
        self.assertEqual(payload, results[0])
        self.assertEqual(payload, results[1])

    def test_token_telemetry_prefers_exact_session_and_marks_stale_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE token_telemetry_samples (
                  sample_id INTEGER PRIMARY KEY, sampled_at TEXT, sampled_at_epoch INTEGER,
                  agent TEXT, session_id TEXT, classification TEXT, model_key TEXT,
                  current_prompt_tokens INTEGER, max_prompt_tokens INTEGER,
                  prompt_source TEXT, prompt_reliable INTEGER, context_limit INTEGER,
                  context_pressure REAL, delta_processed INTEGER,
                  velocity_tokens_per_hour REAL, calls_per_hour REAL,
                  delta_cache_ratio REAL, reasons_json TEXT
                );
                CREATE TABLE token_telemetry_agent_samples (
                  sample_id INTEGER PRIMARY KEY, sampled_at TEXT, sampled_at_epoch INTEGER,
                  agent TEXT, calls_24h INTEGER, fresh_input_24h INTEGER,
                  cache_read_24h INTEGER, output_24h INTEGER, processed_24h INTEGER,
                  classification TEXT
                );
            """)
            con.execute(
                "INSERT INTO token_telemetry_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, "now", 1000, "erpmanager", "s1", "ABNORMAL", "qwenflash/qwen",
                 185000, 185000, "input+cacheRead", 1, 200000, 0.925, 1010000,
                 12120000, 12, 0.95, json.dumps(["CACHE_CHURN"])),
            )
            con.execute(
                "INSERT INTO token_telemetry_agent_samples VALUES (1,'now',1000,'erpmanager',20,100,900,10,1010,'HIGH')"
            )
            con.commit()
            con.close()
            with patch.object(app, "TOKEN_TELEMETRY_DB_PATH", db), patch.object(app.time, "time", return_value=1100):
                row = app.read_token_telemetry("erpmanager", "s1")
            self.assertTrue(row["available"])
            self.assertEqual("ABNORMAL", row["status"])
            self.assertEqual("session", row["source"])
            self.assertEqual(185000, row["current_prompt_tokens"])
            self.assertEqual(1010, row["processed_24h"])
            self.assertEqual(["CACHE_CHURN"], row["reasons"])
            with patch.object(app, "TOKEN_TELEMETRY_DB_PATH", db), patch.object(app.time, "time", return_value=3000):
                stale = app.read_token_telemetry("erpmanager", "s1")
            self.assertFalse(stale["available"])
            self.assertEqual("UNAVAILABLE", stale["status"])

    def test_openclaw_ui_has_separate_token_state_column(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('"Token State"', html)
        self.assertIn(".token-state.token-abnormal", html)
        self.assertIn('ABNORMAL:"token-abnormal"', html)


class _ProbeResponse:
    """Stand-in for the proxy-free urllib opener response."""

    def __init__(self, payload, status=200):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


class _FakeProbeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def _completion_payload(content, reasoning=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}


class ModelProbeTests(unittest.TestCase):
    def setUp(self):
        app._model_probe_cache = {}
        app._model_probe_cache_at = 0.0

    def test_probe_accepts_only_non_empty_completion_text(self):
        opener = _FakeProbeOpener(_ProbeResponse(_completion_payload("OK", reasoning="thinking")))
        with patch.object(app, "_MODEL_PROBE_OPENER", opener):
            result = app.probe_model_inference(name="qwen", base_url="http://127.0.0.1:18002")
        self.assertTrue(result["ok"])
        self.assertEqual(200, result["http_status"])
        self.assertTrue(result["has_text"])
        self.assertFalse(result["reasoning_only"])
        self.assertIsNone(result["error"])
        self.assertEqual("stop", result["finish_reason"])
        self.assertEqual("http://127.0.0.1:18002/v1/chat/completions", result["endpoint"])

    def test_probe_rejects_empty_content_with_reasoning_only(self):
        for empty in ("", None):
            with self.subTest(content=empty):
                opener = _FakeProbeOpener(_ProbeResponse(_completion_payload(empty, reasoning="still thinking", finish="length")))
                with patch.object(app, "_MODEL_PROBE_OPENER", opener):
                    result = app.probe_model_inference(name="strata", base_url="http://127.0.0.1:18086")
                self.assertFalse(result["ok"])
                self.assertEqual(200, result["http_status"])
                self.assertFalse(result["has_text"])
                self.assertTrue(result["reasoning_only"])
                self.assertEqual("reasoning_only_no_content", result["error"])

    def test_probe_rejects_malformed_or_missing_choices(self):
        cases = {
            "not_json": _ProbeResponse(b"<html>bad gateway</html>"),
            "no_choices": _ProbeResponse({"object": "chat.completion"}),
            "empty_choices": _ProbeResponse({"choices": []}),
            "choice_not_object": _ProbeResponse({"choices": ["oops"]}),
            "whitespace_content": _ProbeResponse(_completion_payload("   ")),
        }
        for label, response in cases.items():
            with self.subTest(case=label):
                opener = _FakeProbeOpener(response)
                with patch.object(app, "_MODEL_PROBE_OPENER", opener):
                    result = app.probe_model_inference(name="qwen", base_url="http://127.0.0.1:18002")
                self.assertFalse(result["ok"], label)
                self.assertFalse(result["has_text"], label)
                self.assertEqual("no_completion_text", result["error"], label)

    def test_probe_sanitizes_opener_error_without_propagating(self):
        opener = _FakeProbeOpener(error=RuntimeError(
            "401 client error for http://openclaw:SUPERVALUETOKEN@127.0.0.1:43483/v1/chat/completions"
        ))
        with patch.object(app, "_MODEL_PROBE_OPENER", opener):
            result = app.probe_model_inference(name="qwen", base_url="http://127.0.0.1:18002")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["http_status"])
        self.assertFalse(result["has_text"])
        self.assertIsNotNone(result["error"])
        self.assertNotIn("SUPERVALUETOKEN", result["error"])
        self.assertIn("[REDACTED]", result["error"])
        self.assertIsInstance(result["response_ms"], int)

    def test_collect_model_probes_uses_cache_within_ttl(self):
        calls = []

        def fake_probe(*, name, base_url, timeout_seconds=8.0):
            calls.append(name)
            return {"name": name, "ok": True}

        with patch.object(app, "probe_model_inference", side_effect=fake_probe):
            first = app.collect_model_probes()
            second = app.collect_model_probes()
        expected_names = sorted(target["name"] for target in app.MODEL_PROBE_TARGETS)
        self.assertEqual(expected_names, sorted(calls[: len(expected_names)]))
        self.assertEqual(len(expected_names), len(calls))
        self.assertTrue(first["all_ok"])
        self.assertIs(first, second)
        self.assertIsInstance(first["checked_at"], int)
        self.assertRegex(first["last_updated"], r"^\d{2}:\d{2}:\d{2}$")

        with patch.object(app, "probe_model_inference", side_effect=fake_probe):
            app._model_probe_cache_at = 0.0
            third = app.collect_model_probes()
        self.assertEqual(2 * len(expected_names), len(calls))
        self.assertIsNot(first, third)

    def test_api_status_reports_healthy_model_probes(self):
        healthy = {"status": "ok", "state": "ok"}
        payload = {
            "models": [{"name": "qwen", "ok": True, "http_status": 200, "has_text": True},
                       {"name": "strata", "ok": True, "http_status": 200, "has_text": True}],
            "all_ok": True,
            "checked_at": 1790782018,
            "last_updated": "22:26:58",
        }
        with patch.multiple(app, get_cpu=lambda: healthy, get_memory=lambda: healthy, get_disk=lambda: healthy, get_network=lambda: healthy, get_gpus=lambda: [{"status": "ok", "vram_headroom_state": "ok", "vram_headroom_gb": 10.0, "temperature_c": 60}], get_services=lambda: {}, collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}}, get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "unverified": 0}}, _collect_agent_watchdog=lambda: {"available": True, "stale": False, "agent_count": 0, "abnormal_count": 0, "agents": {}}, collect_model_probes=lambda: payload):
            result = app.api_status()
        self.assertEqual(payload, result["model_probes"])
        self.assertEqual(2, len(result["model_probes"]["models"]))
        self.assertTrue(result["model_probes"]["all_ok"])
        self.assertEqual("healthy", result["overall_breakdown"]["model_probes"])

    def test_api_status_isolates_model_probe_failure(self):
        healthy = {"status": "ok", "state": "ok"}

        def boom():
            raise RuntimeError("probe endpoint refused with credential=TOPSECRET")

        with patch.multiple(app, get_cpu=lambda: healthy, get_memory=lambda: healthy, get_disk=lambda: healthy, get_network=lambda: healthy, get_gpus=lambda: [{"status": "ok", "vram_headroom_state": "ok", "vram_headroom_gb": 10.0, "temperature_c": 60}], get_services=lambda: {}, collect_openclaw_status=lambda: {"summary": {"gateway": "healthy"}, "gateway": {"ok": True}}, get_openconnector_dashboard=lambda: {"summary": {"managed": 2, "healthy": 2, "unverified": 0}}, _collect_agent_watchdog=lambda: {"available": True, "stale": False, "agent_count": 0, "abnormal_count": 0, "agents": {}}, collect_model_probes=boom):
            result = app.api_status()
        probes = result["model_probes"]
        self.assertEqual([], probes["models"])
        self.assertFalse(probes["all_ok"])
        self.assertIsInstance(probes["checked_at"], int)
        self.assertRegex(probes["last_updated"], r"^\d{2}:\d{2}:\d{2}$")
        self.assertIn("[REDACTED]", probes["error"])
        self.assertNotIn("TOPSECRET", probes["error"])
        self.assertEqual("attention", result["overall_breakdown"]["model_probes"])
        self.assertEqual("normal", result["overall"])


if __name__ == "__main__":
    unittest.main()
