import unittest
from unittest.mock import patch

from src.dashboard import app, state, _state_lock
from src.crypto import hash_password


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.load_settings_patcher = patch("src.dashboard.load_settings", return_value={"master_password_hash": ""})
        self.load_settings_patcher.start()
        self.addCleanup(self.load_settings_patcher.stop)
        state["status"] = "idle"
        state["current_account"] = ""
        state["current_task"] = ""
        state["progress"] = 0
        state["progress_total"] = 0
        state["logs"] = []
        state["account_logs"] = {}
        state["accounts"] = {}
        state["last_run"] = None
        state["total_points"] = 0
        state["ai"] = {
            "active": False,
            "last_update": "",
            "last_event": "",
            "task": "",
            "model": "",
            "last_level": "",
        }

    def test_dashboard_static_files_are_served(self):
        response = self.client.get("/")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Rewards Command Center", response.data)
            self.assertIn(b"app.js", response.data)
        finally:
            response.close()

    def test_dashboard_overview_endpoint_returns_rollup(self):
        state["accounts"] = {
            "acct:abc": {
                "id": "user@example.com",
                "email": "user@example.com",
                "display_name": "user***",
                "status": "done",
                "points": 1200,
                "earned_today": 50,
                "earned_yesterday": 40,
                "delta_vs_yesterday": 10,
                "trend": "up",
                "tracks": {},
            }
        }
        state["account_logs"] = {}

        response = self.client.get("/api/dashboard/overview")
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertIn("overview", payload)
            self.assertEqual(payload["overview"]["earned_today"], 50)
            self.assertEqual(payload["overview"]["earned_yesterday"], 40)
        finally:
            response.close()

    def test_unknown_api_route_returns_json_404_instead_of_dashboard_shell(self):
        response = self.client.get("/api/not-a-real-route")
        try:
            self.assertEqual(response.status_code, 404)
            payload = response.get_json()
            self.assertEqual(payload["error"], "Not found")
        finally:
            response.close()

    @patch("src.dashboard.load_settings", return_value={"master_password_hash": hash_password("secret")})
    def test_protected_api_requires_dashboard_auth_when_password_is_configured(self, _load_settings):
        response = self.client.get("/api/status")
        try:
            self.assertEqual(response.status_code, 401)
            payload = response.get_json()
            self.assertEqual(payload["code"], "auth_required")
        finally:
            response.close()

    @patch("src.dashboard.load_settings", return_value={"master_password_hash": hash_password("secret")})
    def test_dashboard_auth_unlocks_protected_api(self, _load_settings):
        check = self.client.get("/api/auth/check")
        try:
            self.assertEqual(check.status_code, 200)
            payload = check.get_json()
            self.assertTrue(payload["required"])
            self.assertFalse(payload["authenticated"])
        finally:
            check.close()

        login = self.client.post("/api/auth", json={"password": "secret"})
        try:
            self.assertEqual(login.status_code, 200)
        finally:
            login.close()

        response = self.client.get("/api/status")
        try:
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()

    def test_run_route_rejects_second_launch_while_first_run_is_active(self):
        first = self.client.post("/api/run", json={"task": "all"})
        try:
            self.assertEqual(first.status_code, 200)
            payload = first.get_json()
            self.assertEqual(payload["status"], "started")
        finally:
            first.close()

        second = self.client.post("/api/run", json={"task": "all"})
        try:
            self.assertEqual(second.status_code, 409)
            payload = second.get_json()
            self.assertEqual(payload["error"], "Bot is already running")
        finally:
            second.close()

        with _state_lock:
            state["status"] = "idle"
            state["job_id"] = ""
            state["current_task"] = ""
            state["current_account"] = ""
            state["progress"] = 0
            state["progress_total"] = 0
            state["accounts"] = {}
            state["account_logs"] = {}
            state["logs"] = []
            state["total_points"] = 0
            state["ai"] = {}
            state["last_run"] = None
            state["streak"] = 0
            state["status"] = "idle"

    def test_dashboard_history_endpoint_returns_snapshots(self):
        with patch("src.dashboard._recent_account_snapshots", return_value=[{"date": "2026-04-11", "earned_today": 50}]):
            response = self.client.get("/api/dashboard/accounts/acct:abc/history?days=7")
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["account_key"], "acct:abc")
            self.assertEqual(len(payload["history"]), 1)
        finally:
            response.close()

    def test_dashboard_account_logs_endpoint_returns_live_logs_without_date(self):
        state["account_logs"] = {"acct:abc": [{"time": "12:00:00", "level": "info", "message": "hello"}]}
        response = self.client.get("/api/dashboard/accounts/acct:abc/logs")
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["account_key"], "acct:abc")
            self.assertEqual(payload["logs"][0]["message"], "hello")
        finally:
            response.close()

        js_response = self.client.get("/app.js")
        try:
            self.assertEqual(js_response.status_code, 200)
            self.assertIn(b"const API = location.origin;", js_response.data)
        finally:
            js_response.close()

        css_response = self.client.get("/style.css")
        try:
            self.assertEqual(css_response.status_code, 200)
            self.assertIn(b".profile-board", css_response.data)
        finally:
            css_response.close()

        overview_module = self.client.get("/modules/overview-panels.js")
        try:
            self.assertEqual(overview_module.status_code, 200)
            self.assertIn(b"AutoBingOverviewPanels", overview_module.data)
        finally:
            overview_module.close()

        profile_module = self.client.get("/modules/profile-surfaces.js")
        try:
            self.assertEqual(profile_module.status_code, 200)
            self.assertIn(b"AutoBingProfileSurfaces", profile_module.data)
        finally:
            profile_module.close()

    def test_status_endpoint_exposes_profiles_summary_and_ai(self):
        state["status"] = "running"
        state["current_account"] = "yunat***"
        state["current_task"] = "Daily Set"
        state["progress"] = 1
        state["progress_total"] = 3
        state["accounts"] = {
            "yunat***": {
                "id": "yunat@example.com",
                "email": "yunat@example.com",
                "display_name": "yunat***",
                "status": "running",
                "task": "Daily Set",
                "progress": 1,
                "progress_total": 3,
                "points": 775,
                "last_message": "Starting Daily Set",
                "last_level": "info",
                "updated_at": "2026-04-06T12:00:00",
                "earned_today": 42,
                "earned_yesterday": 30,
                "delta_vs_yesterday": 12,
                "trend": "up",
                "tracks": {
                    "daily_set": {
                        "label": "Daily Set",
                        "current": 1,
                        "max": 3,
                        "percent": 33,
                        "status": "running",
                        "detail": "1 / 3",
                    }
                },
            }
        }
        state["account_logs"] = {
            "yunat***": [{"time": "12:00:00", "level": "info", "message": "Starting Daily Set"}]
        }
        state["ai"] = {
            "active": True,
            "last_event": "Bắt đầu xử lý",
            "task": "Daily Set",
            "model": "cx/gpt-5.4",
            "last_level": "info",
        }

        response = self.client.get("/api/status")
        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()

            self.assertIn("profiles", payload)
            self.assertIn("summary", payload)
            self.assertIn("overview", payload)
            self.assertIn("current_profile", payload)
            self.assertIn("ai", payload)
            self.assertEqual(payload["summary"]["running"], 1)
            self.assertEqual(payload["current_profile"]["email"], "yunat@example.com")
            self.assertEqual(payload["profiles"][0]["last_message"], "Starting Daily Set")
            self.assertEqual(payload["profiles"][0]["earned_today"], 42)
            self.assertIn("daily_set", payload["profiles"][0]["tracks"])
            self.assertEqual(payload["ai"]["model"], "cx/gpt-5.4")
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
