"""
Filesystem-backed worker job store.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.job_protocol import JobSpec


DEFAULT_JOBS_ROOT = Path(".omx") / "worker-jobs"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jobs_root(path: str | None = None) -> Path:
    root = Path(path) if path else DEFAULT_JOBS_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_directory(job_id: str, root: str | None = None) -> Path:
    directory = jobs_root(root) / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def job_paths(job_id: str, root: str | None = None) -> dict[str, Path]:
    directory = job_directory(job_id, root)
    return {
        "dir": directory,
        "spec": directory / "job.json",
        "state": directory / "state.json",
        "events": directory / "events.jsonl",
        "stdout": directory / "stdout.log",
        "stderr": directory / "stderr.log",
        "cancel": directory / "cancel.requested",
    }


def write_job_spec(job: JobSpec, root: str | None = None) -> dict[str, Path]:
    paths = job_paths(job.job_id, root)
    paths["spec"].write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def read_state(job_id: str, root: str | None = None) -> dict[str, Any]:
    paths = job_paths(job_id, root)
    if not paths["state"].exists():
        return {
            "protocol_version": "0.1",
            "worker_kind": "python-sidecar",
            "job_id": job_id,
            "status": "unknown",
            "updated_at": _utcnow(),
        }
    return json.loads(paths["state"].read_text(encoding="utf-8"))


def read_events(job_id: str, root: str | None = None) -> list[dict[str, Any]]:
    paths = job_paths(job_id, root)
    if not paths["events"].exists():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in paths["events"].read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        events.append(json.loads(raw_line))
    return events


def start_job_process(job: JobSpec, root: str | None = None) -> dict[str, Any]:
    paths = write_job_spec(job, root)
    stdout = paths["stdout"].open("a", encoding="utf-8")
    stderr = paths["stderr"].open("a", encoding="utf-8")

    command = [
        sys.executable,
        "-m",
        "src.worker_runtime",
        "--job-file",
        str(paths["spec"]),
        "--state-file",
        str(paths["state"]),
        "--events-file",
        str(paths["events"]),
    ]

    popen_kwargs: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "cwd": str(Path.cwd()),
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    process = subprocess.Popen(command, **popen_kwargs)
    stdout.close()
    stderr.close()
    initial_state = {
        "protocol_version": "0.1",
        "worker_kind": "python-sidecar",
        "job_id": job.job_id,
        "status": "accepted",
        "task": job.task,
        "target_emails": list(job.target_emails),
        "secret_ref": job.secret_ref,
        "correlation_id": job.correlation_id,
        "pid": process.pid,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    paths["state"].write_text(json.dumps(initial_state, ensure_ascii=False, indent=2), encoding="utf-8")
    return initial_state


def cancel_job(job_id: str, root: str | None = None) -> dict[str, Any]:
    paths = job_paths(job_id, root)
    paths["cancel"].parent.mkdir(parents=True, exist_ok=True)
    paths["cancel"].write_text("cancelled\n", encoding="utf-8")
    state = read_state(job_id, root)
    pid = state.get("pid")
    if isinstance(pid, int):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    state["status"] = "cancelled"
    state["updated_at"] = _utcnow()
    paths["state"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
