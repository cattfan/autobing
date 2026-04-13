"""
CLI surface for normalized control-plane seams.
"""

from __future__ import annotations

import argparse
import json

from src.control_plane import (
    build_run_request,
    build_schedule_snapshot,
    build_schedule_update,
    apply_schedule_update,
)
from src.scheduler import Scheduler
from src.utils import load_settings, save_settings


def _coerce_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.control_plane_api")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_request = subparsers.add_parser("build-run-request")
    run_request.add_argument("--task", default="all")
    run_request.add_argument("--target-email", dest="target_emails", action="append", default=[])
    run_request.add_argument("--master-password", default="")

    schedule_snapshot = subparsers.add_parser("schedule-snapshot")
    schedule_snapshot.add_argument("--time", default=None)

    schedule_update = subparsers.add_parser("schedule-update")
    schedule_update.add_argument("--enabled", required=True)
    schedule_update.add_argument("--time", required=True)
    schedule_update.add_argument("--create-task", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "build-run-request":
        request = build_run_request(
            {
                "task": args.task,
                "target_emails": args.target_emails,
            },
            args.master_password,
        )
        print(
            json.dumps(
                {
                    "task": request.task,
                    "target_emails": list(request.target_emails),
                    "targeted": request.targeted,
                },
                ensure_ascii=False,
            )
        )
        return 0

    settings = load_settings()
    if args.command == "schedule-snapshot":
        if args.time:
            settings["schedule_time"] = args.time
        scheduler = Scheduler(settings)
        print(json.dumps(build_schedule_snapshot(settings, scheduler), ensure_ascii=False))
        return 0

    update = build_schedule_update(
        {
            "enabled": _coerce_bool(args.enabled),
            "time": args.time,
            "create_task": args.create_task,
        },
        str(settings.get("schedule_time", "08:00") or "08:00"),
    )
    settings = apply_schedule_update(settings, update)
    save_settings(settings)
    scheduler = Scheduler(settings)
    task_created = False
    if update.create_task:
        task_created = scheduler.setup_windows_task(update.time)
    print(
        json.dumps(
            {
                "status": "ok",
                "settings": {
                    "schedule_enabled": settings["schedule_enabled"],
                    "schedule_time": settings["schedule_time"],
                },
                "task_created": task_created,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
