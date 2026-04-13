"""
Flask Web Dashboard — Full GUI for Rewards Search Automator.
Provides API endpoints for accounts, settings, running tasks, logs, and status.
"""

import os
import json
import random
import threading
import asyncio
from contextlib import AsyncExitStack
import socket
import time
import logging
import secrets
from hashlib import md5
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from flask import Flask, request, jsonify, send_from_directory, send_file, session

from src.utils import (
    logger,
    load_settings,
    save_settings,
    CONFIG_DIR,
    DATA_DIR,
    PROFILES_DIR,
    close_other_tabs,
    emit_diagnostic_log,
    get_proxy_for_session,
    is_sensitive_setting,
    mask_email,
    summarize_search_status,
)
from src.runtime_identity import (
    build_runtime_descriptor,
    build_search_verification,
    choose_search_verification_source,
    describe_search_remaining_items,
    invalidate_runtime_attachment,
    merge_search_status,
)
from src.crypto import (
    load_encrypted_accounts,
    save_encrypted_accounts,
    hash_password,
    verify_password,
    migrate_to_encrypted,
)
from src.ai_agent import AIAgent
from src.streaks import EdgeBrowsingStreak, TaskDetector
from src.edge_streak_native import NativeEdgeStreak
from src.universal_task import UniversalTaskScanner, get_deferred_offer_reason
from src.google_sheets import GoogleSheetsLogger


app = Flask(
    __name__,
    static_folder=None,
)
app.config["SECRET_KEY"] = os.environ.get("AUTOBING_DASHBOARD_SECRET") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ─── Global State ──────────────────────────────────────────────────────────

state = {
    "status": "idle",          # idle, running, error
    "current_account": "",
    "current_task": "",
    "progress": 0,
    "progress_total": 0,
    "logs": [],
    "account_logs": {},        # Per-account logs: {"email5***": [{time, level, message}, ...]}
    "last_run": None,
    "accounts_count": 0,
    "total_points": 0,
    "master_password": "",      # No auth required
    # Per-account tracking (key = "email5***", value = per-account status)
    "accounts": {},
    "ai": {
        "active": False,
        "last_update": "",
        "last_event": "",
        "task": "",
        "model": "",
        "last_level": "",
    },
}

LOG_MAX = 500
KEEP_EXISTING_SECRET = "__KEEP_EXISTING_SECRET__"

import contextvars
_current_log_handler = contextvars.ContextVar("current_log_handler", default=None)
_current_log_key = contextvars.ContextVar("current_log_key", default=None)

# Lock bảo vệ global state dict — tránh race condition khi nhiều accounts chạy đồng thời
_state_lock = threading.Lock()
_snapshot_lock = threading.RLock()
ACCOUNT_DAILY_SNAPSHOTS_PATH = DATA_DIR / "account_daily_snapshots.jsonl"
DASHBOARD_STATE_FILE = DATA_DIR / "dashboard_state.json"

_last_state_hash = None

def _state_sync_worker():
    """Background thread running inside the worker process to sync state to disk."""
    global _last_state_hash
    while True:
        try:
            with _state_lock:
                current_state_json = json.dumps(state, ensure_ascii=False)
            current_hash = hash(current_state_json)
            if current_hash != _last_state_hash:
                tmp_path = DASHBOARD_STATE_FILE.with_suffix(".tmp")
                tmp_path.write_text(current_state_json, encoding="utf-8")
                tmp_path.replace(DASHBOARD_STATE_FILE)
                _last_state_hash = current_hash
        except Exception as e:
            logger.debug(f"State sync failed: {e}")
        time.sleep(1)

def start_state_sync_for_worker():
    """Start the disk synchronization loop in a background thread."""
    t = threading.Thread(target=_state_sync_worker, daemon=True)
    t.start()



def _select_mobile_runtime_strategy(gpm_enabled: bool, gpm_mobile_profile_id: str | None) -> tuple[bool, str]:
    """Decide whether the account can use same-account mobile GPM control."""
    if not gpm_enabled:
        return True, "gpm_mobile_disabled"
    if not str(gpm_mobile_profile_id or "").strip():
        return True, "missing_gpm_mobile_profile_id"
    return False, "gpm_mobile_profile"


def add_log(level: str, message: str):
    """Add a log message to the state and also to file/console logger."""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    with _state_lock:
        state["logs"].append(entry)
        if len(state["logs"]) > LOG_MAX:
            state["logs"] = state["logs"][-LOG_MAX:]
            
        # Per-account in-memory log for dashboard
        _k = _current_log_key.get()
        if _k:
            if _k not in state["account_logs"]:
                state["account_logs"][_k] = []
            state["account_logs"][_k].append(entry)
            if len(state["account_logs"][_k]) > LOG_MAX:
                state["account_logs"][_k] = state["account_logs"][_k][-LOG_MAX:]
            if _k in state["accounts"]:
                state["accounts"][_k]["last_log_time"] = entry["time"]
                state["accounts"][_k]["last_message"] = message
                state["accounts"][_k]["last_level"] = level
                state["accounts"][_k]["log_count"] = len(state["account_logs"][_k])
                state["accounts"][_k]["updated_at"] = datetime.now().isoformat(timespec="seconds")

    # Per-account file handler logging
    _h = _current_log_handler.get()
    if _h:
        try:
            record = logging.LogRecord(
                name="AccLog", level=getattr(logging, level.upper(), logging.INFO),
                pathname="", lineno=0, msg=message, args=(), exc_info=None,
            )
            _h.emit(record)
        except Exception:
            pass

    # Also write to file/console logger for debugging
    if level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
    else:
        logger.info(message)


def _diag_log(settings: dict, message: str, *, level: str = "info", scope: str = "dashboard", **fields) -> None:
    """Emit structured diagnostic log lines into the dashboard/global log stream."""
    emit_diagnostic_log(
        add_log,
        settings,
        message,
        level=level,
        scope=scope,
        **fields,
    )


