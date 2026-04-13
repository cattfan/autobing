# ADR-002: Python Worker Boundary

## Status
Accepted for planning. Execution details pending.

## Decision
Keep the Python sidecar limited to run-scoped automation.

## Public Contract
- `start_job`
- `cancel_job`
- `query_job`
- `subscribe_events`
- `health`
- `capabilities`

## Non-Goals
- No product-state ownership in the worker
- No scheduler authority in the worker
- No settings/account CRUD through the worker boundary

## Follow-Ups
- Freeze message schema, correlation IDs, and restart behavior before worker extraction.
