# ADR-005: Exceptional Retained-Sidecar Domains

## Status
Accepted for planning. Execution details pending.

## Decision
Treat the following as retained-sidecar or exceptional late-port domains until a separate architect review approves migration:

- GPM integration
- Patchright mobile automation
- Native Edge streak / native Edge special-case browsing

## Why
- These domains are tightly coupled to Windows process behavior and current runtime heuristics.
- Forcing them into early Rust migration would raise parity risk disproportionately.

## Follow-Ups
- Define dedicated parity evidence and rollback criteria before any port begins.
