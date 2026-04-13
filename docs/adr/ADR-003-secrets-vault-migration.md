# ADR-003: Secrets Vault Migration

## Status
Accepted for planning. Execution details pending.

## Decision
Rust owns secrets from the first distributable Rust milestone, using Windows-backed protection.

## Baseline Risk
Current Python storage still includes plaintext account persistence patterns that are not acceptable for broad distribution.

## Planned Direction
- Windows DPAPI-backed local vault
- Explicit encrypted export package
- Guided recovery/import flow
- Fail-closed downgrade behavior

## Phase-Current Secret Handoff
- Rust control plane may materialize a per-job temporary secret file and pass it as `secret_ref=file:<path>`.
- Python worker reads the file once and deletes it immediately after loading.
- `env:` secret refs remain supported for smoke tests and operator workflows, but the intended production path is Rust-owned vault materialization.

## Remaining Follow-Up
- A future brokered handoff may replace file materialization, but the current repository now has a concrete end-to-end mechanism instead of an unresolved placeholder.
