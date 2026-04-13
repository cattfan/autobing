# ADR-004: Background Execution Authority

## Status
Accepted for planning. Execution details pending.

## Decision
Rust control plane is the only scheduling authority. Windows Task Scheduler is the only V1 wake-up mechanism.

## Why
- Avoid split ownership between dashboard code, in-process schedulers, and OS tasks.
- Make duplicate-run prevention measurable.

## Consequences
- Python worker never self-schedules.
- Future resident behavior must still respect Rust as the single authority.
