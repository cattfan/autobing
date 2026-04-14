"""
Minimal Python worker-facing API surface for the future Rust control plane.

This module does not replace the current runtime yet. It provides stable,
run-scoped protocol shapes and a CLI that Rust can later invoke or validate
against while the full worker extraction is still in progress.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.job_protocol import (
    StartJobCommand,
    WorkerCapabilities,
    WorkerHealth,
    start_job_to_run_request,
    parse_job_spec,
)
from src.worker_store import cancel_job, read_events, read_state, start_job_process


def _job_payload_from_args(args) -> dict:
    target_emails = [
        email.strip()
        for email in getattr(args, "target_emails", []) or []
        if isinstance(email, str) and email.strip()
    ]
    payload: dict = {
        "job_id": getattr(args, "job_id", "") or "",
        "task": getattr(args, "task", "all") or "all",
        "target_emails": target_emails,
    }
    secret_ref = getattr(args, "secret_ref", None)
    correlation_id = getattr(args, "correlation_id", None)
    if secret_ref:
        payload["secret_ref"] = secret_ref
    if correlation_id:
        payload["correlation_id"] = correlation_id
    return payload


def _load_json_payload(raw_json: str | None, file_path: str | None, args=None) -> dict:
    if args is not None:
        arg_payload = _job_payload_from_args(args)
        if arg_payload.get("job_id"):
            return arg_payload
    if raw_json:
        return json.loads(raw_json)
    if file_path:
        return json.loads(Path(file_path).read_text(encoding="utf-8-sig"))
    raise ValueError("either --json or --file is required")


def cli(argv: list[str] | None = None) -> int:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "internal-runtime":
        from src.worker_runtime import main as runtime_main
        return runtime_main(sys.argv[2:])

    parser = argparse.ArgumentParser(prog="python -m src.worker_api" if not getattr(sys, "frozen", False) else "autobing-worker.exe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health")
    subparsers.add_parser("capabilities")
    query = subparsers.add_parser("query-job")
    query.add_argument("--job-id", required=True)
    query.add_argument("--events", action="store_true")

    subscribe = subparsers.add_parser("subscribe-events")
    subscribe.add_argument("--job-id", required=True)

    cancel = subparsers.add_parser("cancel-job")
    cancel.add_argument("--job-id", required=True)

    normalize = subparsers.add_parser("normalize-start-job")
    normalize.add_argument("--json")
    normalize.add_argument("--file")
    normalize.add_argument("--job-id")
    normalize.add_argument("--task", default="all")
    normalize.add_argument("--target-email", dest="target_emails", action="append", default=[])
    normalize.add_argument("--secret-ref")
    normalize.add_argument("--correlation-id")

    start = subparsers.add_parser("start-job")
    start.add_argument("--json")
    start.add_argument("--file")
    start.add_argument("--job-id")
    start.add_argument("--task", default="all")
    start.add_argument("--target-email", dest="target_emails", action="append", default=[])
    start.add_argument("--secret-ref")
    start.add_argument("--correlation-id")

    args = parser.parse_args(argv)

    if args.command == "health":
        print(json.dumps(WorkerHealth().to_dict(), ensure_ascii=False))
        return 0

    if args.command == "capabilities":
        print(json.dumps(WorkerCapabilities().to_dict(), ensure_ascii=False))
        return 0

    if args.command == "query-job":
        payload = read_events(args.job_id) if args.events else read_state(args.job_id)
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if args.command == "subscribe-events":
        print(json.dumps(read_events(args.job_id), ensure_ascii=False))
        return 0

    if args.command == "cancel-job":
        print(json.dumps(cancel_job(args.job_id), ensure_ascii=False))
        return 0

    payload = _load_json_payload(args.json, args.file, args)
    job = parse_job_spec(payload)
    command = StartJobCommand(job=job)

    if args.command == "normalize-start-job":
        print(json.dumps(command.to_dict(), ensure_ascii=False))
        return 0

    try:
        from src.crypto import load_encrypted_accounts
        import os
        accounts = load_encrypted_accounts()
        for email in command.target_emails:
            for acc in accounts:
                if acc.get("email") == email and acc.get("password"):
                    os.environ["REWARDS_BOT_PASSWORD"] = acc["password"]
                    break
    except Exception:
        pass

    result = start_job_process(job)
    result["public_request"] = command.to_dict()
    result["run_request"] = {
        "task": start_job_to_run_request(command, "<redacted>").task,
        "target_emails": list(start_job_to_run_request(command, "<redacted>").target_emails),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
