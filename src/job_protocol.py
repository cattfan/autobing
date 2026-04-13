"""
Run-scoped worker protocol shapes for the future Rust control plane.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.control_plane import BotRunRequest, SUPPORTED_RUN_TASKS


PROTOCOL_VERSION = "0.1"
WORKER_KIND = "python-sidecar"
RUN_SCOPED_COMMANDS = (
    "start_job",
    "cancel_job",
    "query_job",
    "subscribe_events",
    "health",
    "capabilities",
)
RETAINED_SIDECAR_DOMAINS = (
    "gpm",
    "patchright_mobile",
    "native_edge_streak",
)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    task: str
    target_emails: tuple[str, ...] = ()
    secret_ref: str | None = None
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task": self.task,
            "target_emails": list(self.target_emails),
            "secret_ref": self.secret_ref,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class StartJobCommand:
    job: JobSpec
    protocol_version: str = PROTOCOL_VERSION
    command: str = "start_job"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["job"] = self.job.to_dict()
        return payload


@dataclass(frozen=True)
class WorkerCapabilities:
    protocol_version: str = PROTOCOL_VERSION
    worker_kind: str = WORKER_KIND
    commands: tuple[str, ...] = RUN_SCOPED_COMMANDS
    retained_sidecar_domains: tuple[str, ...] = RETAINED_SIDECAR_DOMAINS

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "worker_kind": self.worker_kind,
            "commands": list(self.commands),
            "retained_sidecar_domains": list(self.retained_sidecar_domains),
        }


@dataclass(frozen=True)
class WorkerHealth:
    protocol_version: str = PROTOCOL_VERSION
    worker_kind: str = WORKER_KIND
    status: str = "ok"
    python_runtime: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def parse_job_spec(payload: dict[str, Any]) -> JobSpec:
    requested_task = str(payload.get("task", "all") or "all").strip().lower()
    task = requested_task if requested_task in SUPPORTED_RUN_TASKS else "all"
    return JobSpec(
        job_id=str(payload.get("job_id", "") or "").strip() or "job-local",
        task=task,
        target_emails=_normalize_target_emails(payload.get("target_emails", [])),
        secret_ref=str(payload.get("secret_ref", "") or "").strip() or None,
        correlation_id=str(payload.get("correlation_id", "") or "").strip() or None,
    )


def start_job_to_run_request(command: StartJobCommand, master_password: str) -> BotRunRequest:
    return BotRunRequest(
        task=command.job.task,
        password=master_password,
        target_emails=command.job.target_emails,
    )
