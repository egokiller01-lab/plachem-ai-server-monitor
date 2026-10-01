"""Focused tests for the AI Server remote SSH collector (read-only)."""
import importlib
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

app = importlib.import_module("app")


FAKE_SMI_BLOCK = "\n".join(
    [
        "NVIDIA GeForce RTX 3090, 0, 24042, 24576, 44, 18.95, 320.00, 0, 00000000:01:00.0, 1, 3, 16, 16",
        "NVIDIA GeForce RTX 3090, 0, 24041, 24576, 54, 32.50, 320.00, 0, 00000000:22:00.0, 1, 3, 8, 16",
        "NVIDIA GeForce RTX 3090, 0, 24041, 24576, 53, 32.40, 320.00, 0, 00000000:4D:00.0, 1, 3, 8, 16",
    ]
)

GPU_LINE = (
    "NVIDIA GeForce RTX 3090, 0, 20000, 24576, 50, 100.0, 320.00, 0, "
    "00000000:01:00.0, 3, 3, 16, 16"
)

NET_HEADER = " Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed"


def net_block(rx: int, tx: int) -> str:
    return (
        f"{NET_HEADER}\n"
        f"    lo: 111 2 0 0 0 0 0 0 222 3 0 0 0 0 0 0\n"
        f"enp71s0: {rx} 1234 0 0 0 0 0 0 {tx} 5678 0 0 0 0 0 0\n"
        f"    wlp72s0: 555 6 0 0 0 0 0 0 777 8 0 0 0 0 0\n"
        f"tailscale0: 999 1 0 0 0 0 0 0 888 2 0 0 0 0 0\n"
    )


def full_payload(rx0=1000, tx0=2000, rx1=None, tx1=None, gpus=FAKE_SMI_BLOCK, extra_sys=""):
    rx1 = rx0 + 5000 if rx1 is None else rx1
    tx1 = tx0 + 1500 if tx1 is None else tx1
    return (
        f"__GPUS__\n{gpus}\n__SYS__\n"
        "LOAD 2.10\nNPROC 16\nMEM 65536000 32768000\n"
        "DISK 1000000000 500000000 53%\nTEMP 45000\nUPTIME 15600\n"
        f"{extra_sys}"
        f"__NET0__\n{net_block(rx0, tx0)}\n"
        f"__NET1__\n{net_block(rx1, tx1)}\n"
    )


