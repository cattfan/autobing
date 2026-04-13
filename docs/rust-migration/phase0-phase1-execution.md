# Phase 0 / Phase 1 Execution Notes

## Goal

Lay down the repository artifacts and Python control-plane seams required before any serious Rust implementation begins.

## Done In This Phase

- Approved PRD and test spec copied into repository-adjacent execution context.
- Feature traceability matrix added under `docs/rust-migration/`.
- ADR stubs added under `docs/adr/`.
- Python control-plane seam introduced for:
  - normalized run requests
  - run-state resets
  - schedule update parsing
  - schedule snapshot shaping
  - Windows Task Scheduler command building

## Not In Scope Yet

- Broad Tauri UI implementation
- Python worker extraction
- GPM replacement
- Patchright replacement
- Native Edge streak migration

## Immediate Next Steps

1. Extend Phase 1 seam extraction beyond scheduling into a first-class run/job model around dashboard/background execution.
2. Freeze ADR details for secret handoff from Rust vault to Python worker.
3. Decide repository layout for Rust control plane bootstrap so Phase 3 can start without reworking Python seams again.
