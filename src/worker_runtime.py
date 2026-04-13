"""
Background Python worker runtime for the future Rust control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.dashboard import _run_bot_async, start_state_sync_for_worker


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_event(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _resolve_password(secret_ref: str | None) -> tuple[str, str | None]:
    direct = os.environ.get("REWARDS_BOT_PASSWORD", "").strip()
    if direct:
        return direct, None

    ref = str(secret_ref or "").strip()
    if not ref:
        return "", "REWARDS_BOT_PASSWORD is required for live worker jobs"

    if ref.startswith("env:"):
        env_name = ref.split(":", 1)[1].strip()
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, None
        return "", f"secret_ref env variable is missing: {env_name}"

    if ref.startswith("file:"):
        file_path = ref.split(":", 1)[1].strip()
        if not file_path:
            return "", "secret_ref file path is empty"
        path = Path(file_path)
        if not path.exists():
            return "", f"secret_ref file does not exist: {file_path}"
        value = path.read_text(encoding="utf-8-sig").strip()
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return value, None

    return "", f"unsupported secret_ref scheme: {ref}"


async def _run_job(job_payload: dict, state_file: Path, events_file: Path) -> int:
    job_id = str(job_payload.get("job_id", "") or "job-local").strip() or "job-local"
    correlation_id = str(job_payload.get("correlation_id", "") or "").strip() or None
    task = str(job_payload.get("task", "all") or "all").strip() or "all"
    secret_ref = str(job_payload.get("secret_ref", "") or "").strip() or None
    target_emails = [
        email.strip()
        for email in job_payload.get("target_emails", [])
        if isinstance(email, str) and email.strip()
    ]

    def write_state(status: str, **extra: object) -> None:
        payload = {
            "protocol_version": "0.1",
            "worker_kind": "python-sidecar",
            "job_id": job_id,
            "status": status,
            "task": task,
            "target_emails": target_emails,
            "updated_at": _utcnow(),
            "correlation_id": correlation_id,
        }
        payload.update(extra)
        _write_json(state_file, payload)

    def emit(event_type: str, **extra: object) -> None:
        payload = {
            "protocol_version": "0.1",
            "job_id": job_id,
            "event_type": event_type,
            "timestamp": _utcnow(),
            "correlation_id": correlation_id,
        }
        payload.update(extra)
        _append_event(events_file, payload)

    write_state("running", pid=os.getpid(), started_at=_utcnow())
    emit("job_running", pid=os.getpid(), task=task)

    password, password_error = _resolve_password(secret_ref)
    if password_error:
        message = password_error
        write_state("failed", error=message, completed_at=_utcnow())
        emit("job_failed", error=message)
        return 1

    cancel_marker = state_file.parent / "cancel.requested"
    if cancel_marker.exists():
        message = "job cancelled before execution"
        write_state("cancelled", completed_at=_utcnow(), error=message)
        emit("job_cancelled", error=message)
        return 0

    try:
        start_state_sync_for_worker()
        await _run_bot_async(task, password, target_emails)
        if cancel_marker.exists():
            message = "job cancelled during execution"
            write_state("cancelled", completed_at=_utcnow(), error=message)
            emit("job_cancelled", error=message)
            return 0

        write_state("completed", completed_at=_utcnow())
        emit("job_completed")
        return 0
    except Exception as exc:
        message = str(exc)
        write_state("failed", error=message, completed_at=_utcnow())
        emit("job_failed", error=message)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.worker_runtime")
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--events-file", required=True)
    args = parser.parse_args(argv)

    job_payload = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
    return asyncio.run(
        _run_job(
            job_payload,
            Path(args.state_file),
            Path(args.events_file),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
