"""
Rewards Search Automator — Main Entry Point
A powerful bot combining features from Automate Bing Rewards Searches
and microsoft-rewards-bot, built with Python + Playwright.

Usage:
    python main.py           # Interactive CLI menu
    python main.py --web     # Launch Web Dashboard GUI
    python main.py --auto    # Auto-run all tasks (for scheduled execution)
"""

from __future__ import annotations
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
    emit_diagnostic_log,
    get_proxy_for_session,
    is_sensitive_setting,
    mask_email,
    summarize_search_status,
)
from src.crypto import (
    load_encrypted_accounts,
    save_encrypted_accounts,
    load_plaintext_accounts,
    migrate_to_encrypted,
    hash_password,
    prompt_master_password,
)
from src.browser import BrowserManager, load_storage_state_cookies
from src.login import LoginManager
from src.searcher import Searcher
from src.universal_task import UniversalTaskScanner, get_deferred_offer_reason
from src.google_sheets import GoogleSheetsLogger
from src.quiz import QuizSolver
from src.points import PointsTracker
from src.notifier import Notifier
from src.scheduler import Scheduler
from src.control_plane import ScheduleUpdate, apply_schedule_update
from src.trends import TrendsManager
from src.humanizer import Humanizer
from src.ai_agent import AIAgent
from src.streaks import EdgeBrowsingStreak, TaskDetector
from src.edge_streak_native import NativeEdgeStreak
from src.manual_captcha import ManualCaptchaHandler
from src.runtime_identity import (
    build_runtime_descriptor,
    describe_search_remaining_items,
)
from src.page_agent_flow import PageAgentFlow
from src.dashboard import (
    _select_mobile_runtime_strategy,
    _start_gpm_profile,
    _stop_gpm_profile,
)


def _configure_stdio() -> None:
    """Buộc stdout/stderr dùng UTF-8 trên Windows — fix emoji & tiếng Việt trong CMD."""
    import ctypes
    # Đặt code page CMD lên 65001 (UTF-8) theo cách Win32 API
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()

# legacy_windows=False: buộc Rich dùng ANSI/UTF-8 thay vì Windows Console API cũ
# force_terminal=True: không bị detect sai là non-interactive pipe
console = Console(force_terminal=True, legacy_windows=False)

