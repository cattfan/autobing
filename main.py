"""
Rewards Search Automator — Main Entry Point
A powerful bot combining features from Automate Bing Rewards Searches
and microsoft-rewards-bot, built with Python + Playwright.

Usage:
    python main.py           # Interactive CLI menu
    python main.py --web     # Launch Web Dashboard GUI
    python main.py --auto    # Auto-run all tasks (for scheduled execution)
"""

import asyncio
import sys
import json
import signal
import random
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.text import Text
from rich import box

from src.utils import (
    logger,
    load_settings,
    save_settings,
    CONFIG_DIR,
    PROFILES_DIR,
    close_other_tabs,
    get_proxy_for_session,
    is_sensitive_setting,
)
from src.crypto import (
    load_encrypted_accounts,
    save_encrypted_accounts,
    load_plaintext_accounts,
    migrate_to_encrypted,
    hash_password,
    prompt_master_password,
)
from src.browser import BrowserManager
from src.login import LoginManager
from src.searcher import Searcher
from src.universal_task import UniversalTaskScanner
from src.quiz import QuizSolver
from src.points import PointsTracker
from src.notifier import Notifier
from src.scheduler import Scheduler
from src.trends import TrendsManager
from src.humanizer import Humanizer
from src.ai_agent import AIAgent
from src.streaks import EdgeBrowsingStreak, TaskDetector
from src.manual_captcha import ManualCaptchaHandler