def _update_account_state(account_key: str, **kwargs) -> None:
    """Thread-safe update of per-account state within state['accounts']."""
    with _state_lock:
        if account_key not in state["accounts"]:
            state["accounts"][account_key] = {
                "id": account_key,
                "email": "",
                "display_name": account_key,
                "task": "",
                "progress": 0,
                "progress_total": 0,
                "status": "pending",
                "points": 0,
                "last_message": "",
                "last_level": "info",
                "last_log_time": "",
                "log_count": 0,
                "updated_at": "",
            }
        state["accounts"][account_key].update(kwargs)
        state["accounts"][account_key]["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _update_ai_state(**kwargs) -> None:
    """Thread-safe dashboard snapshot for AI runtime activity."""
    with _state_lock:
        ai_state = state.setdefault("ai", {})
        ai_state.update(kwargs)
        ai_state["last_update"] = datetime.now().isoformat(timespec="seconds")


def _normalize_account_status(status: str) -> str:
    if status in {"running", "done", "error", "idle"}:
        return status
    return "idle"


def _account_state_key(email: str) -> str:
    """Return a stable per-account key for dashboard state/log maps."""
    text = str(email or "").strip()
    if not text or "@" not in text:
        return text
    return f"acct:{md5(text.lower().encode('utf-8')).hexdigest()[:10]}"


def _account_display_label(email: str) -> str:
    """Return a masked-but-distinguishable label for UI surfaces."""
    text = str(email or "").strip()
    if "@" not in text:
        return text
    local, domain = text.split("@", 1)
    prefix = local[:5]
    suffix = local[-2:] if len(local) > 7 else local[5:]
    masked_local = prefix + "***" + suffix if suffix else prefix + "***"
    return f"{masked_local}@{domain}"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _current_reset_context(now: datetime | None = None) -> tuple[str, str]:
    local_now = (now or datetime.now()).astimezone()
    reset_key = local_now.date().isoformat()
    next_reset = (local_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return reset_key, next_reset.isoformat(timespec="seconds")


def _read_account_daily_snapshots_unlocked() -> list[dict]:
    if not ACCOUNT_DAILY_SNAPSHOTS_PATH.exists():
        return []
    records: list[dict] = []
    try:
        with open(ACCOUNT_DAILY_SNAPSHOTS_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    except Exception:
        return []
    return records


def _read_account_daily_snapshots() -> list[dict]:
    with _snapshot_lock:
        return _read_account_daily_snapshots_unlocked()


def _write_account_daily_snapshots_unlocked(records: list[dict]) -> None:
    ACCOUNT_DAILY_SNAPSHOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(ACCOUNT_DAILY_SNAPSHOTS_PATH.parent),
        delete=False,
        prefix="account_daily_snapshots.",
        suffix=".tmp",
    ) as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        temp_path = fh.name
    os.replace(temp_path, ACCOUNT_DAILY_SNAPSHOTS_PATH)


def _write_account_daily_snapshots(records: list[dict]) -> None:
    with _snapshot_lock:
        _write_account_daily_snapshots_unlocked(records)


def _upsert_account_daily_snapshot(record: dict) -> None:
    with _snapshot_lock:
        records = _read_account_daily_snapshots_unlocked()
        key = (record.get("account_key", ""), record.get("date", ""))
        updated = False
        for idx, existing in enumerate(records):
            if (existing.get("account_key", ""), existing.get("date", "")) == key:
                records[idx] = record
                updated = True
                break
        if not updated:
            records.append(record)
        records.sort(key=lambda item: (item.get("account_key", ""), item.get("date", "")))
        _write_account_daily_snapshots_unlocked(records)


def _recent_account_snapshots(account_key: str, days: int = 30) -> list[dict]:
    records = [
        row for row in _read_account_daily_snapshots()
        if row.get("account_key") == account_key
    ]
    records.sort(key=lambda item: item.get("date", ""))
    return records[-days:] if len(records) > days else records


def _normalize_track(label: str, current: int, maximum: int, *, status_hint: str = "idle") -> dict:
    current = _safe_int(current)
    maximum = _safe_int(maximum)
    percent = max(0, min(100, round((current / maximum) * 100))) if maximum > 0 else 0
    if status_hint == "error":
        status = "error"
    elif maximum > 0 and current >= maximum:
        status = "done"
    elif current > 0 or status_hint == "running":
        status = "running"
    elif status_hint == "blocked":
        status = "blocked"
    else:
        status = "idle"
    detail = f"{current}/{maximum}" if maximum > 0 else "Not available"
    return {
        "label": label,
        "current": current,
        "max": maximum,
        "percent": percent,
        "status": status,
        "detail": detail,
    }


def _build_profile_tracks(item: dict, *, summary: dict | None = None) -> dict:
    summary = summary or {}
    search_status = item.get("search_status") or {}
    task_overview = item.get("task_overview") or {}
    category_status = item.get("category_status") or {}
    status_hint = _normalize_account_status(item.get("status", "idle"))
    current_task = str(item.get("task", "") or "").lower()
    task_progress = _safe_int(item.get("progress"))
    task_progress_total = _safe_int(item.get("progress_total"))

    def _value(name: str, snapshot_name: str | None = None) -> int:
        if name in search_status:
            return _safe_int(search_status.get(name))
        if snapshot_name:
            return _safe_int(summary.get("snapshot", {}).get(snapshot_name))
        return 0

    daily = task_overview.get("daily_set", {})
    daily_current = _safe_int(daily.get("completed", summary.get("snapshot", {}).get("daily_set_completed", 0)))
    daily_total = _safe_int(daily.get("total", summary.get("snapshot", {}).get("daily_set_total", 0)))

    promos = category_status.get("more_promo", {})
    promo_current = _safe_int(promos.get("completed", summary.get("snapshot", {}).get("promos_completed", 0)))
    promo_total = _safe_int(promos.get("total", summary.get("snapshot", {}).get("promos_total", 0)))

    tracks = {
        "total_points": _normalize_track(
            "Total Points",
            _safe_int(item.get("points", summary.get("points_now", 0))),
            max(_safe_int(item.get("points", summary.get("points_now", 0))), 1),
            status_hint=status_hint,
        ),
        "daily_set": _normalize_track("Daily Set", daily_current, daily_total, status_hint=status_hint),
        "pc_search": _normalize_track(
            "PC Search",
            _value("pc_current", "pc_current"),
            _value("pc_max", "pc_max"),
            status_hint="running" if current_task == "desktop searches" else status_hint,
        ),
        "mobile_search": _normalize_track(
            "Mobile Search",
            _value("mobile_current", "mobile_current"),
            _value("mobile_max", "mobile_max"),
            status_hint="running" if current_task == "mobile searches" else status_hint,
        ),
        "edge": _normalize_track(
            "Edge",
            _value("edge_current", "edge_current"),
            _value("edge_max", "edge_max"),
            status_hint="running" if "edge" in current_task else status_hint,
        ),
        "promos": _normalize_track("Promotions", promo_current, promo_total, status_hint=status_hint),
    }

    if current_task == "desktop searches" and task_progress_total > 0:
        tracks["pc_search"] = _normalize_track("PC Search", task_progress, task_progress_total, status_hint="running")
    elif current_task == "mobile searches" and task_progress_total > 0:
        tracks["mobile_search"] = _normalize_track("Mobile Search", task_progress, task_progress_total, status_hint="running")
    elif current_task == "edge searches" and task_progress_total > 0:
        tracks["edge"] = _normalize_track("Edge", task_progress, task_progress_total, status_hint="running")

    return tracks


def _build_profile_day_summary(item: dict) -> dict:
    account_key = item.get("key") or item.get("id") or ""
    reset_key, reset_at = _current_reset_context()
    records = _recent_account_snapshots(account_key, days=30)
    today_record = next((row for row in reversed(records) if row.get("date") == reset_key), {})
    yesterday_record = records[-2] if len(records) >= 2 and records[-1].get("date") == reset_key else (records[-1] if records and records[-1].get("date") != reset_key else {})

    points_now = _safe_int(item.get("points", today_record.get("points_now", 0)))
    earned_today = _safe_int(item.get("earned_today", today_record.get("earned_today", 0)))
    earned_yesterday = _safe_int(item.get("earned_yesterday", yesterday_record.get("earned_today", 0)))
    delta_vs_yesterday = earned_today - earned_yesterday
    trend = "up" if delta_vs_yesterday > 0 else "down" if delta_vs_yesterday < 0 else "flat"
    return {
        "points_now": points_now,
        "earned_today": earned_today,
        "earned_yesterday": earned_yesterday,
        "delta_vs_yesterday": delta_vs_yesterday,
        "trend": trend,
        "reset_key": reset_key,
        "reset_at": reset_at,
        "snapshot": today_record,
        "history_available": bool(records),
    }


def _record_account_daily_snapshot(
    *,
    account_key: str,
    email: str,
    total_points: int,
    earned_today: int,
    search_status: dict | None,
    task_overview: dict | None,
    category_status: dict | None,
    verification_state: str,
    runtime_family: str,
) -> None:
    reset_key, _ = _current_reset_context()
    existing_today = next(
        (row for row in reversed(_recent_account_snapshots(account_key, days=30)) if row.get("date") == reset_key),
        None,
    )
    search_status = search_status or {}
    task_overview = task_overview or {}
    category_status = category_status or {}
    daily = task_overview.get("daily_set", {})
    promos = category_status.get("more_promo", {})
    normalized_total_points = _safe_int(total_points)
    normalized_earned_today = _safe_int(earned_today)
    if existing_today:
        day_start_points = _safe_int(existing_today.get("points_now", 0)) - _safe_int(existing_today.get("earned_today", 0))
        normalized_earned_today = max(
            _safe_int(existing_today.get("earned_today", 0)),
            normalized_total_points - day_start_points,
        )
    record = {
        "date": reset_key,
        "account_key": account_key,
        "email": email,
        "points_now": normalized_total_points,
        "earned_today": normalized_earned_today,
        "pc_current": _safe_int(search_status.get("pc_current", 0)),
        "pc_max": _safe_int(search_status.get("pc_max", 0)),
        "mobile_current": _safe_int(search_status.get("mobile_current", 0)),
        "mobile_max": _safe_int(search_status.get("mobile_max", 0)),
        "edge_current": _safe_int(search_status.get("edge_current", 0)),
        "edge_max": _safe_int(search_status.get("edge_max", 0)),
        "daily_set_completed": _safe_int(daily.get("completed", 0)),
        "daily_set_total": _safe_int(daily.get("total", 0)),
        "promos_completed": _safe_int(promos.get("completed", 0)),
        "promos_total": _safe_int(promos.get("total", 0)),
        "verification_state": verification_state,
        "runtime_family": runtime_family,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    _upsert_account_daily_snapshot(record)


def _build_profile_views(accounts_snapshot: dict, account_logs_snapshot: dict) -> list[dict]:
    """Build a stable profile list for the dashboard without breaking the legacy accounts map."""
    profiles: list[dict] = []
    for account_key, raw_state in accounts_snapshot.items():
        item = dict(raw_state or {})
        item["key"] = account_key
        status = _normalize_account_status(item.get("status", "idle"))
        progress = int(item.get("progress", 0) or 0)
        progress_total = int(item.get("progress_total", 0) or 0)
        points = int(item.get("points", 0) or 0)
        logs = account_logs_snapshot.get(account_key, [])
        label = item.get("display_name") or item.get("email") or account_key
        profile_id = item.get("email") or item.get("id") or account_key
        last_message = item.get("last_message") or (logs[-1]["message"] if logs else "")
        last_level = item.get("last_level") or (logs[-1]["level"] if logs else "info")
        last_log_time = item.get("last_log_time") or (logs[-1]["time"] if logs else "")
        day_summary = _build_profile_day_summary(item)
        tracks = _build_profile_tracks(item, summary=day_summary)

        profiles.append({
            "id": profile_id,
            "key": account_key,
            "email": item.get("email", ""),
            "label": label,
            "status": status,
            "task": item.get("task", ""),
            "progress": progress,
            "progress_total": progress_total,
            "progress_percent": (
                100 if status == "done"
                else max(0, min(100, round((progress / progress_total) * 100)))
                if progress_total > 0 else 0
            ),
            "points": points,
            "daily_streak": int(item.get("streak", 0) or 0),
            "updated_at": item.get("updated_at", ""),
            "last_log_time": last_log_time,
            "last_message": last_message,
            "last_level": last_level,
            "has_logs": bool(logs),
            "log_count": int(item.get("log_count", len(logs)) or 0),
            "points_now": day_summary["points_now"],
            "earned_today": day_summary["earned_today"],
            "earned_yesterday": day_summary["earned_yesterday"],
            "delta_vs_yesterday": day_summary["delta_vs_yesterday"],
            "trend": day_summary["trend"],
            "reset_key": day_summary["reset_key"],
            "reset_at": day_summary["reset_at"],
            "tracks": tracks,
            "verification_state": item.get("verification_state", day_summary["snapshot"].get("verification_state", "idle")),
            "remaining_items": item.get("remaining_items", []),
            "runtime_family": item.get("runtime_family", day_summary["snapshot"].get("runtime_family", "")),
            "history_available": day_summary["history_available"],
        })

    def _profile_sort_key(profile: dict) -> tuple[int, str]:
        order = {"running": 0, "error": 1, "done": 2, "idle": 3}
        return order.get(profile["status"], 4), profile["label"].lower()

    profiles.sort(key=_profile_sort_key)
    return profiles


def _build_profile_summary(profiles: list[dict]) -> dict:
    summary = {
        "total": len(profiles),
        "running": 0,
        "done": 0,
        "error": 0,
        "idle": 0,
        "profiles_with_logs": 0,
        "total_points": 0,
    }
    for profile in profiles:
        bucket = profile["status"]
        summary[bucket] = summary.get(bucket, 0) + 1
        if profile.get("has_logs"):
            summary["profiles_with_logs"] += 1
        summary["total_points"] += int(profile.get("points", 0) or 0)
    return summary


def _build_dashboard_overview(profiles: list[dict]) -> dict:
    reset_key, reset_at = _current_reset_context()
    earned_today = sum(_safe_int(profile.get("earned_today", 0)) for profile in profiles)
    earned_yesterday = sum(_safe_int(profile.get("earned_yesterday", 0)) for profile in profiles)
    delta_vs_yesterday = earned_today - earned_yesterday
    trend = "up" if delta_vs_yesterday > 0 else "down" if delta_vs_yesterday < 0 else "flat"
    return {
        "earned_today": earned_today,
        "earned_yesterday": earned_yesterday,
        "delta_vs_yesterday": delta_vs_yesterday,
        "trend": trend,
        "reset_key": reset_key,
        "reset_at": reset_at,
        "accounts_with_history": sum(1 for profile in profiles if profile.get("history_available")),
        "accounts_needing_attention": sum(
            1 for profile in profiles
            if profile.get("status") == "error" or profile.get("remaining_items")
        ),
    }


def _mobile_credit_delta(before_status: dict, after_status: dict) -> int:
    return max(
        0,
        int(after_status.get("mobile_current", 0))
        - int(before_status.get("mobile_current", 0)),
    )


def _total_points_delta(before_status: dict, after_status: dict) -> int:
    return max(
        0,
        int(after_status.get("total_points", 0))
        - int(before_status.get("total_points", 0)),
    )


def _edge_streak_attempt_allowed(edge_streak_info: dict) -> bool:
    """Return True when the native Edge streak loop should run."""
    info = edge_streak_info or {}
    exists = bool(info.get("exists", False))
    done = bool(info.get("done", False))
    minutes_done = int(info.get("minutes", 0) or 0)
    minutes_target = int(info.get("target", 30) or 30)
    return exists and not done and minutes_done < minutes_target


def _effective_max_threads(settings: dict) -> tuple[int, str]:
    """Return the configured dashboard account concurrency.

    GPM browser profiles are materially heavier than local-only runs and the
    desktop↔mobile handoff introduces profile lifecycle races. Cap effective
    concurrency so multi-account runs stay reliable instead of chasing raw fanout.
    """
    configured = max(1, int(settings.get("max_threads", 10) or 1))
    if settings.get("gpm_integration_enabled", False):
        effective = min(configured, 2)
        if effective != configured:
            return effective, "capped to 2 while GPM profile lifecycle handoffs are active"
    return configured, ""


def _profile_lock_keys_for_account(settings: dict, account: dict) -> list[str]:
    """Return every profile identity that must be serialized for one account."""
    keys: list[str] = []
    if settings.get("gpm_integration_enabled", False):
        desktop = str(account.get("gpm_profile_id") or "").strip()
        mobile = str(account.get("gpm_mobile_profile_id") or "").strip()
        if desktop:
            keys.append(f"gpm:{desktop}")
        if mobile:
            keys.append(f"gpm:{mobile}")
    if not keys:
        keys.append(f"native:{account.get('email', '')}")
    return sorted(set(keys))


def _account_timeout_seconds(idx: int, max_threads: int, *, base_timeout_seconds: float = 4500.0) -> float:
    """Return a queue-aware timeout budget for one account run.

    `_safe_process` wraps the full account coroutine, which includes waiting for
    the global semaphore. When concurrency is capped below the selected account
    count, later accounts can spend tens of minutes waiting before their actual
    work starts. Add one base-timeout window per full batch ahead in the queue so
    each account still gets the intended active-processing budget.
    """
    effective_threads = max(1, int(max_threads or 1))
    batches_ahead = max(0, int(idx) // effective_threads)
    return float(base_timeout_seconds) * (1 + batches_ahead)


async def _wait_for_mobile_credit_update(searcher, page, settings: dict, *, baseline_status: dict) -> dict:
    """Poll mobile credits after a search pass so dashboard logs reflect real crediting."""
    attempts = max(1, int(settings.get("mobile_credit_postcheck_attempts", 3)))
    delay_seconds = max(2.0, float(settings.get("mobile_credit_postcheck_delay_seconds", 6)))
    latest_status = baseline_status

    for attempt in range(attempts):
        await asyncio.sleep(delay_seconds)
        latest_status = await _read_search_status_with_mobile_recheck(searcher, page, settings)
        _diag_log(
            settings,
            "Polled mobile credits after search pass",
            scope="mobile-postcheck",
            attempt=attempt + 1,
            attempts=attempts,
            baseline=summarize_search_status(baseline_status),
            latest=summarize_search_status(latest_status),
        )
        mobile_delta = _mobile_credit_delta(baseline_status, latest_status)
        points_delta = _total_points_delta(baseline_status, latest_status)
        if mobile_delta > 0:
            add_log(
                "info",
                f"📱 Mobile credits advanced after pass on attempt {attempt + 1}: "
                f"{latest_status.get('mobile_current', 0)}/{latest_status.get('mobile_max', 0)}",
            )
            return latest_status
        if points_delta > 0:
            add_log(
                "info",
                f"📱 Total points advanced after mobile pass on attempt {attempt + 1}: +{points_delta}",
            )
            return latest_status

    return latest_status


def _describe_deferred_items(snapshot: dict) -> list[str]:
    deferred_tasks = snapshot.get("deferred_tasks", [])
    descriptions: list[str] = []
    for item in deferred_tasks[:5]:
        title = str(item.get("title", "") or "").strip()
        reason = str(item.get("reason", "") or "").strip()
        if not title:
            continue
        if reason == "multi_day_search_bar":
            descriptions.append(f"Deferred: {title[:60]} (multi-day search-bar offer)")
        elif reason == "external_referral":
            descriptions.append(f"Deferred: {title[:60]} (requires friend referral activity)")
        else:
            descriptions.append(f"Deferred: {title[:60]}")
    if len(deferred_tasks) > 5:
        descriptions.append(f"{len(deferred_tasks) - 5} more deferred offer(s)")
    return descriptions


def _empty_search_status() -> dict:
    return {
        "pc_current": 0,
        "pc_max": 0,
        "mobile_current": 0,
        "mobile_max": 0,
        "edge_current": 0,
        "edge_max": 0,
        "total_points": 0,
    }


def _merge_search_status_sources(primary_status: dict, fallback_status: dict) -> dict:
    """Merge search counters from two readers, preferring the strongest non-zero evidence."""
    merged = dict(primary_status or {})
    fallback_status = fallback_status or {}
    for current_key, max_key in (
        ("pc_current", "pc_max"),
        ("mobile_current", "mobile_max"),
        ("edge_current", "edge_max"),
    ):
        primary_pair = (
            int(merged.get(current_key, 0) or 0),
            int(merged.get(max_key, 0) or 0),
        )
        fallback_pair = (
            int(fallback_status.get(current_key, 0) or 0),
            int(fallback_status.get(max_key, 0) or 0),
        )
        if fallback_pair[1] > primary_pair[1] or (
            fallback_pair[1] == primary_pair[1] and fallback_pair[0] > primary_pair[0]
        ):
            merged[current_key], merged[max_key] = fallback_pair
    merged["total_points"] = max(
        int(merged.get("total_points", 0) or 0),
        int(fallback_status.get("total_points", 0) or 0),
    )
    return merged


def _mode_status_resolved(status: dict, mode: str) -> bool:
    current_value, max_value = _mode_credit(status, mode)
    return current_value > 0 or max_value > 0


async def _read_search_status_for_runtime_descriptor(
    settings: dict,
    account: dict,
    session_proxy,
    login_mgr,
    searcher,
    storage_state_path: Path,
    runtime_descriptor: dict | None,
) -> tuple[dict, dict]:
    """Read Rewards counters from the runtime family that originally performed the work."""
    from src.browser import BrowserManager, load_storage_state_cookies

    mode = str((runtime_descriptor or {}).get("mode", "desktop") or "desktop")
    if not runtime_descriptor:
        return _empty_search_status(), build_search_verification(
            mode,
            None,
            verified=False,
            reason="missing_runtime_descriptor",
        )
    if not runtime_descriptor.get("account_proven", False):
        return _empty_search_status(), build_search_verification(
            mode,
            runtime_descriptor,
            verified=False,
            reason="runtime_account_unproven",
        )

    runtime_settings = dict(settings)
    runtime_settings["use_stealth"] = False
    browser_mgr = BrowserManager(runtime_settings)
    browser_mgr.set_account(account["email"])
    started_gpm_profile_id = ""
    ctx = None
    patchright_pw = None
    patchright_browser = None

    try:
        family = str(runtime_descriptor.get("family", "") or "")
        source_id = str(runtime_descriptor.get("source_id", "") or "")
        runtime_cdp_url = str(runtime_descriptor.get("cdp_url", "") or "")
        live_for_account_run = bool(runtime_descriptor.get("live_for_account_run", False))

        if family in {"gpm_desktop", "gpm_mobile"}:
            if live_for_account_run and runtime_cdp_url:
                await browser_mgr.start_connected_edge(runtime_cdp_url)
            else:
                if not source_id:
                    raise RuntimeError("missing_gpm_profile_id")
                runtime_cdp_url = await _start_gpm_profile_serialized(
                    source_id,
                    settings.get("gpm_api_url", "http://127.0.0.1:9495").rstrip("/"),
                )
                started_gpm_profile_id = source_id
                await browser_mgr.start_connected_edge(runtime_cdp_url)
            ctx, page = await _open_account_context(
                browser_mgr,
                login_mgr,
                account,
                session_proxy,
                mode,
                storage_state_path,
                attach_existing_edge=True,
                attached_cdp_url=runtime_cdp_url,
            )
        elif family == "native_edge":
            native_cdp = await browser_mgr.start_native_edge_runtime(account["email"])
            ctx, page = await _open_account_context(
                browser_mgr,
                login_mgr,
                account,
                session_proxy,
                mode,
                storage_state_path,
                attach_existing_edge=True,
                attached_cdp_url=native_cdp,
            )
        elif family == "managed_edge":
            await browser_mgr.start()
            ctx, page = await _open_account_context(
                browser_mgr,
                login_mgr,
                account,
                session_proxy,
                mode,
                storage_state_path,
                attach_existing_edge=False,
            )
        elif family == "patchright_mobile":
            patchright_pw, patchright_browser, ctx, page = await browser_mgr.create_mobile_patchright(
                load_storage_state_cookies(storage_state_path)
            )
            if not await login_mgr.is_logged_in(page):
                page = await login_mgr.login(
                    page,
                    account["email"],
                    account["password"],
                    account.get("totp_secret"),
                )
                ctx = page.context
        else:
            raise RuntimeError(f"unsupported_runtime_family:{family or 'unknown'}")

        if mode == "mobile":
            await browser_mgr.toggle_mobile_emulation(page, enable=True)
            await asyncio.sleep(1)

        status = await _read_search_status_with_mobile_recheck(
            searcher,
            page,
            settings,
            recheck_mobile=(mode == "mobile" and live_for_account_run),
        )
        if not _mode_status_resolved(status, mode):
            return status, build_search_verification(
                mode,
                runtime_descriptor,
                verified=False,
                reason="ambiguous_search_status",
            )
        return status, build_search_verification(
            mode,
            runtime_descriptor,
            verified=True,
        )
    except Exception as e:
        return _empty_search_status(), build_search_verification(
            mode,
            runtime_descriptor,
            verified=False,
            reason=str(e),
        )
    finally:
        try:
            if ctx is not None:
                await _persist_storage_state(ctx, storage_state_path)
        except Exception:
            pass
        if patchright_browser is not None:
            try:
                await patchright_browser.close()
            except Exception:
                pass
        if patchright_pw is not None:
            try:
                await patchright_pw.stop()
            except Exception:
                pass
        try:
            await browser_mgr.close()
        except Exception:
            pass
        if started_gpm_profile_id:
            try:
                await _stop_gpm_profile_serialized(
                    started_gpm_profile_id,
                    settings.get("gpm_api_url", "http://127.0.0.1:9495").rstrip("/"),
                )
            except Exception:
                pass


async def _collect_search_status_snapshot(
    settings: dict,
    account: dict,
    session_proxy,
    login_mgr,
    searcher,
    storage_state_path: Path,
    *,
    desktop_runtime: dict | None,
    mobile_runtime: dict | None,
) -> tuple[dict, dict]:
    """Verify desktop/mobile counters using the runtime families that produced them."""
    desktop_source = choose_search_verification_source(
        "desktop",
        desktop_runtime=desktop_runtime,
        mobile_runtime=mobile_runtime,
    )
    mobile_source = choose_search_verification_source(
        "mobile",
        desktop_runtime=desktop_runtime,
        mobile_runtime=mobile_runtime,
    )

    desktop_status, desktop_meta = await _read_search_status_for_runtime_descriptor(
        settings,
        account,
        session_proxy,
        login_mgr,
        searcher,
        storage_state_path,
        desktop_source,
    )
    mobile_status, mobile_meta = await _read_search_status_for_runtime_descriptor(
        settings,
        account,
        session_proxy,
        login_mgr,
        searcher,
        storage_state_path,
        mobile_source,
    )

    merged = merge_search_status(
        desktop_status=desktop_status,
        mobile_status=mobile_status,
    )
    return merged, {
        "desktop": desktop_meta,
        "mobile": mobile_meta,
        "edge": build_search_verification(
            "edge",
            desktop_source,
            verified=bool(desktop_meta.get("verified", False)),
            reason=desktop_meta.get("reason", ""),
        ),
    }


def _storage_state_path(email: str) -> Path:
    """Return the shared storage-state file for an account."""
    safe_email = email.replace("@", "_at_").replace(".", "_")
    return PROFILES_DIR / f"{safe_email}_state.json"


async def _persist_storage_state(context, storage_state_path: Path | None) -> None:
    """Persist cookies/local storage so later dashboard sessions reuse the login."""
    if not storage_state_path:
        return
    try:
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(storage_state_path))
    except Exception as e:
        logger.debug(f"Could not persist storage state {storage_state_path}: {e}")


# Cache GPM availability per run: None = not checked, True = online, False = offline
_gpm_available_cache: dict[str, bool | None] = {}
_gpm_lifecycle_lock = asyncio.Lock()


async def _check_gpm_online(api_url: str) -> bool:
    """Quick check if AntiDetect Login app is running. Caches result per api_url."""
    global _gpm_available_cache
    if _gpm_available_cache.get(api_url) is not None:
        return _gpm_available_cache[api_url]
    import httpx
    try:
        from src.utils import load_settings
        settings = load_settings()
        platform = settings.get("browser_type", "gpm")
        
        async with httpx.AsyncClient(timeout=3.0) as client:
            if platform == "genlogin":
                r = await client.get(f"{api_url}/profiles", follow_redirects=True)
            elif platform == "adspower":
                r = await client.get(f"{api_url}/status", follow_redirects=True)
            elif platform == "dolphin":
                r = await client.get(f"{api_url}/v1.0/browser_profiles", follow_redirects=True)
            elif platform == "vmlogin":
                r = await client.get(f"{api_url}/api/v1/profile/list", follow_redirects=True)
            else:
                r = await client.get(f"{api_url}/api/v1/profiles", follow_redirects=True)
            
            _gpm_available_cache[api_url] = r.status_code < 500
    except Exception:
        _gpm_available_cache[api_url] = False
    return _gpm_available_cache[api_url]




async def _start_gpm_profile(gpm_profile_id: str, api_url: str) -> str:
    """Start AntiDetect profile and return CDP url. Raises on failure.

    Uses httpx async to avoid blocking the event loop.
    Pre-checks GPM/AntiDetect is online (cached) to avoid long timeout per account.
    """
    global _gpm_available_cache
    
    from src.utils import load_settings
    settings = load_settings()
    platform = settings.get("browser_type", "gpm")
    

    # Fast-fail: if GPM was already confirmed offline this run, don't retry
    if not await _check_gpm_online(api_url):
        raise RuntimeError(
            f"AntiDetect Login app is not running at {api_url} for platform {platform}. "
            "Please start the Application before running the bot."
        )

    import httpx
    import re
    import subprocess
    async with httpx.AsyncClient(timeout=15.0) as client:
        req_url = f"{api_url}/api/v1/profiles/start/{gpm_profile_id}"
        if platform == "genlogin":
            req_url = f"{api_url}/profiles/start/{gpm_profile_id}"
        elif platform == "adspower":
            req_url = f"{api_url}/api/v1/browser/start?user_id={gpm_profile_id}"
        elif platform == "dolphin":
            req_url = f"{api_url}/v1.0/browser_profiles/{gpm_profile_id}/start?automation=1"
        elif platform == "vmlogin":
            req_url = f"{api_url}/api/v1/profile/start?profileId={gpm_profile_id}"
            
        resp = await client.get(req_url)
        resp.raise_for_status()
        data = resp.json()
        
        # Mark as online
        _gpm_available_cache[api_url] = True
        
        # Recursively search for port in dict
        def extract_port(d):
            if not isinstance(d, dict): return None
            # Common keys: remote_debugging_port, port, debug_port, ws
            if "remote_debugging_port" in d: return d["remote_debugging_port"]
            if "debug_port" in d: return d["debug_port"]
            if "port" in d: return d["port"]
            if "ws" in d and isinstance(d["ws"], dict):
                ws_url = d["ws"].get("puppeteer")
                if ws_url:
                    # extract port from ws://127.0.0.1:PORT/
                    m = re.search(r"ws://[0-9\.]+:(?P<port>\d+)", str(ws_url))
                    if m: return int(m.group("port"))
            # recursive
            for v in d.values():
                if isinstance(v, dict):
                    res = extract_port(v)
                    if res: return res
            return None

        port = extract_port(data)
        if port:
            return f"http://127.0.0.1:{port}"
        
        # If profile is already in use, Login app won't return the port via start API.
        # We can extract the port by parsing the command line of running processes.
        if isinstance(data.get("message"), str) and "ProfileInUse" in data.get("message", ""):
            try:
                # Use powershell via subprocess to dump command lines of all processes
                cmd = ['powershell', '-NoProfile', '-Command', 'Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine']
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')
                for line in out.splitlines():
                    if gpm_profile_id in line and "--remote-debugging-port=" in line:
                        m = re.search(r"--remote-debugging-port=(\d+)", line)
                        if m:
                            _gpm_available_cache[api_url] = True
                            return f"http://127.0.0.1:{m.group(1)}"
            except Exception as subprocess_err:
                pass
                
        raise RuntimeError(str(data.get("message", "Unknown AntiDetect API error")))



def _stop_gpm_profile(gpm_profile_id: str, api_url: str):
    import urllib.request
    try:
        req = urllib.request.Request(f"{api_url}/api/v1/profiles/stop/{gpm_profile_id}")
        urllib.request.urlopen(req, timeout=10)
    except Exception as _e:
        logger.debug(f"GPM stop suppressed: {_e}")


async def _start_gpm_profile_serialized(gpm_profile_id: str, api_url: str) -> str:
    async with _gpm_lifecycle_lock:
        return await _start_gpm_profile(gpm_profile_id, api_url)


async def _stop_gpm_profile_serialized(gpm_profile_id: str, api_url: str) -> None:
    async with _gpm_lifecycle_lock:
        _stop_gpm_profile(gpm_profile_id, api_url)


async def _open_account_context(
    browser_mgr,
    login_mgr,
    account: dict,
    session_proxy: dict | None,
    mode: str,
    storage_state_path: Path,
    *,
    user_agent: str | None = None,
    use_persistent_profile: bool = False,
    reopen_with_clean_edge: bool = False,
    attach_existing_edge: bool = False,
    attached_cdp_url: str = "",
):
    """Open a context for one mode, reusing stored session state when available."""
    async def _spawn_page():
        storage_state = None if attach_existing_edge else (
            str(storage_state_path) if storage_state_path.exists() else None
        )
        browser = getattr(browser_mgr, "browser", None)
        is_connected = False
        try:
            is_connected = browser is not None and browser.is_connected()
        except Exception:
            is_connected = False

        if not is_connected:
            try:
                await browser_mgr.close()
            except Exception as _e:
                logger.debug(f"browser close suppressed: {_e}")
            if attach_existing_edge:
                await browser_mgr.start_connected_edge(attached_cdp_url)
            elif reopen_with_clean_edge:
                await browser_mgr.start_clean_edge()
            else:
                await browser_mgr.start()

        ctx_local = await browser_mgr.create_context(
            mode=mode,
            account_email=account["email"],
            proxy=session_proxy,
            user_agent=user_agent,
            storage_state=storage_state,
            use_persistent_profile=use_persistent_profile,
        )
        page_local = await browser_mgr.new_page(ctx_local)
        
        # Resize OS window via CDP according to user request
        try:
            client = await page_local.context.new_cdp_session(page_local)
            result = await client.send("Browser.getWindowForTarget")
            window_id = result.get("windowId")
            if window_id:
                if mode == "mobile":
                    await client.send("Browser.setWindowBounds", {
                        "windowId": window_id,
                        "bounds": {"width": 400, "height": 850, "windowState": "normal"}
                    })
                else:
                    await client.send("Browser.setWindowBounds", {
                        "windowId": window_id,
                        "bounds": {"windowState": "maximized"}
                    })
        except Exception as e:
            logger.debug(f"Could not resize window via CDP: {e}")

        return page_local

    page = await _spawn_page()
    logged_in = await login_mgr.is_logged_in(page)

    if attach_existing_edge and logged_in:
        pass
    elif not storage_state_path.exists():
        page = await login_mgr.login(
            page,
            account["email"],
            account["password"],
            account.get("totp_secret"),
            recover_page=_spawn_page,
        )
    elif not logged_in:
        if not attach_existing_edge:
            try:
                await page.context.close()
            except Exception:
                pass
            try:
                await browser_mgr.close()
            except Exception:
                pass
            if reopen_with_clean_edge:
                await browser_mgr.start_clean_edge()
            else:
                await browser_mgr.start()
            original_exists = storage_state_path.exists()
            try:
                storage_state_path.unlink()
            except Exception:
                if original_exists:
                    logger.debug(f"Could not remove stale storage state: {storage_state_path}")
        page = await _spawn_page()
        page = await login_mgr.login(
            page,
            account["email"],
            account["password"],
            account.get("totp_secret"),
            recover_page=_spawn_page,
        )

    ctx = page.context
    await _persist_storage_state(ctx, storage_state_path)
    return ctx, page


async def _page_is_usable(page) -> bool:
    """Best-effort health check before issuing a long search batch."""
    if page is None:
        return False
    try:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed) and is_closed():
            return False
    except Exception:
        return False
    try:
        context = page.context
    except Exception:
        return False
    try:
        browser = getattr(context, "browser", None)
        is_connected = getattr(browser, "is_connected", None)
        if callable(is_connected) and not is_connected():
            return False
    except Exception:
        return False
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            await page.evaluate("() => 1")
        except Exception:
            return False
    return True


async def _ensure_usable_desktop_search_page(
    settings: dict,
    browser_mgr,
    login_mgr,
    account: dict,
    session_proxy,
    storage_state_path: Path,
    desktop_runtime: dict | None,
    ctx,
    page,
):
    """Recover a dead desktop search page from the live runtime before search #1."""
    if await _page_is_usable(page):
        return ctx, page

    runtime_cdp_url = str((desktop_runtime or {}).get("cdp_url", "") or "")
    live_for_account_run = bool((desktop_runtime or {}).get("live_for_account_run", False))
    runtime_family = str((desktop_runtime or {}).get("family", "") or "")
    masked_email = mask_email(account.get("email", ""))

    if not (live_for_account_run and runtime_cdp_url):
        raise RuntimeError(
            "Desktop search page is no longer usable before search start and no live runtime is available for reacquire."
        )

    add_log("warning", "🖥️ Desktop page became unusable; reacquiring from live runtime...")
    _diag_log(
        settings,
        "Reacquiring unusable desktop page from live runtime",
        scope="desktop-reacquire",
        account=masked_email,
        runtime_family=runtime_family,
        cdp_url=runtime_cdp_url,
    )

    try:
        ctx, page = await _open_account_context(
            browser_mgr,
            login_mgr,
            account,
            session_proxy,
            "desktop",
            storage_state_path,
            attach_existing_edge=True,
            attached_cdp_url=runtime_cdp_url,
        )
    except Exception as e:
        raise RuntimeError(
            f"Desktop search page is no longer usable before search start and could not be reacquired from {runtime_cdp_url}: {e}"
        ) from e

    if not await _page_is_usable(page):
        raise RuntimeError(
            f"Desktop search page is no longer usable before search start and the live runtime {runtime_cdp_url} still returned an unusable page."
        )

    add_log("info", "🖥️ Reacquired desktop page from live runtime")
    return ctx, page



# ─── Auth ──────────────────────────────────────────────────────────────────

def _dashboard_password_hash(settings: dict | None = None) -> str:
    settings = settings or load_settings()
    return str(settings.get("master_password_hash", "") or "").strip()


def _dashboard_auth_required(settings: dict | None = None) -> bool:
    return bool(_dashboard_password_hash(settings))


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _ensure_dashboard_bind_is_safe(host: str, settings: dict | None = None) -> None:
    settings = settings or load_settings()
    if _dashboard_auth_required(settings):
        return
    if not _is_loopback_host(host):
        raise RuntimeError(
            "Dashboard authentication is not configured. "
            "Refusing to bind the dashboard to a non-loopback host."
        )


def _dashboard_request_authenticated(settings: dict | None = None) -> bool:
    settings = settings or load_settings()
    if not _dashboard_auth_required(settings):
        return True
    return bool(session.get("dashboard_authenticated", False))


@app.before_request
def require_dashboard_auth():
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    if path in {"/api/auth", "/api/auth/check"}:
        return None
    settings = load_settings()
    if _dashboard_request_authenticated(settings):
        return None
    return jsonify({"error": "Authentication required", "code": "auth_required"}), 401

@app.route("/api/auth", methods=["POST"])
def auth():
    """Authenticate dashboard access using the configured master password hash."""
    settings = load_settings()
    password_hash = _dashboard_password_hash(settings)
    if not password_hash:
        session["dashboard_authenticated"] = True
        return jsonify({"status": "ok", "message": "Dashboard auth not required", "required": False})

    data = request.json or {}
    password = str(data.get("password", "") or "")
    if not verify_password(password, password_hash):
        session["dashboard_authenticated"] = False
        return jsonify({"error": "Wrong password", "required": True}), 401

    session["dashboard_authenticated"] = True
    with _state_lock:
        state["master_password"] = password
    return jsonify({"status": "ok", "message": "Authenticated", "required": True})


@app.route("/api/auth/check", methods=["GET"])
def auth_check():
    """Return whether the current dashboard session is authenticated."""
    settings = load_settings()
    required = _dashboard_auth_required(settings)
    return jsonify({
        "authenticated": _dashboard_request_authenticated(settings),
        "required": required,
    })


# ─── Accounts ──────────────────────────────────────────────────────────────

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    """List accounts (email only, no passwords)."""
    try:
        accounts = load_encrypted_accounts(state["master_password"])
        safe = [
            {
                "email": a["email"],
                "has_totp": bool(a.get("totp_secret")),
                "has_proxy": bool(a.get("proxy")),
                "gpm_profile_id": a.get("gpm_profile_id", ""),
                "gpm_mobile_profile_id": a.get("gpm_mobile_profile_id", ""),
                "has_session": storage_state.exists(),
                "session_updated": (
                    datetime.fromtimestamp(storage_state.stat().st_mtime).strftime("%H:%M:%S")
                    if storage_state.exists()
                    else None
                ),
            }
            for a in accounts
            for storage_state in [_storage_state_path(a["email"])]
        ]
        return jsonify({"accounts": safe})
    except FileNotFoundError:
        return jsonify({"accounts": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts", methods=["POST"])
def add_account():
    """Add a new account."""

    data = request.json or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    old_email = data.get("old_email", "").strip()

    if not email:
        return jsonify({"error": "Email is required"}), 400
    if not old_email and not password:
        return jsonify({"error": "Password is required for new accounts"}), 400

    account = {
        "email": email,
        "totp_secret": data.get("totp_secret", "").strip() or None,
        "proxy": data.get("proxy", "").strip() or None,
        "gpm_profile_id": data.get("gpm_profile_id", "").strip() or None,
        "gpm_mobile_profile_id": data.get("gpm_mobile_profile_id", "").strip() or None,
    }

    try:
        try:
            accounts = load_encrypted_accounts(state["master_password"])
        except FileNotFoundError:
            accounts = []

        if old_email:
            # We are editing an existing account
            idx = next((i for i, a in enumerate(accounts) if a["email"] == old_email), -1)
            if idx != -1:
                account["password"] = password if password else accounts[idx]["password"]
                accounts[idx] = account
                save_encrypted_accounts(accounts, state["master_password"])
                add_log("info", f"Account updated: {email[:5]}***")
                return jsonify({"status": "ok"})
            else:
                return jsonify({"error": "Old account not found"}), 404
        else:
            # Check duplicate
            if any(a["email"] == email for a in accounts):
                return jsonify({"error": "Account already exists"}), 409

            account["password"] = password
            accounts.append(account)
            save_encrypted_accounts(accounts, state["master_password"])
            add_log("info", f"Account added: {email[:5]}***")
            return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/import", methods=["POST"])
def import_accounts():
    """Import accounts from raw JSON payload."""
    try:
        data = request.json
        if not data or not isinstance(data, list):
            return jsonify({"error": "Invalid format, expected JSON array"}), 400
        
        accounts = load_encrypted_accounts(state["master_password"])
        # Merge by email
        existing_emails = {a["email"] for a in accounts}
        imported_count = 0
        for new_acc in data:
            if "email" in new_acc and new_acc["email"] not in existing_emails:
                accounts.append(new_acc)
                existing_emails.add(new_acc["email"])
                imported_count += 1
            elif "email" in new_acc:
                # update existing
                for a in accounts:
                    if a["email"] == new_acc["email"]:
                        a.update(new_acc)
                        imported_count += 1

        save_encrypted_accounts(accounts, state["master_password"])
        add_log("info", f"Successfully imported {imported_count} accounts")
        return jsonify({"status": "ok", "message": f"Imported {imported_count} accounts"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/export", methods=["GET"])
def export_accounts():
    """Export all accounts as plain JSON."""
    try:
        accounts = load_encrypted_accounts(state["master_password"])
        return jsonify(accounts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<email>", methods=["DELETE"])
def delete_account(email):
    """Delete an account."""

    try:
        accounts = load_encrypted_accounts(state["master_password"])
        accounts = [a for a in accounts if a["email"] != email]
        save_encrypted_accounts(accounts, state["master_password"])
        storage_state = _storage_state_path(email)
        if storage_state.exists():
            storage_state.unlink()
        add_log("info", f"Account removed: {email[:5]}***")
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Settings ──────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Get all settings (hide passwords/tokens)."""
    settings = load_settings()
    safe = {}
    for k, v in settings.items():
        if is_sensitive_setting(k):
            safe[k] = "***" if v else ""
        else:
            safe[k] = v
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Update settings."""
    data = request.json or {}
    settings = load_settings()

    for key, value in data.items():
        if value == KEEP_EXISTING_SECRET:
            continue
        if "password_hash" not in key:
            settings[key] = value

    save_settings(settings)
    add_log("info", "Settings updated")
    return jsonify({"status": "ok"})


@app.route("/api/gpm/profiles", methods=["GET"])
def get_gpm_profiles():
    """Fetch profiles from GPM Login API."""
    settings = load_settings()
    api_url = settings.get("gpm_api_url", "http://127.0.0.1:9495").rstrip("/")
    import urllib.request
    try:
        req = urllib.request.Request(f"{api_url}/api/v1/profiles?page=1&per_page=300")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("success"):
                return jsonify({"profiles": data["data"]["data"]})
            return jsonify({"profiles": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Bot Control ───────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def run_bot():
    """Start the bot in a background thread."""
    data = request.json or {}
    task = data.get("task", "all")  # all, searches, daily, punch, promos, bootstrap
    target_emails = data.get("target_emails", [])

    global _gpm_available_cache
    _gpm_available_cache.clear()

    with _state_lock:
        if state["status"] == "running":
            return jsonify({"error": "Bot is already running"}), 409

        state["status"] = "running"
        state["current_task"] = task
        state["current_account"] = ""
        state["progress"] = 0
        state["progress_total"] = 0
        state["total_points"] = 0
        state["logs"] = []
        state["account_logs"] = {}
        state["accounts"] = {}  # Reset per-account tracking on new run
        state["ai"] = {
            "active": False,
            "last_update": datetime.now().isoformat(timespec="seconds"),
            "last_event": "Đã khởi tạo phiên chạy mới.",
            "task": task,
            "model": load_settings().get("ai_model", ""),
            "last_level": "info",
        }
    
    if target_emails:
        add_log("info", f"Starting task: {task} (Targeted: {len(target_emails)})")
    else:
        add_log("info", f"Starting task: {task} (All Accounts)")

    import subprocess
    import sys
    
    job_id = "job-" + datetime.now().strftime("%Y%m%d%H%M%S")
    with _state_lock:
        state["job_id"] = job_id

    cmd = [
        "cargo", "run", "-q", "-p", "autobing-control-plane", "--",
        "start-job",
        "--job-id", job_id,
        "--task", task,
        "--secret-ref", "env:REWARDS_BOT_PASSWORD"
    ]
    for email in target_emails:
        cmd.extend(["--target-email", email])

    env = os.environ.copy()
    env["REWARDS_BOT_PASSWORD"] = state["master_password"]
    
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    
    try:
        subprocess.Popen(cmd, env=env, **kwargs)
        logger.info(f"Launched Rust control plane for job {job_id}")
    except Exception as e:
        add_log("error", f"Failed to launch Rust control plane: {e}")
        with _state_lock:
            state["status"] = "error"

    return jsonify({"status": "started", "task": task, "job_id": job_id})


def _get_effective_state():
    global state
    with _state_lock:
        if DASHBOARD_STATE_FILE.exists():
            try:
                ext_state = json.loads(DASHBOARD_STATE_FILE.read_text(encoding="utf-8"))
                ext_status = ext_state.get("status", "idle")
                
                if ext_status in ("running", "stopping"):
                    return ext_state
                
                state["accounts"] = ext_state.get("accounts", state["accounts"])
                state["account_logs"] = ext_state.get("account_logs", state.get("account_logs", {}))
                state["logs"] = ext_state.get("logs", state.get("logs", []))
                state["status"] = ext_status
                state["current_task"] = ext_state.get("current_task", "")
                state["progress"] = ext_state.get("progress", 0)
                state["progress_total"] = ext_state.get("progress_total", 0)
                
                try:
                    DASHBOARD_STATE_FILE.unlink()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Failed to read external state: {e}")
        return state


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    """Stop the bot (sets stop flag)."""
    from src.worker_store import cancel_job
    with _state_lock:
        eff_state = _get_effective_state()
        if eff_state["status"] != "running":
            return jsonify({"error": "Bot is not running"}), 400
        state["status"] = "stopping"
        job_id = state.get("job_id") or eff_state.get("job_id")
        if job_id:
             try:
                 cancel_job(job_id)
             except Exception as e:
                 logger.error(f"Failed to cancel job: {e}")
                 
    add_log("warning", "Stop requested")
    return jsonify({"status": "stopping"})


@app.route("/api/status", methods=["GET"])
def get_status():
    """Get current bot status including per-account progress."""
    settings = load_settings()
    eff = _get_effective_state()
    # No need to hold the lock while building profiles since eff is a distinct dict snapshot
    accounts_snapshot = dict(eff.get("accounts", {}))
    account_logs_snapshot = dict(eff.get("account_logs", {}))
    current_account = eff.get("current_account", "")
    current_task = eff.get("current_task", "")
    progress = eff.get("progress", 0)
    progress_total = eff.get("progress_total", 0)
    last_run = eff.get("last_run", None)
    total_points = eff.get("total_points", 0)
    status_value = eff.get("status", "idle")
    ai_snapshot = dict(eff.get("ai", {}))
    
    profiles = _build_profile_views(accounts_snapshot, account_logs_snapshot)
    summary = _build_profile_summary(profiles)
    overview = _build_dashboard_overview(profiles)
    current_profile = next((profile for profile in profiles if profile["key"] == current_account), None)
    ai_snapshot["enabled"] = bool(settings.get("ai_enabled", False))
    ai_snapshot["configured"] = bool(settings.get("ai_api_key") or settings.get("ai_api_url"))
    ai_snapshot["model"] = ai_snapshot.get("model") or settings.get("ai_model", "")
    return jsonify({
        "status": status_value,
        "current_account": current_account,
        "current_task": current_task,
        "progress": progress,
        "progress_total": progress_total,
        "last_run": last_run,
        "total_points": total_points,
        "accounts": accounts_snapshot,
        "profiles": profiles,
        "summary": summary,
        "overview": overview,
        "current_profile": current_profile,
        "ai": ai_snapshot,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })


@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Get log entries."""
    since = request.args.get("since", 0, type=int)
    eff = _get_effective_state()
    return jsonify({"logs": eff.get("logs", [])[since:]})


@app.route("/api/logs/accounts", methods=["GET"])
def get_account_logs():
    """Get per-account log entries for dashboard tabs."""
    account = request.args.get("account", "").strip()
    since = request.args.get("since", 0, type=int)
    eff = _get_effective_state()
    account_logs = eff.get("account_logs", {})
    if account:
        logs = list(account_logs.get(account, []))
        return jsonify({"logs": logs[since:], "account": account})
    # Return list of accounts that have logs
    accounts_with_logs = list(account_logs.keys())

    return jsonify({"accounts": accounts_with_logs})


def _resolve_account_email(account_key: str) -> str:
    with _state_lock:
        account = state["accounts"].get(account_key, {})
        email = str(account.get("email", "") or "").strip()
    if email:
        return email
    try:
        for account in load_encrypted_accounts(""):
            if _account_state_key(account.get("email", "")) == account_key:
                return account.get("email", "")
    except Exception:
        pass
    return ""


def _read_archived_account_log(email: str, date_key: str) -> list[dict]:
    if not email or not date_key:
        return []
    safe_email = email.replace("@", "_at_").replace(".", "_")
    compact_date = date_key.replace("-", "")
    log_path = DATA_DIR / "logs" / f"acc_{safe_email}_{compact_date}.log"
    if not log_path.exists():
        return []
    entries: list[dict] = []
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.rstrip()
                if not line:
                    continue
                time_part = line[:8] if len(line) >= 8 else ""
                level_part = line[10:18].strip() if len(line) >= 18 else "info"
                message = line[20:].strip() if len(line) >= 20 else line
                entries.append({
                    "time": time_part,
                    "level": level_part.lower(),
                    "message": message,
                })
    except Exception:
        return []
    return entries


@app.route("/api/dashboard/overview", methods=["GET"])
def get_dashboard_overview():
    with _state_lock:
        accounts_snapshot = dict(state["accounts"])
        account_logs_snapshot = dict(state["account_logs"])
    profiles = _build_profile_views(accounts_snapshot, account_logs_snapshot)
    return jsonify({"overview": _build_dashboard_overview(profiles)})


@app.route("/api/dashboard/accounts/<account_key>/history", methods=["GET"])
def get_dashboard_account_history(account_key: str):
    days = request.args.get("days", 30, type=int)
    days = max(1, min(days, 90))
    records = _recent_account_snapshots(account_key, days)
    return jsonify({"account_key": account_key, "history": records})


@app.route("/api/dashboard/accounts/<account_key>/logs", methods=["GET"])
def get_dashboard_account_logs(account_key: str):
    date_key = request.args.get("date", "").strip()
    email = _resolve_account_email(account_key)
    if not date_key:
        with _state_lock:
            logs = list(state["account_logs"].get(account_key, []))
        return jsonify({"account_key": account_key, "email": email, "logs": logs, "date": _current_reset_context()[0]})
    return jsonify({
        "account_key": account_key,
        "email": email,
        "logs": _read_archived_account_log(email, date_key),
        "date": date_key,
    })


def _search_count_setting(settings: dict, mode: str) -> int:
    """Return configured search count for a mode."""
    key = "desktop_searches" if mode == "desktop" else f"{mode}_searches"
    return int(settings.get(key, 30))


def _mode_credit(status: dict, mode: str) -> tuple[int, int]:
    """Return current/max points for one search mode."""
    if mode == "desktop":
        return status.get("pc_current", 0), status.get("pc_max", 0)
    if mode == "mobile":
        return status.get("mobile_current", 0), status.get("mobile_max", 0)
    return status.get("edge_current", 0), status.get("edge_max", 0)


def _needs_desktop_credit_recheck(status: dict, settings: dict) -> bool:
    """Detect ambiguous desktop 0/0 reads that should not auto-trigger a full batch."""
    desktop_searches = _search_count_setting(settings, "desktop")
    current, maximum = _mode_credit(status, "desktop")
    return desktop_searches > 0 and current == 0 and maximum == 0


def _needs_mobile_credit_recheck(status: dict, settings: dict) -> bool:
    """Detect ambiguous mobile 0/0 reads that should not auto-skip searches."""
    mobile_searches = _search_count_setting(settings, "mobile")
    current, maximum = _mode_credit(status, "mobile")
    return mobile_searches > 0 and current == 0 and maximum == 0


async def _read_search_status_with_mobile_recheck(
    searcher,
    page,
    settings: dict,
    *,
    recheck_mobile: bool = True,
) -> dict:
    """Retry Rewards counters when mobile credits come back as an ambiguous 0/0."""
    status = await searcher.get_search_points_status(page)
    if recheck_mobile and _needs_mobile_credit_recheck(status, settings):
        try:
            task_status = await TaskDetector.get_all_tasks(page)
            status = _merge_search_status_sources(
                status,
                {
                    **task_status.get("searches", {}),
                    "total_points": task_status.get("total_points", 0),
                },
            )
        except Exception as e:
            logger.debug(f"TaskDetector search-status fallback skipped: {e}")
    _diag_log(
        settings,
        "Initial search-credit read",
        scope="search-status",
        status=summarize_search_status(status),
        page_url=getattr(page, "url", ""),
    )
    if not recheck_mobile or not _needs_mobile_credit_recheck(status, settings):
        return status

    retries = max(1, int(settings.get("mobile_credit_recheck_attempts", 2)))
    delay_seconds = max(1.0, float(settings.get("mobile_credit_recheck_delay_seconds", 3)))

    add_log("info", "📱 Mobile credits returned 0/0; rechecking before skip.")
    for attempt in range(retries):
        await asyncio.sleep(delay_seconds)
        refreshed = await searcher.get_search_points_status(page)
        if _needs_mobile_credit_recheck(refreshed, settings):
            try:
                task_status = await TaskDetector.get_all_tasks(page)
                refreshed = _merge_search_status_sources(
                    refreshed,
                    {
                        **task_status.get("searches", {}),
                        "total_points": task_status.get("total_points", 0),
                    },
                )
            except Exception as e:
                logger.debug(f"TaskDetector search-status fallback skipped on retry: {e}")
        status = refreshed
        _diag_log(
            settings,
            "Mobile credit recheck attempt finished",
            scope="search-status",
            attempt=attempt + 1,
            retries=retries,
            status=summarize_search_status(status),
        )
        if not _needs_mobile_credit_recheck(status, settings):
            add_log(
                "info",
                f"📱 Mobile credit recheck resolved on attempt {attempt + 1}: "
                f"{status.get('mobile_current', 0)}/{status.get('mobile_max', 0)}",
            )
            break

    return status


async def _probe_search_status_in_mode(
    settings: dict,
    account: dict,
    session_proxy,
    login_mgr,
    searcher,
    storage_state_path: Path,
    *,
    mode: str,
) -> dict:
    """Read Rewards counters in a dedicated browser mode/runtime."""
    from src.browser import BrowserManager

    runtime_settings = dict(settings)
    runtime_settings["use_stealth"] = False
    runtime_settings["headless"] = True
    browser_mgr = BrowserManager(runtime_settings)
    browser_mgr.set_account(account["email"])
    masked_email = mask_email(account.get("email", ""))

    try:
        _diag_log(
            settings,
            "Opening dedicated probe runtime",
            scope="search-probe",
            account=masked_email,
            mode=mode,
            has_storage_state=storage_state_path.exists(),
            proxy=bool(session_proxy),
        )
        await browser_mgr.start()
        ctx, page = await _open_account_context(
            browser_mgr,
            login_mgr,
            account,
            session_proxy,
            mode,
            storage_state_path,
            use_persistent_profile=False,
        )
        if mode == "mobile":
            try:
                await browser_mgr.toggle_mobile_emulation(page, enable=True)
                await asyncio.sleep(1)
                _diag_log(
                    settings,
                    "Mobile emulation enabled for probe",
                    scope="search-probe",
                    account=masked_email,
                )
            except Exception as e:
                add_log("warning", f"📱 Mobile probe emulation activation failed: {e}")
        status = await _read_search_status_with_mobile_recheck(searcher, page, settings)
        _diag_log(
            settings,
            "Probe runtime returned search status",
            scope="search-probe",
            account=masked_email,
            mode=mode,
            status=summarize_search_status(status),
        )
        await _persist_storage_state(ctx, storage_state_path)
        return status
    finally:
        try:
            await browser_mgr.close()
        except Exception:
            pass


async def _resolve_mobile_search_requirement(
    settings: dict,
    account: dict,
    session_proxy,
    login_mgr,
    searcher,
    storage_state_path: Path,
    baseline_status: dict,
) -> dict:
    """Resolve ambiguous mobile 0/0 credits before deciding to skip mobile searches."""
    if not _needs_mobile_credit_recheck(baseline_status, settings):
        return baseline_status

    _diag_log(
        settings,
        "Resolving ambiguous mobile credits",
        scope="mobile-resolution",
        account=mask_email(account.get("email", "")),
        baseline=summarize_search_status(baseline_status),
    )
    add_log("info", "📱 Mobile credits ambiguous on desktop session; probing mobile runtime...")
    try:
        probed_status = await _probe_search_status_in_mode(
            settings,
            account,
            session_proxy,
            login_mgr,
            searcher,
            storage_state_path,
            mode="mobile",
        )
    except Exception as e:
        add_log("warning", f"📱 Mobile probe failed: {e}")
        return baseline_status

    if _needs_mobile_credit_recheck(probed_status, settings):
        add_log("warning", "📱 Mobile probe still returned 0/0; will not auto-skip mobile searches.")
        _diag_log(
            settings,
            "Mobile probe remained ambiguous",
            scope="mobile-resolution",
            account=mask_email(account.get("email", "")),
            probed=summarize_search_status(probed_status),
        )
        return baseline_status

    merged = dict(baseline_status)
    merged["mobile_current"] = probed_status.get("mobile_current", 0)
    merged["mobile_max"] = probed_status.get("mobile_max", 0)
    if probed_status.get("total_points", 0) > 0:
        merged["total_points"] = probed_status.get("total_points", 0)
    add_log(
        "info",
        f"📱 Mobile runtime probe resolved credits: "
        f"{merged.get('mobile_current', 0)}/{merged.get('mobile_max', 0)}",
    )
    _diag_log(
        settings,
        "Mobile credits resolved after probe merge",
        scope="mobile-resolution",
        account=mask_email(account.get("email", "")),
        merged=summarize_search_status(merged),
    )
    return merged


async def _resolve_desktop_search_requirement(
    settings: dict,
    account: dict,
    session_proxy,
    login_mgr,
    searcher,
    storage_state_path: Path,
    baseline_status: dict,
    desktop_runtime: dict | None,
) -> dict:
    """Resolve ambiguous desktop 0/0 credits before deciding to run a fallback batch."""
    if not _needs_desktop_credit_recheck(baseline_status, settings):
        return baseline_status

    masked_email = mask_email(account.get("email", ""))
    runtime_family = str((desktop_runtime or {}).get("family", "") or "")
    runtime_is_live = bool((desktop_runtime or {}).get("live_for_account_run", False))
    if runtime_family == "gpm_desktop" and runtime_is_live:
        _diag_log(
            settings,
            "Desktop credits are ambiguous on the live GPM runtime; probing a dedicated desktop runtime before planning a full fallback batch.",
            scope="desktop-resolution",
            account=masked_email,
            baseline=summarize_search_status(baseline_status),
            runtime_family=runtime_family,
        )
        try:
            probed_status = await _probe_search_status_in_mode(
                settings,
                account,
                session_proxy,
                login_mgr,
                searcher,
                storage_state_path,
                mode="desktop",
            )
            if not _needs_desktop_credit_recheck(probed_status, settings):
                merged = dict(baseline_status)
                merged["pc_current"] = probed_status.get("pc_current", 0)
                merged["pc_max"] = probed_status.get("pc_max", 0)
                merged["edge_current"] = probed_status.get("edge_current", merged.get("edge_current", 0))
                merged["edge_max"] = probed_status.get("edge_max", merged.get("edge_max", 0))
                if probed_status.get("total_points", 0) > 0:
                    merged["total_points"] = probed_status.get("total_points", 0)
                add_log(
                    "info",
                    f"🖥️ Dedicated desktop probe resolved credits: "
                    f"{merged.get('pc_current', 0)}/{merged.get('pc_max', 0)}",
                )
                _diag_log(
                    settings,
                    "Desktop credits resolved through dedicated desktop probe",
                    scope="desktop-resolution",
                    account=masked_email,
                    merged=summarize_search_status(merged),
                )
                return merged
        except Exception as e:
            add_log("warning", f"🖥️ Dedicated desktop probe failed: {e}")
            _diag_log(
                settings,
                "Dedicated desktop probe raised an exception",
                scope="desktop-resolution",
                account=masked_email,
                error=str(e),
            )

    _diag_log(
        settings,
        "Resolving ambiguous desktop credits",
        scope="desktop-resolution",
        account=masked_email,
        baseline=summarize_search_status(baseline_status),
        runtime_family=runtime_family,
    )
    add_log("info", "🖥️ Desktop credits ambiguous on current session; probing original desktop runtime...")
    try:
        probed_status, verification = await _read_search_status_for_runtime_descriptor(
            settings,
            account,
            session_proxy,
            login_mgr,
            searcher,
            storage_state_path,
            desktop_runtime,
        )
    except Exception as e:
        add_log("warning", f"🖥️ Desktop probe failed: {e}")
        return baseline_status

    if not verification.get("verified", False):
        _diag_log(
            settings,
            "Desktop probe could not verify original runtime",
            scope="desktop-resolution",
            account=masked_email,
            reason=verification.get("reason", ""),
        )
        return baseline_status

    if _needs_desktop_credit_recheck(probed_status, settings):
        add_log("warning", "🖥️ Desktop probe still returned 0/0; will not auto-skip desktop searches.")
        _diag_log(
            settings,
            "Desktop probe remained ambiguous",
            scope="desktop-resolution",
            account=masked_email,
            probed=summarize_search_status(probed_status),
        )
        return baseline_status

    merged = dict(baseline_status)
    merged["pc_current"] = probed_status.get("pc_current", 0)
    merged["pc_max"] = probed_status.get("pc_max", 0)
    merged["edge_current"] = probed_status.get("edge_current", merged.get("edge_current", 0))
    merged["edge_max"] = probed_status.get("edge_max", merged.get("edge_max", 0))
    if probed_status.get("total_points", 0) > 0:
        merged["total_points"] = probed_status.get("total_points", 0)
    add_log(
        "info",
        f"🖥️ Desktop runtime probe resolved credits: "
        f"{merged.get('pc_current', 0)}/{merged.get('pc_max', 0)}",
    )
    _diag_log(
        settings,
        "Desktop credits resolved after probe merge",
        scope="desktop-resolution",
        account=masked_email,
        merged=summarize_search_status(merged),
    )
    return merged


def _normalize_reward_title(value: str) -> str:
    normalized = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " "
        for ch in (value or "").replace("\u200b", " ").replace("\xa0", " ")
    )
    return " ".join(normalized.split())


def _reconcile_verification_with_session_proof(snapshot: dict, session_proofs: dict | None = None) -> dict:
    """Apply run-local Daily Set proof when final APIs lag behind observed completion."""
    if not session_proofs:
        return snapshot

    reporting_overrides = snapshot.setdefault("reporting_overrides", {})
    if session_proofs.get("ignore_bing_app_checkin", False):
        reporting_overrides["ignore_bing_app_checkin"] = True
    if session_proofs.get("ignore_edge_streak", False):
        reporting_overrides["ignore_edge_streak"] = True

    if not session_proofs.get("daily_set_complete", False):
        return snapshot

    task_overview = snapshot.setdefault("task_overview", {})
    daily_overview = task_overview.setdefault("daily_set", {})
    daily_total = int(daily_overview.get("total", 0))
    if daily_total > 0:
        daily_overview["completed"] = daily_total

    category_status = snapshot.setdefault("category_status", {})
    daily_category = category_status.setdefault("daily_set", {"completed": 0, "total": daily_total})
    daily_category_total = int(daily_category.get("total", 0))
    if daily_category_total > 0:
        daily_category["completed"] = daily_category_total

    stale_title_set = {
        _normalize_reward_title(title)
        for title in session_proofs.get("daily_set_titles", [])
        if title
    }
    if stale_title_set:
        snapshot["pending_tasks"] = [
            title
            for title in snapshot.get("pending_tasks", [])
            if _normalize_reward_title(title) not in stale_title_set
        ]

    pending_by_category = snapshot.setdefault("pending_by_category", {})
    pending_by_category["daily_set"] = []
    return snapshot


async def _collect_final_verification(
    page,
    searcher,
    humanizer,
    settings,
    *,
    search_status_override: dict | None = None,
    search_verification_override: dict | None = None,
) -> dict:
    """Capture the final Rewards state used for honest end-of-run reporting."""
    snapshot = {
        "search_status": {},
        "task_overview": {},
        "category_status": {},
        "pending_tasks": [],
        "pending_by_category": {},
        "deferred_tasks": [],
    }

    if search_status_override is not None:
        snapshot["search_status"] = dict(search_status_override)
    else:
        snapshot["search_status"] = await _read_search_status_with_mobile_recheck(
            searcher,
            page,
            settings,
        )
    if search_verification_override is not None:
        snapshot["search_verification"] = dict(search_verification_override)
    snapshot["task_overview"] = await TaskDetector().get_all_tasks(page)
    snapshot["search_status"] = _merge_search_status_sources(
        snapshot["search_status"],
        {
            **snapshot["task_overview"].get("searches", {}),
            "total_points": snapshot["task_overview"].get("total_points", 0),
        },
    )

    try:
        scanner = UniversalTaskScanner(
            humanizer=humanizer,
            settings=settings,
        )
        tasks = await scanner._fetch_all_tasks(page)
        seen_titles = set()
        for reward_task in tasks:
            category = reward_task.category or "unknown"
            category_status = snapshot["category_status"].setdefault(
                category,
                {"completed": 0, "total": 0},
            )
            category_status["total"] += 1
            if reward_task.is_complete:
                category_status["completed"] += 1
                continue
            if reward_task.is_locked:
                continue
            deferred_reason = get_deferred_offer_reason(reward_task)
            if deferred_reason:
                snapshot["deferred_tasks"].append({
                    "title": (reward_task.title or reward_task.id or reward_task.category).strip(),
                    "reason": deferred_reason,
                    "category": category,
                })
                continue
            title = (reward_task.title or reward_task.id or reward_task.category).strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            snapshot["pending_tasks"].append(title)
            snapshot["pending_by_category"].setdefault(category, []).append(title)
    except Exception as e:
        logger.debug(f"Final task verification scan failed: {e}")

    _diag_log(
        settings,
        "Final verification snapshot collected",
        scope="final-verification",
        search_status=summarize_search_status(snapshot["search_status"]),
        categories=snapshot.get("category_status", {}),
        pending_count=len(snapshot.get("pending_tasks", [])),
        deferred_count=len(snapshot.get("deferred_tasks", [])),
    )

    return snapshot


def _describe_remaining_items(snapshot: dict) -> list[str]:
    """Flatten the final verification payload into human-readable remaining work."""
    remaining = describe_search_remaining_items(snapshot)
    task_overview = snapshot.get("task_overview", {})
    reporting_overrides = snapshot.get("reporting_overrides", {})

    daily_set = task_overview.get("daily_set", {})
    daily_done = daily_set.get("completed", 0)
    daily_total = daily_set.get("total", 0)
    if daily_total > 0 and daily_done < daily_total:
        remaining.append(f"Daily Set {daily_done}/{daily_total}")

    bing_app = task_overview.get("streaks", {}).get("bing_app", {})
    if (
        not reporting_overrides.get("ignore_bing_app_checkin", False)
        and not bing_app.get("done", False)
        and bing_app.get("exists", False)
    ):
        remaining.append(f"Mobile App Check-in {bing_app.get('current', 0)}/1")

    edge_streak = task_overview.get("streaks", {}).get("edge", {})
    edge_minutes = edge_streak.get("minutes", 0)
    edge_target = edge_streak.get("target", 30)
    if (
        not reporting_overrides.get("ignore_edge_streak", False)
        and edge_target > 0
        and not edge_streak.get("done", False)
        and edge_streak.get("exists", False)
    ):
        remaining.append(f"Edge Minutes {edge_minutes}/{edge_target}")

    pending_tasks = snapshot.get("pending_tasks", [])
    
    # Filter out notoriously slow-updating tasks (Quests, URL visits)
    filtered_tasks = []
    ignored_keywords = ["click to complete", "click here", "explore on bing", "tulip", "ipl", "cherry blossoms", "league"]
    for title in pending_tasks:
        lower_ttl = title.lower()
        if not any(k in lower_ttl for k in ignored_keywords):
            filtered_tasks.append(title)
            
    for title in filtered_tasks[:5]:
        remaining.append(f"Task: {title[:60]}")
    if len(filtered_tasks) > 5:
        remaining.append(f"{len(filtered_tasks) - 5} more task(s)")

    return remaining


# ─── Statistics ────────────────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Get points statistics."""
    try:
        from src.points import PointsTracker
        settings = load_settings()
        tracker = PointsTracker(settings)
        stats = tracker.get_statistics()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph", methods=["GET"])
def get_graph():
    """Get the points progress graph."""
    graph_path = DATA_DIR / "graph.png"
    if graph_path.exists():
        return send_file(str(graph_path), mimetype="image/png")

    # Generate fresh
    try:
        from src.points import PointsTracker
        settings = load_settings()
        tracker = PointsTracker(settings)
        path = tracker.generate_graph()
        if path and Path(path).exists():
            return send_file(path, mimetype="image/png")
    except Exception:
        pass

    return jsonify({"error": "No graph available"}), 404


# ─── Schedule ──────────────────────────────────────────────────────────────

@app.route("/api/schedule", methods=["GET"])
def get_schedule():
    """Get schedule info."""
    settings = load_settings()
    from src.scheduler import Scheduler
    scheduler = Scheduler(settings)
    return jsonify({
        "enabled": settings.get("schedule_enabled", False),
        "time": settings.get("schedule_time", "08:00"),
        "windows_task_exists": scheduler.check_task_status(),
        "countdown": scheduler.get_countdown(),
    })


@app.route("/api/schedule", methods=["POST"])
def set_schedule():
    """Set schedule."""
    data = request.json or {}
    settings = load_settings()
    settings["schedule_enabled"] = data.get("enabled", False)
    settings["schedule_time"] = data.get("time", "08:00")
    save_settings(settings)

    if data.get("create_task"):
        from src.scheduler import Scheduler
        scheduler = Scheduler(settings)
        scheduler.setup_windows_task(settings["schedule_time"])

    return jsonify({"status": "ok"})


# ─── Background Bot Runner ────────────────────────────────────────────────

def _run_bot_thread(task: str, password: str, target_emails: list = None):
    """Run bot tasks in a new event loop thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_bot_async(task, password, target_emails))
    except Exception as e:
        state["status"] = "error"
        add_log("error", f"Fatal error: {str(e)}")
        logger.error(f"Bot thread error: {e}")
    finally:
        loop.close()


async def _run_bot_async(task: str, password: str, target_emails: list = None):
    """Async bot execution."""
    from src.browser import BrowserManager
    from src.login import LoginManager
    from src.searcher import Searcher
    from src.universal_task import UniversalTaskScanner
    from src.ai_agent import AIAgent
    from src.points import PointsTracker
    from src.notifier import Notifier
    from src.trends import TrendsManager
    from src.humanizer import Humanizer
    from src.streaks import TaskDetector, BingAppStreak, EdgeBrowsingStreak
    from src.manual_captcha import ManualCaptchaHandler

    settings = load_settings()
    _update_ai_state(
        active=False,
        last_event="AI chưa được gọi trong phiên này.",
        task="",
        model=settings.get("ai_model", ""),
        last_level="info",
    )
    accounts = load_encrypted_accounts(password)
    if target_emails:
        accounts = [a for a in accounts if a["email"] in target_emails]

    if not accounts:
        state["status"] = "idle"
        add_log("info", "No matching accounts found to run.")
        return

    overall_complete = True

    max_threads, max_threads_reason = _effective_max_threads(settings)
    if max_threads_reason:
        add_log(
            "info",
            f"🧵 Effective max_threads={max_threads} — {max_threads_reason}",
        )
        _diag_log(
            settings,
            "Adjusted effective account concurrency",
            scope="orchestration",
            configured_max_threads=int(settings.get("max_threads", 10) or 1),
            effective_max_threads=max_threads,
            reason=max_threads_reason,
        )
    semaphore = asyncio.Semaphore(max_threads)
    gpm_enabled = settings.get("gpm_integration_enabled", False)
    gpm_api_url = settings.get("gpm_api_url", "http://127.0.0.1:9495").rstrip("/")
    
    # Track locks per profile ID to prevent overlapping runs on the same browser instance
    _profile_locks = {}

    async def _process_single_account(idx, account):
        nonlocal overall_complete
        
        email = account["email"]
        gpm_profile_id = account.get("gpm_profile_id")
        for key in _profile_lock_keys_for_account(settings, account):
            if key not in _profile_locks:
                _profile_locks[key] = asyncio.Lock()

        async with semaphore, AsyncExitStack() as account_lock_stack:
            for key in _profile_lock_keys_for_account(settings, account):
                await account_lock_stack.enter_async_context(_profile_locks[key])
            # Mỗi account có TrendsManager riêng để tránh _used_queries collision
            # khi nhiều accounts chạy đồng thời (shared set gây query trùng/bỏ sót)
            trends = TrendsManager()
            humanizer = Humanizer(
                delay_min=settings.get("delay_min", 3),
                delay_max=settings.get("delay_max", 8),
            )
            notifier = Notifier(settings)
            points_tracker = PointsTracker(settings)
            challenge_handler = ManualCaptchaHandler(
                settings,
                notifier=notifier,
                on_log=add_log,
            )
            def _handle_ai_event(level: str, message: str, meta: dict) -> None:
                _update_ai_state(
                    active=bool(meta.get("active", False)),
                    last_event=message,
                    task=meta.get("task", ""),
                    model=meta.get("model", settings.get("ai_model", "")),
                    last_level=level,
                )
            login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)
            searcher = Searcher(
                humanizer,
                trends,
                settings,
                challenge_handler=challenge_handler,
            )

            if state["status"] == "stopping":
                add_log("warning", "Stopped by user")
                # Không set idle ở đây — để _run_bot_async finally block xử lý
                return

            # ── Inter-account delay (except first) ──
            if idx > 0:
                if max_threads == 1:
                    import random as _rng
                    delay = _rng.randint(30, 120)
                    add_log("info", f"ΓÅ Waiting {delay}s before next account (anti-detection)...")
                    state["current_task"] = f"Cooldown ({delay}s)"
                    await asyncio.sleep(delay)
                else:
                    # Stagger concurrent accounts slightly
                    stagger = idx * (10 if gpm_enabled else 2)
                    await asyncio.sleep(stagger)

            account_key = _account_state_key(email)
            display_label = _account_display_label(email)

            # ── Per-account log file ──
            _acc_log_handler = None
            try:
                acc_log_dir = DATA_DIR / "logs"
                acc_log_dir.mkdir(parents=True, exist_ok=True)
                safe_email = email.replace("@", "_at_").replace(".", "_")
                acc_log_file = acc_log_dir / f"acc_{safe_email}_{datetime.now().strftime('%Y%m%d')}.log"
                _acc_log_handler = logging.FileHandler(str(acc_log_file), encoding="utf-8")
                _acc_log_handler.setLevel(logging.INFO)
                _acc_log_handler.setFormatter(
                    logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
                )
            except Exception:
                _acc_log_handler = None

            # Track per-account context implicitly for add_log
            _current_log_key.set(account_key)
            _current_log_handler.set(_acc_log_handler)

            searcher.set_account_context(email)
            session_proxy = get_proxy_for_session(account)
            storage_state_path = _storage_state_path(email)
            state["current_account"] = account_key
            _update_account_state(
                account_key,
                id=email,
                email=email,
                display_name=display_label,
                status="running",
                task="Starting",
            )
            add_log("info", f"│││ Account {idx + 1}/{len(accounts)}: {display_label} │││")
            _diag_log(
                settings,
                "Starting account run",
                scope="account",
                account=mask_email(email),
                index=idx + 1,
                total_accounts=len(accounts),
                has_proxy=bool(session_proxy),
                has_storage_state=storage_state_path.exists(),
                task=task,
                gpm_enabled=bool(gpm_enabled),
                diagnostic_logging=bool(settings.get("diagnostic_logging", True)),
            )
            account_complete = True
            run_start_points = 0

            try:
                attach_runtime = False
                cdp_url = ""
                session_proofs: dict = {"ignore_bing_app_checkin": True}
                desktop_runtime: dict | None = None
                mobile_runtime: dict | None = None

                # Desktop session / session bootstrap / activities

                # ══ PRIORITY 0: Edge Session (Edge Streak + Edge Searches) ══
                if task in ("all", "searches"):
                    state["current_task"] = "Edge Session"
                    add_log("info", "🔓 Edge Session")
                    try:
                        edge_runtime_settings = dict(settings)
                        edge_runtime_settings["use_stealth"] = False
                        bm3 = BrowserManager(edge_runtime_settings)
                        bm3.set_account(email)

                        # Priority 1: Try GPM if enabled
                        edge_streak_native = False
                        edge_streak_cdp_url = ""
                        
                        if gpm_enabled and gpm_profile_id:
                            if not attach_runtime:
                                add_log("info", f"Starting GPM Login profile {gpm_profile_id[:8]} for Edge session...")
                                try:
                                    cdp_url = await _start_gpm_profile_serialized(gpm_profile_id, gpm_api_url)
                                    attach_runtime = True
                                    desktop_runtime = build_runtime_descriptor(
                                        "gpm_desktop",
                                        gpm_profile_id,
                                        "desktop",
                                        cdp_url=cdp_url,
                                        live_for_account_run=True,
                                    )
                                    add_log("info", f"GPM Profile started via {cdp_url}")
                                except Exception as e:
                                    add_log("warning", f"GPM start failed: {e}. Falling back to default tools.")
                            
                            if attach_runtime and cdp_url:
                                try:
                                    edge_streak_cdp_url = cdp_url
                                    await bm3.start_connected_edge(edge_streak_cdp_url)
                                    desktop_runtime = build_runtime_descriptor(
                                        "gpm_desktop",
                                        gpm_profile_id,
                                        "desktop",
                                        cdp_url=edge_streak_cdp_url,
                                        live_for_account_run=True,
                                    )
                                    edge_streak_native = True
                                except Exception as e:
                                    add_log("warning", f"Connecting to GPM Edge failed: {e}")

                        # Priority 2: NATIVE Edge runtime (subprocess + CDP)
                        if not edge_streak_native and bool(settings.get("native_edge_runtime_enabled", True)):
                            try:
                                edge_streak_cdp_url = await bm3.start_native_edge_runtime(email)
                                add_log("info", f"Using native Edge runtime for searches ({edge_streak_cdp_url})")
                                edge_streak_native = True
                            except Exception as native_err:
                                add_log("warning", f"Native Edge runtime failed ({native_err}), falling back to Playwright Edge")

                        if not edge_streak_native:
                            # Fallback to Playwright-managed persistent Edge
                            storage_state = (
                                str(storage_state_path) if storage_state_path.exists() else None
                            )
                            ctx3, page3 = await bm3.start_clean_edge_persistent(
                                account_email=email,
                                storage_state=storage_state,
                            )
                            add_log("info", "Using Playwright-managed Edge (telemetry may be limited)")
                        else:
                            # Use _open_account_context with native runtime (same as desktop)
                            ctx3, page3 = await _open_account_context(
                                bm3,
                                login_mgr,
                                account,
                                session_proxy,
                                "desktop",
                                storage_state_path,
                                attach_existing_edge=True,
                                attached_cdp_url=edge_streak_cdp_url,
                            )

                        # Verify login in the context
                        if not await login_mgr.is_logged_in(page3):
                            add_log("info", "Edge session not logged in, logging in...")
                            page3 = await login_mgr.login(
                                page3,
                                account["email"],
                                account["password"],
                                account.get("totp_secret"),
                            )

                        # Edge searches (skip if done or no points)
                        edge_status = await searcher.get_search_points_status(page3)
                        edge_done = edge_status.get("edge_current", 0)
                        edge_max = edge_status.get("edge_max", 0)
                        edge_remaining_pts = max(0, edge_max - edge_done)
                        remaining_edge = (edge_remaining_pts + 2) // 3 if edge_remaining_pts > 0 else 0

                        if remaining_edge > 0:
                            state["current_task"] = "Edge Searches"
                            state["progress"] = 0
                            state["progress_total"] = remaining_edge
                            add_log("info", f"🔓 Edge — {edge_done}/{edge_max} pts ({remaining_edge} searches left)")

                            def on_edge(c, t, q):
                                state["progress"] = c
                            searcher.on_progress = on_edge
                            edge_stats = await searcher.run_searches(page3, remaining_edge, "edge")
                            if edge_stats.get("fatal_error"):
                                raise RuntimeError(edge_stats["fatal_error"])
                            add_log("info", "✅ Edge searches done")
                        else:
                            if edge_max == 0:
                                add_log("info", "⏩ Edge searches not available")
                            else:
                                add_log("info", f"⏩ Edge searches already complete ({edge_done}/{edge_max})")

                        # Close search browser before streak
                        gpm_edge_session = bool(gpm_enabled and gpm_profile_id)
                        if gpm_edge_session:
                            await _persist_storage_state(ctx3, storage_state_path)
                            await bm3.close()
                            session_proofs["ignore_edge_streak"] = True
                            add_log("info", "⏩ Skipping Edge Browsing Streak for GPM profile")
                        else:
                            task_detector = TaskDetector()
                            edge_streak_info = (await task_detector.get_all_tasks(page3)).get("streaks", {}).get("edge", {})
                            minutes_done = edge_streak_info.get("minutes", 0)
                            minutes_target = edge_streak_info.get("target", 30)
                            streak_done = edge_streak_info.get("done", False)

                            if not edge_streak_info.get("exists", False):
                                session_proofs["ignore_edge_streak"] = True
                                add_log("info", "⏩ Edge Browsing Streak task is not available")
                                _update_account_state(account_key, task="Edge Session", progress=edge_done, progress_total=max(edge_max, 0))
                            elif streak_done or minutes_done >= minutes_target:
                                add_log("info", f"⏩ Edge Browsing Streak already complete ({minutes_done}/{minutes_target})")
                                session_proofs["ignore_edge_streak"] = False
                                _update_account_state(account_key, task="Edge Session", progress=minutes_target, progress_total=minutes_target)
                            elif not _edge_streak_attempt_allowed(edge_streak_info):
                                add_log("info", "⏩ Edge Browsing Streak is not actionable in this run")
                                session_proofs["ignore_edge_streak"] = False
                                _update_account_state(account_key, task="Edge Session", progress=minutes_done, progress_total=minutes_target)
                            else:
                                remaining_minutes = max(0, minutes_target - minutes_done)
                                run_minutes = remaining_minutes + 5
                                add_log("info", f"🌐 Edge Browsing Streak — {minutes_done}/{minutes_target} min ({remaining_minutes} left)")
                                state["current_task"] = "Edge Browsing Streak"
                                state["progress"] = minutes_done
                                state["progress_total"] = minutes_target
                                _update_account_state(
                                    account_key,
                                    task="Edge Browsing Streak",
                                    progress=minutes_done,
                                    progress_total=minutes_target,
                                )
                                native_streak = NativeEdgeStreak(account_email=email)

                                def on_streak_progress(done, total):
                                    credited_now = min(minutes_done + done, minutes_target)
                                    state["current_task"] = "Edge Browsing Streak"
                                    state["progress"] = credited_now
                                    state["progress_total"] = minutes_target
                                    _update_account_state(
                                        account_key,
                                        task="Edge Browsing Streak",
                                        progress=credited_now,
                                        progress_total=minutes_target,
                                    )

                                await native_streak.browse(
                                    target_minutes=run_minutes,
                                    on_progress=on_streak_progress,
                                )

                                try:
                                    refreshed_tasks = await task_detector.get_all_tasks(page3)
                                    edge_streak_info = refreshed_tasks.get("streaks", {}).get("edge", {})
                                    refreshed_minutes = edge_streak_info.get("minutes", minutes_done)
                                    refreshed_target = edge_streak_info.get("target", minutes_target)
                                    _update_account_state(
                                        account_key,
                                        task="Edge Browsing Streak",
                                        progress=refreshed_minutes,
                                        progress_total=refreshed_target,
                                    )
                                except Exception as verify_error:
                                    add_log("warning", f"⚠️ Edge streak verify failed: {verify_error}")
                                session_proofs["ignore_edge_streak"] = not bool(edge_streak_info.get("exists", False))

                            try:
                                await asyncio.wait_for(
                                    _persist_storage_state(ctx3, storage_state_path),
                                    timeout=12,
                                )
                            except Exception as persist_error:
                                add_log("warning", f"⚠️ Edge session storage persist timed out: {persist_error}")
                            try:
                                await asyncio.wait_for(bm3.close(), timeout=15)
                            except Exception as close_error:
                                add_log("warning", f"⚠️ Edge session shutdown timed out: {close_error}")
                    except Exception as e:
                        add_log("warning", f"⚠️ Edge session error: {e}")
                        try:
                            await asyncio.wait_for(bm3.close(), timeout=15)
                        except Exception:
                            pass


                if task in ("all", "searches", "daily", "punch", "promos", "bootstrap"):
                    bm = BrowserManager(settings)
                    bm.set_account(email)  # Unique fingerprint per account

                    if gpm_enabled and gpm_profile_id:
                        if not attach_runtime:
                            add_log("info", f"Starting GPM Login profile {gpm_profile_id[:8]}...")
                            try:
                                cdp_url = await _start_gpm_profile_serialized(gpm_profile_id, gpm_api_url)
                                attach_runtime = True
                                desktop_runtime = build_runtime_descriptor(
                                    "gpm_desktop",
                                    gpm_profile_id,
                                    "desktop",
                                    cdp_url=cdp_url,
                                    live_for_account_run=True,
                                )
                                add_log("info", f"GPM Profile started via {cdp_url}")
                            except Exception as e:
                                add_log("warning", f"GPM start failed: {e}. Falling back to default tools.")
                        
                        if attach_runtime and cdp_url:
                            try:
                                await bm.start_connected_edge(cdp_url)
                                desktop_runtime = build_runtime_descriptor(
                                    "gpm_desktop",
                                    gpm_profile_id,
                                    "desktop",
                                    cdp_url=cdp_url,
                                    live_for_account_run=True,
                                )
                            except Exception as e:
                                add_log("warning", f"Failed to attach to re-used GPM profile: {e}")

                    if task == "bootstrap" and bool(settings.get("bootstrap_attach_existing_edge", True)) and not attach_runtime:
                        cdp_url = str(settings.get("edge_cdp_url", "http://127.0.0.1:9222")).strip()
                        add_log("info", f"Trying Edge attach bootstrap via {cdp_url}...")
                        try:
                            await bm.start_connected_edge(cdp_url)
                            add_log("info", "Attached to existing Edge debug session")
                            attach_runtime = True
                            desktop_runtime = build_runtime_descriptor(
                                "attached_edge",
                                cdp_url,
                                "desktop",
                                account_proven=False,
                                cdp_url=cdp_url,
                                live_for_account_run=True,
                            )
                        except Exception as attach_error:
                            add_log(
                                "warning",
                                f"Could not attach to Edge debug session ({attach_error}). Falling back to managed Edge login.",
                            )
                    if not attach_runtime and bool(settings.get("native_edge_runtime_enabled", True)):
                        try:
                            cdp_url = await bm.start_native_edge_runtime(email)
                            add_log("info", f"Using dedicated native Edge runtime ({cdp_url})")
                            attach_runtime = True
                            desktop_runtime = build_runtime_descriptor(
                                "native_edge",
                                cdp_url,
                                "desktop",
                                cdp_url=cdp_url,
                                live_for_account_run=True,
                            )
                        except Exception as native_error:
                            add_log(
                                "warning",
                                f"Could not start dedicated Edge runtime ({native_error}). Falling back to legacy managed browser.",
                            )
                    if not attach_runtime:
                        await bm.start()
                        desktop_runtime = build_runtime_descriptor(
                            "managed_edge",
                            email,
                            "desktop",
                        )
                    ctx, page = await _open_account_context(
                        bm,
                        login_mgr,
                        account,
                        session_proxy,
                        "desktop",
                        storage_state_path,
                        attach_existing_edge=attach_runtime,
                        attached_cdp_url=cdp_url if attach_runtime else "",
                    )
                    add_log("info", "✅ Logged in")

                    if task == "bootstrap":
                        add_log(
                            "info",
                            "✅ Session bootstrap complete. Future runs will reuse this saved login when possible.",
                        )
                        await _persist_storage_state(ctx, storage_state_path)
                        await bm.close()
                        return

                    # Clean up any leftover tabs from previous runs
                    await close_other_tabs(page)

                    # Warm-up: visit random sites before tasks (anti-detection)
                    add_log("info", "🌍 Warming up browser...")
                    await humanizer.warm_up_browsing(page)

                    # ══ PRIORITY 1: Universal Task Scanner (Daily Set + Punch Cards + Quests + Promos) ══
                    # Spec: Edge Streak → Daily Set → Quiz → Promos → Search
                    if task in ("all", "daily", "punch", "promos"):
                        state["current_task"] = "All Tasks (Smart Scanner)"
                        _update_account_state(account_key, task="Tasks", progress=0)
                        ai = AIAgent(settings, on_event=_handle_ai_event, humanizer=humanizer)
                        add_log("info", "🧠 Smart Task Scanner starting...")
                        if ai.enabled:
                            add_log("info", "🤖 AI Agent enabled for complex tasks")

                        scanner = UniversalTaskScanner(
                            humanizer=humanizer,
                            ai_agent=ai,
                            on_log=add_log,
                            settings=settings,
                            challenge_handler=challenge_handler,
                        )
                        scan_result = await scanner.scan_and_complete(
                            page, account_email=email,
                        )
                        session_proofs.update(scan_result.get("session_proofs", {}))
                        add_log("info",
                                f"🧠 Smart Scanner: {scan_result['completed']}/{scan_result['total']} completed, "
                                f"{scan_result['skipped_locked']} locked, {scan_result['failed']} failed")
                        await close_other_tabs(page)

                    # ══ PRIORITY 5: Desktop Searches ══
                    if task in ("all", "searches"):
                        # ── Check current progress first ──
                        add_log("info", "🔌 Checking search credits...")
                        status_before = await _read_search_status_with_mobile_recheck(
                            searcher,
                            page,
                            settings,
                        )
                        status_before = await _resolve_mobile_search_requirement(
                            settings,
                            account,
                            session_proxy,
                            login_mgr,
                            searcher,
                            storage_state_path,
                            status_before,
                        )
                        status_before = await _resolve_desktop_search_requirement(
                            settings,
                            account,
                            session_proxy,
                            login_mgr,
                            searcher,
                            storage_state_path,
                            status_before,
                            desktop_runtime,
                        )
                        run_start_points = _safe_int(status_before.get("total_points", 0))
                        _update_account_state(
                            account_key,
                            points=status_before.get("total_points", 0),
                            search_status=dict(status_before),
                        )

                        # Desktop searches (API returns points, 3 points per search)
                        pc_done = status_before.get("pc_current", 0)
                        pc_max = status_before.get("pc_max", 0)
                        remaining_points = max(0, pc_max - pc_done)
                        # Convert points to search count (3 points per search)
                        remaining_desktop = (remaining_points + 2) // 3  # ceil division
                        
                        desktop_status_ambiguous = _needs_desktop_credit_recheck(status_before, settings)
                        # If counters are still ambiguous after a family-aware probe, use the fallback batch.
                        if desktop_status_ambiguous:
                            remaining_desktop = settings.get("desktop_searches", 30)
                            add_log("info", f"🖥️ Desktop — API missing counters after probe. Probing ({remaining_desktop} searches)")

                        if remaining_desktop > 0:
                            state["current_task"] = "Desktop Searches"
                            state["progress"] = 0
                            state["progress_total"] = remaining_desktop
                            _update_account_state(account_key, task="Desktop Searches",
                                                  progress=0, progress_total=remaining_desktop)
                            add_log("info", f"🖥️ Desktop — {pc_done}/{pc_max} pts ({remaining_desktop} searches left)")

                            def on_desktop(c, t, q):
                                state["progress"] = c
                                _update_account_state(account_key, progress=c)
                                if c % 5 == 0:
                                    add_log("info", f"Desktop {c}/{t}: {q[:30]}")

                            searcher.on_progress = on_desktop
                            ctx, page = await _ensure_usable_desktop_search_page(
                                settings,
                                bm,
                                login_mgr,
                                account,
                                session_proxy,
                                storage_state_path,
                                desktop_runtime,
                                ctx,
                                page,
                            )
                            desktop_stats = await searcher.run_searches(page, remaining_desktop, "desktop")
                            if desktop_stats.get("fatal_error"):
                                raise RuntimeError(desktop_stats["fatal_error"])
                            add_log("info", "✅ Desktop searches done")
                        else:
                            add_log("info", f"⏩ Desktop searches already complete ({pc_done}/{pc_max})")

                    # Read points
                    try:
                        points_info = await points_tracker.read_points(page)
                        pts = points_info.get("total_points", 0)
                        streak_val = points_info.get("streak", 0)
                        state["total_points"] = pts
                        state["streak"] = streak_val
                        _update_account_state(account_key, points=pts, streak=streak_val)
                        add_log("info", f"💰 Points: {state['total_points']:,} | 🔥 Streak: {streak_val}")
                    except Exception as _e:
                        logger.debug(f"Points read suppressed: {_e}")

                    await _persist_storage_state(ctx, storage_state_path)

                # ══ Mobile searches — separate GPM Android profile ══
                # Instead of CDP device emulation on the desktop profile,
                # we use a dedicated GPM Android profile for mobile searches.
                if task in ("all", "searches"):
                    mob_done = status_before.get("mobile_current", 0)
                    mob_max = status_before.get("mobile_max", 0)
                    mobile_status_ambiguous = _needs_mobile_credit_recheck(status_before, settings)
                    mob_remaining_pts = max(0, mob_max - mob_done)
                    if mobile_status_ambiguous:
                        mob_searches = _search_count_setting(settings, "mobile")
                        add_log(
                            "info",
                            "📱 Mobile credits remain ambiguous after probe; "
                            f"running configured batch ({mob_searches} searches) instead of auto-skipping.",
                        )
                    else:
                        mob_searches = (mob_remaining_pts + 2) // 3
                    _diag_log(
                        settings,
                        "Resolved mobile search plan",
                        scope="mobile-plan",
                        account=mask_email(email),
                        ambiguous=mobile_status_ambiguous,
                        remaining_points=mob_remaining_pts,
                        planned_searches=mob_searches,
                        baseline=summarize_search_status(status_before),
                    )

                    gpm_mobile_id = account.get("gpm_mobile_profile_id")
                    fallback_to_native_mobile, mobile_runtime_strategy = _select_mobile_runtime_strategy(
                        gpm_enabled,
                        gpm_mobile_id,
                    )
                    if fallback_to_native_mobile:
                        if mobile_runtime_strategy == "missing_gpm_mobile_profile_id":
                            add_log(
                                "warning",
                                "📱 Mobile GPM profile is not configured for this account; "
                                "using native mobile fallback, so same-account GPM control is unavailable.",
                            )
                        else:
                            add_log(
                                "info",
                                "📱 Mobile GPM integration is disabled; using native mobile fallback.",
                            )
                        _diag_log(
                            settings,
                            "Selected native mobile fallback runtime",
                            scope="mobile-runtime-selection",
                            account=mask_email(email),
                            strategy=mobile_runtime_strategy,
                            gpm_enabled=gpm_enabled,
                            has_gpm_mobile_profile=bool(str(gpm_mobile_id or "").strip()),
                        )
                    else:
                        _diag_log(
                            settings,
                            "Selected mobile GPM runtime",
                            scope="mobile-runtime-selection",
                            account=mask_email(email),
                            strategy=mobile_runtime_strategy,
                            gpm_enabled=gpm_enabled,
                            has_gpm_mobile_profile=True,
                        )
                    
                    if mob_searches <= 0:
                        add_log("info", f"⏩ Mobile searches already complete ({mob_done}/{mob_max})")
                    else:
                        add_log("info", f"📱 Mobile — {mob_done}/{mob_max} pts ({mob_searches} searches needed)")

                        active_mobile_page = None
                        ctx_mob = None
                        bm_mobile = None
                        patchright_pw = None
                        patchright_browser = None
                        
                        # 1. Save state & close PC browser first
                        await _persist_storage_state(ctx, storage_state_path)
                        try:
                            await bm.close()
                        except Exception:
                            pass

                        # 2. Stop PC GPM profile
                        if gpm_profile_id:
                            try:
                                add_log("info", "Waiting 4s for PC browser profile data sync...")
                                await asyncio.sleep(4)
                                await _stop_gpm_profile_serialized(gpm_profile_id, gpm_api_url)
                                add_log("info", f"Stopped PC GPM profile {gpm_profile_id[:8]}")
                                attach_runtime, cdp_url, desktop_runtime = invalidate_runtime_attachment(
                                    attach_runtime,
                                    cdp_url,
                                    desktop_runtime,
                                    reason="desktop_gpm_profile_stopped_before_mobile_pass",
                                )
                            except Exception as e:
                                add_log("debug", f"PC GPM stop: {e}")

                        # 3. Cooldown between PC and Mobile sessions
                        add_log("info", "⏳ Waiting 30s cooldown before mobile session...")
                        state["current_task"] = "Cooldown (30s)"
                        _update_account_state(account_key, task="Cooldown (30s)")
                        await asyncio.sleep(30)

                        # 4. Start Mobile Profile (Native or GPM)
                        try:
                            bm_mobile = BrowserManager(settings)
                            bm_mobile.set_account(email)

                            if fallback_to_native_mobile:
                                page_mob = None
                                if bool(settings.get("mobile_patchright_enabled", True)):
                                    from src.browser import load_storage_state_cookies

                                    try:
                                        add_log("info", "📱 Native fallback: Launching patchright mobile Edge...")
                                        patchright_pw, patchright_browser, ctx_mob, page_mob = await bm_mobile.create_mobile_patchright(
                                            load_storage_state_cookies(storage_state_path)
                                        )
                                        mobile_runtime = build_runtime_descriptor(
                                            "patchright_mobile", email, "mobile",
                                        )
                                        try:
                                            await bm_mobile.toggle_mobile_emulation(page_mob, enable=True)
                                            await asyncio.sleep(1)
                                        except Exception as patch_emu_err:
                                            add_log(
                                                "warning",
                                                f"📱 Patchright mobile CDP overlay failed: {patch_emu_err}",
                                            )
                                    except Exception as patch_err:
                                        add_log(
                                            "warning",
                                            f"📱 Patchright mobile startup failed, falling back to emulation: {patch_err}",
                                        )

                                if page_mob is None:
                                    add_log("info", "📱 Native Emulation fallback: Launching dedicated headless session...")
                                    await bm_mobile.start()
                                    ctx_mob = await bm_mobile.create_context(
                                        mode="mobile",
                                        account_email=email,
                                        proxy=session_proxy,
                                        storage_state=str(storage_state_path) if storage_state_path.exists() else None,
                                        use_persistent_profile=False,
                                    )
                                    page_mob = await bm_mobile.new_page(ctx_mob)
                                    mobile_runtime = build_runtime_descriptor(
                                        "managed_edge", email, "mobile",
                                    )
                                    try:
                                        await bm_mobile.toggle_mobile_emulation(page_mob, enable=True)
                                        await asyncio.sleep(1)
                                    except Exception as emu_err:
                                        add_log("warning", f"📱 Mobile emulation activation failed: {emu_err}")
                            else:
                                mobile_cdp = await _start_gpm_profile_serialized(gpm_mobile_id, gpm_api_url)
                                add_log("info", f"📱 Mobile GPM profile started via {mobile_cdp}")
                                mobile_runtime = build_runtime_descriptor(
                                    "gpm_mobile", gpm_mobile_id, "mobile",
                                )
                                await bm_mobile.start_connected_edge(mobile_cdp)
                                ctx_mob = await bm_mobile.create_context(
                                    mode="mobile",
                                    account_email=email,
                                )
                                page_mob = await bm_mobile.new_page(ctx_mob)

                            # Check login status
                            if not await login_mgr.is_logged_in(page_mob):
                                add_log("info", "📱 Logging in on mobile profile...")
                                try:
                                    page_mob = await login_mgr.login(
                                        page_mob, email, account["password"],
                                        account.get("totp_secret"),
                                    )
                                    try:
                                        await bm_mobile.toggle_mobile_emulation(page_mob, enable=True)
                                        await asyncio.sleep(1)
                                    except Exception as emu_err:
                                        add_log("debug", f"📱 Mobile emulation refresh after login: {emu_err}")
                                    ctx_mob = page_mob.context
                                except Exception as login_err:
                                    add_log("warning", f"📱 Mobile login attempt: {login_err}")

                            ctx_mob = page_mob.context
                            await page_mob.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=35000)
                            try:
                                await bm_mobile.toggle_mobile_emulation(page_mob, enable=True)
                                await asyncio.sleep(1)
                            except Exception as emu_err:
                                add_log("debug", f"📱 Mobile emulation refresh after navigation: {emu_err}")
                            if hasattr(bm_mobile, "capture_runtime_signature"):
                                runtime_signature = await bm_mobile.capture_runtime_signature(page_mob)
                                _diag_log(
                                    settings,
                                    "Mobile runtime signature before mobile pass",
                                    scope="mobile-runtime",
                                    account=mask_email(email),
                                    runtime_family=(mobile_runtime or {}).get("family", ""),
                                    signature=runtime_signature,
                                )
                            active_mobile_page = page_mob
                            
                        except Exception as mob_err:
                            import traceback
                            add_log("error", f"📱 Mobile init error: {mob_err}")
                            add_log("error", f"📱 {traceback.format_exc()[:500]}")
                        
                        
                        if active_mobile_page:
                            try:
                                status_before_mobile = await _read_search_status_with_mobile_recheck(
                                    searcher,
                                    active_mobile_page,
                                    settings,
                                )
                                _diag_log(
                                    settings,
                                    "Collected mobile credits before mobile pass",
                                    scope="mobile-pass",
                                    account=mask_email(email),
                                    before=summarize_search_status(status_before_mobile),
                                    planned_searches=mob_searches,
                                )

                                # Run mobile searches
                                state["current_task"] = "Mobile Searches"
                                state["progress"] = 0
                                state["progress_total"] = mob_searches
                                _update_account_state(
                                    account_key,
                                    task="Mobile Searches",
                                    progress=0,
                                    progress_total=mob_searches,
                                )

                                def on_mobile(c, t, q):
                                    state["progress"] = c
                                    _update_account_state(account_key, progress=c)
                                    if c % 5 == 0:
                                        add_log("info", f"Mobile {c}/{t}: {q[:30]}")

                                searcher.on_progress = on_mobile
                                searcher.set_account_context(email)

                                mob_result = await searcher.run_searches(
                                    active_mobile_page, mob_searches, mode="mobile",
                                )
                                _diag_log(
                                    settings,
                                    "Mobile search loop finished",
                                    scope="mobile-pass",
                                    account=mask_email(email),
                                    completed=mob_result.get("completed", 0),
                                    failed=mob_result.get("failed", 0),
                                    requested=mob_searches,
                                    fatal_error=mob_result.get("fatal_error", ""),
                                )
                                status_after_mobile = await _wait_for_mobile_credit_update(
                                    searcher,
                                    active_mobile_page,
                                    settings,
                                    baseline_status=status_before_mobile,
                                )
                                credit_delta = _mobile_credit_delta(
                                    status_before_mobile,
                                    status_after_mobile,
                                )
                                points_delta = _total_points_delta(
                                    status_before_mobile,
                                    status_after_mobile,
                                )
                                credit_proven = credit_delta > 0 or points_delta > 0
                                add_log(
                                    "info",
                                    f"📱 Mobile: {mob_result.get('completed', 0)}/{mob_searches} OK, "
                                    f"{mob_result.get('failed', 0)} failed",
                                )
                                if credit_delta > 0:
                                    add_log(
                                        "info",
                                        "📱 Mobile search pass credited "
                                        f"{credit_delta} points "
                                        f"({status_after_mobile.get('mobile_current', 0)}/"
                                        f"{status_after_mobile.get('mobile_max', 0)})",
                                    )
                                elif points_delta > 0:
                                    add_log(
                                        "info",
                                        "📱 Mobile search pass credited via total-points delta "
                                        f"(+{points_delta}) while counters stayed ambiguous",
                                    )
                                else:
                                    add_log(
                                        "warning",
                                        "📱 Mobile search pass finished without observed credit change "
                                        f"({status_before_mobile.get('mobile_current', 0)}/"
                                        f"{status_before_mobile.get('mobile_max', 0)} -> "
                                        f"{status_after_mobile.get('mobile_current', 0)}/"
                                        f"{status_after_mobile.get('mobile_max', 0)})",
                                    )
                                _diag_log(
                                    settings,
                                    "Finished mobile search pass verification",
                                    scope="mobile-pass",
                                    account=mask_email(email),
                                    before=summarize_search_status(status_before_mobile),
                                    after=summarize_search_status(status_after_mobile),
                                    credit_delta=credit_delta,
                                    points_delta=points_delta,
                                    credit_proven=credit_proven,
                                )
                                _update_account_state(
                                    account_key,
                                    points=status_after_mobile.get("total_points", state.get("total_points", 0)),
                                    search_status=dict(status_after_mobile),
                                )

                            except Exception as run_err:
                                add_log("error", f"📱 Mobile search execution failed: {run_err}")
                            finally:
                                if fallback_to_native_mobile:
                                    if ctx_mob is not None:
                                        try:
                                            await _persist_storage_state(ctx_mob, storage_state_path)
                                        except Exception:
                                            pass
                                    if patchright_browser is not None:
                                        try:
                                            await patchright_browser.close()
                                        except Exception:
                                            pass
                                    if patchright_pw is not None:
                                        try:
                                            await patchright_pw.stop()
                                        except Exception:
                                            pass
                                    if bm_mobile is not None:
                                        try:
                                            await bm_mobile.close()
                                        except Exception:
                                            pass
                                else:
                                    # Shutdown GPM connection
                                    if bm_mobile is not None:
                                        try:
                                            await bm_mobile.close()
                                        except Exception:
                                            pass
                                    try:
                                        add_log("info", "Waiting 4s for mobile browser profile data sync...")
                                        await asyncio.sleep(4)
                                        await _stop_gpm_profile_serialized(gpm_mobile_id, gpm_api_url)
                                        add_log("info", f"📱 Stopped Mobile GPM profile {gpm_mobile_id[:8]}")
                                    except Exception:
                                        pass

                    # If we used native mode without closing `bm` (unreachable now but kept for safety)
                    if not (gpm_enabled and gpm_mobile_id) and not fallback_to_native_mobile:
                        try:
                            await _persist_storage_state(ctx, storage_state_path)
                            await bm.close()
                        except Exception:
                            pass


                # ── Post-run Verification (with error handling) ──
                if task in ("all", "searches"):
                    add_log("info", "🔄 Verifying search credits...")
                    try:
                        final_status, search_verification = await _collect_search_status_snapshot(
                            settings,
                            account,
                            session_proxy,
                            login_mgr,
                            searcher,
                            storage_state_path,
                            desktop_runtime=desktop_runtime,
                            mobile_runtime=mobile_runtime,
                        )
                        deficit = describe_search_remaining_items({
                            "search_status": final_status,
                            "search_verification": search_verification,
                        })

                        if deficit:
                            add_log("warning", f"⚠️ Search deficit: {', '.join(deficit)}")
                        else:
                            add_log("info", "✅ All search credits verified")
                        current_points = max(
                            _safe_int(state.get("total_points", 0)),
                            _safe_int(final_status.get("total_points", 0)),
                        )
                        earned_today_value = max(0, current_points - run_start_points)
                        _update_account_state(
                            account_key,
                            points=current_points,
                            earned_today=earned_today_value,
                            search_status=dict(final_status),
                            verification_state="verified" if not deficit else "incomplete",
                            remaining_items=list(deficit),
                        )
                        _record_account_daily_snapshot(
                            account_key=account_key,
                            email=email,
                            total_points=current_points,
                            earned_today=earned_today_value,
                            search_status=final_status,
                            task_overview={},
                            category_status={},
                            verification_state="verified" if not deficit else "incomplete",
                            runtime_family=((desktop_runtime or {}).get("family") or (mobile_runtime or {}).get("family") or ""),
                        )
                    except Exception as e:
                        add_log("warning", f"⚠️ Verification error: {e}")

                # ── Bing App Rewards intentionally skipped ──────────────────
                if task == "all":
                    add_log("info", "⏭️ Skipping Bing App Rewards (phone-only flow disabled)")
                    _diag_log(
                        settings,
                        "Skipped Bing App Rewards during all-task run",
                        scope="bing-app",
                        account=mask_email(email),
                        bing_app_read_to_earn=bool(settings.get("bing_app_read_to_earn", False)),
                        bing_app_checkin=bool(settings.get("bing_app_checkin", False)),
                    )

                # Edge Browsing Streak is already handled above in the Edge Session block
                # (lines 573-600) — no duplicate needed

                if task == "all":
                    add_log("info", "🔄 Final Rewards verification...")
                    bm_final = None
                    try:
                        verified_search_status, search_verification = await _collect_search_status_snapshot(
                            settings,
                            account,
                            session_proxy,
                            login_mgr,
                            searcher,
                            storage_state_path,
                            desktop_runtime=desktop_runtime,
                            mobile_runtime=mobile_runtime,
                        )
                        bm_final = BrowserManager(settings)
                        bm_final.set_account(email)
                        await bm_final.start()
                        ctx_final, page_final = await _open_account_context(
                            bm_final,
                            login_mgr,
                            account,
                            session_proxy,
                            "desktop",
                            storage_state_path,
                            attach_existing_edge=False,
                        )

                        verification = await _collect_final_verification(
                            page_final,
                            searcher,
                            humanizer,
                            settings,
                            search_status_override=verified_search_status,
                            search_verification_override=search_verification,
                        )
                        verification = _reconcile_verification_with_session_proof(
                            verification,
                            session_proofs,
                        )
                        remaining_items = _describe_remaining_items(verification)
                        deferred_items = _describe_deferred_items(verification)
                        _diag_log(
                            settings,
                            "Final account verification evaluated",
                            scope="account",
                            account=mask_email(email),
                            verification_status=summarize_search_status(verification.get("search_status", {})),
                            remaining_items=remaining_items,
                            deferred_items=deferred_items,
                        )

                        if remaining_items:
                            account_complete = False
                            overall_complete = False
                            add_log(
                                "warning",
                                "⚠️ Run finished with remaining items: "
                                + ", ".join(remaining_items[:8]),
                            )
                        else:
                            add_log("info", f"✅ Account {email[:5]}*** fully verified")
                        if deferred_items:
                            add_log(
                                "info",
                                "ℹ️ Deferred offers: " + ", ".join(deferred_items[:5]),
                            )
                        current_points = max(
                            _safe_int(state.get("total_points", 0)),
                            _safe_int(verification.get("search_status", {}).get("total_points", 0)),
                        )
                        earned_today_value = max(0, current_points - run_start_points)
                        verification_state = "verified" if not remaining_items else "incomplete"
                        _update_account_state(
                            account_key,
                            points=current_points,
                            earned_today=earned_today_value,
                            search_status=dict(verification.get("search_status", {})),
                            task_overview=dict(verification.get("task_overview", {})),
                            category_status=dict(verification.get("category_status", {})),
                            remaining_items=list(remaining_items),
                            verification_state=verification_state,
                            runtime_family=((desktop_runtime or {}).get("family") or (mobile_runtime or {}).get("family") or ""),
                        )
                        _record_account_daily_snapshot(
                            account_key=account_key,
                            email=email,
                            total_points=current_points,
                            earned_today=earned_today_value,
                            search_status=verification.get("search_status", {}),
                            task_overview=verification.get("task_overview", {}),
                            category_status=verification.get("category_status", {}),
                            verification_state=verification_state,
                            runtime_family=((desktop_runtime or {}).get("family") or (mobile_runtime or {}).get("family") or ""),
                        )
                        
                        # Google Sheets Webhook
                        webhook_url = settings.get("google_sheets_webhook_url", "")
                        if settings.get("google_sheets_enabled", False) and webhook_url:
                            try:
                                # We don't have direct access to punch_stats vs promo_stats in the exact same format
                                # as main.py here without passing it correctly, but we can extract from 'verification' object
                                v_cats = verification.get("category_status", {})
                                pc = v_cats.get("punch_card", {}).get("completed", 0)
                                mp = v_cats.get("more_promo", {}).get("completed", 0)
                                offers_total = pc + mp
                            
                                GoogleSheetsLogger.log_account(
                                    webhook_url=webhook_url,
                                    email=email,
                                    total_points=state.get("total_points", 0),
                                    earned_today=0,  # Not tracked separately in dashboard
                                    pc_search=verification.get("search_status", {}).get("pc_current", 0),
                                    mobile_search=verification.get("search_status", {}).get("mobile_current", 0),
                                    offers=offers_total
                                )
                            except Exception as e:
                                add_log("warning", f"Failed to log to Google Sheets: {e}")
                            
                        await _persist_storage_state(ctx_final, storage_state_path)
                    except Exception as e:
                        account_complete = False
                        overall_complete = False
                        add_log("warning", f"⚠️ Final verification error: {e}")
                    finally:
                        if bm_final is not None:
                            try:
                                await bm_final.close()
                            except Exception as _e:
                                logger.debug(f"bm_final close suppressed: {_e}")
                else:
                    add_log("info", f"✅ Task '{task}' finished for {account_key}")
                _update_account_state(account_key, status="done", task="Completed")

            except Exception as e:
                overall_complete = False
                _update_account_state(account_key, status="error", task=f"Error: {str(e)[:40]}")
                add_log("error", f"❌ {account_key}: {str(e)}")
                logger.error(f"Account {email} error: {e}")
                _diag_log(
                    settings,
                    "Account run raised exception",
                    level="error",
                    scope="account",
                    account=mask_email(email),
                    error=str(e),
                )
                notifier.send_error(email, str(e))
            finally:
                if gpm_enabled and gpm_profile_id:
                    try:
                        # Grace period: allow Chromium time to flush cookies/localStorage to SQLite db
                        # before GPM force-kills the process
                        add_log("info", "Waiting 4s for browser profile data sync...")
                        await asyncio.sleep(4)
                        await _stop_gpm_profile_serialized(gpm_profile_id, gpm_api_url)
                        add_log("info", f"Stopped GPM Profile {gpm_profile_id[:8]}")
                    except Exception as stop_e:
                        add_log("debug", f"Failed to stop GPM Profile: {stop_e}")
                gpm_mobile_id = account.get("gpm_mobile_profile_id")
                if gpm_enabled and gpm_mobile_id:
                    try:
                        await _stop_gpm_profile_serialized(gpm_mobile_id, gpm_api_url)
                    except Exception:
                        pass
                # Close per-account log handler
                if _acc_log_handler:
                    try:
                        _acc_log_handler.close()
                    except Exception:
                        pass


    async def _safe_process(idx, acc):
        try:
            timeout_seconds = _account_timeout_seconds(idx, max_threads)
            add_log(
                "info",
                f"⏱️ Account slot timeout budget: {int(timeout_seconds // 60)} min",
            )
            await asyncio.wait_for(_process_single_account(idx, acc), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            email = acc.get("email", f"acc_{idx}")
            account_key = _account_state_key(email) if "@" in email else email
            display_label = _account_display_label(email) if "@" in email else email
            timeout_minutes = int(timeout_seconds // 60)
            _update_account_state(
                account_key,
                id=email,
                email=email,
                display_name=display_label,
                status="error",
                task="Timeout",
            )
            add_log("error", f"❌ {email}: Quá thời gian {timeout_minutes} phút, tự ngắt.")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Safe process wrapper error: {e}")

    # Execute all accounts concurrently with timeouts
    await asyncio.gather(*[_safe_process(idx, acc) for idx, acc in enumerate(accounts)])

    state["status"] = "idle"
    state["current_task"] = ""
    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _update_ai_state(active=False)
    if task == "all":
        if overall_complete:
            add_log("info", "🏁 All tasks completed and verified!")
        else:
            add_log("warning", "🏁 Run finished with remaining tasks. Check the warnings above.")
    else:
        add_log("info", f"🏁 Task '{task}' finished")


# ─── Static Files ──────────────────────────────────────────────────────────

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

@app.route("/")
@app.route("/index.html")
def index():
    return send_file(DASHBOARD_DIR / "index.html")

@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(str(DASHBOARD_DIR / "assets"), filename)

@app.route("/<path:filename>")
def dashboard_file(filename):
    """Serve top-level dashboard files such as favicon or direct index links."""
    if filename.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    file_path = DASHBOARD_DIR / filename
    if file_path.is_file():
        return send_file(file_path)
    return send_file(DASHBOARD_DIR / "index.html")


def start_dashboard(port: int = 8080, host: str = "127.0.0.1"):
    """Start the dashboard server (waitress production WSGI)."""
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    _ensure_dashboard_bind_is_safe(host, load_settings())
    startup_state = {"error": None}

    def _serve():
        try:
            try:
                from waitress import serve
                serve(app, host=host, port=port, threads=4)
            except ImportError:
                # Fallback to Flask dev server (suppress warning)
                import os
                os.environ["WERKZEUG_RUN_MAIN"] = "true"
                app.run(host=host, port=port, debug=False, use_reloader=False)
        except Exception as exc:
            startup_state["error"] = exc
            logger.error(f"Dashboard server crashed during startup: {exc}")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.time() + 5
    ready = False
    while time.time() < deadline:
        if startup_state["error"] is not None:
            raise RuntimeError(f"Dashboard failed to start on {host}:{port}") from startup_state["error"]
        try:
            with socket.create_connection((host, port), timeout=0.5):
                ready = True
                break
        except OSError:
            time.sleep(0.1)
    if not ready:
        raise RuntimeError(f"Dashboard failed to become ready on {host}:{port} within 5 seconds")
    logger.info(f"Dashboard started: http://{host}:{port}")
    return thread
