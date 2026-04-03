"""
runner.py — Bot Execution Runner (Fix 9: cấu trúc tách biệt từ dashboard)

Module này expose các runner functions từ dashboard để:
1. Cho phép import runner logic mà không import toàn bộ Flask app
2. Chuẩn bị cho future extraction khi dashboard.py đủ ổn định

Usage:
    from src.runner import run_bot_thread, run_bot_async

Khi codebase stable, chuyển toàn bộ:
    - _run_bot_thread
    - _run_bot_async
    - _process_single_account
    - _open_account_context
    - _persist_storage_state
    - _collect_final_verification
    - _start_gpm_profile / _stop_gpm_profile

vào file này và import ngược lại vào dashboard.py.
"""

from __future__ import annotations

# ─── Re-export runner interface ─────────────────────────────────────────────
# Tạm thời import từ dashboard để giữ backward compat
# (tránh circular import bằng cách lazy-import khi cần)


def get_runner_functions():
    """
    Lazy import runner functions from dashboard.
    Call this inside an async context or after app init.
    """
    from src.dashboard import (  # noqa: F401  -- re-export
        _run_bot_thread as run_bot_thread,
        _run_bot_async as run_bot_async,
        _start_gpm_profile as start_gpm_profile,
        _stop_gpm_profile as stop_gpm_profile,
        _open_account_context as open_account_context,
        _persist_storage_state as persist_storage_state,
        _update_account_state as update_account_state,
        add_log,
        state,
    )
    return {
        "run_bot_thread": run_bot_thread,
        "run_bot_async": run_bot_async,
        "start_gpm_profile": start_gpm_profile,
        "stop_gpm_profile": stop_gpm_profile,
        "open_account_context": open_account_context,
        "persist_storage_state": persist_storage_state,
        "update_account_state": update_account_state,
        "add_log": add_log,
        "state": state,
    }


__all__ = ["get_runner_functions"]