class RemoteCollectorParsingTests(unittest.TestCase):
    def test_parses_three_real_shaped_gpus_with_all_fields(self):
        parsed = app.parse_ai_server_output(full_payload(), 1700000000)
        self.assertEqual(parsed["error"], None)
        self.assertEqual(len(parsed["gpus"]), 3)
        gpu = parsed["gpus"][1]
        self.assertEqual(gpu["name"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(gpu["vram_used_gb"], 23.5)
        self.assertEqual(gpu["temperature_c"], 54.0)
        self.assertEqual(gpu["power_draw_w"], 32.5)
        self.assertEqual(gpu["power_limit_w"], 320.0)
        self.assertEqual(
            (gpu["pcie_gen_current"], gpu["pcie_gen_max"]), (1, 3)
        )
        self.assertEqual(
            (gpu["pcie_width_current"], gpu["pcie_width_max"]), (8, 16)
        )
        self.assertEqual(gpu["source"], "ssh:ai-server")

    def test_system_block_includes_cpu_ram_network_disk_temp(self):
        parsed = app.parse_ai_server_output(full_payload(), 1700000000)
        system = parsed["system"]
        self.assertEqual(system["cpu_cores"], 16)
        self.assertEqual(system["cpu_temp_c"], 45.0)
        self.assertEqual(system["memory_total_gb"], 62.5)
        self.assertEqual(system["disk_usage_percent"], 53.0)
        # wired interface only: deltas 5000 rx / 1500 tx per ~1s window
        self.assertEqual(system["download_bps"], 5000.0)
        self.assertEqual(system["upload_bps"], 1500.0)
        self.assertEqual(system["uptime_seconds"], 15600.0)
        self.assertEqual(system["status"], "ok")

    def test_counter_reset_yields_null_bps_instead_of_fake_traffic(self):
        parsed = app.parse_ai_server_output(
            full_payload(rx0=9000, tx0=9000, rx1=10, tx1=10), 1700000000
        )
        self.assertIsNone(parsed["system"]["download_bps"])
        self.assertIsNone(parsed["system"]["upload_bps"])

    def test_no_gpus_detected_never_fabricates_slots(self):
        parsed = app.parse_ai_server_output(
            full_payload(gpus="No devices were found"), 1700000000
        )
        self.assertEqual(parsed["gpus"], [])
        plan = app.build_dashboard_servers(
            [{"status": "ok", "name": "CMP"}],
            now=1700000000.0,
            ai_gpus=parsed["gpus"],
            ai_system=parsed["system"],
            ai_error=None,
            ai_collected_ts=1700000000,
        )
        servers = {item["id"]: item for item in plan["servers"]}
        self.assertEqual(servers["ai"]["detected_slots"], 0)
        for slot in servers["ai"]["slots"]:
            self.assertIsNone(slot["usage_percent"])
            self.assertIsNone(slot["power_draw_w"])

    def test_error_payload_carries_no_telemetry(self):
        payload = app._ai_server_error_payload("ai-server", "ssh_timeout", 1700000000)
        self.assertEqual(payload["gpus"], [])
        self.assertEqual(payload["error"], "ssh_timeout")
        for key, value in payload["system"].items():
            if key not in {"status", "host", "stale", "error", "source"}:
                self.assertIsNone(value, key)


class RemoteCollectorBehaviorTests(unittest.TestCase):
    def setUp(self):
        app._ai_server_reset_for_tests()

    def test_disabled_host_returns_none_without_subprocess(self):
        with mock.patch.object(app, "AI_SSH_HOST", ""):
            self.assertIsNone(app.collect_ai_server(now=1700000000.0))

    def test_success_then_within_interval_returns_cache_without_second_ssh(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=full_payload(), stderr="")
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(app.subprocess, "run", return_value=completed) as run:
            first = app.collect_ai_server(now=1000.0)
            second = app.collect_ai_server(now=1005.0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(first["gpus"]), 3)
        self.assertEqual(second["collected_ts"], first["collected_ts"])
        self.assertIsNone(second.get("stale"))
        self.assertIsNone(second["error"])

    def test_failure_serves_cached_sample_as_stale(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=full_payload(), stderr="")
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(app.subprocess, "run", return_value=completed):
            app.collect_ai_server(now=1000.0)
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(
                 app.subprocess, "run",
                 side_effect=subprocess_timeout(),
             ):
            stale = app.collect_ai_server(now=1100.0)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["error"], "ssh_timeout")
        self.assertEqual(len(stale["gpus"]), 3)
        plan = app.build_dashboard_servers(
            [{"status": "ok", "name": "CMP"}],
            now=1100.0,
            ai_gpus=stale["gpus"],
            ai_system=dict(stale["system"]),
            ai_error=stale["error"],
            ai_collected_ts=stale["collected_ts"],
        )
        servers = {item["id"]: item for item in plan["servers"]}
        self.assertEqual(plan["severity"], "warning")
        self.assertTrue(servers["ai"]["slots"][0]["stale"])
        self.assertTrue(servers["ai"]["system"]["stale"])
        # Main slots keep their own state and are not marked stale
        self.assertFalse(servers["main"]["slots"][0]["stale"])

    def test_first_failure_returns_error_payload_not_guaranteed_values(self):
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(
                 app.subprocess, "run",
                 side_effect=RuntimeError("ssh: connect to host port 22: Connection refused"),
             ):
            payload = app.collect_ai_server(now=1000.0)
        self.assertEqual(payload["error"], "ssh_timeout")
        self.assertEqual(payload["gpus"], [])
        self.assertIsNone(payload["system"]["cpu_usage_percent"])


class PlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        app._ai_server_reset_for_tests()

    def test_3of4_real_plus_plan_slot4_install_pending(self):
        parsed = app.parse_ai_server_output(full_payload(), 1700000000)
        plan = app.build_dashboard_servers(
            [
                {"status": "ok", "name": "CMP170HX-A", "usage_percent": 3},
                {"status": "ok", "name": "CMP170HX-B", "usage_percent": 4},
            ],
            now=1700000000.0,
            ai_gpus=parsed["gpus"],
            ai_system=parsed["system"],
            ai_error=None,
            ai_collected_ts=1700000000,
        )
        servers = {item["id"]: item for item in plan["servers"]}
        self.assertEqual(servers["main"]["detected_slots"], 2)
        self.assertEqual(servers["ai"]["detected_slots"], 3)
        self.assertEqual(servers["ai"]["planned_slots"], 4)
        self.assertEqual(
            [slot["index"] for slot in servers["ai"]["slots"][:3]], [0, 1, 2]
        )
        slot4 = servers["ai"]["slots"][3]
        self.assertEqual(slot4["state"], "offline")
        self.assertEqual(slot4["install_reason"], "install_pending")
        self.assertEqual(slot4["server_id"], "ai")
        self.assertEqual(slot4["planned_model"], "RTX 3090")
        self.assertFalse(slot4["detected"])
        self.assertEqual(slot4["stale_after_seconds"], app.GPU_STALE_AFTER_SECONDS)
        self.assertIsNone(slot4["usage_percent"])
        self.assertEqual(plan["severity"], "ok")

    def test_stale_boundary_at_gpu_stale_after_seconds(self):
        parsed = app.parse_ai_server_output(full_payload(), 1000)
        fresh = app.build_dashboard_servers(
            [{"status": "ok"}],
            now=float(1000 + app.GPU_STALE_AFTER_SECONDS - 1),
            ai_gpus=parsed["gpus"],
            ai_system=parsed["system"],
            ai_error=None,
            ai_collected_ts=1000,
        )
        stale = app.build_dashboard_servers(
            [{"status": "ok"}],
            now=float(1000 + app.GPU_STALE_AFTER_SECONDS + 1),
            ai_gpus=parsed["gpus"],
            ai_system=parsed["system"],
            ai_error=None,
            ai_collected_ts=1000,
        )
        fresh_servers = {item["id"]: item for item in fresh["servers"]}
        stale_servers = {item["id"]: item for item in stale["servers"]}
        self.assertFalse(fresh_servers["ai"]["slots"][0]["stale"])
        self.assertEqual(fresh["severity"], "ok")
        self.assertTrue(stale_servers["ai"]["slots"][0]["stale"])
        self.assertEqual(stale["severity"], "warning")


class SingleFlightConcurrencyTests(unittest.TestCase):
    """2026-10-01 production regression (3c5dffb): while an SSH refresh is in
    flight, concurrent callers must NOT label a valid cached sample stale."""

    def setUp(self):
        app._ai_server_reset_for_tests()

    def _payload(self, gpu_line=GPU_LINE):
        return (
            f"__GPUS__\n{gpu_line}\n__SYS__\n"
            "LOAD 1.0\nNPROC 8\nMEM 65536000 32768000\n"
            "DISK 1000000000 500000000 50%\nTEMP 45000\nUPTIME 100\n"
            "__NET0__\nx\n__NET1__\ny\n"
        )

    def test_concurrent_request_during_refresh_is_not_marked_stale(self):
        started = threading.Event()
        release = threading.Event()
        state = {"ssh_calls": 0}

        def slow_run(args, **kwargs):
            # The seed collection (call #1) returns at once; the refresh
            # (call #2) blocks so we can observe concurrent arrivals.
            state["ssh_calls"] += 1
            if state["ssh_calls"] > 1:
                started.set()
                release.wait(5.0)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=self._payload(), stderr=""
            )

        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(app.subprocess, "run", side_effect=slow_run):
            app.collect_ai_server(now=1000.0)  # seed a valid sample
            self.assertEqual(app._ai_server_cache["result"]["collected_ts"], 1000)

            outcome = {}

            def refresher():
                outcome["A"] = app.collect_ai_server(now=1011.0)

            worker = threading.Thread(target=refresher)
            worker.start()
            self.assertTrue(started.wait(5.0), "refresh never started SSH")
            calls_before = state["ssh_calls"]  # seed + the one in-flight refresh
            time.sleep(0.1)  # SSH round-trip in flight; cache age 11s (< 60s)
            outcome["B"] = app.collect_ai_server(now=1011.3)
            calls_after = state["ssh_calls"]
            release.set()
            worker.join(timeout=10.0)

        concurrent = outcome["B"]
        self.assertIsNone(concurrent.get("stale"), "valid cache must not be stale")
        self.assertIsNone(concurrent.get("error"))
        self.assertEqual(len(concurrent["gpus"]), 1)
        # The in-flight refresh keeps its own fresh sample.
        self.assertEqual(outcome["A"]["collected_ts"], 1011)
        self.assertIsNone(outcome["A"].get("stale"))
        # Single-flight: the concurrent caller added no SSH round-trip at all
        # (only the seed collection and the one in-flight refresh ever ran).
        self.assertEqual(calls_after, calls_before, "concurrent callers must not stack SSH")
        self.assertEqual(calls_before, 2, "expected exactly seed + one refresh")

    def test_first_call_with_no_cache_waits_for_inflight_outcome(self):
        started = threading.Event()
        release = threading.Event()

        def slow_run(args, **kwargs):
            started.set()
            release.wait(3.0)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=self._payload(), stderr=""
            )

        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(app.subprocess, "run", side_effect=slow_run):
            outcome = {}
            worker = threading.Thread(
                target=lambda: outcome.update(A=app.collect_ai_server(now=1000.0))
            )
            worker.start()
            self.assertTrue(started.wait(5.0))
            time.sleep(0.1)
            outcome["B"] = app.collect_ai_server(now=1000.2)
            release.set()
            worker.join(timeout=10.0)

        # No bogus error panel for the caller that arrived mid first-collection.
        self.assertEqual(outcome["B"].get("error"), None)
        self.assertEqual(len(outcome["B"]["gpus"]), 1)

    def test_success_does_not_trigger_backoff_for_next_caller(self):
        # 2026-10-01 defect #2: after a successful refresh, the *next* caller
        # (no refresh in flight, cache age ≈ TTL) must run its own collection
        # and return fresh data — not the stale "collect_failed" backoff path.
        calls = []

        def ok_run(args, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=self._payload(), stderr=""
            )

        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(app.subprocess, "run", side_effect=ok_run):
            app.collect_ai_server(now=1000.0)              # seed
            first = app.collect_ai_server(now=1010.0)      # refresh succeeds
            self.assertIsNone(first.get("stale"))
            self.assertIsNone(first.get("error"))
            self.assertEqual(first["collected_ts"], 1010)
            follow = app.collect_ai_server(now=1010.2)     # cache fresh again
            self.assertIsNone(follow.get("stale"))
            self.assertIsNone(follow.get("error"))          # no bogus collect_failed
            later = app.collect_ai_server(now=1020.0)      # next window: real SSH
            self.assertEqual(len(calls), 3, "seed + refresh + next-window collect")
            self.assertIsNone(later.get("stale"))
            self.assertIsNone(later.get("error"))

    def test_real_failure_still_marks_cached_sample_stale(self):
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(
                 app.subprocess, "run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=self._payload(), stderr=""),
             ):
            app.collect_ai_server(now=1000.0)
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(
                 app.subprocess, "run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="ssh: connect refused"),
             ):
            stale = app.collect_ai_server(now=1011.0)
        self.assertTrue(stale["stale"], "a genuine SSH failure must mark stale=True")
        self.assertEqual(stale["error"], "ssh_exit_255")
        self.assertEqual(len(stale["gpus"]), 1)

    def test_cached_sample_past_stale_boundary_reports_stale(self):
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(
                 app.subprocess, "run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=self._payload(), stderr=""),
             ):
            app.collect_ai_server(now=1000.0)
        # Another refresh is in flight while the cache is already past 60s.
        with app._ai_server_lock:
            app._ai_server_updating = True
        try:
            payload = app.collect_ai_server(now=1000.0 + app.GPU_STALE_AFTER_SECONDS + 5)
        finally:
            with app._ai_server_lock:
                app._ai_server_updating = False
        self.assertTrue(payload["stale"], "age past 60s must stay stale")

    def test_plan_keeps_slots_fresh_while_refresh_in_flight(self):
        with mock.patch.object(app, "AI_SSH_HOST", "ai-server"), \
             mock.patch.object(
                 app.subprocess, "run",
                 return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=self._payload(), stderr=""),
             ):
            seed = app.collect_ai_server(now=1000.0)
        plan = app.build_dashboard_servers(
            [{"status": "ok", "name": "CMP-A"}, {"status": "ok", "name": "CMP-B"}],
            now=1011.3,
            ai_gpus=seed["gpus"],
            ai_system=dict(seed["system"]),
            ai_error=None,
            ai_collected_ts=seed["collected_ts"],
        )
        servers = {item["id"]: item for item in plan["servers"]}
        self.assertEqual(plan["severity"], "ok")
        self.assertEqual([s.get("stale") for s in servers["ai"]["slots"][:1]], [False])


