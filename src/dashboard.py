"""
Flask Web Dashboard — Full GUI for Rewards Search Automator.
Provides API endpoints for accounts, settings, running tasks, logs, and status.
"""

import os
import json
import random
import threading
import asyncio
import socket
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, send_file

from src.utils import (
    logger,
    load_settings,
    save_settings,
    CONFIG_DIR,
    DATA_DIR,
    PROFILES_DIR,
    close_other_tabs,
    get_proxy_for_session,
    is_sensitive_setting,
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
from src.universal_task import UniversalTaskScanner


app = Flask(
    __name__,
    static_folder=None,
)

# ─── Global State ──────────────────────────────────────────────────────────

state = {
    "status": "idle",          # idle, running, error
    "current_account": "",
    "current_task": "",
    "progress": 0,
    "progress_total": 0,
    "logs": [],
    "last_run": None,
    "accounts_count": 0,
    "total_points": 0,
    "master_password": "",      # No auth required
}

LOG_MAX = 500
KEEP_EXISTING_SECRET = "__KEEP_EXISTING_SECRET__"


def add_log(level: str, message: str):
    """Add a log message to the state and also to file/console logger."""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    state["logs"].append(entry)
    if len(state["logs"]) > LOG_MAX:
        state["logs"] = state["logs"][-LOG_MAX:]
    # Also write to file/console logger for debugging
    if level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
    else:
        logger.info(message)


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
            except Exception:
                pass
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
    file_path = DASHBOARD_DIR / filename
    if file_path.is_file():
        return send_file(file_path)
    return send_file(DASHBOARD_DIR / "index.html")


# ─── Auth ──────────────────────────────────────────────────────────────────

@app.route("/api/auth", methods=["POST"])
def auth():
    """No-op auth — always succeeds."""
    return jsonify({"status": "ok", "message": "Authenticated"})


@app.route("/api/auth/check", methods=["GET"])
def auth_check():
    """Always authenticated."""
    return jsonify({"authenticated": True})


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

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    account = {
        "email": email,
        "password": password,
        "totp_secret": data.get("totp_secret", "").strip() or None,
        "proxy": data.get("proxy", "").strip() or None,
    }

    try:
        try:
            accounts = load_encrypted_accounts(state["master_password"])
        except FileNotFoundError:
            accounts = []

        # Check duplicate
        if any(a["email"] == email for a in accounts):
            return jsonify({"error": "Account already exists"}), 409

        accounts.append(account)
        save_encrypted_accounts(accounts, state["master_password"])
        add_log("info", f"Account added: {email[:5]}***")
        return jsonify({"status": "ok"})
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


@app.route("/api/accounts/import", methods=["POST"])
def import_accounts():
    """Import from plaintext accounts.json."""

    if migrate_to_encrypted(state["master_password"]):
        return jsonify({"status": "ok", "message": "Accounts imported"})
    return jsonify({"error": "No accounts.json found"}), 404


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
        if key in settings and "password_hash" not in key:
            settings[key] = value

    save_settings(settings)
    add_log("info", "Settings updated")
    return jsonify({"status": "ok"})


# ─── Bot Control ───────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def run_bot():
    """Start the bot in a background thread."""
    if state["status"] == "running":
        return jsonify({"error": "Bot is already running"}), 409


    data = request.json or {}
    task = data.get("task", "all")  # all, searches, daily, punch, promos, bootstrap

    state["status"] = "running"
    state["current_task"] = task
    state["logs"] = []
    add_log("info", f"Starting task: {task}")

    thread = threading.Thread(
        target=_run_bot_thread,
        args=(task, state["master_password"]),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "task": task})


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    """Stop the bot (sets stop flag)."""
    if state["status"] != "running":
        return jsonify({"error": "Bot is not running"}), 400

    state["status"] = "stopping"
    add_log("warning", "Stop requested")
    return jsonify({"status": "stopping"})


@app.route("/api/status", methods=["GET"])
def get_status():
    """Get current bot status."""
    return jsonify({
        "status": state["status"],
        "current_account": state["current_account"],
        "current_task": state["current_task"],
        "progress": state["progress"],
        "progress_total": state["progress_total"],
        "last_run": state["last_run"],
        "total_points": state["total_points"],
    })


