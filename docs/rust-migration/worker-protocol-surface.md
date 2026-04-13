# Worker Protocol Surface

This document mirrors the Phase 0/1 implementation currently present in:

- [src/job_protocol.py](C:/Users/CATTFAN/Desktop/autobing-team-phase01/src/job_protocol.py)
- [src/worker_api.py](C:/Users/CATTFAN/Desktop/autobing-team-phase01/src/worker_api.py)
- [crates/autobing-protocol/src/lib.rs](C:/Users/CATTFAN/Desktop/autobing-team-phase01/crates/autobing-protocol/src/lib.rs)
- [crates/autobing-control-plane/src/lib.rs](C:/Users/CATTFAN/Desktop/autobing-team-phase01/crates/autobing-control-plane/src/lib.rs)

## Public Commands

- `start_job`
- `cancel_job`
- `query_job`
- `subscribe_events`
- `health`
- `capabilities`

## Deliberate Omissions

The worker protocol does **not** own:

- account CRUD
- settings mutation
- scheduler control
- product history ownership
- raw Playwright/session internals

## Current Python CLI

Current worker-facing Python CLI surface:

- `python -m src.worker_api health`
- `python -m src.worker_api capabilities`
- `python -m src.worker_api normalize-start-job --file <json>`
- `python -m src.worker_api start-job --file <json>`
- `python -m src.worker_api query-job --job-id <id>`
- `python -m src.worker_api query-job --job-id <id> --events`
- `python -m src.worker_api subscribe-events --job-id <id>`
- `python -m src.worker_api cancel-job --job-id <id>`

`normalize-start-job` exists so the future Rust control plane can validate and shape a start-job payload without committing to the final secret handoff mechanism yet.

Current temporary `secret_ref` forms:

- `env:NAME` -> read the password/token from environment variable `NAME`
- `file:C:\\path\\to\\secret.txt` -> read UTF-8/UTF-8-SIG file contents

Direct `REWARDS_BOT_PASSWORD` still overrides `secret_ref` when present.

Current Rust control-plane CLI surface:

- `cargo run -p autobing-control-plane --`
- `cargo run -p autobing-control-plane -- health`
- `cargo run -p autobing-control-plane -- capabilities`
- `cargo run -p autobing-control-plane -- build-run-request --task searches --target-email user@example.com`
- `cargo run -p autobing-control-plane -- schedule-snapshot`
- `cargo run -p autobing-control-plane -- schedule-update --enabled true --time 08:00`
- `cargo run -p autobing-control-plane -- vault-store --key account/main --secret my-password`
- `cargo run -p autobing-control-plane -- vault-read --key account/main`
- `cargo run -p autobing-control-plane -- worker-health`
- `cargo run -p autobing-control-plane -- worker-capabilities`
- `cargo run -p autobing-control-plane -- start-job --file <json>`
- `cargo run -p autobing-control-plane -- start-job --job-id job-1 --task searches --target-email user@example.com --secret-ref env:REWARDS_BOT_PASSWORD`
- `cargo run -p autobing-control-plane -- start-job --job-id job-1 --task searches --target-email user@example.com --vault-key account/main`
- `cargo run -p autobing-control-plane -- query-job --job-id <id>`
- `cargo run -p autobing-control-plane -- subscribe-events --job-id <id>`
- `cargo run -p autobing-control-plane -- cancel-job --job-id <id>`

## Open ADR Freeze

Before a real `start_job` launches live work from Rust, ADR-003 still needs the final answer for secret delivery:

- inject ephemeral decrypted credentials into the worker command
- or broker credential access through a narrower runtime path
