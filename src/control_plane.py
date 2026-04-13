"""
Control-plane seams for Phase 0/1 Rust distribution migration.

This module keeps product-state shaping logic separate from the
browser-automation runtime so Phase 1 can normalize the current Python
control plane without changing execution behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_RUN_TASK = "all"
SUPPORTED_RUN_TASKS = {
    "all",
    "searches",
    "daily",
    "punch",
    "promos",
    "bootstrap",
}
WINDOWS_TASK_NAME = "RewardsSearchAutomator"


def _normalize_target_emails(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        email = item.strip()
        if email:
            normalized.append(email)
    return tuple(normalized)


@dataclass(frozen=True)
class BotRunRequest:
    task: str
    password: str
    target_emails: tuple[str, ...] = ()

    @property
    def targeted(self) -> bool:
        return bool(self.target_emails)


def build_run_request(data: Mapping[str, Any] | None, master_password: str) -> BotRunRequest:
    payload = data or {}
    requested_task = str(payload.get("task", DEFAULT_RUN_TASK) or DEFAULT_RUN_TASK).strip().lower()
    task = requested_task if requested_task in SUPPORTED_RUN_TASKS else DEFAULT_RUN_TASK
    return BotRunRequest(
        task=task,
        password=master_password,
        target_emails=_normalize_target_emails(payload.get("target_emails", [])),
    )


def build_run_state_reset(task: str, ai_model: str) -> dict[str, Any]:
    return {
        "status": "running",
        "current_task": task,
        "current_account": "",
        "progress": 0,
        "progress_total": 0,
        "total_points": 0,
        "logs": [],
        "account_logs": {},
        "accounts": {},
        "ai": {
            "active": False,
            "last_update": "",
            "last_event": "Đã khởi tạo phiên chạy mới.",
            "task": task,
            "model": ai_model,
            "last_level": "info",
        },
    }


@dataclass(frozen=True)
class ScheduleUpdate:
    enabled: bool
    time: str
    create_task: bool = False


def build_schedule_update(data: Mapping[str, Any] | None, current_time: str = "08:00") -> ScheduleUpdate:
    payload = data or {}
    time_value = str(payload.get("time", current_time) or current_time).strip() or current_time
    return ScheduleUpdate(
        enabled=bool(payload.get("enabled", False)),
        time=time_value,
        create_task=bool(payload.get("create_task", False)),
    )


def apply_schedule_update(settings: Mapping[str, Any], update: ScheduleUpdate) -> dict[str, Any]:
    merged = dict(settings)
    merged["schedule_enabled"] = update.enabled
    merged["schedule_time"] = update.time
    return merged


def build_schedule_snapshot(settings: Mapping[str, Any], scheduler) -> dict[str, Any]:
    return {
        "enabled": bool(settings.get("schedule_enabled", False)),
        "time": str(settings.get("schedule_time", "08:00") or "08:00"),
        "windows_task_exists": scheduler.check_task_status(),
        "countdown": scheduler.get_countdown(),
    }


def build_windows_task_command(python_path: str, script_path: str) -> str:
    return f'"{python_path}" "{script_path}" --auto'