INDEX_HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _render_ai_panel_gauges(system_payload, collect_error):
    """Render the real AI panel renderer against a payload using headless Chromium.

    Loads the shipped static/index.html from disk (no server needed) and calls
    the page's own renderAiSystemPanel(), so the assertion covers the code that
    actually runs in the browser.
    """
    from playwright.sync_api import sync_playwright

    html = INDEX_HTML.read_text(encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{INDEX_HTML}", wait_until="commit", timeout=30000)
        page.wait_for_function("typeof renderAiSystemPanel === 'function'", timeout=20000)
        values = page.evaluate(
            """([system, collectError]) => {
              renderAiSystemPanel({ id: 'ai', system, collect_error: collectError });
              const panel = document.querySelector('#ai-system-panel');
              return {
                className: panel.className,
                head: panel.querySelector('.ai-system-head').textContent.replace(/\s+/g, ' ').trim(),
                gauges: Array.from(panel.querySelectorAll('.ai-system-gauge strong')).map(e => e.textContent.trim()),
                labels: Array.from(panel.querySelectorAll('.ai-system-gauge > span')).map(e => e.textContent.trim()),
              };
            }""",
            [system_payload, collect_error],
        )
        browser.close()
    return values


class AiPanelDashContractTests(unittest.TestCase):
    """Blocking defect regression (QwenTest 2026-10-01): gauges must never show
    a substitute string such as "Unknown"; missing telemetry is exactly "--"."""

    def test_ai_panel_never_calls_format_bps_directly(self):
        # formatBps() intentionally returns "Unknown" for the Main KPI tiles.
        # The AI system panel must format bytes through its own dash fallback.
        html = INDEX_HTML.read_text(encoding="utf-8")
        start = html.index("function renderAiSystemPanel")
        body = html[start:html.index("function renderGpuServerSection", start)]
        direct_calls = [
            line.strip()
            for line in body.splitlines()
            if "formatBps(" in line and "bpsVal" not in line and "Contract" not in line
            and "formatBps() yields" not in line and "Main KPI" not in line
        ]
        self.assertEqual(direct_calls, [], "AI panel must not call formatBps directly")
        self.assertIn("bpsVal", body)
        self.assertIn("netVal", body)

    @unittest.skipIf(
        __import__("importlib").util.find_spec("playwright") is None, "playwright unavailable"
    )
    def test_first_ssh_failure_all_five_gauges_are_dash(self):
        payload = app._ai_server_error_payload("ai-server", "ssh_timeout", 1700000000)
        rendered = _render_ai_panel_gauges(payload["system"], payload["error"])
        self.assertEqual(len(rendered["gauges"]), 5, rendered["labels"])
        self.assertEqual(rendered["gauges"], ["--"] * 5, rendered)
        self.assertNotIn("Unknown", " ".join(rendered["gauges"]))
        self.assertIn("ai-system-error", rendered["className"])
        self.assertIn("COLLECT ERROR", rendered["head"])

    @unittest.skipIf(
        __import__("importlib").util.find_spec("playwright") is None, "playwright unavailable"
    )
    def test_system_null_all_five_gauges_are_dash(self):
        rendered = _render_ai_panel_gauges(None, None)
        self.assertEqual(rendered["gauges"], ["--"] * 5, rendered)

    @unittest.skipIf(
        __import__("importlib").util.find_spec("playwright") is None, "playwright unavailable"
    )
    def test_partial_null_network_renders_dash_per_direction(self):
        base = {
            "status": "ok", "cpu_usage_percent": 12.0, "cpu_cores": 64, "cpu_load1": 1.0,
            "memory_used_gb": 8.0, "memory_total_gb": 64.0, "memory_usage_percent": 12.5,
            "disk_used_gb": 100.0, "disk_total_gb": 1000.0, "disk_usage_percent": 10.0,
            "cpu_temp_c": 44.0, "download_bps": None, "upload_bps": None,
        }
        rendered = _render_ai_panel_gauges(base, None)
        net = rendered["gauges"][rendered["labels"].index("Network ↓ / ↑")]
        self.assertEqual(net, "--")
        self.assertNotIn("Unknown", " ".join(rendered["gauges"]))
        one_sided = dict(base, download_bps=1024.0, upload_bps=None)
        rendered2 = _render_ai_panel_gauges(one_sided, None)
        net2 = rendered2["gauges"][rendered2["labels"].index("Network ↓ / ↑")]
        self.assertEqual(net2, "1 KB/s / --")

    @unittest.skipIf(
        __import__("importlib").util.find_spec("playwright") is None, "playwright unavailable"
    )
    def test_live_payload_still_formats_real_numbers(self):
        base = {
            "status": "ok", "cpu_usage_percent": 5.0, "cpu_cores": 64, "cpu_load1": 2.1,
            "memory_used_gb": 9.2, "memory_total_gb": 62.6, "memory_usage_percent": 14.7,
            "disk_used_gb": 157.2, "disk_total_gb": 1831.7, "disk_usage_percent": 10.0,
            "cpu_temp_c": 43.0, "download_bps": 1818.0, "upload_bps": 1036.0,
        }
        rendered = _render_ai_panel_gauges(base, None)
        labels, gauges = rendered["labels"], rendered["gauges"]
        self.assertIn("LIVE", rendered["head"])
        # formatBps() rounds to whole KB/s: 1818 -> "2 KB/s", 1036 -> "1 KB/s".
        self.assertEqual(gauges[labels.index("Network ↓ / ↑")], "2 KB/s / 1 KB/s")
        self.assertIn("43°C", gauges[labels.index("CPU Temp")])
        self.assertIn("9.2 / 62.6 GB", gauges[labels.index("RAM")])
        self.assertNotIn("--", " ".join(gauges))


def subprocess_timeout():
    import subprocess as _sp

    return _sp.TimeoutExpired(cmd=["ssh"], timeout=app.AI_SSH_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
