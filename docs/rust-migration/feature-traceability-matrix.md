# Rust Migration Feature Traceability Matrix

This matrix turns the approved PRD and test spec into an execution-facing inventory.

| Feature family | Current Python owner(s) | Target ownership | Planned phase | Verification gate |
| --- | --- | --- | --- | --- |
| Installer / updater / rollback | N/A | Early Rust ownership | Phase 3 | Product lifecycle gates from test spec |
| Local auth and desktop shell | `src/dashboard.py`, `dashboard/*` | Early Rust ownership | Phase 3 | Local auth/UI/API gates |
| Account vault and settings | `src/crypto.py`, `src/dashboard.py`, `config/settings.json` | Early Rust ownership | Phase 3 | Vault migration + settings migration gates |
| Scheduling / run policy | `src/scheduler.py`, `main.py`, `src/dashboard.py` | Early Rust ownership | Phase 1 -> Phase 3 | Duplicate-schedule rate = 0 |
| Logs / history / support diagnostics | `src/dashboard.py`, `src/points.py`, `src/google_sheets.py`, `src/notifier.py` | Early Rust ownership | Phase 3-4 | Product service deterministic suites |
| Desktop search runtime | `src/searcher.py`, `src/browser.py`, `src/login.py` | Late Rust ownership | Phase 5 | Search parity >= 98% |
| Mobile search runtime | `src/searcher.py`, `src/browser.py`, `main.py`, `src/dashboard.py` | Late Rust ownership | Phase 5 | Search parity >= 98% |
| Edge search runtime | `src/searcher.py`, `src/browser.py`, `src/streaks.py` | Late Rust ownership | Phase 5 | Search parity >= 98% |
| Daily Set / quiz / poll flows | `src/daily_set.py`, `src/universal_task.py`, `src/quiz.py`, `src/quiz_solver.py` | Late Rust ownership | Phase 6 | Rewards parity >= 95% pilot / 98% default-on |
| Promotions / Explore on Bing | `src/universal_task.py`, `src/dashboard_scraper.py` | Late Rust ownership | Phase 6 | Rewards parity >= 95% pilot / 98% default-on |
| Captcha orchestration | `src/manual_captcha.py`, `src/captcha_solver.py`, `src/login.py` | Late Rust ownership | Phase 6 | Captcha scenario gates |
| AI / page-agent integration | `src/ai_agent.py`, `src/page_agent_flow.py`, `src/universal_task.py` | Late Rust ownership | Phase 6 | AI/plugin scenario gates |
| GPM integration | `src/dashboard.py`, `main.py`, `src/browser.py` | Retained sidecar / exceptional late-port | Separate architect review | Dedicated parity evidence |
| Patchright mobile automation | `main.py`, `src/dashboard.py`, `src/browser.py` | Retained sidecar / exceptional late-port | Separate architect review | Dedicated parity evidence |
| Native Edge streak | `src/edge_streak_native.py`, `src/streaks.py`, `src/browser.py`, `src/dashboard.py` | Retained sidecar / exceptional late-port | Separate architect review | Dedicated parity evidence |

## Phase 0/1 Execution Notes

- Phase 0 in repo means this matrix and ADR set exist in version control, not only in `.omx/`.
- Phase 1 means current Python code starts exposing explicit control-plane seams for:
  - run job requests
  - schedule policy
  - scheduler entrypoint building
  - runner boundary
- No current feature family should silently change target ownership without updating this matrix.
