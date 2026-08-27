import time
import unittest
from unittest.mock import patch

import app


class OpenConnectorDashboardTests(unittest.TestCase):
    def test_dashboard_reports_real_run_health_without_secrets(self) -> None:
        now_iso = "2026-08-27T05:00:00.000Z"
        connections = [
            {"service": service, "configured": True, "profile": {"displayName": "private account"}}
            for service in app.OPENCONNECTOR_MANAGED_ACTIONS
        ]
        runs = [{
            "service": service,
            "actionId": action,
            "caller": "http",
            "completedAt": now_iso,
            "durationMs": 100,
            "ok": True,
            "outputSummary": {"token": "must not leak"},
        } for service, action in app.OPENCONNECTOR_MANAGED_ACTIONS.items()]

        def fake_get(path: str, timeout: float = 2.5):
            return connections if path == "/api/connections" else {"items": runs}

        with patch.object(app, "_openconnector_admin_get", side_effect=fake_get), patch.object(time, "time", return_value=app._parse_iso_timestamp(now_iso) + 60):
            dashboard = app.get_openconnector_dashboard()

        self.assertEqual(dashboard["summary"]["managed"], 13)
        self.assertEqual(dashboard["summary"]["healthy"], 13)
        self.assertEqual(dashboard["summary"]["success_rate_24h"], 100.0)
        self.assertNotIn("private account", str(dashboard))
        self.assertNotIn("must not leak", str(dashboard))

    def test_dashboard_flags_failed_read(self) -> None:
        connections = [{"service": service, "configured": True} for service in app.OPENCONNECTOR_MANAGED_ACTIONS]
        runs = [{"service": "gmail", "actionId": "gmail.get_profile", "completedAt": "2026-08-27T05:00:00.000Z", "ok": False, "errorCode": "oauth_token_refresh_failed"}]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:01:00.000Z")):
            dashboard = app.get_openconnector_dashboard()
        gmail = next(item for item in dashboard["services"] if item["service"] == "gmail")
        self.assertEqual(gmail["state"], "error")
        self.assertTrue(any(alert["service"] == "gmail" for alert in dashboard["alerts"]))


if __name__ == "__main__":
    unittest.main()