BANNER = """
[bold cyan]+-------------------------------------------------+
|        Rewards Search Automator  v1.0           |
|        Automated Microsoft Rewards Bot          |
+-------------------------------------------------+[/bold cyan]
[dim]Register: https://rewards.bing.com/welcome?rh=CE9698B&ref=rafsrchae[/dim]
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

    menu.add_row("1", "Run All Tasks")
    menu.add_row("2", "Run Searches Only")
    menu.add_row("3", "Run Daily Set Only")
    menu.add_row("4", "Run Punch Cards Only")
    menu.add_row("5", "Run Promotions Only")
    menu.add_row("6", "View Points & Statistics")
    menu.add_row("7", "Settings")
    menu.add_row("8", "Manage Accounts")
    menu.add_row("9", "Setup Auto Schedule")
    menu.add_row("10", "Test Notifications")
    menu.add_row("11", "Start Web Dashboard")
    menu.add_row("12", "Run Page-Agent Flow")
    menu.add_row("0", "Exit")

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


def _remaining_search_count(current_points: int, max_points: int) -> int:
    """Convert a point deficit into the minimum search count needed to fill it."""
    if max_points <= 0 or current_points >= max_points:
        return 0
    return (max_points - current_points + 2) // 3


def _normalize_reward_title(value: str) -> str:
    """Normalize Rewards task titles for reconciliation across locale/UI variants."""
    normalized = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " "
        for ch in (value or "").replace("\u200b", " ").replace("\xa0", " ")
    )
    return " ".join(normalized.split())


def _needs_mobile_credit_recheck(status: dict, settings: dict) -> bool:
    """Detect ambiguous mobile 0/0 reads that should be retried before gating work."""
    mobile_searches = _search_count_setting(settings, "mobile")
    current, maximum = _mode_credit(status, "mobile")
    return mobile_searches > 0 and current == 0 and maximum == 0


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


async def _read_search_status_with_mobile_recheck(
    searcher: Searcher,
    page,
    settings: dict,
) -> dict:
    """Re-read Rewards counters when mobile credits come back as an ambiguous 0/0."""
    status = await searcher.get_search_points_status(page)
    if _needs_mobile_credit_recheck(status, settings):
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
    emit_diagnostic_log(
        logger,
        settings,
        "Initial search-credit read",
        scope="search-status",
        status=summarize_search_status(status),
        page_url=getattr(page, "url", ""),
    )
    if not _needs_mobile_credit_recheck(status, settings):
        return status

    retries = max(1, int(settings.get("mobile_credit_recheck_attempts", 2)))
    delay_seconds = max(1.0, float(settings.get("mobile_credit_recheck_delay_seconds", 3)))

    logger.info(
        "Mobile credits returned 0/0; rechecking before deciding whether to skip searches."
    )
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
        emit_diagnostic_log(
            logger,
            settings,
            "Mobile credit recheck attempt finished",
            scope="search-status",
            attempt=attempt + 1,
            retries=retries,
            status=summarize_search_status(status),
        )
        if not _needs_mobile_credit_recheck(status, settings):
            logger.info(
                f"Mobile credit recheck resolved on attempt {attempt + 1}: "
                f"{status.get('mobile_current', 0)}/{status.get('mobile_max', 0)}"
            )
            break

    return status


async def _probe_search_status_in_mode(
    settings: dict,
    account: dict,
    session_proxy: dict | None,
    login_mgr: LoginManager,
    searcher: Searcher,
    storage_state_path,
    *,
    mode: str,
) -> dict:
    """Read Rewards counters from a dedicated browser mode/runtime."""
    runtime_settings = dict(settings)
    runtime_settings["use_stealth"] = False
    runtime_settings["headless"] = True
    browser_mgr = BrowserManager(runtime_settings)
    browser_mgr.set_account(account["email"])
    ctx = None
    masked_email = mask_email(account.get("email", ""))

    try:
        emit_diagnostic_log(
            logger,
            settings,
            "Opening dedicated probe runtime",
            scope="search-probe",
            account=masked_email,
            mode=mode,
            has_storage_state=storage_state_path.exists(),
            proxy=bool(session_proxy),
        )
        await browser_mgr.start()
        ctx = await browser_mgr.create_context(
            mode=mode,
            account_email=account["email"] if mode == "desktop" else f"{account['email']}_{mode}_probe",
            proxy=session_proxy,
            storage_state=str(storage_state_path) if storage_state_path.exists() else None,
            use_persistent_profile=False,
        )
        page = await browser_mgr.new_page(ctx)

        if not await login_mgr.is_logged_in(page):
            emit_diagnostic_log(
                logger,
                settings,
                "Probe runtime needs fresh login",
                scope="search-probe",
                account=masked_email,
                mode=mode,
            )
            page = await login_mgr.login(
                page,
                account["email"],
                account["password"],
                account.get("totp_secret"),
            )

        ctx = page.context
        if mode == "mobile":
            try:
                await browser_mgr.toggle_mobile_emulation(page, enable=True)
                await asyncio.sleep(1)
                emit_diagnostic_log(
                    logger,
                    settings,
                    "Mobile emulation enabled for probe",
                    scope="search-probe",
                    account=masked_email,
                )
            except Exception as e:
                logger.warning(f"Mobile probe emulation activation failed: {e}")
        status = await _read_search_status_with_mobile_recheck(
            searcher,
            page,
            settings,
        )
        emit_diagnostic_log(
            logger,
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
            if ctx is not None:
                await browser_mgr.close_context(ctx)
        except Exception:
            pass
        try:
            await browser_mgr.close()
        except Exception:
            pass


async def _resolve_mobile_search_requirement(
    settings: dict,
    account: dict,
    session_proxy: dict | None,
    login_mgr: LoginManager,
    searcher: Searcher,
    storage_state_path,
    baseline_status: dict,
) -> dict:
    """Resolve ambiguous mobile 0/0 credits before deciding to skip mobile searches."""
    if not _needs_mobile_credit_recheck(baseline_status, settings):
        return baseline_status

    emit_diagnostic_log(
        logger,
        settings,
        "Resolving ambiguous mobile credits",
        scope="mobile-resolution",
        account=mask_email(account.get("email", "")),
        baseline=summarize_search_status(baseline_status),
    )
    logger.info("Mobile credits still ambiguous after desktop read; probing mobile runtime.")
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
        logger.warning(f"Mobile runtime probe failed: {e}")
        return baseline_status

    if _needs_mobile_credit_recheck(probed_status, settings):
        logger.warning(
            "Mobile runtime probe still returned 0/0; treating mobile credits as unknown and not auto-skipping."
        )
        emit_diagnostic_log(
            logger,
            settings,
            "Mobile probe remained ambiguous",
            scope="mobile-resolution",
            account=mask_email(account.get("email", "")),
            probed=summarize_search_status(probed_status),
        )
        return baseline_status

    merged_status = dict(baseline_status)
    merged_status["mobile_current"] = probed_status.get("mobile_current", 0)
    merged_status["mobile_max"] = probed_status.get("mobile_max", 0)
    if probed_status.get("total_points", 0) > 0:
        merged_status["total_points"] = probed_status.get("total_points", 0)

    logger.info(
        f"Mobile runtime probe resolved credits: "
        f"{merged_status.get('mobile_current', 0)}/{merged_status.get('mobile_max', 0)}"
    )
    emit_diagnostic_log(
        logger,
        settings,
        "Mobile credits resolved after probe merge",
        scope="mobile-resolution",
        account=mask_email(account.get("email", "")),
        merged=summarize_search_status(merged_status),
    )
    return merged_status


async def _run_mobile_search_pass(
    settings: dict,
    account: dict,
    session_proxy: dict | None,
    login_mgr: LoginManager,
    searcher: Searcher,
    storage_state_path,
    *,
    count: int,
    title: str = "Mobile Searches",
) -> dict:
    """Run one bounded mobile-search pass in a dedicated mobile context."""
    if count <= 0:
        return {"completed": 0, "total": 0}

    mobile_runtime_settings = dict(settings)
    mobile_runtime_settings["use_stealth"] = False
    browser_mgr = BrowserManager(mobile_runtime_settings)
    browser_mgr.set_account(account["email"])
    masked_email = mask_email(account.get("email", ""))
    ctx_mobile = None
    page_mobile = None
    patchright_pw = None
    patchright_browser = None
    runtime_family = "managed_edge"
    gpm_mobile_id = str(account.get("gpm_mobile_profile_id") or "").strip()
    gpm_enabled = bool(settings.get("gpm_integration_enabled", False))
    gpm_api_url = str(settings.get("gpm_api_url", "http://127.0.0.1:9495")).rstrip("/")
    fallback_to_native_mobile, mobile_runtime_strategy = _select_mobile_runtime_strategy(
        gpm_enabled,
        gpm_mobile_id,
    )
    mobile_runtime = None

    try:
        emit_diagnostic_log(
            logger,
            settings,
            "Starting dedicated mobile search pass",
            scope="mobile-pass",
            account=masked_email,
            count=count,
            has_storage_state=storage_state_path.exists(),
            proxy=bool(session_proxy),
            strategy=mobile_runtime_strategy,
            has_gpm_mobile_profile=bool(gpm_mobile_id),
        )

        if fallback_to_native_mobile:
            if bool(settings.get("mobile_patchright_enabled", True)):
                try:
                    patchright_pw, patchright_browser, ctx_mobile, page_mobile = await browser_mgr.create_mobile_patchright(
                        load_storage_state_cookies(storage_state_path)
                    )
                    runtime_family = "patchright_mobile"
                    mobile_runtime = build_runtime_descriptor(
                        "patchright_mobile",
                        account["email"],
                        "mobile",
                    )
                    logger.info("Mobile search pass using patchright mobile runtime")
                except Exception as e:
                    logger.warning(f"Patchright mobile startup failed, falling back to emulation: {e}")

            if page_mobile is None:
                await browser_mgr.start()
                ctx_mobile = await browser_mgr.create_context(
                    mode="mobile",
                    account_email=account["email"] + "_mobile",
                    proxy=session_proxy,
                    storage_state=str(storage_state_path) if storage_state_path.exists() else None,
                    use_persistent_profile=False,
                )
                page_mobile = await browser_mgr.new_page(ctx_mobile)
                mobile_runtime = build_runtime_descriptor(
                    "managed_edge",
                    account["email"],
                    "mobile",
                )
                try:
                    await browser_mgr.toggle_mobile_emulation(page_mobile, enable=True)
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.warning(f"Mobile emulation activation failed: {e}")
        else:
            mobile_cdp = await _start_gpm_profile(gpm_mobile_id, gpm_api_url)
            await browser_mgr.start_connected_edge(mobile_cdp)
            ctx_mobile = await browser_mgr.create_context(
                mode="mobile",
                account_email=account["email"],
            )
            page_mobile = await browser_mgr.new_page(ctx_mobile)
            runtime_family = "gpm_mobile"
            mobile_runtime = build_runtime_descriptor(
                "gpm_mobile",
                gpm_mobile_id,
                "mobile",
                cdp_url=mobile_cdp,
            )
            logger.info(f"Mobile search pass using GPM mobile runtime ({gpm_mobile_id[:8]})")

        logged_in = await login_mgr.is_logged_in(page_mobile)
        if not logged_in:
            page_mobile = await login_mgr.login(
                page_mobile,
                account["email"],
                account["password"],
                account.get("totp_secret"),
            )
            try:
                await browser_mgr.toggle_mobile_emulation(page_mobile, enable=True)
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Mobile runtime refresh after login failed: {e}")
        ctx_mobile = page_mobile.context
        await _persist_storage_state(ctx_mobile, storage_state_path)
        await page_mobile.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=35000)
        try:
            await browser_mgr.toggle_mobile_emulation(page_mobile, enable=True)
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Mobile runtime refresh after navigation failed: {e}")
        if hasattr(browser_mgr, "capture_runtime_signature"):
            runtime_signature = await browser_mgr.capture_runtime_signature(page_mobile)
            emit_diagnostic_log(
                logger,
                settings,
                "Mobile runtime signature before search pass",
                scope="mobile-runtime",
                account=masked_email,
                runtime_family=runtime_family,
                signature=runtime_signature,
            )

        status_before = await _read_search_status_with_mobile_recheck(
            searcher,
            page_mobile,
            settings,
        )
        emit_diagnostic_log(
            logger,
            settings,
            "Collected mobile credits before search pass",
            scope="mobile-pass",
            account=masked_email,
            before=summarize_search_status(status_before),
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            console.print(f"\n[bold][ {title} ][/bold]")
            task_m = progress.add_task("Mobile searches", total=count)

            def on_mobile_progress(current, total, query):
                progress.update(task_m, completed=current)

            searcher.on_progress = on_mobile_progress
            mobile_stats = await searcher.run_searches(
                page_mobile, count, "mobile"
            )

        _raise_if_search_stopped("Mobile", mobile_stats)
        emit_diagnostic_log(
            logger,
            settings,
            "Mobile search loop finished",
            scope="mobile-pass",
            account=masked_email,
            completed=mobile_stats.get("completed", 0),
            failed=mobile_stats.get("failed", 0),
            requested=count,
            fatal_error=mobile_stats.get("fatal_error", ""),
        )
        await _persist_storage_state(ctx_mobile, storage_state_path)
        status_after = await _wait_for_mobile_credit_update(
            searcher,
            page_mobile,
            settings,
            baseline_status=status_before,
        )
        credit_delta = _mobile_credit_delta(status_before, status_after)
        if credit_delta > 0:
            logger.info(
                f"Mobile search pass credited {credit_delta} points "
                f"({status_after.get('mobile_current', 0)}/{status_after.get('mobile_max', 0)})"
            )
        else:
            logger.warning(
                "Mobile search pass completed without observed credit change "
                f"({status_before.get('mobile_current', 0)}/{status_before.get('mobile_max', 0)} -> "
                f"{status_after.get('mobile_current', 0)}/{status_after.get('mobile_max', 0)})"
            )
        emit_diagnostic_log(
            logger,
            settings,
            "Finished mobile search pass verification",
            scope="mobile-pass",
            account=masked_email,
            before=summarize_search_status(status_before),
            after=summarize_search_status(status_after),
            credit_delta=credit_delta,
        )
        return {
            "completed": mobile_stats["completed"],
            "total": count,
            "credit_proven": credit_delta > 0,
            "status_before": status_before,
            "status_after": status_after,
            "runtime_family": runtime_family,
            "runtime_descriptor": mobile_runtime,
        }
    finally:
        try:
            if ctx_mobile is not None:
                await _persist_storage_state(ctx_mobile, storage_state_path)
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
        if not fallback_to_native_mobile and gpm_mobile_id:
            try:
                _stop_gpm_profile(gpm_mobile_id, gpm_api_url)
            except Exception:
                pass


def _mobile_credit_delta(before_status: dict, after_status: dict) -> int:
    """Return the observed change in mobile Rewards credits between two reads."""
    return max(
        0,
        int(after_status.get("mobile_current", 0))
        - int(before_status.get("mobile_current", 0)),
    )


def _edge_streak_attempt_allowed(edge_streak_info: dict) -> bool:
    """Return True when the native Edge streak loop should run.

    The native streak loop relies on refreshed task counters for verification and
    does not require an offerId to execute. Some live task payloads expose
    `exists/minutes/target/done` without an `offerId`, so do not block the loop
    on that field alone.
    """
    info = edge_streak_info or {}
    exists = bool(info.get("exists", False))
    done = bool(info.get("done", False))
    minutes_done = int(info.get("minutes", 0) or 0)
    minutes_target = int(info.get("target", 30) or 30)
    return exists and not done and minutes_done < minutes_target


async def _wait_for_mobile_credit_update(
    searcher: Searcher,
    page,
    settings: dict,
    *,
    baseline_status: dict,
) -> dict:
    """Poll mobile credits after a search pass so we know whether the pass was actually credited."""
    attempts = max(1, int(settings.get("mobile_credit_postcheck_attempts", 3)))
    delay_seconds = max(2.0, float(settings.get("mobile_credit_postcheck_delay_seconds", 6)))
    latest_status = baseline_status

    for attempt in range(attempts):
        await asyncio.sleep(delay_seconds)
        latest_status = await _read_search_status_with_mobile_recheck(
            searcher,
            page,
            settings,
        )
        emit_diagnostic_log(
            logger,
            settings,
            "Polled mobile credits after search pass",
            scope="mobile-postcheck",
            attempt=attempt + 1,
            attempts=attempts,
            baseline=summarize_search_status(baseline_status),
            latest=summarize_search_status(latest_status),
        )
        if _mobile_credit_delta(baseline_status, latest_status) > 0:
            logger.info(
                f"Mobile credits advanced after pass on attempt {attempt + 1}: "
                f"{latest_status.get('mobile_current', 0)}/{latest_status.get('mobile_max', 0)}"
            )
            return latest_status

    return latest_status


def _describe_deferred_items(snapshot: dict) -> list[str]:
    """Flatten deferred non-actionable Rewards offers for user-facing summaries."""
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


def _reconcile_verification_with_session_proof(
    snapshot: dict,
    session_proofs: dict | None = None,
) -> dict:
    """Apply run-local proofs when final Rewards APIs lag behind observed completion."""
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
    daily_category = category_status.setdefault(
        "daily_set",
        {"completed": 0, "total": daily_total},
    )
    daily_category_total = int(daily_category.get("total", 0))
    if daily_category_total > 0:
        daily_category["completed"] = daily_category_total

    pending_by_category = snapshot.setdefault("pending_by_category", {})
    stale_daily_titles = [
        _normalize_reward_title(title)
        for title in pending_by_category.get("daily_set", [])
        if title
    ]
    proof_titles = [
        _normalize_reward_title(title)
        for title in session_proofs.get("daily_set_titles", [])
        if title
    ]
    stale_title_set = set(stale_daily_titles or proof_titles)

    if stale_title_set:
        snapshot["pending_tasks"] = [
            title
            for title in snapshot.get("pending_tasks", [])
            if _normalize_reward_title(title) not in stale_title_set
        ]
    pending_by_category["daily_set"] = []

    return snapshot


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
    masked_email = mask_email(account.get("email", ""))

    snapshot = {
        "search_status": {},
        "task_overview": {},
        "category_status": {},
        "pending_tasks": [],
        "pending_by_category": {},
        "deferred_tasks": [],
    }

    try:
        emit_diagnostic_log(
            logger,
            settings,
            "Starting final verification snapshot",
            scope="final-verification",
            account=masked_email,
            has_storage_state=storage_state_path.exists(),
        )
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

        snapshot["search_status"] = await _read_search_status_with_mobile_recheck(
            searcher,
            page,
            settings,
        )
        snapshot["task_overview"] = await TaskDetector().get_all_tasks(page)
        snapshot["search_status"] = _merge_search_status_sources(
            snapshot["search_status"],
            {
                **snapshot["task_overview"].get("searches", {}),
                "total_points": snapshot["task_overview"].get("total_points", 0),
            },
        )

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
                    "title": (reward_task.title or reward_task.id or category).strip(),
                    "reason": deferred_reason,
                    "category": category,
                })
                continue

            title = (reward_task.title or reward_task.id or category).strip()
            if title and title not in seen_titles:
                seen_titles.add(title)
                snapshot["pending_tasks"].append(title)
                snapshot["pending_by_category"].setdefault(category, []).append(title)

        emit_diagnostic_log(
            logger,
            settings,
            "Final verification snapshot collected",
            scope="final-verification",
            account=masked_email,
            search_status=summarize_search_status(snapshot["search_status"]),
            categories=snapshot.get("category_status", {}),
            pending_count=len(snapshot.get("pending_tasks", [])),
            deferred_count=len(snapshot.get("deferred_tasks", [])),
        )

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
        and bing_app.get("exists", False)
        and not bing_app.get("done", False)
    ):
        remaining.append(f"Mobile App Check-in {bing_app.get('current', 0)}/1")

    edge_streak = task_overview.get("streaks", {}).get("edge", {})
    edge_minutes = edge_streak.get("minutes", 0)
    edge_target = edge_streak.get("target", 30)
    if (
        not reporting_overrides.get("ignore_edge_streak", False)
        and edge_streak.get("exists", False)
        and edge_target > 0
        and not edge_streak.get("done", False)
    ):
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
        final_status = await _read_search_status_with_mobile_recheck(
            searcher,
            page,
            settings,
        )
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
        masked_email = mask_email(email)
        searcher.set_account_context(email)
        session_proxy = get_proxy_for_session(account)
        storage_state_path = _storage_state_path(email)
        console.print(
            f"\n[bold cyan]--- Account {idx + 1}/{len(accounts)}: {email[:5]}*** ---[/bold cyan]"
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
        session_proofs: dict = {"ignore_bing_app_checkin": True}

        try:
            emit_diagnostic_log(
                logger,
                settings,
                "Starting account run",
                scope="account",
                account=masked_email,
                index=idx + 1,
                total_accounts=len(accounts),
                has_proxy=bool(session_proxy),
                has_storage_state=storage_state_path.exists(),
                ai_enabled=bool(settings.get("ai_enabled", False)),
                diagnostic_logging=bool(settings.get("diagnostic_logging", True)),
            )
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
                console.print("\n[bold][ Desktop Session ][/bold]")
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
                    console.print(f"[bold red][WARN] {issue}[/bold red]")
                    notifier.send_error(email, issue)
                    await browser_mgr.close()
                    continue

                # ── Check search credits via API ──
                await close_other_tabs(page)
                console.print("[dim]Checking search credits...[/dim]")
                search_status = await _read_search_status_with_mobile_recheck(
                    searcher,
                    page,
                    settings,
                )
                search_status = await _resolve_mobile_search_requirement(
                    settings,
                    account,
                    session_proxy,
                    login_mgr,
                    searcher,
                    storage_state_path,
                    search_status,
                )
                pc_done = search_status.get("pc_current", 0)
                pc_max = search_status.get("pc_max", 0)
                mob_done = search_status.get("mobile_current", 0)
                mob_max = search_status.get("mobile_max", 0)
                edge_done = search_status.get("edge_current", 0)
                edge_max = search_status.get("edge_max", 0)
                final_search_status = dict(search_status)
                emit_diagnostic_log(
                    logger,
                    settings,
                    "Read initial Rewards counters",
                    scope="account",
                    account=masked_email,
                    search_status=summarize_search_status(search_status),
                )

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
                    emit_diagnostic_log(
                        logger,
                        settings,
                        "Planning desktop search pass",
                        scope="desktop-search",
                        account=masked_email,
                        remaining_points=remaining_pc,
                        search_count=remaining_desktop,
                    )
                    task_id = progress.add_task("Desktop searches", total=remaining_desktop)

                    def on_desktop_progress(current, total, query):
                        progress.update(task_id, completed=current)

                    searcher.on_progress = on_desktop_progress
                    desktop_stats = await searcher.run_searches(page, remaining_desktop, "desktop")
                    _raise_if_search_stopped("Desktop", desktop_stats)
                    all_searches["desktop"] = {"completed": desktop_stats["completed"], "total": remaining_desktop}
                    emit_diagnostic_log(
                        logger,
                        settings,
                        "Desktop search pass finished",
                        scope="desktop-search",
                        account=masked_email,
                        completed=desktop_stats.get("completed", 0),
                        failed=desktop_stats.get("failed", 0),
                        requested=remaining_desktop,
                    )
                else:
                    console.print(f"[green][SKIP] Desktop searches already complete ({pc_done}/{pc_max})[/green]")
                    all_searches["desktop"] = {"completed": 0, "total": 0}

                # ── Universal Tasks (Daily Set + Punch Cards + Promotions) ──
                console.print("\n[bold][ All Tasks: Daily Set + Punch Cards + Promotions ][/bold]")
                if settings.get("page_agent_enabled", False):
                    pa_flow = PageAgentFlow(settings)
                    await pa_flow.inject(page)
                    available_flows = set(pa_flow.list_flows())

                    daily_result = (
                        await pa_flow.run_flow(page, "daily_set")
                        if "daily_set" in available_flows
                        else {"completed": 0, "total_steps": 0, "success": False}
                    )
                    console.print(f"[green]Page-agent Daily Set: {daily_result}[/green]")

                    keep_result = (
                        await pa_flow.run_flow(page, "keep_earning")
                        if "keep_earning" in available_flows
                        else {"completed": 0, "total_steps": 0, "success": False}
                    )
                    console.print(f"[green]Page-agent Keep Earning: {keep_result}[/green]")

                    explore_result = (
                        await pa_flow.run_flow(page, "explore_bing")
                        if "explore_bing" in available_flows
                        else {"completed": 0, "total_steps": 0, "success": False}
                    )
                    console.print(f"[green]Page-agent Explore on Bing: {explore_result}[/green]")

                    daily_stats = {
                        "completed": daily_result.get("completed", 0),
                        "total": daily_result.get("total_steps", 0),
                    }
                    punch_stats = {"completed": 0, "total": 0}
                    promo_stats = {
                        "completed": keep_result.get("completed", 0) + explore_result.get("completed", 0),
                        "total": keep_result.get("total_steps", 0) + explore_result.get("total_steps", 0),
                    }
                    session_proofs = {}
                    # Log for consistency
                    emit_diagnostic_log(
                        logger,
                        settings,
                        "Page-agent flows completed",
                        scope="tasks",
                        account=masked_email,
                        daily=daily_stats,
                        keep=keep_result,
                        explore=explore_result,
                        promo=promo_stats,
                    )
                else:
                    task_stats = await universal_tasks.scan_and_complete(page, account_email=email)
                    daily_stats = _category_progress(task_stats, "daily_set")
                    punch_stats = _category_progress(task_stats, "punch_card")
                    promo_stats = _category_progress(task_stats, "more_promo")
                    session_proofs = dict(task_stats.get("session_proofs", {}))
                    emit_diagnostic_log(
                        logger,
                        settings,
                        "Universal task scan finished",
                        scope="tasks",
                        account=masked_email,
                        completed=task_stats.get("completed", 0),
                        failed=task_stats.get("failed", 0),
                        deferred=task_stats.get("deferred", 0),
                        skipped_done=task_stats.get("skipped_done", 0),
                        skipped_locked=task_stats.get("skipped_locked", 0),
                        by_category=task_stats.get("by_category", {}),
                    )
                await close_other_tabs(page)
                await _persist_storage_state(ctx, storage_state_path)
                await browser_mgr.close_context(ctx)

            # ─── Mobile Searches (skip if already done) ───────────────
            mobile_status_ambiguous = _needs_mobile_credit_recheck(search_status, settings)
            remaining_mob = max(0, mob_max - mob_done)
            if mobile_status_ambiguous:
                remaining_mobile = _search_count_setting(settings, "mobile")
                logger.info(
                    f"Mobile credits remain ambiguous after probe; running configured mobile search batch ({remaining_mobile}) instead of auto-skipping."
                )
            else:
                remaining_mobile = (remaining_mob + 2) // 3 if remaining_mob > 0 else 0
            emit_diagnostic_log(
                logger,
                settings,
                "Resolved mobile search plan",
                scope="mobile-plan",
                account=masked_email,
                ambiguous=mobile_status_ambiguous,
                remaining_points=remaining_mob,
                planned_searches=remaining_mobile,
                baseline=summarize_search_status(search_status),
            )

            if remaining_mobile > 0:
                all_searches["mobile"] = await _run_mobile_search_pass(
                    settings,
                    account,
                    session_proxy,
                    login_mgr,
                    searcher,
                    storage_state_path,
                    count=remaining_mobile,
                    title="Mobile Searches",
                )
            else:
                console.print(f"\n[green][SKIP] Mobile searches already complete ({mob_done}/{mob_max})[/green]")
                all_searches["mobile"] = {"completed": 0, "total": 0}

            # ─── Edge Session (searches + browsing streak) ─────────────
            console.print("\n[bold][ Edge Session ][/bold]")
            edge_streak_info: dict = {}
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
                        console.print("[green][SKIP] Edge searches not available[/green]")
                    else:
                        console.print(f"[green][SKIP] Edge searches already complete ({edge_done}/{edge_max})[/green]")
                    all_searches["edge"] = {"completed": 0, "total": 0}

                # Edge Browsing Streak (reuse same browser!)
                console.print("\n[bold][ Edge Browsing Streak ][/bold]")
                task_detector = TaskDetector()
                tasks = await task_detector.get_all_tasks(page_edge)
                edge_streak_info = tasks.get("streaks", {}).get("edge", {})
                minutes_done = edge_streak_info.get("minutes", 0)
                minutes_target = edge_streak_info.get("target", 30)
                streak_done = edge_streak_info.get("done", False)
                edge_exists = edge_streak_info.get("exists", False)

                if not edge_exists:
                    console.print("[green][SKIP] Edge Streak task is not available or already completed[/green]")
                elif streak_done or minutes_done >= minutes_target:
                    console.print(f"[green][SKIP] Edge Streak already complete ({minutes_done}/{minutes_target} min)[/green]")
                elif not _edge_streak_attempt_allowed(edge_streak_info):
                    console.print("[green][SKIP] Edge Streak task is not actionable for this run[/green]")
                else:
                    # --- Verify-and-Retry Loop ---
                    max_attempts = 3
                    credited = minutes_done

                    for attempt in range(1, max_attempts + 1):
                        remaining = max(0, minutes_target - credited)
                        if remaining <= 0:
                            break

                        run_min = remaining + 5  # +5 buffer
                        console.print(
                            f"[dim]  [Attempt {attempt}/{max_attempts}] "
                            f"Credited: {credited}/{minutes_target} min. "
                            f"Running native Edge for {run_min} min...[/dim]"
                        )

                        native_streak = NativeEdgeStreak(account_email=email)

                        def on_streak_progress(done, total):
                            console.print(f"[dim]  Streak: {min(credited + done, minutes_target)}/{total} min[/dim]")

                        await native_streak.browse(
                            target_minutes=run_min,
                            on_progress=on_streak_progress,
                        )

                        # Verify via API
                        console.print("[dim]  Verifying via API...[/dim]")
                        try:
                            refreshed_tasks = await task_detector.get_all_tasks(page_edge)
                            refreshed_edge = refreshed_tasks.get("streaks", {}).get("edge", {})
                            credited = refreshed_edge.get("minutes", 0)
                            r_target = refreshed_edge.get("target", minutes_target)
                            r_done = refreshed_edge.get("done", False)
                            console.print(
                                f"[dim]  API: {credited}/{r_target} min (done={r_done})[/dim]"
                            )
                            if r_done or credited >= r_target:
                                break
                        except Exception:
                            console.print("[dim]  API check failed, continuing...[/dim]")

                    if credited >= minutes_target:
                        console.print("[green][OK] Edge Browsing Streak completed[/green]")
                    else:
                        console.print(
                            f"[yellow][WARN] Edge Streak: {credited}/{minutes_target} min "
                            f"after {max_attempts} attempts[/yellow]"
                        )


                await _persist_storage_state(ctx_edge, storage_state_path)
                await browser_mgr3.close()
            except Exception as e:
                console.print(f"[yellow][WARN] Edge session error: {e}[/yellow]")
                try:
                    await browser_mgr3.close()
                except Exception:
                    pass
            finally:
                session_proofs["ignore_edge_streak"] = not bool(edge_streak_info.get("exists", False))

            # ─── Log & Notify ────────────────────────────────────────
            # Create verification-only settings to prevent Native Edge from reopening loops
            verify_settings = dict(settings)
            verify_settings["native_edge_runtime_enabled"] = False
            verify_settings["bootstrap_attach_existing_edge"] = False

            final_search_status, final_points_info = await _refresh_account_summary(
                verify_settings,
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

            console.print("\n[bold][ Final Rewards Verification ][/bold]")
            try:
                verification = await _collect_final_verification(
                    verify_settings,
                    account,
                    session_proxy,
                    login_mgr,
                    searcher,
                    humanizer,
                    storage_state_path,
                )
                verification = _reconcile_verification_with_session_proof(
                    verification,
                    session_proofs,
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

                mobile_gap = max(
                    0,
                    int(final_search_status.get("mobile_max", 0))
                    - int(final_search_status.get("mobile_current", 0)),
                )
                should_retry_mobile = (
                    mobile_gap > 0
                    and int(settings.get("mobile_search_recovery_passes", 1)) > 0
                    and not bool(all_searches["mobile"].get("recovery_attempted", False))
                )

                if should_retry_mobile:
                    recovery_count = (mobile_gap + 2) // 3
                    logger.info(
                        f"Mobile credits still short after verification ({final_search_status.get('mobile_current', 0)}/{final_search_status.get('mobile_max', 0)}); running one bounded recovery pass."
                    )
                    emit_diagnostic_log(
                        logger,
                        settings,
                        "Starting bounded mobile recovery pass",
                        scope="mobile-recovery",
                        account=masked_email,
                        mobile_gap=mobile_gap,
                        recovery_count=recovery_count,
                        verification_status=summarize_search_status(final_search_status),
                    )
                    recovery_stats = await _run_mobile_search_pass(
                        settings,
                        account,
                        session_proxy,
                        login_mgr,
                        searcher,
                        storage_state_path,
                        count=recovery_count,
                        title="Mobile Search Recovery",
                    )
                    recovery_stats["recovery_attempted"] = True
                    all_searches["mobile"] = {
                        "completed": all_searches["mobile"].get("completed", 0)
                        + recovery_stats.get("completed", 0),
                        "total": all_searches["mobile"].get("total", 0)
                        + recovery_stats.get("total", 0),
                        "recovery_attempted": True,
                    }

                    final_search_status, final_points_info = await _refresh_account_summary(
                        verify_settings,
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

                    verification = await _collect_final_verification(
                        verify_settings,
                        account,
                        session_proxy,
                        login_mgr,
                        searcher,
                        humanizer,
                        storage_state_path,
                    )
                    verification = _reconcile_verification_with_session_proof(
                        verification,
                        session_proofs,
                    )
                    if verification.get("search_status"):
                        final_search_status = verification["search_status"]
                    remaining_items = _describe_remaining_items(verification)
                    mobile_gap = max(
                        0,
                        int(final_search_status.get("mobile_max", 0))
                        - int(final_search_status.get("mobile_current", 0)),
                    )
                    if mobile_gap > 0:
                        logger.warning(
                            f"Mobile recovery pass finished with remaining deficit: {final_search_status.get('mobile_current', 0)}/{final_search_status.get('mobile_max', 0)}"
                        )
                    emit_diagnostic_log(
                        logger,
                        settings,
                        "Completed bounded mobile recovery pass",
                        scope="mobile-recovery",
                        account=masked_email,
                        recovery_stats=recovery_stats,
                        verification_status=summarize_search_status(final_search_status),
                        mobile_gap=mobile_gap,
                    )

                remaining_items = _describe_remaining_items(verification)
                deferred_items = _describe_deferred_items(verification)
                emit_diagnostic_log(
                    logger,
                    settings,
                    "Final account verification evaluated",
                    scope="account",
                    account=masked_email,
                    verification_status=summarize_search_status(verification.get("search_status", {})),
                    remaining_items=remaining_items,
                    deferred_items=deferred_items,
                )
                account_complete = len(remaining_items) == 0
                if account_complete:
                    console.print("[green][OK] Rewards state fully verified[/green]")
                else:
                    overall_complete = False
                    console.print(
                        "[yellow]  Remaining items:[/yellow] "
                        + ", ".join(remaining_items[:8])
                    )
                if deferred_items:
                    console.print(
                        "[cyan]  Deferred offers:[/cyan] "
                        + ", ".join(deferred_items[:5])
                    )
            except Exception as e:
                account_complete = False
                overall_complete = False
                remaining_items = [f"Verification error: {e}"]
                emit_diagnostic_log(
                    logger,
                    settings,
                    "Final verification raised exception",
                    level="error",
                    scope="account",
                    account=masked_email,
                    error=str(e),
                )
                console.print(f"[yellow][WARN] Final verification error: {e}[/yellow]")

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
            emit_diagnostic_log(
                logger,
                settings,
                "Account summary recorded",
                scope="account",
                account=masked_email,
                total_points=total_points,
                earned_today=earned_today,
                streak=streak,
                searches=all_searches,
                daily=daily_stats,
                punch=punch_stats,
                promo=promo_stats,
                verified_complete=account_complete,
            )
            
            # Google Sheets Webhook
            webhook_url = settings.get("google_sheets_webhook_url", "")
            if settings.get("google_sheets_enabled", False) and webhook_url:
                try:
                    offers_total = punch_stats.get("completed", 0) + promo_stats.get("completed", 0)
                    from src.google_sheets import GoogleSheetsLogger
                    
                    GoogleSheetsLogger.log_account(
                        webhook_url=webhook_url,
                        email=email,
                        total_points=total_points,
                        earned_today=earned_today,
                        pc_search=final_search_status.get("pc_current", 0),
                        mobile_search=final_search_status.get("mobile_current", 0),
                        offers=offers_total
                    )
                except Exception as e:
                    console.print(f"[dim]Failed to log to Google Sheets: {e}[/dim]")
                    
            await browser_mgr.close()

        except Exception as e:
            overall_complete = False
            console.print(f"[bold red]Error: {e}[/bold red]")
            logger.error(f"Account {email} error: {e}")
            emit_diagnostic_log(
                logger,
                settings,
                "Account run raised exception",
                level="error",
                scope="account",
                account=masked_email,
                error=str(e),
            )
            notifier.send_error(email, str(e))
            try:
                await browser_mgr.close()
            except Exception:
                pass

        # Inter-account cooldown (reduce detection risk)
        if idx < len(accounts) - 1:
            cooldown = random.randint(30, 90)
            console.print(f"\n[dim]Waiting {cooldown}s before next account...[/dim]")
            await asyncio.sleep(cooldown)

    if overall_complete:
        console.print("\n[bold green][DONE] All tasks completed and verified![/bold green]")
    else:
        console.print(
            "\n[bold yellow] Run finished with remaining tasks. Review the warnings above.[/bold yellow]"
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
    title_icon = "OK" if verified_complete else "WARN"
    border = "cyan" if verified_complete else "yellow"
    table = Table(
        title=f"{title_icon} Summary for {email[:5]}***",
        box=box.DOUBLE_EDGE,
        border_style=border,
    )
    table.add_column("Task", style="bold")
    table.add_column("Result", style="green")

    table.add_row("Points", f"{points:,}")
    table.add_row("Streak", f"{streak} days")
    table.add_row("Status", "Verified" if verified_complete else "Incomplete")
    table.add_row("Desktop", f"{searches['desktop'].get('completed', 0)}/{searches['desktop'].get('total', 0)}")
    table.add_row("Mobile", f"{searches['mobile'].get('completed', 0)}/{searches['mobile'].get('total', 0)}")
    table.add_row("Edge", f"{searches['edge'].get('completed', 0)}/{searches['edge'].get('total', 0)}")
    table.add_row("Daily Set", f"{daily.get('completed', 0)}/{daily.get('total', 0)}")
    table.add_row("Punch Cards", f"{punch.get('completed', 0)}/{punch.get('total', 0)}")
    table.add_row("Promotions", f"{promo.get('completed', 0)}/{promo.get('total', 0)}")
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
        console.print(f"\n[bold cyan][ Searches for {email[:5]}*** ][/bold cyan]")

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
                status = await _read_search_status_with_mobile_recheck(
                    searcher,
                    page,
                    settings,
                )
                if mode == "mobile":
                    status = await _resolve_mobile_search_requirement(
                        settings,
                        account,
                        session_proxy,
                        login_mgr,
                        searcher,
                        storage_state_path,
                        status,
                    )
                cur, mx = _mode_credit(status, mode)

                if mx > 0 and cur >= mx:
                    console.print(f"  [green][SKIP] {mode} already full ({cur}/{mx})[/green]")
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
    console.print("\n[bold cyan][ Account Management ][/bold cyan]")
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

        console.print("[green][OK] Account added[/green]")

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
            console.print("[green][OK] Accounts migrated to encrypted storage[/green]")
        else:
            console.print("[yellow]No accounts.json found[/yellow]")


def manage_settings(settings):
    """Settings management."""
    console.print("\n[bold cyan][ Settings ][/bold cyan]")

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
            console.print("[green][OK] Setting updated[/green]")
        else:
            console.print(f"[red]Unknown setting: {key}[/red]")

    elif choice == "2":
        from src.utils import get_default_settings
        settings = get_default_settings()
        save_settings(settings)
        console.print("[green][OK] Settings reset to defaults[/green]")


def view_points(settings):
    """View points history and statistics."""
    tracker = PointsTracker(settings)
    stats = tracker.get_statistics()

    table = Table(title="Points Statistics", box=box.DOUBLE_EDGE, border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="cyan")

    table.add_row("Days Tracked", str(stats["days_tracked"]))
    table.add_row("Total Earned", f"{stats['total_earned']:,}")
    table.add_row("Daily Average", str(stats["avg_daily"]))
    table.add_row("Current Streak", f"{stats['streak']} days")
    table.add_row("Max Streak", f"{stats.get('max_streak', 0)} days")
    table.add_row("Est. Monthly", f"{stats.get('estimated_monthly', 0):,.0f}")

    console.print(table)

    if Confirm.ask("Generate graph?", default=True):
        path = tracker.generate_graph()
        if path:
            console.print(f"[green]Graph saved: {path}[/green]")


def setup_schedule(settings):
    """Setup auto-scheduling."""
    scheduler = Scheduler(settings)

    console.print("\n[bold cyan][ Auto Schedule Setup ][/bold cyan]")
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
        updated = apply_schedule_update(
            settings,
            ScheduleUpdate(enabled=True, time=time_str, create_task=True),
        )
        settings.update(updated)
        save_settings(settings)

        if scheduler.setup_windows_task(time_str):
            console.print(f"[green][OK] Scheduled daily at {time_str}[/green]")
        else:
            console.print("[red][ERR] Failed to create Windows Task[/red]")

    elif choice == "2":
        if scheduler.remove_windows_task():
            updated = apply_schedule_update(
                settings,
                ScheduleUpdate(
                    enabled=False,
                    time=str(settings.get("schedule_time", "08:00") or "08:00"),
                ),
            )
            settings.update(updated)
            save_settings(settings)
            console.print("[green][OK] Schedule removed[/green]")


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

    # Parse --flow argument
    flow_name = None
    if "--flow" in sys.argv:
        try:
            idx = sys.argv.index("--flow")
            flow_name = sys.argv[idx + 1]
            console.print(f"[cyan]Running flow: {flow_name}[/cyan]")
            settings = load_settings()
            password = prompt_master_password(settings["master_password_hash"])
            accounts = load_encrypted_accounts(password)
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
                login_mgr = LoginManager(Humanizer())
                if not await login_mgr.is_logged_in(page):
                    page = await login_mgr.login(page, acc["email"], acc["password"], acc.get("totp_secret"))
                pa_flow = PageAgentFlow(settings)
                await pa_flow.inject(page)
                result = await pa_flow.run_flow(page, flow_name)
                console.print(f"[green]Flow result for {acc['email']}: {result}[/green]")
                await bm.close()
            return
        except Exception as e:
            console.print(f"[red]Error running flow: {e}[/red]")
            return

    # ─── CLI Mode (explicit --cli flag) ────────────────────────
    if "--cli" in sys.argv:
        # Interactive menu
        while True:
            settings = load_settings()
            choice = show_menu()

            if choice == "0":
                console.print("[bold cyan]Goodbye![/bold cyan]")
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
    console.print(f"[bold cyan]Starting Web Dashboard on port {port}...[/bold cyan]")

    start_dashboard(port, host)
    url = f"http://{display_host}:{port}"
    console.print(f"[green]Dashboard: {url}[/green]")
    webbrowser.open(url)
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    # Use threading Event (avoids asyncio CancelledError on Windows)
    stop_event = _th.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    stop_event.wait()
    console.print("\n[bold cyan]Dashboard stopped. Goodbye![/bold cyan]")


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
    elif choice == "12":
        flow_name = Prompt.ask("Enter flow name (e.g., bing_daily_set)")
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
            pa_flow = PageAgentFlow(settings)
            await pa_flow.inject(page)
            result = await pa_flow.run_flow(page, flow_name)
            console.print(f"[green]Flow {flow_name} for {acc['email']}: {result}[/green]")
            await bm.close()


# ─── Graceful Shutdown ───────────────────────────────────────────────

_browser_managers: list = []  # Track active browser managers for cleanup


def _shutdown_handler(sig, frame):
    """Handle Ctrl+C / SIGTERM gracefully."""
    console.print("\n[bold yellow][WARN] Shutting down gracefully...[/bold yellow]")
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
