import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch, sentinel

from main import setup_schedule
from src.control_plane import (
    BotRunRequest,
    ScheduleUpdate,
    apply_schedule_update,
    build_run_request,
    build_run_state_reset,
    build_schedule_snapshot,
    build_schedule_update,
    build_windows_task_command,
)
from src.dashboard import app
from src.runner import get_runner_functions
from src.scheduler import Scheduler


class FrozenDateTime(datetime):
    current = datetime(2026, 4, 13, 7, 30, 0)

    @classmethod
    def now(cls):
        return cls.current


class SchedulerTests(unittest.TestCase):
    def test_build_windows_task_command_quotes_python_and_script_paths(self):
        command = build_windows_task_command(r"C:\Python\python.exe", r"C:\repo\main.py")

        self.assertEqual(command, r'"C:\Python\python.exe" "C:\repo\main.py" --auto')

    def test_setup_windows_task_returns_false_outside_windows(self):
        scheduler = Scheduler({"schedule_time": "08:00"})

        with patch("src.scheduler.os.name", "posix"):
            self.assertFalse(scheduler.setup_windows_task())

    def test_setup_windows_task_uses_single_windows_task_entrypoint(self):
        scheduler = Scheduler({"schedule_time": "09:45"})

        delete_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        create_result = SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("src.scheduler.os.name", "nt"), \
             patch("src.scheduler.sys.executable", r"C:\Python\python.exe"), \
             patch("src.scheduler.subprocess.run", side_effect=[delete_result, create_result]) as run_mock:
            self.assertTrue(scheduler.setup_windows_task())

        delete_call = run_mock.call_args_list[0]
        create_call = run_mock.call_args_list[1]

        self.assertEqual(
            delete_call.args[0],
            ["schtasks", "/delete", "/tn", "RewardsSearchAutomator", "/f"],
        )
        self.assertEqual(create_call.args[0][0:4], ["schtasks", "/create", "/tn", "RewardsSearchAutomator"])
        self.assertIn("--auto", create_call.args[0][5])
        self.assertIn(r'"C:\Python\python.exe"', create_call.args[0][5])
        self.assertIn("main.py", create_call.args[0][5])
        self.assertEqual(create_call.args[0][6:10], ["/sc", "DAILY", "/st", "09:45"])

    def test_check_task_status_parses_windows_task_output(self):
        scheduler = Scheduler({"schedule_time": "08:00"})
        status_output = "\n".join(
            [
                "TaskName: RewardsSearchAutomator",
                "Status: Ready",
                "Next Run Time: 4/13/2026 8:00:00 AM",
            ]
        )

        with patch("src.scheduler.os.name", "nt"), \
             patch(
                 "src.scheduler.subprocess.run",
                 return_value=SimpleNamespace(returncode=0, stdout=status_output, stderr=""),
             ):
            status = scheduler.check_task_status()

        self.assertEqual(status["TaskName"], "RewardsSearchAutomator")
        self.assertEqual(status["Status"], "Ready")
        self.assertEqual(status["Next Run Time"], "4/13/2026 8:00:00 AM")

    def test_get_next_run_time_stays_cached_until_reset(self):
        scheduler = Scheduler({"schedule_time": "08:00"})

        with patch("src.scheduler.datetime", FrozenDateTime), \
             patch("random.randint", return_value=0), \
             patch("random.uniform", return_value=0):
            first = scheduler.get_next_run_time()
            second = scheduler.get_next_run_time()
            scheduler.reset_schedule()
            third = scheduler.get_next_run_time()

        self.assertEqual(first, datetime(2026, 4, 13, 8, 0, 0))
        self.assertIs(first, second)
        self.assertEqual(third, datetime(2026, 4, 13, 8, 0, 0))

    def test_should_run_now_uses_five_minute_window(self):
        scheduler = Scheduler({"schedule_time": "08:00"})

        with patch("src.scheduler.datetime", FrozenDateTime):
            FrozenDateTime.current = datetime(2026, 4, 13, 8, 4, 59)
            self.assertTrue(scheduler.should_run_now())
            FrozenDateTime.current = datetime(2026, 4, 13, 8, 5, 1)
            self.assertFalse(scheduler.should_run_now())


class DashboardScheduleRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_schedule_get_route_uses_scheduler_seam(self):
        settings = {"schedule_enabled": True, "schedule_time": "09:30", "master_password_hash": ""}
        scheduler = Mock()
        scheduler.check_task_status.return_value = {"Status": "Ready"}
        scheduler.get_countdown.return_value = "1h 2m 3s"

        with patch("src.dashboard.load_settings", return_value=settings), \
             patch("src.scheduler.Scheduler", return_value=scheduler):
            response = self.client.get("/api/schedule")

        try:
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["time"], "09:30")
            self.assertEqual(payload["windows_task_exists"], {"Status": "Ready"})
            self.assertEqual(payload["countdown"], "1h 2m 3s")
        finally:
            response.close()

    def test_schedule_post_route_persists_settings_and_creates_task_only_when_requested(self):
        base_settings = {"master_password_hash": ""}
        scheduler = Mock()

        with patch("src.dashboard.load_settings", return_value=dict(base_settings)), \
             patch("src.dashboard.save_settings") as save_settings, \
             patch("src.scheduler.Scheduler", return_value=scheduler):
            response = self.client.post(
                "/api/schedule",
                json={"enabled": True, "time": "10:15", "create_task": True},
            )

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["status"], "ok")
        finally:
            response.close()

        save_settings.assert_called_once_with(
            {"master_password_hash": "", "schedule_enabled": True, "schedule_time": "10:15"}
        )
        scheduler.setup_windows_task.assert_called_once_with("10:15")

    def test_schedule_post_route_skips_task_creation_when_not_requested(self):
        scheduler = Mock()

        with patch("src.dashboard.load_settings", return_value={"master_password_hash": ""}), \
             patch("src.dashboard.save_settings"), \
             patch("src.scheduler.Scheduler", return_value=scheduler):
            response = self.client.post(
                "/api/schedule",
                json={"enabled": False, "time": "08:00", "create_task": False},
            )

        try:
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()

        scheduler.setup_windows_task.assert_not_called()