@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Get log entries."""
    since = request.args.get("since", 0, type=int)
    return jsonify({"logs": state["logs"][since:]})


async def _collect_final_verification(page, searcher, humanizer, settings) -> dict:
    """Capture the final Rewards state used for honest end-of-run reporting."""
    snapshot = {
        "search_status": {},
        "task_overview": {},
        "pending_tasks": [],
    }

    snapshot["search_status"] = await searcher.get_search_points_status(page)
    snapshot["task_overview"] = await TaskDetector().get_all_tasks(page)

    try:
        scanner = UniversalTaskScanner(
            humanizer=humanizer,
            settings=settings,
        )
        tasks = await scanner._fetch_all_tasks(page)
        seen_titles = set()
        for reward_task in tasks:
            if reward_task.is_complete or reward_task.is_locked:
                continue
            title = (reward_task.title or reward_task.id or reward_task.category).strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            snapshot["pending_tasks"].append(title)
    except Exception as e:
        logger.debug(f"Final task verification scan failed: {e}")

    return snapshot


def _describe_remaining_items(snapshot: dict) -> list[str]:
    """Flatten the final verification payload into human-readable remaining work."""
    remaining = []
    search_status = snapshot.get("search_status", {})
    task_overview = snapshot.get("task_overview", {})

    pc_current = search_status.get("pc_current", 0)
    pc_max = search_status.get("pc_max", 0)
    if pc_max > 0 and pc_current < pc_max:
        remaining.append(f"Desktop {pc_current}/{pc_max}")

    mobile_current = search_status.get("mobile_current", 0)
    mobile_max = search_status.get("mobile_max", 0)
    if mobile_max > 0 and mobile_current < mobile_max:
        remaining.append(f"Mobile {mobile_current}/{mobile_max}")

    edge_current = search_status.get("edge_current", 0)
    edge_max = search_status.get("edge_max", 0)
    if edge_max > 0 and edge_current < edge_max:
        remaining.append(f"Edge Search {edge_current}/{edge_max}")

    daily_set = task_overview.get("daily_set", {})
    daily_done = daily_set.get("completed", 0)
    daily_total = daily_set.get("total", 0)
    if daily_total > 0 and daily_done < daily_total:
        remaining.append(f"Daily Set {daily_done}/{daily_total}")

    bing_app = task_overview.get("streaks", {}).get("bing_app", {})
    if not bing_app.get("done", False):
        remaining.append(f"Mobile App Check-in {bing_app.get('current', 0)}/1")

    edge_streak = task_overview.get("streaks", {}).get("edge", {})
    edge_minutes = edge_streak.get("minutes", 0)
    edge_target = edge_streak.get("target", 30)
    if edge_target > 0 and not edge_streak.get("done", False):
        remaining.append(f"Edge Minutes {edge_minutes}/{edge_target}")

    pending_tasks = snapshot.get("pending_tasks", [])
    for title in pending_tasks[:5]:
        remaining.append(f"Task: {title[:60]}")
    if len(pending_tasks) > 5:
        remaining.append(f"{len(pending_tasks) - 5} more task(s)")

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

def _run_bot_thread(task: str, password: str):
    """Run bot tasks in a new event loop thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_bot_async(task, password))
    except Exception as e:
        state["status"] = "error"
        add_log("error", f"Fatal error: {str(e)}")
        logger.error(f"Bot thread error: {e}")
    finally:
        loop.close()


