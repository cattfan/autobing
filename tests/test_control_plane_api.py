import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.control_plane_api import cli


class ControlPlaneApiCliTests(unittest.TestCase):
    def test_build_run_request_supports_direct_flags(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("sys.stdout", stream):
                exit_code = cli(
                    [
                        "build-run-request",
                        "--task",
                        "searches",
                        "--target-email",
                        "one@example.com",
                        "--target-email",
                        "two@example.com",
                        "--master-password",
                        "secret",
                    ]
                )
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["task"], "searches")
        self.assertEqual(payload["target_emails"], ["one@example.com", "two@example.com"])
        self.assertTrue(payload["targeted"])

    def test_schedule_snapshot_uses_scheduler_seam(self):
        scheduler = Mock()
        scheduler.check_task_status.return_value = {"Status": "Ready"}
        scheduler.get_countdown.return_value = "10m 0s"

        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("src.control_plane_api.load_settings", return_value={"schedule_enabled": True, "schedule_time": "09:00"}), \
                 patch("src.control_plane_api.Scheduler", return_value=scheduler), \
                 patch("sys.stdout", stream):
                exit_code = cli(["schedule-snapshot"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["time"], "09:00")
        self.assertEqual(payload["windows_task_exists"], {"Status": "Ready"})

    def test_schedule_update_persists_settings_and_can_skip_task_creation(self):
        scheduler = Mock()

        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("src.control_plane_api.load_settings", return_value={"schedule_enabled": False, "schedule_time": "08:00"}), \
                 patch("src.control_plane_api.save_settings") as save_settings, \
                 patch("src.control_plane_api.Scheduler", return_value=scheduler), \
                 patch("sys.stdout", stream):
                exit_code = cli(["schedule-update", "--enabled", "true", "--time", "10:30"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["settings"]["schedule_time"], "10:30")
        self.assertFalse(payload["task_created"])
        scheduler.setup_windows_task.assert_not_called()
        save_settings.assert_called_once()
