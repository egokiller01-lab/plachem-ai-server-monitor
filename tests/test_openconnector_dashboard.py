import time
import unittest
from unittest.mock import patch

import app


class OpenConnectorDashboardTests(unittest.TestCase):
    def test_dashboard_reports_real_run_health_without_secrets(self) -> None:
        now_iso = "2026-08-27T05:00:00.000Z"
        connections = [
            {"service": service, "configured": True, "authType": "api_key", "profile": {"displayName": "private account"}}
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

        updates = {service: {"connection_last_modified_at": "2026-08-27T01:00:00.000Z", "oauth_client_last_modified_at": "2026-08-27T02:00:00.000Z"} for service in app.OPENCONNECTOR_MANAGED_ACTIONS}
        with patch.object(app, "_openconnector_admin_get", side_effect=fake_get), patch.object(time, "time", return_value=app._parse_iso_timestamp(now_iso) + 60), patch.object(app, "get_openconnector_connection_updates", return_value=updates):
            dashboard = app.get_openconnector_dashboard()

        self.assertEqual(dashboard["summary"]["managed"], 13)
        self.assertTrue(all(s["auth_type"] == "api_key" for s in dashboard["services"]))
        self.assertEqual(dashboard["summary"]["healthy"], 13)
        self.assertEqual(dashboard["summary"]["success_rate_24h"], 100.0)
        self.assertEqual(dashboard["summary"]["expiry_unknown"], 13)
        self.assertEqual(len(dashboard["service_usage_24h"]), 13)
        self.assertEqual(dashboard["caller_usage_24h"][0]["caller"], "http")
        self.assertNotIn("private account", str(dashboard))
        self.assertNotIn("must not leak", str(dashboard))
        for item in dashboard["services"]:
            self.assertEqual(item["connection_last_modified_at"], "2026-08-27T01:00:00.000Z")
            self.assertEqual(item["oauth_client_last_modified_at"], "2026-08-27T02:00:00.000Z")

    def test_dashboard_flags_failed_read(self) -> None:
        connections = [{"service": service, "configured": True} for service in app.OPENCONNECTOR_MANAGED_ACTIONS]
        runs = [
            {"service": "gmail", "actionId": "gmail.get_profile", "completedAt": "2026-08-27T05:00:00.000Z", "ok": False, "errorCode": "oauth_token_refresh_failed"},
            {"service": "gmail", "actionId": "gmail.get_profile", "completedAt": "2026-08-27T04:00:00.000Z", "ok": True},
        ]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:01:00.000Z")), patch.object(app, "get_openconnector_connection_updates", side_effect=RuntimeError("docker unavailable")):
            dashboard = app.get_openconnector_dashboard()
        gmail = next(item for item in dashboard["services"] if item["service"] == "gmail")
        self.assertEqual(gmail["state"], "error")
        self.assertTrue(any(alert["service"] == "gmail" for alert in dashboard["alerts"]))
        self.assertEqual(gmail["last_read_at"], "2026-08-27T04:00:00.000Z")
        self.assertIsNone(gmail["connection_last_modified_at"])
        self.assertIsNone(gmail["oauth_client_last_modified_at"])


if __name__ == "__main__":
    unittest.main()