async def _run_bot_async(task: str, password: str):
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
    accounts = load_encrypted_accounts(password)
    overall_complete = True

    humanizer = Humanizer(
        delay_min=settings.get("delay_min", 3),
        delay_max=settings.get("delay_max", 8),
    )
    trends = TrendsManager()
    notifier = Notifier(settings)
    points_tracker = PointsTracker(settings)
    challenge_handler = ManualCaptchaHandler(
        settings,
        notifier=notifier,
        on_log=add_log,
    )
    login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)
    searcher = Searcher(
        humanizer,
        trends,
        settings,
        challenge_handler=challenge_handler,
    )

    for idx, account in enumerate(accounts):
        if state["status"] == "stopping":
            add_log("warning", "Stopped by user")
            state["status"] = "idle"
            return

        # ── Inter-account delay (except first) ──
        if idx > 0:
            import random as _rng
            delay = _rng.randint(30, 120)
            add_log("info", f"⏳ Waiting {delay}s before next account (anti-detection)...")
            state["current_task"] = f"Cooldown ({delay}s)"
            await asyncio.sleep(delay)

        email = account["email"]
        searcher.set_account_context(email)
        session_proxy = get_proxy_for_session(account)
        storage_state_path = _storage_state_path(email)
        state["current_account"] = email[:5] + "***"
        add_log("info", f"━━━ Account {idx + 1}/{len(accounts)}: {email[:5]}*** ━━━")
        account_complete = True

        try:
            # Desktop session / session bootstrap / activities
            if task in ("all", "searches", "daily", "punch", "promos", "bootstrap"):
                bm = BrowserManager(settings)
                bm.set_account(email)  # Unique fingerprint per account
                attach_runtime = False
                cdp_url = ""
                if task == "bootstrap" and bool(settings.get("bootstrap_attach_existing_edge", True)):
                    cdp_url = str(settings.get("edge_cdp_url", "http://127.0.0.1:9222")).strip()
                    add_log("info", f"Trying Edge attach bootstrap via {cdp_url}...")
                    try:
                        await bm.start_connected_edge(cdp_url)
                        add_log("info", "Attached to existing Edge debug session")
                        attach_runtime = True
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
                    except Exception as native_error:
                        add_log(
                            "warning",
                            f"Could not start dedicated Edge runtime ({native_error}). Falling back to legacy managed browser.",
                        )
                if not attach_runtime:
                    await bm.start()
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
                    continue

                # Clean up any leftover tabs from previous runs
                await close_other_tabs(page)

                # Warm-up: visit random sites before tasks (anti-detection)
                add_log("info", "🌐 Warming up browser...")
                await humanizer.warm_up_browsing(page)

                if task in ("all", "searches"):
                    # ── Check current progress first ──
                    add_log("info", "🔍 Checking search credits...")
                    status_before = await searcher.get_search_points_status(page)

                    # Desktop searches (API returns points, 3 points per search)
                    pc_done = status_before.get("pc_current", 0)
                    pc_max = status_before.get("pc_max", 0)
                    remaining_points = max(0, pc_max - pc_done)
                    # Convert points to search count (3 points per search)
                    remaining_desktop = (remaining_points + 2) // 3  # ceil division
                    if pc_max == 0:
                        remaining_desktop = settings.get("desktop_searches", 30)

                    if remaining_desktop > 0:
                        state["current_task"] = "Desktop Searches"
                        state["progress"] = 0
                        state["progress_total"] = remaining_desktop
                        add_log("info", f"🖥️ Desktop — {pc_done}/{pc_max} pts ({remaining_desktop} searches left)")

                        def on_desktop(c, t, q):
                            state["progress"] = c
                            if c % 5 == 0:
                                add_log("info", f"Desktop {c}/{t}: {q[:30]}")

                        searcher.on_progress = on_desktop
                        desktop_stats = await searcher.run_searches(page, remaining_desktop, "desktop")
                        if desktop_stats.get("fatal_error"):
                            raise RuntimeError(desktop_stats["fatal_error"])
                        add_log("info", "✅ Desktop searches done")
                    else:
                        add_log("info", f"⏭️ Desktop searches already complete ({pc_done}/{pc_max})")

                # ══ Universal Task Scanner (Daily Set + Punch Cards + Quests + Promos) ══
                if task in ("all", "daily", "punch", "promos"):
                    state["current_task"] = "All Tasks (Smart Scanner)"
                    ai = AIAgent(settings)
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
                    add_log("info",
                            f"🧠 Smart Scanner: {scan_result['completed']}/{scan_result['total']} completed, "
                            f"{scan_result['skipped_locked']} locked, {scan_result['failed']} failed")
                    await close_other_tabs(page)

                # Read points
                try:
                    points_info = await points_tracker.read_points(page)
                    state["total_points"] = points_info.get("total_points", 0)
                    add_log("info", f"💰 Points: {state['total_points']:,}")
                except Exception:
                    pass

                await _persist_storage_state(ctx, storage_state_path)

            # ══ Mobile searches — direct CDP device emulation ══
            # Replicates exactly what the RSA extension does internally:
            # Apply device emulation via CDP → search normally → remove emulation
            if task in ("all", "searches"):
                # Auto-calculate from API (like desktop does on line 706-712)
                mob_done = status_before.get("mobile_current", 0)
                mob_max = status_before.get("mobile_max", 0)
                mob_remaining_pts = max(0, mob_max - mob_done)
                mob_searches = (mob_remaining_pts + 2) // 3  # ceil division (3 pts per search)
                if mob_max == 0:
                    mob_searches = settings.get("mobile_searches", 20)

                if mob_searches <= 0:
                    add_log("info", f"⏭️ Mobile searches already complete ({mob_done}/{mob_max})")
                else:
                    add_log("info", f"📱 Mobile — {mob_done}/{mob_max} pts ({mob_searches} searches needed)")

                    cdp_client = None
                    try:
                        state["current_task"] = "Mobile Searches"
                        state["progress"] = 0
                        state["progress_total"] = mob_searches

                        # 1. Get mobile UA and viewport
                        from src.utils import get_random_user_agent, get_random_viewport
                        mobile_ua = get_random_user_agent("mobile")
                        mobile_vp = get_random_viewport("mobile")
                        is_iphone = "iPhone" in mobile_ua

                        # 2. Build Client Hints metadata (ported from extension's getUAMetadata)
                        ua_metadata = {"mobile": True, "architecture": "arm64"}
                        if is_iphone:
                            ua_metadata["platform"] = "iOS"
                            import re as _re
                            ios_match = _re.search(r"OS\s+(\d+_\d+)", mobile_ua)
                            ua_metadata["platformVersion"] = ios_match.group(1).replace("_", ".") if ios_match else "18.0"
                            ua_metadata["model"] = "iPhone"
                            ua_metadata["brands"] = []
                            ua_metadata["fullVersion"] = ""
                        else:
                            # Android
                            ua_metadata["platform"] = "Android"
                            import re as _re
                            android_match = _re.search(r"Android\s+([0-9.]+)", mobile_ua)
                            ua_metadata["platformVersion"] = android_match.group(1) if android_match else "14.0"
                            model_match = _re.search(r";\s*([^;)]+)\)\s*AppleWebKit", mobile_ua)
                            ua_metadata["model"] = model_match.group(1).strip() if model_match else "Pixel 8 Pro"
                            chrome_match = _re.search(r"Chrome/(\d+)", mobile_ua)
                            edge_match = _re.search(r"EdgA?/(\d+)", mobile_ua)
                            chrome_ver = chrome_match.group(1) if chrome_match else "131"
                            brands = [
                                {"brand": "Not_A Brand", "version": "8"},
                                {"brand": "Chromium", "version": chrome_ver},
                            ]
                            if edge_match:
                                brands.append({"brand": "Microsoft Edge", "version": edge_match.group(1)})
                            else:
                                brands.append({"brand": "Google Chrome", "version": chrome_ver})
                            ua_metadata["brands"] = brands
                            ua_metadata["fullVersion"] = f"{chrome_ver}.0.0.0"

                        # 3. Apply CDP device emulation
                        cdp_client = await page.context.new_cdp_session(page)

                        await cdp_client.send("Emulation.clearDeviceMetricsOverride")
                        await cdp_client.send("Emulation.setDeviceMetricsOverride", {
                            "mobile": True,
                            "fitWindow": True,
                            "width": mobile_vp["width"],
                            "height": mobile_vp["height"],
                            "deviceScaleFactor": 3,
                        })

                        ua_override = {"userAgent": mobile_ua}
                        if not is_iphone:
                            ua_override["userAgentMetadata"] = ua_metadata
                        await cdp_client.send("Network.setUserAgentOverride", ua_override)

                        await cdp_client.send("Network.setBypassServiceWorker", {"bypass": True})

                        add_log("info", f"📱 Emulation applied: {ua_metadata.get('model', 'iPhone')}, "
                                f"UA: {mobile_ua[:60]}...")

                        # 4. Clear Bing cookies/cache for fresh mobile session
                        try:
                            await cdp_client.send("Network.clearBrowserCookies")
                            await cdp_client.send("Network.clearBrowserCache")
                            add_log("info", "📱 Cleared browser cookies & cache")
                        except Exception as clear_err:
                            add_log("warning", f"📱 Cookie clear: {clear_err}")
                        try:
                            await page.context.clear_cookies()
                        except Exception:
                            pass

                        # 5. Re-login with mobile UA
                        add_log("info", "📱 Re-establishing session with mobile UA...")
                        await page.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(3)

                        current_url = page.url.lower()
                        if "login" in current_url or "live.com" in current_url:
                            add_log("info", "📱 Login page detected, re-authenticating...")
                            try:
                                await page.wait_for_url("**/rewards.bing.com/**", timeout=30000)
                                add_log("info", "📱 Auto-login succeeded")
                            except Exception:
                                add_log("warning", "📱 Auto-login timeout, proceeding anyway")

                        # 6. Navigate to Bing and search
                        await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(1.5)

                        # Credit probe: check mobile credits after 3 searches
                        async def mobile_credit_probe():
                            try:
                                data = await page.evaluate("""
                                    async () => {
                                        try {
                                            const r = await fetch('https://rewards.bing.com/api/getuserinfo?type=1',
                                                {credentials: 'include'});
                                            const d = await r.json();
                                            const c = d?.dashboard?.userStatus?.counters?.mobileSearch;
                                            const v = Array.isArray(c) ? c[0] : c;
                                            return v?.pointProgress || 0;
                                        } catch(e) { return -1; }
                                    }
                                """)
                                return data
                            except Exception:
                                return -1

                        searcher.set_account_context(email)
                        mobile_result = await searcher.run_searches(
                            page, mob_searches, mode="mobile",
                            credit_probe_fn=mobile_credit_probe,
                        )
                        add_log("info", f"📱 Mobile searches: {mobile_result.get('completed', 0)}/{mob_searches} OK, "
                                f"{mobile_result.get('failed', 0)} failed")

                    except Exception as mob_err:
                        import traceback
                        add_log("error", f"📱 Mobile search error: {mob_err}")
                        add_log("error", f"📱 {traceback.format_exc()[:500]}")
                    finally:
                        # Remove emulation (reset all CDP overrides)
                        if cdp_client:
                            try:
                                await cdp_client.send("Emulation.clearDeviceMetricsOverride")
                                await cdp_client.send("Network.setUserAgentOverride", {"userAgent": ""})
                                await cdp_client.send("Network.setBypassServiceWorker", {"bypass": False})
                                add_log("info", "📱 Emulation cleared")
                            except Exception:
                                pass
                            try:
                                await cdp_client.detach()
                            except Exception:
                                pass

                # ═══ Mobile Supplementary Search (deficit retry) ═══
                # After emulation is cleared, check API for mobile deficit and retry
                try:
                    await asyncio.sleep(3)
                    # Navigate desktop page to rewards to check mobile credits
                    try:
                        await page.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(3)
                    except Exception:
                        pass
                    
                    # Fetch raw API data via page.evaluate
                    raw_data = await page.evaluate("""
                        async () => {
                            try {
                                const resp = await fetch('https://rewards.bing.com/api/getuserinfo?type=1');
                                const data = await resp.json();
                                const counters = data?.dashboard?.userStatus?.counters || {};
                                const result = {};
                                for (const [key, value] of Object.entries(counters)) {
                                    const v = Array.isArray(value) ? value[0] : value;
                                    if (v && typeof v === 'object') {
                                        result[key] = {
                                            progress: v.pointProgress || 0,
                                            max: v.pointProgressMax || 0,
                                            complete: v.complete || false,
                                        };
                                    }
                                }
                                return result;
                            } catch(e) { return {error: e.message}; }
                        }
                    """)
                    
                    mob_current = 0
                    mob_max_api = 0
                    if raw_data:
                        add_log("info", f"📊 RAW API counters: {json.dumps(raw_data, indent=None)}")
                        mob_data = raw_data.get("mobileSearch", {})
                        mob_current = mob_data.get("progress", 0)
                        mob_max_api = mob_data.get("max", 0)
                        add_log("info", f"📱 POST-search mobile credits: {mob_current}/{mob_max_api}")
                    else:
                        add_log("warning", "📊 RAW API returned null")
                        try:
                            post_status = await asyncio.wait_for(
                                searcher.get_search_points_status(page),
                                timeout=15,
                            )
                            mob_current = post_status.get("mobile_current", 0)
                            mob_max_api = post_status.get("mobile_max", 60)
                            add_log("info", f"📱 POST-search mobile credits: {mob_current}/{mob_max_api}")
                        except Exception:
                            pass

                    # Supplementary mobile searches if deficit exists
                    mob_deficit_pts = max(0, mob_max_api - mob_current)
                    mob_deficit_searches = (mob_deficit_pts + 2) // 3
                    max_mobile_retries = 2
                    retry_round = 0

                    while mob_deficit_searches > 0 and retry_round < max_mobile_retries:
                        retry_round += 1
                        add_log("info", f"📱 Mobile deficit: {mob_current}/{mob_max_api} pts "
                                f"({mob_deficit_searches} more needed, round {retry_round}/{max_mobile_retries})")
                        
                        state["current_task"] = "Mobile Supplementary"
                        state["progress"] = 0
                        state["progress_total"] = mob_deficit_searches

                        cdp_client2 = None
                        try:
                            from src.utils import get_random_user_agent, get_random_viewport
                            mobile_ua2 = get_random_user_agent("mobile")
                            mobile_vp2 = get_random_viewport("mobile")
                            is_iphone2 = "iPhone" in mobile_ua2

                            # Build Client Hints metadata
                            ua_metadata2 = {"mobile": True, "architecture": "arm64"}
                            if is_iphone2:
                                ua_metadata2["platform"] = "iOS"
                                import re as _re2
                                ios_match2 = _re2.search(r"OS\s+(\d+_\d+)", mobile_ua2)
                                ua_metadata2["platformVersion"] = ios_match2.group(1).replace("_", ".") if ios_match2 else "18.0"
                                ua_metadata2["model"] = "iPhone"
                                ua_metadata2["brands"] = []
                                ua_metadata2["fullVersion"] = ""
                            else:
                                ua_metadata2["platform"] = "Android"
                                import re as _re2
                                android_match2 = _re2.search(r"Android\s+([0-9.]+)", mobile_ua2)
                                ua_metadata2["platformVersion"] = android_match2.group(1) if android_match2 else "14.0"
                                model_match2 = _re2.search(r";\s*([^;)]+)\)\s*AppleWebKit", mobile_ua2)
                                ua_metadata2["model"] = model_match2.group(1).strip() if model_match2 else "Pixel 8 Pro"
                                chrome_match2 = _re2.search(r"Chrome/(\d+)", mobile_ua2)
                                chrome_ver2 = chrome_match2.group(1) if chrome_match2 else "131"
                                brands2 = [
                                    {"brand": "Not_A Brand", "version": "8"},
                                    {"brand": "Chromium", "version": chrome_ver2},
                                    {"brand": "Google Chrome", "version": chrome_ver2},
                                ]
                                ua_metadata2["brands"] = brands2
                                ua_metadata2["fullVersion"] = f"{chrome_ver2}.0.0.0"

                            # Apply CDP emulation
                            cdp_client2 = await page.context.new_cdp_session(page)
                            await cdp_client2.send("Emulation.clearDeviceMetricsOverride")
                            await cdp_client2.send("Emulation.setDeviceMetricsOverride", {
                                "mobile": True,
                                "fitWindow": True,
                                "width": mobile_vp2["width"],
                                "height": mobile_vp2["height"],
                                "deviceScaleFactor": 3,
                                "screenOrientation": {"type": "portraitPrimary", "angle": 0},
                            })
                            await cdp_client2.send("Network.setUserAgentOverride", {
                                "userAgent": mobile_ua2,
                                "platform": ua_metadata2["platform"],
                                "userAgentMetadata": {
                                    "mobile": True,
                                    "platform": ua_metadata2["platform"],
                                    "platformVersion": ua_metadata2["platformVersion"],
                                    "architecture": "arm64",
                                    "model": ua_metadata2.get("model", ""),
                                    "brands": ua_metadata2.get("brands", []),
                                    "fullVersion": ua_metadata2.get("fullVersion", ""),
                                },
                            })
                            add_log("info", f"📱 Supplementary emulation applied: {ua_metadata2.get('model', 'mobile')}")

                            # Clear cookies and re-login for mobile
                            await page.context.clear_cookies()
                            await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=15000)
                            await asyncio.sleep(2)
                            await page.goto("https://www.bing.com/rewards/signin", wait_until="domcontentloaded", timeout=15000)
                            await asyncio.sleep(3)
                            try:
                                await login_mgr.login(page, email, account.get("password", ""), account.get("totp_secret", ""))
                            except Exception:
                                pass

                            # Run supplementary searches
                            def on_mob_supp(c, t, q):
                                state["progress"] = c
                            searcher.on_progress = on_mob_supp
                            searcher.set_account_context(email)
                            supp_result = await searcher.run_searches(
                                page, mob_deficit_searches, mode="mobile",
                            )
                            add_log("info", f"📱 Supplementary mobile: {supp_result.get('completed', 0)}/{mob_deficit_searches} OK")

                        except Exception as supp_err:
                            add_log("warning", f"📱 Supplementary mobile error: {supp_err}")
                        finally:
                            if cdp_client2:
                                try:
                                    await cdp_client2.send("Emulation.clearDeviceMetricsOverride")
                                    await cdp_client2.send("Network.setUserAgentOverride", {"userAgent": ""})
                                    await cdp_client2.send("Network.setBypassServiceWorker", {"bypass": False})
                                except Exception:
                                    pass
                                try:
                                    await cdp_client2.detach()
                                except Exception:
                                    pass

                        # Re-check mobile credits after supplementary round
                        await asyncio.sleep(5)
                        try:
                            await page.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=15000)
                            await asyncio.sleep(3)
                            recheck = await page.evaluate("""
                                async () => {
                                    try {
                                        const resp = await fetch('https://rewards.bing.com/api/getuserinfo?type=1');
                                        const data = await resp.json();
                                        const mob = data?.dashboard?.userStatus?.counters?.mobileSearch;
                                        const m = Array.isArray(mob) ? mob[0] : mob;
                                        return m ? {progress: m.pointProgress||0, max: m.pointProgressMax||0} : null;
                                    } catch(e) { return null; }
                                }
                            """)
                            if recheck:
                                mob_current = recheck.get("progress", 0)
                                mob_max_api = recheck.get("max", mob_max_api)
                                add_log("info", f"📱 After supplementary: {mob_current}/{mob_max_api}")
                                mob_deficit_pts = max(0, mob_max_api - mob_current)
                                mob_deficit_searches = (mob_deficit_pts + 2) // 3
                            else:
                                mob_deficit_searches = 0
                        except Exception:
                            mob_deficit_searches = 0

                except asyncio.TimeoutError:
                    add_log("warning", "📱 Post-search check timed out")
                except Exception as pe:
                    add_log("warning", f"📱 Post-search check failed: {pe}")

                # Close desktop Edge browser
                await _persist_storage_state(ctx, storage_state_path)
                await bm.close()


            # Edge session (searches + browsing streak)
            if task in ("all", "searches"):
                state["current_task"] = "Edge Session"
                add_log("info", "🔷 Edge Session")
                try:
                    edge_runtime_settings = dict(settings)
                    edge_runtime_settings["use_stealth"] = False
                    bm3 = BrowserManager(edge_runtime_settings)
                    bm3.set_account(email)

                    # Use NATIVE Edge runtime (subprocess + CDP) for searches
                    edge_streak_native = False
                    edge_streak_cdp_url = ""
                    if bool(settings.get("native_edge_runtime_enabled", True)):
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
                        add_log("info", f"🔷 Edge — {edge_done}/{edge_max} pts ({remaining_edge} searches left)")

                        def on_edge(c, t, q):
                            state["progress"] = c
                        searcher.on_progress = on_edge
                        edge_stats = await searcher.run_searches(page3, remaining_edge, "edge")
                        if edge_stats.get("fatal_error"):
                            raise RuntimeError(edge_stats["fatal_error"])
                        add_log("info", "✅ Edge searches done")
                    else:
                        if edge_max == 0:
                            add_log("info", "⏭️ Edge searches not available")
                        else:
                            add_log("info", f"⏭️ Edge searches already complete ({edge_done}/{edge_max})")

                    # Close search browser before streak
                    await _persist_storage_state(ctx3, storage_state_path)
                    await bm3.close()

                    # ── Edge Browsing Streak ──
                    state["current_task"] = "Edge Browsing Streak"
                    add_log("info", "🌐 Edge Browsing Streak — checking availability...")

                    # The dashboard's TaskDetector already queried the API
                    # during Edge searches. Re-use that data or re-query.
                    try:
                        bm_streak = BrowserManager(edge_runtime_settings)
                        streak_cdp = await bm_streak.start_native_edge_runtime(email)
                        ctx_s, page_s = await _open_account_context(
                            bm_streak, login_mgr, account,
                            session_proxy, "desktop", storage_state_path,
                            attach_existing_edge=True,
                            attached_cdp_url=streak_cdp,
                        )
                        if not await login_mgr.is_logged_in(page_s):
                            page_s = await login_mgr.login(
                                page_s, email, account["password"],
                                account.get("totp_secret"),
                            )

                        task_detector = TaskDetector()
                        tasks = await task_detector.get_all_tasks(page_s)
                        edge_info = tasks.get("streaks", {}).get("edge", {})

                        # Check if Edge Streak promo exists in API at all
                        offer_id = edge_info.get("offerId", "")
                        edge_hash = edge_info.get("hash", "")
                        min_done = edge_info.get("minutes", 0)
                        min_target = edge_info.get("target", 0)
                        streak_complete = edge_info.get("done", False)

                        if min_target == 0 and not offer_id and not edge_hash:
                            # Edge Streak promo doesn't exist — not available
                            add_log(
                                "info",
                                "⏭️ Edge Browsing Streak not available for this "
                                "account/region — skipping",
                            )
                        elif streak_complete or min_done >= min_target:
                            add_log(
                                "info",
                                f"⏭️ Edge Streak already complete "
                                f"({min_done}/{min_target} min)",
                            )
                        else:
                            # Promo exists but not complete — try to complete
                            add_log(
                                "info",
                                f"Edge Streak: {min_done}/{min_target} min, "
                                f"offerId={offer_id}",
                            )
                            streak_credited = False
                            dest_url = edge_info.get("destinationUrl", "")

                            # Try API credit if offerId exists
                            if offer_id and not streak_credited:
                                add_log("info", f"📡 Trying API credit...")
                                api_result = await page_s.evaluate("""
                                    async (offerId) => {
                                        try {
                                            const r = await fetch(
                                                'https://prod.rewardsplatform.microsoft.com/dapi/me/activities',
                                                {
                                                    method: 'POST',
                                                    credentials: 'include',
                                                    headers: {
                                                        'Content-Type': 'application/json',
                                                        'Accept': 'application/json',
                                                    },
                                                    body: JSON.stringify({
                                                        id: crypto.randomUUID(),
                                                        offerId: offerId,
                                                        type: 'urlreward',
                                                        amount: 1,
                                                        timestamp: new Date().toISOString(),
                                                        attributes: { type: 'urlreward' },
                                                    }),
                                                }
                                            );
                                            const text = await r.text();
                                            return {status: r.status, body: text.substring(0, 300)};
                                        } catch(e) { return {error: e.message}; }
                                    }
                                """, offer_id)
                                add_log("info", f"   API: {json.dumps(api_result)}")
                                await asyncio.sleep(5)
                                t2 = await task_detector.get_all_tasks(page_s)
                                e2 = t2.get("streaks", {}).get("edge", {})
                                if e2.get("done") or e2.get("minutes", 0) >= e2.get("target", 30):
                                    add_log("info", "✅ Edge Streak credited via API!")
                                    streak_credited = True

                            # Try card click
                            if dest_url and not streak_credited:
                                add_log("info", f"🖱️ Trying card activation...")
                                try:
                                    full_url = (
                                        dest_url if dest_url.startswith("http")
                                        else f"https://rewards.bing.com{dest_url}"
                                    )
                                    await page_s.goto(full_url, wait_until="domcontentloaded", timeout=15000)
                                    await asyncio.sleep(5)
                                    await page_s.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=15000)
                                    await asyncio.sleep(3)
                                    t3 = await task_detector.get_all_tasks(page_s)
                                    e3 = t3.get("streaks", {}).get("edge", {})
                                    if e3.get("done") or e3.get("minutes", 0) >= e3.get("target", 30):
                                        add_log("info", "✅ Edge Streak credited via card!")
                                        streak_credited = True
                                except Exception as ce:
                                    add_log("debug", f"Card error: {ce}")

                            # Fallback: Native Edge browsing (no CDP — only way telemetry works)
                            if not streak_credited:
                                add_log(
                                    "info",
                                    "📖 Starting Native Edge browsing (30 min + 5 min buffer)..."
                                    " CDP will be closed — Edge telemetry requires no automation.",
                                )
                                # Close CDP session first — NativeEdgeStreak needs to kill all Edge
                                await bm_streak.close()
                                bm_streak = None  # Mark as closed

                                state["progress"] = 0
                                state["progress_total"] = 30
                                native_streak = NativeEdgeStreak()

                                def _on_native_streak(done, total):
                                    state["progress"] = min(done, total)

                                await native_streak.browse(
                                    target_minutes=30,
                                    on_progress=_on_native_streak,
                                )
                                add_log("info", "✅ Native Edge browsing session completed (30+ min)")

                        if bm_streak is not None:
                            await bm_streak.close()

                    except Exception as streak_err:
                        add_log("warning", f"⚠️ Edge Streak error: {streak_err}")
                        import traceback
                        add_log("debug", traceback.format_exc())
                        try:
                            if bm_streak is not None:
                                await bm_streak.close()
                        except Exception:
                            pass
                except Exception as e:
                    add_log("warning", f"⚠️ Edge session error: {e}")
                    try:
                        await bm3.close()
                    except Exception:
                        pass

            # ── Post-run Verification (with error handling) ──
            if task in ("all", "searches"):
                add_log("info", "🔎 Verifying search credits...")
                try:
                    bm_verify = BrowserManager(settings)
                    bm_verify.set_account(email)
                    verify_attach_runtime = False
                    verify_cdp_url = ""
                    if bool(settings.get("native_edge_runtime_enabled", True)):
                        try:
                            verify_cdp_url = await bm_verify.start_native_edge_runtime(email)
                            verify_attach_runtime = True
                        except Exception:
                            pass
                    if not verify_attach_runtime:
                        await bm_verify.start()
                    ctx_v, page_v = await _open_account_context(
                        bm_verify,
                        login_mgr,
                        account,
                        session_proxy,
                        "desktop",
                        storage_state_path,
                        attach_existing_edge=verify_attach_runtime,
                        attached_cdp_url=verify_cdp_url if verify_attach_runtime else "",
                    )

                    final_status = await searcher.get_search_points_status(page_v)
                    pc_final = final_status.get("pc_current", 0)
                    pc_final_max = final_status.get("pc_max", 0)
                    mob_final = final_status.get("mobile_current", 0)
                    mob_final_max = final_status.get("mobile_max", 0)

                    deficit = []
                    if pc_final_max > 0 and pc_final < pc_final_max:
                        deficit.append(f"Desktop: {pc_final}/{pc_final_max} ({pc_final_max - pc_final} short)")
                    if mob_final_max > 0 and mob_final < mob_final_max:
                        deficit.append(f"Mobile: {mob_final}/{mob_final_max} ({mob_final_max - mob_final} short)")

                    if deficit:
                        add_log("warning", f"⚠️ Search deficit: {', '.join(deficit)}")
                    else:
                        add_log("info", "✅ All search credits verified")

                    await _persist_storage_state(ctx_v, storage_state_path)
                    await bm_verify.close()
                except Exception as e:
                    add_log("warning", f"⚠️ Verification error: {e}")

            # ── Bing App Streak ──
            if task == "all":
                add_log("info", "🔥 Bing App Streak check-in...")
                state["current_task"] = "Bing App Streak"
                try:
                    import random as _rng
                    bing_app_settings = dict(settings)
                    bing_app_settings["use_stealth"] = False
                    bm_app = BrowserManager(bing_app_settings)
                    bm_app.set_account(email)
                    await bm_app.start()
                    bing_app_ua = _rng.choice(BingAppStreak.BING_APP_UA)
                    ctx_app, page_app = await _open_account_context(
                        bm_app,
                        login_mgr,
                        account,
                        session_proxy,
                        "mobile",
                        storage_state_path,
                        user_agent=bing_app_ua,
                        use_persistent_profile=False,
                    )

                    bing_streak = BingAppStreak(humanizer)
                    success = await bing_streak.check_in(page_app)
                    if success:
                        add_log("info", "✅ Bing App Streak check-in done")
                    else:
                        add_log("warning", "⚠️ Bing App Streak check-in may have failed")
                    await _persist_storage_state(ctx_app, storage_state_path)
                    await bm_app.close()
                except Exception as e:
                    add_log("warning", f"⚠️ Bing App Streak error: {e}")

            # Edge Browsing Streak is already handled above in the Edge Session block
            # (lines 573-600) — no duplicate needed

            if task == "all":
                add_log("info", "🔎 Final Rewards verification...")
                bm_final = None
                try:
                    bm_final = BrowserManager(settings)
                    bm_final.set_account(email)
                    final_attach_runtime = False
                    final_cdp_url = ""
                    if bool(settings.get("native_edge_runtime_enabled", True)):
                        try:
                            final_cdp_url = await bm_final.start_native_edge_runtime(email)
                            final_attach_runtime = True
                        except Exception:
                            pass
                    if not final_attach_runtime:
                        await bm_final.start()
                    ctx_final, page_final = await _open_account_context(
                        bm_final,
                        login_mgr,
                        account,
                        session_proxy,
                        "desktop",
                        storage_state_path,
                        attach_existing_edge=final_attach_runtime,
                        attached_cdp_url=final_cdp_url if final_attach_runtime else "",
                    )

                    verification = await _collect_final_verification(
                        page_final,
                        searcher,
                        humanizer,
                        settings,
                    )
                    remaining_items = _describe_remaining_items(verification)

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
                    await _persist_storage_state(ctx_final, storage_state_path)
                except Exception as e:
                    account_complete = False
                    overall_complete = False
                    add_log("warning", f"⚠️ Final verification error: {e}")
                finally:
                    if bm_final is not None:
                        try:
                            await bm_final.close()
                        except Exception:
                            pass
            else:
                add_log("info", f"✅ Task '{task}' finished for {email[:5]}***")

        except Exception as e:
            overall_complete = False
            add_log("error", f"❌ {email[:5]}***: {str(e)}")
            logger.error(f"Account {email} error: {e}")
            notifier.send_error(email, str(e))

    state["status"] = "idle"
    state["current_task"] = ""
    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if task == "all":
        if overall_complete:
            add_log("info", "🏁 All tasks completed and verified!")
        else:
            add_log("warning", "🏁 Run finished with remaining tasks. Check the warnings above.")
    else:
        add_log("info", f"🏁 Task '{task}' finished")


def start_dashboard(port: int = 8080, host: str = "127.0.0.1"):
    """Start the dashboard server (waitress production WSGI)."""
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    def _serve():
        try:
            from waitress import serve
            serve(app, host=host, port=port, threads=4)
        except ImportError:
            # Fallback to Flask dev server (suppress warning)
            import os
            os.environ["WERKZEUG_RUN_MAIN"] = "true"
            app.run(host=host, port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    logger.info(f"Dashboard started: http://{host}:{port}")
    return thread
