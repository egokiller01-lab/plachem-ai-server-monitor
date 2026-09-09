import time
import unittest
from unittest.mock import patch

import app


class OpenConnectorDashboardTests(unittest.TestCase):
    @staticmethod
    def managed_connections(**extra):
        return [
            {"service": source, "configured": True, **extra}
            for source in app.OPENCONNECTOR_MANAGED_CONNECTION_SERVICES.values()
        ]

    @staticmethod
    def managed_runs(completed_at, **extra):
        return [
            {
                "service": app.OPENCONNECTOR_MANAGED_CONNECTION_SERVICES[service],
                "actionId": action,
                "completedAt": completed_at,
                **extra,
            }
            for service, action in app.OPENCONNECTOR_MANAGED_ACTIONS.items()
        ]

    def test_dashboard_reports_real_run_health_without_secrets(self) -> None:
        now_iso = "2026-08-27T05:00:00.000Z"
        connections = self.managed_connections(authType="api_key", profile={"displayName": "private account"})
        runs = self.managed_runs(now_iso, caller="http", durationMs=100, ok=True,
                                 outputSummary={"token": "must not leak"})

        def fake_get(path: str, timeout: float = 2.5):
            if path == "/api/connections":
                return connections
            if path == "/api/runtime-tokens":
                return []
            return {"items": runs}

        updates = {service: {"connection_last_modified_at": "2026-08-27T01:00:00.000Z", "oauth_client_last_modified_at": "2026-08-27T02:00:00.000Z"} for service in app.OPENCONNECTOR_MANAGED_CONNECTION_SERVICES.values()}
        with patch.object(app, "_openconnector_admin_get", side_effect=fake_get), patch.object(time, "time", return_value=app._parse_iso_timestamp(now_iso) + 60), patch.object(app, "get_openconnector_connection_updates", return_value=updates), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()

        self.assertEqual(dashboard["summary"]["managed"], 13)
        self.assertTrue(all(s["metadata_source"] == "openconnector_live_api+local_read_only_db" for s in dashboard["services"]))
        self.assertTrue(all(s["metadata_collected_at"] == "2026-08-27T05:01:00.000Z" for s in dashboard["services"]))
        self.assertTrue(all(s["auth_type"] == "api_key" for s in dashboard["services"]))
        self.assertTrue(all(s["state_reason"] == "recent_authenticated_use" for s in dashboard["services"]))
        self.assertTrue(all(s["credential_expires_at"] is None for s in dashboard["services"]))
        self.assertEqual(dashboard["summary"]["healthy"], 13)
        self.assertEqual(dashboard["summary"]["success_rate_24h"], 100.0)
        self.assertEqual(dashboard["summary"]["provider_registration"], 4)
        self.assertEqual(len(dashboard["service_usage_24h"]), 13)
        self.assertEqual(dashboard["caller_usage_24h"][0]["caller"], "http")
        self.assertNotIn("private account", str(dashboard))
        self.assertNotIn("must not leak", str(dashboard))
        for item in dashboard["services"]:
            self.assertEqual(item["connection_last_modified_at"], "2026-08-27T01:00:00.000Z")
            self.assertEqual(item["oauth_client_last_modified_at"], "2026-08-27T02:00:00.000Z")

    def test_dashboard_flags_failed_read(self) -> None:
        connections = self.managed_connections()
        runs = [
            {"service": "generic_imap", "actionId": "generic_imap.list_folders", "completedAt": "2026-08-27T05:00:00.000Z", "ok": False, "errorCode": "authentication_failed"},
            {"service": "generic_imap", "actionId": "generic_imap.list_folders", "completedAt": "2026-08-27T04:00:00.000Z", "ok": True},
        ]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:01:00.000Z")), patch.object(app, "get_openconnector_connection_updates", side_effect=RuntimeError("docker unavailable")), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        gmail = next(item for item in dashboard["services"] if item["service"] == "gmail")
        self.assertEqual(gmail["state"], "error")
        self.assertEqual(gmail["state_reason"], "authentication_failed")
        self.assertTrue(any(alert["service"] == "gmail" for alert in dashboard["action_queue"]))
        self.assertEqual(gmail["last_verified_at"], "2026-08-27T04:00:00.000Z")
        self.assertIsNone(gmail["connection_last_modified_at"])
        self.assertIsNone(gmail["oauth_client_last_modified_at"])

    def test_legacy_gmail_oauth_does_not_distort_canonical_inventory(self) -> None:
        connections = self.managed_connections() + [
            {"service": "gmail", "configured": True, "authType": "oauth2"},
        ]
        runs = self.managed_runs("2026-08-27T05:00:00.000Z", ok=True) + [
            {"service": "gmail", "actionId": "gmail.get_profile", "completedAt": "2026-08-27T05:00:01.000Z", "ok": False, "errorCode": "oauth_token_refresh_failed"},
        ]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:01:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        gmail = next(item for item in dashboard["services"] if item["service"] == "gmail")
        self.assertEqual(dashboard["summary"]["managed"], 13)
        self.assertEqual(dashboard["summary"]["healthy"], 13)
        self.assertEqual(gmail["state"], "healthy")
        self.assertEqual(gmail["last_verified_action"], "generic_imap.list_folders")
        self.assertFalse(any(item["service"] == "gmail" for item in dashboard["action_queue"]))

    def test_dashboard_state_reason_stale_verification(self) -> None:
        connections = [{"service": service, "configured": True, "authType": "oauth"} for service in app.OPENCONNECTOR_MANAGED_ACTIONS]
        runs = [
            {"service": service, "actionId": action, "caller": "http", "completedAt": "2026-08-20T05:00:00.000Z", "ok": True, "durationMs": 80}
            for service, action in app.OPENCONNECTOR_MANAGED_ACTIONS.items()
        ]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:00:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        self.assertTrue(all(s["state_reason"] == "authenticated_use_stale" for s in dashboard["services"]))
        self.assertTrue(all(s["credential_expires_at"] is None for s in dashboard["services"]))

    def test_dashboard_state_reason_no_run_record(self) -> None:
        connections = [{"service": service, "configured": True, "authType": "oauth"} for service in app.OPENCONNECTOR_MANAGED_ACTIONS]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": []}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:00:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        self.assertTrue(all(s["state_reason"] == "first_verification_required" for s in dashboard["services"]))
        self.assertTrue(all(s["state"] == "attention" for s in dashboard["services"]))

    def test_dashboard_state_reason_not_configured(self) -> None:
        connections = [{"service": service, "configured": False} for service in app.OPENCONNECTOR_MANAGED_ACTIONS]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": []}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:00:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        self.assertTrue(all(s["state_reason"] == "not_configured" for s in dashboard["services"]))
        self.assertTrue(all(s["auth_type"] is None for s in dashboard["services"]))
        self.assertTrue(all(s["credential_expires_at"] is None for s in dashboard["services"]))

    def test_dashboard_exposes_oauth_expiry_without_credentials(self) -> None:
        expires_at = "2026-08-27T06:00:00.000Z"
        connections = self.managed_connections(
            authType="oauth2", credentialExpiresAt=expires_at,
            refreshable=True, authHealth="refreshable",
            profile={"accessToken": "must-not-leak"},
        )
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": []}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:00:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        self.assertEqual(dashboard["summary"]["oauth_auto_refresh"], 13)
        self.assertTrue(all(item["expiration_status"] == "due_2h" for item in dashboard["services"]))
        self.assertNotIn("must-not-leak", str(dashboard))

    def test_any_successful_service_action_is_real_verification(self) -> None:
        connections = [{"service": "googledrive", "configured": True, "authType": "oauth2", "refreshable": True}]
        runs = [{"service": "googledrive", "actionId": "googledrive.files.get", "completedAt": "2026-08-27T05:00:00.000Z", "ok": True}]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:01:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            dashboard = app.get_openconnector_dashboard()
        item = dashboard["services"][0]
        self.assertEqual(item["state"], "healthy")
        self.assertEqual(item["last_verified_action"], "googledrive.files.get")

    def test_invalid_input_does_not_become_authentication_failure(self) -> None:
        connections = [{"service": "supabase", "configured": True, "authType": "api_key"}]
        runs = [
            {"service": "supabase", "actionId": "supabase.run_read_only_query", "completedAt": "2026-08-27T05:00:00.000Z", "ok": False, "errorCode": "invalid_input"},
            {"service": "supabase", "actionId": "supabase.get_project", "completedAt": "2026-08-27T04:00:00.000Z", "ok": True},
        ]
        with patch.object(app, "_openconnector_admin_get", side_effect=[connections, [], {"items": runs}]), patch.object(time, "time", return_value=app._parse_iso_timestamp("2026-08-27T05:01:00.000Z")), patch.object(app, "get_openconnector_connection_updates", return_value={}), patch.object(app, "get_openconnector_lease_review", return_value={"items": [], "error": None}):
            item = app.get_openconnector_dashboard()["services"][0]
        self.assertEqual(item["state"], "healthy")
        self.assertEqual(item["auth_failures_24h"], 0)

    def test_lease_review_is_not_reported_as_actual_expiration(self) -> None:
        payload = {
            "tokens": {
                "token-id": {
                    "tokenName": "OpenClaw agent:test broker token",
                    "leases": {
                        "request-id": {
                            "actions": ["googledrive.files.get"],
                            "connections": ["connection-id"],
                            "createdAt": "2026-08-26T05:00:00.000Z",
                        }
                    },
                }
            }
        }
        with patch.object(app, "OPENCONNECTOR_LEASE_LEDGER") as ledger:
            ledger.read_text.return_value = __import__("json").dumps(payload)
            result = app.get_openconnector_lease_review(app._parse_iso_timestamp("2026-08-27T06:00:00.000Z"))
        item = result["items"][0]
        self.assertEqual(item["review_status"], "overdue")
        self.assertEqual(item["access_class"], "read")
        self.assertEqual(item["expiration_kind"], "review_deadline")
        self.assertNotIn("actions", item)
        self.assertNotIn("connections", item)


if __name__ == "__main__":
    unittest.main()
