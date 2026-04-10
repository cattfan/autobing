import unittest

from src.dashboard import app, state


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_dashboard_static_files_are_served(self):
        response = self.client.get("/")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Rewards Command Center", response.data)
            self.assertIn(b"app.js", response.data)
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
            self.assertIn("current_profile", payload)
            self.assertIn("ai", payload)
            self.assertEqual(payload["summary"]["running"], 1)
            self.assertEqual(payload["current_profile"]["email"], "yunat@example.com")
            self.assertEqual(payload["profiles"][0]["last_message"], "Starting Daily Set")
            self.assertEqual(payload["ai"]["model"], "cx/gpt-5.4")
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
