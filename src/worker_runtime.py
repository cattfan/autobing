"""
Background Python worker runtime for the future Rust control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import traceback
from contextlib import suppress
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
        if env_name == "REWARDS_BOT_PASSWORD":
            return "", None
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
        run_summary = await _run_bot_async(task, password, target_emails)
        if cancel_marker.exists():
            message = "job cancelled during execution"
            write_state("cancelled", completed_at=_utcnow(), error=message)
            emit("job_cancelled", error=message)
            return 0

        overall_complete = bool(run_summary.get("overall_complete", False))
        accounts_total = int(run_summary.get("accounts_total", 0) or 0)
        accounts_completed = int(run_summary.get("accounts_completed", 0) or 0)
        accounts_incomplete = int(run_summary.get("accounts_incomplete", 0) or 0)
        accounts_failed = int(run_summary.get("accounts_failed", 0) or 0)
        accounts_payload = run_summary.get("accounts", {})

        if overall_complete:
            write_state(
                "completed",
                completed_at=_utcnow(),
                accounts_total=accounts_total,
                accounts_completed=accounts_completed,
                accounts_incomplete=accounts_incomplete,
                accounts_failed=accounts_failed,
                accounts=accounts_payload,
            )
            emit(
                "job_completed",
                accounts_total=accounts_total,
                accounts_completed=accounts_completed,
                accounts_incomplete=accounts_incomplete,
                accounts_failed=accounts_failed,
            )
            return 0

        write_state(
            "incomplete",
            completed_at=_utcnow(),
            accounts_total=accounts_total,
            accounts_completed=accounts_completed,
            accounts_incomplete=accounts_incomplete,
            accounts_failed=accounts_failed,
            accounts=accounts_payload,
        )
        emit(
            "job_incomplete",
            accounts_total=accounts_total,
            accounts_completed=accounts_completed,
            accounts_incomplete=accounts_incomplete,
            accounts_failed=accounts_failed,
        )
        return 0
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        message = str(exc) or exc.__class__.__name__
        write_state("failed", error=message, traceback=traceback.format_exc(), completed_at=_utcnow())
        emit("job_failed", error=message)
        return 1


def _merge_progress_into_running_state(state_file: Path, job_payload: dict) -> None:
    from src.dashboard import state as dashboard_state
    import hashlib

    if not state_file.exists():
        return

    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    if loaded.get("status") != "running":
        return

    changed = False
    target_emails = [
        email.strip()
        for email in job_payload.get("target_emails", [])
        if isinstance(email, str) and email.strip()
    ]
    for email in target_emails:
        text_lower = str(email).strip().lower()
        account_key = f"acct:{hashlib.md5(text_lower.encode('utf-8')).hexdigest()[:10]}"

        legacy_key = email.replace("@", "_at_").replace(".", "_")
        safe_email = email.replace("@", "_at_")
        for key in [account_key, legacy_key, safe_email, email]:
            if key in dashboard_state.get("accounts", {}):
                acc_data = dashboard_state["accounts"][key]
                pts = int(acc_data.get("points", 0) or 0)
                st = int(acc_data.get("streak", 0) or 0)
                if pts > int(loaded.get("points", 0) or 0):
                    loaded["points"] = pts
                    changed = True
                if st > int(loaded.get("streak", 0) or 0):
                    loaded["streak"] = st
                    changed = True
                break

    if changed:
        state_file.write_text(json.dumps(loaded, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_job_with_polling(job_payload: dict, state_file: Path, events_file: Path) -> int:
    task_task = asyncio.create_task(_run_job(job_payload, state_file, events_file))

    async def poller():
        while not task_task.done():
            with suppress(Exception):
                _merge_progress_into_running_state(state_file, job_payload)
            await asyncio.sleep(2)

    polling_task = asyncio.create_task(poller())
    try:
        return await task_task
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        message = str(exc) or exc.__class__.__name__
        payload = {
            "protocol_version": "0.1",
            "worker_kind": "python-sidecar",
            "job_id": str(job_payload.get("job_id", "") or "job-local"),
            "status": "failed",
            "task": str(job_payload.get("task", "all") or "all"),
            "target_emails": job_payload.get("target_emails", []),
            "updated_at": _utcnow(),
            "correlation_id": str(job_payload.get("correlation_id", "") or "") or None,
            "error": message,
            "traceback": traceback.format_exc(),
            "completed_at": _utcnow(),
        }
        with suppress(Exception):
            if state_file.exists():
                current = json.loads(state_file.read_text(encoding="utf-8") or "{}")
                payload.update({k: v for k, v in current.items() if k in {"points", "streak"}})
        _write_json(state_file, payload)
        _append_event(events_file, {
            "protocol_version": "0.1",
            "job_id": payload["job_id"],
            "event_type": "job_failed",
            "timestamp": _utcnow(),
            "correlation_id": payload["correlation_id"],
            "error": message,
        })
        return 1
    finally:
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.worker_runtime")
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--events-file", required=True)
    args = parser.parse_args(argv)

    job_payload = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
    return asyncio.run(
        _run_job_with_polling(
            job_payload,
            Path(args.state_file),
            Path(args.events_file),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