class ControlPlaneSeamTests(unittest.TestCase):
    def test_build_run_request_normalizes_invalid_task_to_all(self):
        request = build_run_request(
            {"task": "unknown", "target_emails": ["a@example.com", "  ", None]},
            "secret",
        )

        self.assertEqual(
            request,
            BotRunRequest(task="all", password="secret", target_emails=("a@example.com",)),
        )

    def test_build_run_state_reset_returns_clean_control_plane_state(self):
        state = build_run_state_reset("searches", "cx/gpt-5.4")

        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_task"], "searches")
        self.assertEqual(state["logs"], [])
        self.assertEqual(state["accounts"], {})
        self.assertEqual(state["ai"]["model"], "cx/gpt-5.4")

    def test_build_schedule_update_and_apply_schedule_update_preserve_other_settings(self):
        update = build_schedule_update({"enabled": True, "time": "11:20", "create_task": True}, "08:00")
        merged = apply_schedule_update({"master_password_hash": "", "schedule_time": "08:00"}, update)

        self.assertEqual(update, ScheduleUpdate(enabled=True, time="11:20", create_task=True))
        self.assertEqual(
            merged,
            {"master_password_hash": "", "schedule_time": "11:20", "schedule_enabled": True},
        )

    def test_build_schedule_snapshot_uses_scheduler_seam(self):
        scheduler = Mock()
        scheduler.check_task_status.return_value = {"Status": "Ready"}
        scheduler.get_countdown.return_value = "2h 0m 0s"

        snapshot = build_schedule_snapshot(
            {"schedule_enabled": True, "schedule_time": "10:00"},
            scheduler,
        )

        self.assertEqual(
            snapshot,
            {
                "enabled": True,
                "time": "10:00",
                "windows_task_exists": {"Status": "Ready"},
                "countdown": "2h 0m 0s",
            },
        )


class RunnerSeamTests(unittest.TestCase):
    def test_runner_function_map_exposes_dashboard_execution_seams(self):
        with patch("src.dashboard._run_bot_thread", sentinel.run_bot_thread), \
             patch("src.dashboard._run_bot_async", sentinel.run_bot_async), \
             patch("src.dashboard._start_gpm_profile", sentinel.start_gpm_profile), \
             patch("src.dashboard._stop_gpm_profile", sentinel.stop_gpm_profile), \
             patch("src.dashboard._open_account_context", AsyncMock()), \
             patch("src.dashboard._persist_storage_state", AsyncMock()), \
             patch("src.dashboard._update_account_state", sentinel.update_account_state), \
             patch("src.dashboard.add_log", sentinel.add_log), \
             patch("src.dashboard.state", {"status": "idle"}):
            functions = get_runner_functions()

        self.assertIs(functions["run_bot_thread"], sentinel.run_bot_thread)
        self.assertIs(functions["run_bot_async"], sentinel.run_bot_async)
        self.assertIs(functions["start_gpm_profile"], sentinel.start_gpm_profile)
        self.assertIs(functions["stop_gpm_profile"], sentinel.stop_gpm_profile)
        self.assertIs(functions["update_account_state"], sentinel.update_account_state)
        self.assertIs(functions["add_log"], sentinel.add_log)
        self.assertEqual(functions["state"]["status"], "idle")


class MainScheduleWorkflowTests(unittest.TestCase):
    def test_setup_schedule_persists_new_time_and_creates_windows_task(self):
        settings = {"schedule_enabled": False, "schedule_time": "08:00"}
        scheduler = Mock()
        scheduler.get_countdown.return_value = "Not scheduled"
        scheduler.check_task_status.return_value = None
        scheduler.setup_windows_task.return_value = True

        with patch("main.Scheduler", return_value=scheduler), \
             patch("main.Prompt.ask", side_effect=["1", "10:45"]), \
             patch("main.save_settings") as save_settings, \
             patch("main.console.print"):
            setup_schedule(settings)

        self.assertTrue(settings["schedule_enabled"])
        self.assertEqual(settings["schedule_time"], "10:45")
        save_settings.assert_called_once_with(settings)
        scheduler.setup_windows_task.assert_called_once_with("10:45")

    def test_setup_schedule_disables_saved_schedule_after_successful_removal(self):
        settings = {"schedule_enabled": True, "schedule_time": "08:00"}
        scheduler = Mock()
        scheduler.get_countdown.return_value = "12h 0m 0s"
        scheduler.check_task_status.return_value = {"Status": "Ready"}
        scheduler.remove_windows_task.return_value = True

        with patch("main.Scheduler", return_value=scheduler), \
             patch("main.Prompt.ask", return_value="2"), \
             patch("main.save_settings") as save_settings, \
             patch("main.console.print"):
            setup_schedule(settings)

        self.assertFalse(settings["schedule_enabled"])
        save_settings.assert_called_once_with(settings)
        scheduler.remove_windows_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