def _configure_stdio() -> None:
    """Prefer UTF-8 streams on Windows so Rich output does not crash on emoji."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()

console = Console()

BANNER = """
[bold cyan]╔══════════════════════════════════════════════╗
║      🏆 Rewards Search Automator v1.0        ║
║    ─────────────────────────────────────      ║
║   Automated Microsoft Rewards Farming Bot     ║
╚══════════════════════════════════════════════╝[/bold cyan]
"""


def _runtime_build_marker() -> str:
    """Return a visible build marker so logs show which code is running."""
    watched_files = [
        Path(__file__),
        Path(__file__).parent / "src" / "login.py",
        Path(__file__).parent / "src" / "dashboard.py",
        Path(__file__).parent / "src" / "browser.py",
        Path(__file__).parent / "src" / "searcher.py",
    ]
    latest_mtime = max(
        int(path.stat().st_mtime)
        for path in watched_files
        if path.exists()
    )
    return datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S")


def _print_build_marker() -> None:
    """Emit the current build marker to both terminal and logger."""
    marker = _runtime_build_marker()
    console.print(f"[dim]Build: {marker}[/dim]")
    logger.info(f"Build marker: {marker}")


def show_menu() -> str:
    """Display the main menu and return user choice."""
    console.print(BANNER)
    _print_build_marker()

    menu = Table(show_header=False, box=box.ROUNDED, border_style="bright_cyan")
    menu.add_column("Option", style="bold cyan", width=4)
    menu.add_column("Description", style="white")

    menu.add_row("1", "🚀 Run All Tasks")
    menu.add_row("2", "🔎 Run Searches Only")
    menu.add_row("3", "🎯 Run Daily Set Only")
    menu.add_row("4", "🃏 Run Punch Cards Only")
    menu.add_row("5", "🎁 Run Promotions Only")
    menu.add_row("6", "📊 View Points & Statistics")
    menu.add_row("7", "⚙️  Settings")
    menu.add_row("8", "👥 Manage Accounts")
    menu.add_row("9", "⏰ Setup Auto Schedule")
    menu.add_row("10", "🔔 Test Notifications")
    menu.add_row("11", "🌐 Start Web Dashboard")
    menu.add_row("0", "❌ Exit")

    console.print(menu)
    console.print()

    return Prompt.ask("[bold cyan]Choose an option", default="1")


def _empty_search_status() -> dict:
    """Return a zeroed search-credit status payload."""
    return {
        "pc_current": 0,
        "pc_max": 0,
        "mobile_current": 0,
        "mobile_max": 0,
        "edge_current": 0,
        "edge_max": 0,
    }


def _search_goal_complete(current_points: int, max_points: int, stats: dict) -> bool:
    """Infer whether a search track is complete from API points or local run stats."""
    if max_points > 0:
        return current_points >= max_points

    total = stats.get("total", 0)
    if total > 0:
        return stats.get("completed", 0) >= total

    return False


def _category_goal_complete(stats: dict) -> bool:
    """Return True when a task category has no remaining work."""
    total = int(stats.get("total", 0))
    completed = int(stats.get("completed", 0))
    return total <= 0 or completed >= total


def _storage_state_path(email: str):
    """Return the shared storage-state file for an account."""
    safe_email = email.replace("@", "_at_").replace(".", "_")
    return PROFILES_DIR / f"{safe_email}_state.json"


async def _persist_storage_state(context, storage_state_path) -> None:
    """Persist cookies/local storage so later contexts reuse the same session."""
    try:
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(storage_state_path))
    except Exception as e:
        logger.debug(f"Could not persist storage state {storage_state_path}: {e}")


async def _open_account_context(
    browser_mgr: BrowserManager,
    account: dict,
    session_proxy: dict | None,
    login_mgr: LoginManager,
    mode: str,
    storage_state_path,
):
    """Open a context for one mode, reusing stored session state when available."""
    storage_state = str(storage_state_path) if storage_state_path.exists() else None
    ctx = await browser_mgr.create_context(
        mode=mode,
        account_email=account["email"],
        proxy=session_proxy,
        storage_state=storage_state,
        use_persistent_profile=False,
    )
    page = await browser_mgr.new_page(ctx)

    if not storage_state:
        page = await login_mgr.login(
            page,
            account["email"],
            account["password"],
            account.get("totp_secret"),
        )
    elif not await login_mgr.is_logged_in(page):
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await browser_mgr.close()
        except Exception:
            pass
        await browser_mgr.start()
        if storage_state:
            try:
                storage_state_path.unlink()
            except Exception:
                logger.debug(f"Could not remove stale storage state: {storage_state_path}")
        ctx = await browser_mgr.create_context(
            mode=mode,
            account_email=account["email"],
            proxy=session_proxy,
            storage_state=None,
            use_persistent_profile=False,
        )
        page = await browser_mgr.new_page(ctx)
        page = await login_mgr.login(
            page,
            account["email"],
            account["password"],
            account.get("totp_secret"),
        )

    ctx = page.context
    await _persist_storage_state(ctx, storage_state_path)
    return ctx, page


def _mode_credit(status: dict, mode: str) -> tuple[int, int]:
    """Return current/max points for a search mode."""
    if mode == "desktop":
        return status.get("pc_current", 0), status.get("pc_max", 0)
    if mode == "mobile":
        return status.get("mobile_current", 0), status.get("mobile_max", 0)
    return status.get("edge_current", 0), status.get("edge_max", 0)


def _search_count_setting(settings: dict, mode: str) -> int:
    """Return configured search count for a mode."""
    key = "desktop_searches" if mode == "desktop" else f"{mode}_searches"
    return int(settings.get(key, 30))


def _category_progress(task_stats: dict, category: str) -> dict:
    """Return total completed/total count for one task category."""
    category_stats = task_stats.get("by_category", {}).get(category, {})
    completed_now = int(category_stats.get("completed", 0))
    skipped_done = int(category_stats.get("skipped_done", 0))
    total = int(category_stats.get("total", 0))
    return {
        "completed": completed_now + skipped_done,
        "total": total,
    }


async def _collect_final_verification(
    settings: dict,
    account: dict,
    session_proxy: dict | None,
    login_mgr: LoginManager,
    searcher: Searcher,
    humanizer: Humanizer,
    storage_state_path,
) -> dict:
    """Capture the final Rewards state used for honest CLI reporting."""
    browser_mgr = BrowserManager(settings)
    browser_mgr.set_account(account["email"])
    ctx = None

    snapshot = {
        "search_status": {},
        "task_overview": {},
        "category_status": {},
        "pending_tasks": [],
    }

    try:
        await browser_mgr.start()
        ctx = await browser_mgr.create_context(
            mode="desktop",
            account_email=account["email"],
            proxy=session_proxy,
            storage_state=str(storage_state_path) if storage_state_path.exists() else None,
            use_persistent_profile=False,
        )
        page = await browser_mgr.new_page(ctx)

        if not await login_mgr.is_logged_in(page):
            page = await login_mgr.login(
                page,
                account["email"],
                account["password"],
                account.get("totp_secret"),
            )

        snapshot["search_status"] = await searcher.get_search_points_status(page)
        snapshot["task_overview"] = await TaskDetector().get_all_tasks(page)

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

            title = (reward_task.title or reward_task.id or category).strip()
            if title and title not in seen_titles:
                seen_titles.add(title)
                snapshot["pending_tasks"].append(title)

        await _persist_storage_state(ctx, storage_state_path)
    finally:
        try:
            if ctx is not None:
                await browser_mgr.close_context(ctx)
        except Exception:
            pass
        try:
            await browser_mgr.close()
        except Exception:
            pass

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


def _raise_if_search_stopped(mode: str, stats: dict) -> None:
    """Abort the account session when Bing requests verification."""
    fatal_error = stats.get("fatal_error", "")
    if fatal_error:
        raise RuntimeError(f"{mode} search stopped: {fatal_error}")


async def _refresh_account_summary(
    settings: dict,
    account: dict,
    session_proxy: dict | None,
    login_mgr: LoginManager,
    searcher: Searcher,
    points_tracker: PointsTracker,
    fallback_status: dict,
    fallback_points: dict,
    browser_mgr: BrowserManager | None = None,
    storage_state_path=None,
) -> tuple[dict, dict]:
    """Re-read final points and search credits after all task windows are finished."""
    owns_browser = browser_mgr is None
    ctx = None

    if owns_browser:
        browser_mgr = BrowserManager(settings)
        browser_mgr.set_account(account["email"])

    try:
        if owns_browser:
            await browser_mgr.start()

        if browser_mgr is None:
            return fallback_status, fallback_points

        state_ref = None
        if storage_state_path is not None and storage_state_path.exists():
            state_ref = str(storage_state_path)

        ctx = await browser_mgr.create_context(
            mode="desktop",
            account_email=account["email"],
            proxy=session_proxy,
            storage_state=state_ref,
            use_persistent_profile=False,
        )
        page = await browser_mgr.new_page(ctx)

        if not await login_mgr.is_logged_in(page):
            page = await login_mgr.login(
                page,
                account["email"],
                account["password"],
                account.get("totp_secret"),
            )

        ctx = page.context
        final_status = await searcher.get_search_points_status(page)
        final_points = await points_tracker.read_points(page)
        if storage_state_path is not None:
            await _persist_storage_state(ctx, storage_state_path)
        return final_status, final_points
    except Exception as e:
        logger.warning(
            f"Final summary refresh failed for {account['email'][:5]}***: {e}"
        )
        return fallback_status, fallback_points
    finally:
        try:
            if ctx is not None and browser_mgr is not None:
                await browser_mgr.close_context(ctx)
        except Exception:
            pass
        if owns_browser and browser_mgr is not None:
            try:
                await browser_mgr.close()
            except Exception:
                pass


async def run_all_tasks(
    settings: dict, accounts: list[dict], password: str
) -> None:
    """Run all farming tasks for all accounts."""
    notifier = Notifier(settings)
    humanizer = Humanizer(
        delay_min=settings.get("delay_min", 3),
        delay_max=settings.get("delay_max", 8),
        typing_delay_min=settings.get("typing_delay_min", 50),
        typing_delay_max=settings.get("typing_delay_max", 150),
    )
    trends = TrendsManager()
    points_tracker = PointsTracker(settings)
    challenge_handler = ManualCaptchaHandler(settings, notifier=notifier)
    searcher = Searcher(
        humanizer,
        trends,
        settings,
        challenge_handler=challenge_handler,
    )
    login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)
    ai_agent = AIAgent(settings)
    universal_tasks = UniversalTaskScanner(
        humanizer,
        ai_agent=ai_agent,
        settings=settings,
        challenge_handler=challenge_handler,
    )
    overall_complete = True

    for idx, account in enumerate(accounts):
        email = account["email"]
        searcher.set_account_context(email)
        session_proxy = get_proxy_for_session(account)
        storage_state_path = _storage_state_path(email)
        console.print(
            f"\n[bold cyan]━━━ Account {idx + 1}/{len(accounts)}: {email[:5]}*** ━━━[/bold cyan]"
        )

        browser_mgr = BrowserManager(settings)
        browser_mgr.set_account(email)
        all_searches = {"desktop": {}, "mobile": {}, "edge": {}}
        daily_stats = {}
        punch_stats = {}
        promo_stats = {}
        total_points = 0
        streak = 0
        starting_points = 0
        final_search_status = _empty_search_status()
        pc_done = pc_max = mob_done = mob_max = edge_done = edge_max = 0
        remaining_items: list[str] = []
        account_complete = True

        try:
            await browser_mgr.start()

            # ─── Desktop Context (main session) ────────────────────────
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=30),
                TaskProgressColumn(),
                console=console,
            ) as progress:

                # Desktop context
                console.print("\n[bold]🖥️  Desktop Session[/bold]")
                ctx = await browser_mgr.create_context(
                    mode="desktop",
                    account_email=email,
                    proxy=session_proxy,
                    storage_state=str(storage_state_path) if storage_state_path.exists() else None,
                    use_persistent_profile=False,
                )
                page = await browser_mgr.new_page(ctx)

                # Login
                logged_in = await login_mgr.is_logged_in(page)
                if not logged_in:
                    page = await login_mgr.login(
                        page,
                        email,
                        account["password"],
                        account.get("totp_secret"),
                    )
                ctx = page.context
                await _persist_storage_state(ctx, storage_state_path)

                # Check for account issues
                issue = await login_mgr.detect_account_issues(page)
                if issue:
                    console.print(f"[bold red]⚠️  {issue}[/bold red]")
                    notifier.send_error(email, issue)
                    await browser_mgr.close()
                    continue

                # ── Check search credits via API ──
                await close_other_tabs(page)
                console.print("[dim]🔍 Checking search credits...[/dim]")
                search_status = await searcher.get_search_points_status(page)
                pc_done = search_status.get("pc_current", 0)
                pc_max = search_status.get("pc_max", 0)
                mob_done = search_status.get("mobile_current", 0)
                mob_max = search_status.get("mobile_max", 0)
                edge_done = search_status.get("edge_current", 0)
                edge_max = search_status.get("edge_max", 0)
                final_search_status = dict(search_status)

                console.print(
                    f"[dim]  PC: {pc_done}/{pc_max}  |  "
                    f"Mobile: {mob_done}/{mob_max}  |  "
                    f"Edge: {edge_done}/{edge_max}[/dim]"
                )

                points_info = await points_tracker.read_points(page)
                starting_points = points_info.get("total_points", 0)
                total_points = starting_points
                streak = points_info.get("streak", 0)

                # Desktop searches — skip if already done
                remaining_pc = max(0, pc_max - pc_done)
                remaining_desktop = (remaining_pc + 2) // 3 if remaining_pc > 0 else 0

                if remaining_desktop > 0:
                    task_id = progress.add_task("Desktop searches", total=remaining_desktop)

                    def on_desktop_progress(current, total, query):
                        progress.update(task_id, completed=current)

                    searcher.on_progress = on_desktop_progress
                    desktop_stats = await searcher.run_searches(page, remaining_desktop, "desktop")
                    _raise_if_search_stopped("Desktop", desktop_stats)
                    all_searches["desktop"] = {"completed": desktop_stats["completed"], "total": remaining_desktop}
                else:
                    console.print(f"[green]⏭️  Desktop searches already complete ({pc_done}/{pc_max})[/green]")
                    all_searches["desktop"] = {"completed": 0, "total": 0}

                # ── Universal Tasks (Daily Set + Punch Cards + Promotions) ──
                console.print("\n[bold]🎯 All Tasks (Daily Set + Punch Cards + Promotions)[/bold]")
                task_stats = await universal_tasks.scan_and_complete(page, account_email=email)
                daily_stats = _category_progress(task_stats, "daily_set")
                punch_stats = _category_progress(task_stats, "punch_card")
                promo_stats = _category_progress(task_stats, "more_promo")
                await close_other_tabs(page)
                await _persist_storage_state(ctx, storage_state_path)
                await browser_mgr.close_context(ctx)

            # ─── Mobile Searches (skip if already done) ───────────────
            remaining_mob = max(0, mob_max - mob_done)
            remaining_mobile = (remaining_mob + 2) // 3 if remaining_mob > 0 else 0

            if remaining_mobile > 0:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(bar_width=30),
                    TaskProgressColumn(),
                    console=console,
                ) as progress:

                    console.print("\n[bold]📱 Mobile Searches[/bold]")
                    mobile_runtime_settings = dict(settings)
                    mobile_runtime_settings["use_stealth"] = False
                    browser_mgr2 = BrowserManager(mobile_runtime_settings)
                    browser_mgr2.set_account(email)
                    await browser_mgr2.start()
                    ctx_mobile = await browser_mgr2.create_context(
                        mode="mobile",
                        account_email=email + "_mobile",
                        proxy=session_proxy,
                        storage_state=str(storage_state_path) if storage_state_path.exists() else None,
                        use_persistent_profile=False,
                    )
                    page_mobile = await browser_mgr2.new_page(ctx_mobile)

                    logged_in = await login_mgr.is_logged_in(page_mobile)
                    if not logged_in:
                        page_mobile = await login_mgr.login(
                            page_mobile,
                            email,
                            account["password"],
                            account.get("totp_secret"),
                        )
                    ctx_mobile = page_mobile.context
                    await _persist_storage_state(ctx_mobile, storage_state_path)

                    task_m = progress.add_task("Mobile searches", total=remaining_mobile)

                    def on_mobile_progress(current, total, query):
                        progress.update(task_m, completed=current)

                    searcher.on_progress = on_mobile_progress
                    mobile_stats = await searcher.run_searches(
                        page_mobile, remaining_mobile, "mobile"
                    )
                    _raise_if_search_stopped("Mobile", mobile_stats)
                    all_searches["mobile"] = {
                        "completed": mobile_stats["completed"],
                        "total": remaining_mobile,
                    }

                    await _persist_storage_state(ctx_mobile, storage_state_path)
                    await browser_mgr2.close()
            else:
                console.print(f"\n[green]⏭️  Mobile searches already complete ({mob_done}/{mob_max})[/green]")
                all_searches["mobile"] = {"completed": 0, "total": 0}

            # ─── Edge Session (searches + browsing streak) ─────────────
            console.print("\n[bold]🔷 Edge Session[/bold]")
            try:
                edge_runtime_settings = dict(settings)
                edge_runtime_settings["use_stealth"] = False
                browser_mgr3 = BrowserManager(edge_runtime_settings)
                browser_mgr3.set_account(email)
                await browser_mgr3.start_clean_edge()
                ctx_edge = await browser_mgr3.create_context(
                    mode="edge",
                    account_email=email + "_edge",
                    proxy=session_proxy,
                    storage_state=str(storage_state_path) if storage_state_path.exists() else None,
                    use_persistent_profile=False,
                )
                page_edge = await browser_mgr3.new_page(ctx_edge)

                logged_in = await login_mgr.is_logged_in(page_edge)
                if not logged_in:
                    page_edge = await login_mgr.login(
                        page_edge, email,
                        account["password"], account.get("totp_secret"),
                    )
                ctx_edge = page_edge.context
                await _persist_storage_state(ctx_edge, storage_state_path)

                # Edge searches (skip if done or no points)
                remaining_edge_pts = max(0, edge_max - edge_done)
                remaining_edge = (remaining_edge_pts + 2) // 3 if remaining_edge_pts > 0 else 0

                if remaining_edge > 0:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(bar_width=30),
                        TaskProgressColumn(),
                        console=console,
                    ) as progress:
                        task_e = progress.add_task("Edge searches", total=remaining_edge)

                        def on_edge_progress(current, total, query):
                            progress.update(task_e, completed=current)

                        searcher.on_progress = on_edge_progress
                        edge_stats = await searcher.run_searches(page_edge, remaining_edge, "edge")
                        _raise_if_search_stopped("Edge", edge_stats)
                        all_searches["edge"] = {
                            "completed": edge_stats["completed"],
                            "total": remaining_edge,
                        }
                else:
                    if edge_max == 0:
                        console.print("[green]⏭️  Edge searches not available[/green]")
                    else:
                        console.print(f"[green]⏭️  Edge searches already complete ({edge_done}/{edge_max})[/green]")
                    all_searches["edge"] = {"completed": 0, "total": 0}

                # Edge Browsing Streak (reuse same browser!)
                console.print("\n[bold]🌐 Edge Browsing Streak[/bold]")
                task_detector = TaskDetector()
                tasks = await task_detector.get_all_tasks(page_edge)
                edge_streak_info = tasks.get("streaks", {}).get("edge", {})
                minutes_done = edge_streak_info.get("minutes", 0)
                minutes_target = edge_streak_info.get("target", 30)
                streak_done = edge_streak_info.get("done", False)

                if streak_done or minutes_done >= minutes_target:
                    console.print(f"[green]⏭️  Edge Streak already complete ({minutes_done}/{minutes_target} min)[/green]")
                else:
                    remaining_min = minutes_target - minutes_done
                    browse_cap = max(
                        minutes_target + 15,
                        minutes_done + max(12, remaining_min * 2 + 10),
                    )
                    console.print(
                        f"[dim]  Progress: {minutes_done}/{minutes_target} min "
                        f"— browsing until verified (cap {browse_cap} min)...[/dim]"
                    )

                    edge_streak = EdgeBrowsingStreak(humanizer)

                    def on_streak_progress(done, total):
                        console.print(f"[dim]  Edge streak: {done}/{total} min[/dim]")

                    await edge_streak.browse(
                        page_edge,
                        target_minutes=minutes_target,
                        on_progress=on_streak_progress,
                        initial_minutes=minutes_done,
                        hard_cap_minutes=browse_cap,
                    )
                    refreshed_tasks = await task_detector.get_all_tasks(page_edge)
                    refreshed_edge = refreshed_tasks.get("streaks", {}).get("edge", {})
                    refreshed_done = refreshed_edge.get("minutes", 0)
                    refreshed_target = refreshed_edge.get("target", minutes_target)
                    if refreshed_edge.get("done", False) or refreshed_done >= refreshed_target:
                        console.print("[green]✅ Edge Browsing Streak completed[/green]")
                    else:
                        console.print(
                            f"[yellow]⚠️  Edge Browsing Streak not verified "
                            f"({refreshed_done}/{refreshed_target} min)[/yellow]"
                        )

                await _persist_storage_state(ctx_edge, storage_state_path)
                await browser_mgr3.close()
            except Exception as e:
                console.print(f"[yellow]⚠️  Edge session error: {e}[/yellow]")
                try:
                    await browser_mgr3.close()
                except Exception:
                    pass

            # ─── Log & Notify ────────────────────────────────────────
            final_search_status, final_points_info = await _refresh_account_summary(
                settings,
                account,
                session_proxy,
                login_mgr,
                searcher,
                points_tracker,
                fallback_status=final_search_status,
                fallback_points={
                    "total_points": total_points,
                    "streak": streak,
                },
                storage_state_path=storage_state_path,
            )
            total_points = final_points_info.get("total_points", total_points)
            streak = final_points_info.get("streak", streak)
            earned_today = max(0, total_points - starting_points)

            console.print("\n[bold]🔎 Final Rewards Verification[/bold]")
            try:
                verification = await _collect_final_verification(
                    settings,
                    account,
                    session_proxy,
                    login_mgr,
                    searcher,
                    humanizer,
                    storage_state_path,
                )
                if verification.get("search_status"):
                    final_search_status = verification["search_status"]

                verified_daily = verification.get("task_overview", {}).get("daily_set", {})
                if verified_daily:
                    daily_stats = {
                        "completed": int(verified_daily.get("completed", 0)),
                        "total": int(verified_daily.get("total", 0)),
                    }

                verified_categories = verification.get("category_status", {})
                if "punch_card" in verified_categories:
                    punch_stats = {
                        "completed": int(verified_categories["punch_card"].get("completed", 0)),
                        "total": int(verified_categories["punch_card"].get("total", 0)),
                    }
                if "more_promo" in verified_categories:
                    promo_stats = {
                        "completed": int(verified_categories["more_promo"].get("completed", 0)),
                        "total": int(verified_categories["more_promo"].get("total", 0)),
                    }

                remaining_items = _describe_remaining_items(verification)
                account_complete = len(remaining_items) == 0
                if account_complete:
                    console.print("[green]✅ Rewards state fully verified[/green]")
                else:
                    overall_complete = False
                    console.print(
                        "[yellow]⚠️  Remaining items:[/yellow] "
                        + ", ".join(remaining_items[:8])
                    )
            except Exception as e:
                account_complete = False
                overall_complete = False
                remaining_items = [f"Verification error: {e}"]
                console.print(f"[yellow]⚠️  Final verification error: {e}[/yellow]")

            points_tracker.log_daily(
                total_points=total_points,
                earned_today=earned_today,
                desktop_done=_search_goal_complete(
                    final_search_status.get("pc_current", 0),
                    final_search_status.get("pc_max", 0),
                    all_searches["desktop"],
                ),
                mobile_done=_search_goal_complete(
                    final_search_status.get("mobile_current", 0),
                    final_search_status.get("mobile_max", 0),
                    all_searches["mobile"],
                ),
                edge_done=_search_goal_complete(
                    final_search_status.get("edge_current", 0),
                    final_search_status.get("edge_max", 0),
                    all_searches["edge"],
                ),
                daily_set_done=_category_goal_complete(daily_stats),
                streak=streak,
            )

            # Generate graph
            graph_path = points_tracker.generate_graph()

            # Send notification
            notifier.send_completion(
                account=email,
                points=total_points,
                searches_done=all_searches,
                daily_set=daily_stats,
                punch_cards=punch_stats,
                promotions=promo_stats,
                streak=streak,
                graph_path=graph_path,
                verified_complete=account_complete,
                remaining_items=remaining_items,
            )

            # Check auto-redeem
            redeem_msg = await _check_redeem(points_tracker, total_points, settings, notifier, email)

            # Print summary
            _print_summary(
                email,
                total_points,
                streak,
                all_searches,
                daily_stats,
                punch_stats,
                promo_stats,
                verified_complete=account_complete,
                remaining_items=remaining_items,
            )
            await browser_mgr.close()

        except Exception as e:
            overall_complete = False
            console.print(f"[bold red]Error: {e}[/bold red]")
            logger.error(f"Account {email} error: {e}")
            notifier.send_error(email, str(e))
            try:
                await browser_mgr.close()
            except Exception:
                pass

        # Inter-account cooldown (reduce detection risk)
        if idx < len(accounts) - 1:
            cooldown = random.randint(30, 90)
            console.print(f"\n[dim]⏳ Waiting {cooldown}s before next account...[/dim]")
            await asyncio.sleep(cooldown)

    if overall_complete:
        console.print("\n[bold green]🏁 All tasks completed and verified![/bold green]")
    else:
        console.print(
            "\n[bold yellow]🏁 Run finished with remaining tasks. Review the warnings above.[/bold yellow]"
        )


async def _check_redeem(tracker, points, settings, notifier, email):
    """Check auto-redeem condition."""
    if settings.get("auto_redeem", False):
        goal = settings.get("auto_redeem_goal", 5000)
        if points >= goal:
            notifier.send_redeem_alert(email, points, goal)
            return f"Ready to redeem: {points} >= {goal}"
    return None


def _print_summary(
    email,
    points,
    streak,
    searches,
    daily,
    punch,
    promo,
    verified_complete: bool = True,
    remaining_items: list[str] | None = None,
):
    """Print a summary table."""
    remaining_items = remaining_items or []
    title_icon = "✅" if verified_complete else "⚠️"
    border = "cyan" if verified_complete else "yellow"
    table = Table(
        title=f"{title_icon} Summary for {email[:5]}***",
        box=box.DOUBLE_EDGE,
        border_style=border,
    )
    table.add_column("Task", style="bold")
    table.add_column("Result", style="green")

    table.add_row("💰 Points", f"{points:,}")
    table.add_row("🔥 Streak", f"{streak} days")
    table.add_row("Status", "Verified" if verified_complete else "Incomplete")
    table.add_row("🖥️  Desktop", f"{searches['desktop'].get('completed', 0)}/{searches['desktop'].get('total', 0)}")
    table.add_row("📱 Mobile", f"{searches['mobile'].get('completed', 0)}/{searches['mobile'].get('total', 0)}")
    table.add_row("🔷 Edge", f"{searches['edge'].get('completed', 0)}/{searches['edge'].get('total', 0)}")
    table.add_row("🎯 Daily Set", f"{daily.get('completed', 0)}/{daily.get('total', 0)}")
    table.add_row("🃏 Punch Cards", f"{punch.get('completed', 0)}/{punch.get('total', 0)}")
    table.add_row("🎁 Promotions", f"{promo.get('completed', 0)}/{promo.get('total', 0)}")
    if remaining_items:
        table.add_row("Remaining", f"{len(remaining_items)} item(s)")

    console.print(table)
    if remaining_items:
        console.print("[yellow]Remaining:[/yellow] " + ", ".join(remaining_items[:8]))


async def run_searches_only(settings, accounts, password):
    """Run only search tasks."""
    notifier = Notifier(settings)
    humanizer = Humanizer(
        delay_min=settings.get("delay_min", 3),
        delay_max=settings.get("delay_max", 8),
    )
    trends = TrendsManager()
    challenge_handler = ManualCaptchaHandler(settings, notifier=notifier)
    searcher = Searcher(
        humanizer,
        trends,
        settings,
        challenge_handler=challenge_handler,
    )
    login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)

    for account in accounts:
        email = account["email"]
        searcher.set_account_context(email)
        session_proxy = get_proxy_for_session(account)
        console.print(f"\n[bold cyan]🔎 Searches for {email[:5]}***[/bold cyan]")

        storage_state_path = _storage_state_path(email)
        for mode in ["desktop", "mobile", "edge"]:
            browser_mgr = BrowserManager(settings)
            browser_mgr.set_account(email)
            try:
                await browser_mgr.start()
                ctx = await browser_mgr.create_context(
                    mode=mode, account_email=email + f"_{mode}",
                    proxy=session_proxy,
                    storage_state=str(storage_state_path) if storage_state_path.exists() else None,
                    use_persistent_profile=False,
                )
                page = await browser_mgr.new_page(ctx)

                if not await login_mgr.is_logged_in(page):
                    page = await login_mgr.login(page, email, account["password"], account.get("totp_secret"))
                ctx = page.context
                await _persist_storage_state(ctx, storage_state_path)

                # Check credits first — skip if already full
                status = await searcher.get_search_points_status(page)
                cur, mx = _mode_credit(status, mode)

                if mx > 0 and cur >= mx:
                    console.print(f"  [green]⏭️  {mode} already full ({cur}/{mx})[/green]")
                    await browser_mgr.close()
                    continue

                if mode == "edge" and mx == 0:
                    console.print("  [yellow]Edge credits not available from Rewards API, skipping[/yellow]")
                    await browser_mgr.close()
                    continue

                remaining_pts = max(0, mx - cur) if mx > 0 else 0
                count_raw = _search_count_setting(settings, mode)
                count = min(count_raw, (remaining_pts + 2) // 3) if remaining_pts > 0 else count_raw

                console.print(f"\n[bold]{mode.upper()} — {count} searches (credits: {cur}/{mx})[/bold]")

                with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(), console=console) as p:
                    tid = p.add_task(f"{mode} searches", total=count)
                    searcher.on_progress = lambda c, t, q: p.update(tid, completed=c)
                    stats = await searcher.run_searches(page, count, mode)
                    _raise_if_search_stopped(mode.capitalize(), stats)

                await _persist_storage_state(ctx, storage_state_path)
                await browser_mgr.close()
            except Exception as e:
                console.print(f"[red]{mode} error: {e}[/red]")
                try: await browser_mgr.close()
                except: pass


def manage_accounts(settings):
    """Manage accounts (add, list, remove)."""
    console.print("\n[bold cyan]👥 Account Management[/bold cyan]")
    console.print("1. Add Account")
    console.print("2. List Accounts")
    console.print("3. Import from accounts.json")
    console.print("4. Back")

    choice = Prompt.ask("Choose", default="4")

    if choice == "1":
        email = Prompt.ask("Email")
        password = Prompt.ask("Password", password=True)
        totp = Prompt.ask("TOTP Secret (leave empty if none)", default="")
        proxy = Prompt.ask("Proxy URL (leave empty if none)", default="")

        account = {
            "email": email,
            "password": password,
            "totp_secret": totp or None,
            "proxy": proxy or None,
        }

        master_pw = Prompt.ask("Master password", password=True)

        try:
            accounts = load_encrypted_accounts(master_pw)
        except FileNotFoundError:
            accounts = []
        except ValueError:
            console.print("[red]Wrong master password[/red]")
            return

        accounts.append(account)
        save_encrypted_accounts(accounts, master_pw)

        # Save password hash
        settings["master_password_hash"] = hash_password(master_pw)
        save_settings(settings)

        console.print("[green]✅ Account added[/green]")

    elif choice == "2":
        master_pw = Prompt.ask("Master password", password=True)
        try:
            accounts = load_encrypted_accounts(master_pw)
            for i, acc in enumerate(accounts):
                console.print(f"  {i + 1}. {acc['email']}")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    elif choice == "3":
        master_pw = Prompt.ask("Set master password", password=True)
        if migrate_to_encrypted(master_pw):
            settings["master_password_hash"] = hash_password(master_pw)
            save_settings(settings)
            console.print("[green]✅ Accounts migrated to encrypted storage[/green]")
        else:
            console.print("[yellow]No accounts.json found[/yellow]")


def manage_settings(settings):
    """Settings management."""
    console.print("\n[bold cyan]⚙️  Settings[/bold cyan]")

    table = Table(show_header=True, box=box.SIMPLE)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")

    for key, value in settings.items():
        if is_sensitive_setting(key):
            table.add_row(key, "***" if value else "(empty)")
        else:
            table.add_row(key, str(value))

    console.print(table)

    console.print("\n1. Edit a setting")
    console.print("2. Reset to defaults")
    console.print("3. Back")

    choice = Prompt.ask("Choose", default="3")

    if choice == "1":
        key = Prompt.ask("Setting name")
        if key in settings:
            current = settings[key]
            if isinstance(current, bool):
                settings[key] = Confirm.ask(f"{key}", default=current)
            elif isinstance(current, int):
                settings[key] = IntPrompt.ask(f"{key}", default=current)
            else:
                settings[key] = Prompt.ask(f"{key}", default=str(current))
            save_settings(settings)
            console.print("[green]✅ Setting updated[/green]")
        else:
            console.print(f"[red]Unknown setting: {key}[/red]")

    elif choice == "2":
        from src.utils import get_default_settings
        settings = get_default_settings()
        save_settings(settings)
        console.print("[green]✅ Settings reset to defaults[/green]")


def view_points(settings):
    """View points history and statistics."""
    tracker = PointsTracker(settings)
    stats = tracker.get_statistics()

    table = Table(title="📊 Points Statistics", box=box.DOUBLE_EDGE, border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="cyan")

    table.add_row("📅 Days Tracked", str(stats["days_tracked"]))
    table.add_row("💰 Total Earned", f"{stats['total_earned']:,}")
    table.add_row("📊 Daily Average", str(stats["avg_daily"]))
    table.add_row("🔥 Current Streak", f"{stats['streak']} days")
    table.add_row("🏆 Max Streak", f"{stats.get('max_streak', 0)} days")
    table.add_row("💵 Est. Monthly", f"{stats.get('estimated_monthly', 0):,.0f}")

    console.print(table)

    if Confirm.ask("Generate graph?", default=True):
        path = tracker.generate_graph()
        if path:
            console.print(f"[green]📊 Graph saved: {path}[/green]")


def setup_schedule(settings):
    """Setup auto-scheduling."""
    scheduler = Scheduler(settings)

    console.print("\n[bold cyan]⏰ Auto Schedule Setup[/bold cyan]")
    console.print(f"Current schedule: {settings.get('schedule_time', 'Not set')}")
    console.print(f"Next run: {scheduler.get_countdown()}")

    status = scheduler.check_task_status()
    if status:
        console.print("[green]Windows Task: Active[/green]")
    else:
        console.print("[yellow]Windows Task: Not configured[/yellow]")

    console.print("\n1. Set daily schedule")
    console.print("2. Remove schedule")
    console.print("3. Back")

    choice = Prompt.ask("Choose", default="3")

    if choice == "1":
        time_str = Prompt.ask("Run time (HH:MM)", default="08:00")
        settings["schedule_time"] = time_str
        settings["schedule_enabled"] = True
        save_settings(settings)

        if scheduler.setup_windows_task(time_str):
            console.print(f"[green]✅ Scheduled daily at {time_str}[/green]")
        else:
            console.print("[red]Failed to create Windows Task[/red]")

    elif choice == "2":
        if scheduler.remove_windows_task():
            settings["schedule_enabled"] = False
            save_settings(settings)
            console.print("[green]✅ Schedule removed[/green]")


async def main():
    """Main entry point.

    Default: launch web dashboard
    --cli:  interactive CLI menu
    --auto: auto-run all tasks (for scheduled execution)
    """
    if "--auto" in sys.argv:
        console.print(BANNER)
        _print_build_marker()
        console.print("[cyan]Running in auto mode...[/cyan]")
        settings = load_settings()
        pw_hash = settings.get("master_password_hash", "")
        if not pw_hash:
            logger.error("No master password configured. Run interactive mode first.")
            return

        import os
        password = os.environ.get("REWARDS_BOT_PASSWORD", "")
        if not password:
            password = prompt_master_password(pw_hash)

        accounts = load_encrypted_accounts(password)
        await run_all_tasks(settings, accounts, password)
        return

    # ─── CLI Mode (explicit --cli flag) ────────────────────────
    if "--cli" in sys.argv:
        # Interactive menu
        while True:
            settings = load_settings()
            choice = show_menu()

            if choice == "0":
                console.print("[bold cyan]Goodbye! 👋[/bold cyan]")
                break

            await _handle_cli_choice(choice, settings)
        return

    # ─── Default: Web Dashboard ────────────────────────────────
    console.print(BANNER)
    _print_build_marker()
    import webbrowser
    import signal
    import threading as _th
    from src.dashboard import start_dashboard

    settings = load_settings()
    port = settings.get("dashboard_port", 8080)
    host = settings.get("dashboard_host", "127.0.0.1")
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    console.print(f"[bold cyan]🌐 Starting Web Dashboard on port {port}...[/bold cyan]")

    start_dashboard(port, host)
    url = f"http://{display_host}:{port}"
    console.print(f"[green]Dashboard: {url}[/green]")
    webbrowser.open(url)
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    # Use threading Event (avoids asyncio CancelledError on Windows)
    stop_event = _th.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    stop_event.wait()
    console.print("\n[bold cyan]Dashboard stopped. Goodbye! 👋[/bold cyan]")


async def _handle_cli_choice(choice: str, settings: dict) -> None:
    """Handle a CLI menu choice."""
    if choice in ("1", "2", "3", "4", "5"):
        pw_hash = settings.get("master_password_hash", "")
        if not pw_hash:
            console.print("[yellow]No accounts configured. Setting up now...[/yellow]")
            manage_accounts(settings)
            settings = load_settings()
            pw_hash = settings.get("master_password_hash", "")
            if not pw_hash:
                return

        try:
            password = prompt_master_password(pw_hash)
            accounts = load_encrypted_accounts(password)
        except ValueError:
            console.print("[red]Wrong master password[/red]")
            return
        except FileNotFoundError:
            console.print("[red]No accounts file. Add accounts first.[/red]")
            return

        if choice == "1":
            await run_all_tasks(settings, accounts, password)
        elif choice == "2":
            await run_searches_only(settings, accounts, password)
        elif choice == "3":
            # Daily Set (via Universal Task Scanner)
            humanizer = Humanizer()
            ai_agent = AIAgent(settings)
            challenge_handler = ManualCaptchaHandler(settings, notifier=Notifier(settings))
            ut = UniversalTaskScanner(
                humanizer,
                ai_agent=ai_agent,
                settings=settings,
                challenge_handler=challenge_handler,
            )
            login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)
            for acc in accounts:
                session_proxy = get_proxy_for_session(acc)
                bm = BrowserManager(settings)
                bm.set_account(acc["email"])
                await bm.start()
                ctx = await bm.create_context(
                    mode="desktop",
                    account_email=acc["email"],
                    proxy=session_proxy,
                )
                page = await bm.new_page(ctx)
                if not await login_mgr.is_logged_in(page):
                    page = await login_mgr.login(page, acc["email"], acc["password"], acc.get("totp_secret"))
                ctx = page.context
                await ut.scan_and_complete(page, account_email=acc["email"])
                await bm.close()
        elif choice == "4":
            # Punch Cards (via Universal Task Scanner)
            humanizer = Humanizer()
            ai_agent = AIAgent(settings)
            challenge_handler = ManualCaptchaHandler(settings, notifier=Notifier(settings))
            ut = UniversalTaskScanner(
                humanizer,
                ai_agent=ai_agent,
                settings=settings,
                challenge_handler=challenge_handler,
            )
            login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)
            for acc in accounts:
                session_proxy = get_proxy_for_session(acc)
                bm = BrowserManager(settings)
                bm.set_account(acc["email"])
                await bm.start()
                ctx = await bm.create_context(
                    mode="desktop",
                    account_email=acc["email"],
                    proxy=session_proxy,
                )
                page = await bm.new_page(ctx)
                if not await login_mgr.is_logged_in(page):
                    page = await login_mgr.login(page, acc["email"], acc["password"], acc.get("totp_secret"))
                ctx = page.context
                await ut.scan_and_complete(page, account_email=acc["email"])
                await bm.close()
        elif choice == "5":
            # Promotions (via Universal Task Scanner)
            humanizer = Humanizer()
            ai_agent = AIAgent(settings)
            challenge_handler = ManualCaptchaHandler(settings, notifier=Notifier(settings))
            ut = UniversalTaskScanner(
                humanizer,
                ai_agent=ai_agent,
                settings=settings,
                challenge_handler=challenge_handler,
            )
            login_mgr = LoginManager(humanizer, challenge_handler=challenge_handler)
            for acc in accounts:
                session_proxy = get_proxy_for_session(acc)
                bm = BrowserManager(settings)
                bm.set_account(acc["email"])
                await bm.start()
                ctx = await bm.create_context(
                    mode="desktop",
                    account_email=acc["email"],
                    proxy=session_proxy,
                )
                page = await bm.new_page(ctx)
                if not await login_mgr.is_logged_in(page):
                    page = await login_mgr.login(page, acc["email"], acc["password"], acc.get("totp_secret"))
                ctx = page.context
                await ut.scan_and_complete(page, account_email=acc["email"])
                await bm.close()

        Prompt.ask("\n[dim]Press Enter to continue[/dim]")

    elif choice == "6":
        view_points(settings)
    elif choice == "7":
        manage_settings(settings)
    elif choice == "8":
        manage_accounts(settings)
    elif choice == "9":
        setup_schedule(settings)
    elif choice == "10":
        notifier = Notifier(settings)
        results = notifier.test_notifications()
        for channel, result in results.items():
            console.print(f"  {channel}: {result}")
    elif choice == "11":
        from src.dashboard import start_dashboard
        port = settings.get("dashboard_port", 8080)
        host = settings.get("dashboard_host", "127.0.0.1")
        display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
        start_dashboard(port, host)
        console.print(f"[green]Dashboard: http://{display_host}:{port}[/green]")
        Prompt.ask("Press Enter to continue")


# ─── Graceful Shutdown ───────────────────────────────────────────────

_browser_managers: list = []  # Track active browser managers for cleanup


def _shutdown_handler(sig, frame):
    """Handle Ctrl+C / SIGTERM gracefully."""
    console.print("\n[bold yellow]⚠️  Shutting down gracefully...[/bold yellow]")
    logger.info("Shutdown signal received, cleaning up...")
    # Close any tracked browser managers
    for bm in _browser_managers:
        try:
            asyncio.get_event_loop().run_until_complete(bm.close())
        except Exception:
            pass
    logger.info("Cleanup complete. Goodbye!")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    asyncio.run(main())
