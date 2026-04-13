# ADR-001: Shell And Control-Plane Shape

## Status
Accepted for planning. Execution details pending.

## Decision
Use a Windows-first Tauri desktop shell with a Rust local control-plane service.

## Drivers
- Windows installability for non-technical users
- Rust ownership of product state
- Clear separation from automation worker internals

## Consequences
- Flask dashboard is transitional only.
- Rust becomes the future owner of UI/API, settings, lifecycle, and support surfaces.

## Follow-Ups
- Finalize packaging/update stack before Phase 3 execution.
