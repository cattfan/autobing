"""
Task scheduling: daily auto-run and Windows Task Scheduler integration.
"""

from __future__ import annotations
import os
import sys
import subprocess
from datetime import datetime, timedelta
from typing import Optional

import schedule

from src.control_plane import WINDOWS_TASK_NAME, build_windows_task_command
from src.utils import logger


class Scheduler:
    """Manages scheduled execution of the Rewards bot."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.schedule_time = settings.get("schedule_time", "08:00")
        self.is_running = False
        self._cached_next_run: Optional[datetime] = None

    def setup_windows_task(self, time_str: Optional[str] = None) -> bool:
        """
        Create a Windows Task Scheduler task to run the bot daily.

        Args:
            time_str: Time in HH:MM format (defaults to settings)

        Returns:
            True if task was created successfully
        """
        if os.name != "nt":
            logger.error("Windows Task Scheduler is only available on Windows")
            return False

        run_time = time_str or self.schedule_time
        python_path = sys.executable
        script_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "main.py")
        )

        task_name = WINDOWS_TASK_NAME

        # Delete existing task if any
        try:
            subprocess.run(
                ["schtasks", "/delete", "/tn", task_name, "/f"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

        # Create new scheduled task
        cmd = [
            "schtasks", "/create",
            "/tn", task_name,
            "/tr", build_windows_task_command(python_path, script_path),
            "/sc", "DAILY",
            "/st", run_time,
            "/f",  # Force overwrite
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )

            if result.returncode == 0:
                logger.info(
                    f"✅ Windows Task created: '{task_name}' at {run_time} daily"
                )
                return True
            else:
                logger.error(f"Failed to create task: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error("Task creation timed out")
            return False
        except Exception as e:
            logger.error(f"Task creation error: {e}")
            return False

    def remove_windows_task(self) -> bool:
        """Remove the Windows Task Scheduler task."""
        if os.name != "nt":
            return False

        try:
            result = subprocess.run(
                ["schtasks", "/delete", "/tn", WINDOWS_TASK_NAME, "/f"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info("Windows Task removed")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to remove task: {e}")
            return False

    def check_task_status(self) -> Optional[dict]:
        """Check if Windows scheduled task exists and its status."""
        if os.name != "nt":
            return None

        try:
            result = subprocess.run(
                [
                    "schtasks", "/query", "/tn", WINDOWS_TASK_NAME,
                    "/fo", "LIST", "/v",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                info = {}
                for line in result.stdout.split("\n"):
                    if ":" in line:
                        key, _, value = line.partition(":")
                        info[key.strip()] = value.strip()
                return info

            return None

        except Exception:
            return None

    def get_next_run_time(self) -> Optional[datetime]:
        """Calculate next run time (cached so countdown is stable)."""
        if self._cached_next_run and self._cached_next_run > datetime.now():
            return self._cached_next_run

        try:
            hour, minute = map(int, self.schedule_time.split(":"))
            now = datetime.now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if next_run <= now:
                next_run += timedelta(days=1)

            # Add random variance: ±30 minutes (anti-detection)
            import random
            variance_minutes = random.randint(-30, 30)
            next_run += timedelta(minutes=variance_minutes)

            # Weekend: add extra 1-2 hours (people wake up later)
            if next_run.weekday() >= 5:  # Saturday=5, Sunday=6
                next_run += timedelta(hours=random.uniform(1, 2))

            self._cached_next_run = next_run
            return next_run

        except (ValueError, Exception):
            return None

    def reset_schedule(self) -> None:
        """Clear cached next run time (call after bot finishes running)."""
        self._cached_next_run = None

    def get_countdown(self) -> str:
        """Get countdown string to next run."""
        next_run = self.get_next_run_time()
        if not next_run:
            return "Not scheduled"

        diff = next_run - datetime.now()
        hours, remainder = divmod(int(diff.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)

        return f"{hours}h {minutes}m {seconds}s"

    def should_run_now(self) -> bool:
        """Check if the bot should run now (within 5 min window of schedule)."""
        try:
            hour, minute = map(int, self.schedule_time.split(":"))
            now = datetime.now()
            scheduled = now.replace(hour=hour, minute=minute, second=0)

            diff = abs((now - scheduled).total_seconds())
            return diff < 300  # Within 5 minutes

        except (ValueError, Exception):
            return False
