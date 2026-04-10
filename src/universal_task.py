"""
Universal Task Scanner & Executor for Microsoft Rewards.

Replaces task-specific modules with a single intelligent system:
1. Scans ALL tasks from Rewards API
2. Auto-classifies by type (visit, quiz, poll, time-gated, unknown)
3. Executes with appropriate handler
4. Tracks state for time-gated tasks
5. Falls back to AI Vision for complex/unknown tasks

This is the ONLY task handler needed — Daily Set, Punch Cards,
Quests, Promotions are ALL handled here.
"""

from __future__ import annotations
import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field, asdict
from urllib.parse import urlsplit, parse_qs

from playwright.async_api import Page, BrowserContext

from src.utils import (
    emit_diagnostic_log,
    logger,
    REWARDS_URL,
    close_other_tabs,
    select_active_daily_set_items,
)
from src.humanizer import Humanizer

# ─── Task State Persistence ────────────────────────────────────────────────

STATE_FILE = Path("data/task_state.json")
TASK_CACHE_WINDOW = timedelta(hours=12)
STRICT_COMPLETION_CATEGORIES = {"daily_set", "more_promo"}


def _load_state() -> dict:
    """Load task state from disk."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"Could not load task state: {e}")
    return {}


def _save_state(state: dict) -> None:
    """Save task state to disk."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug(f"Could not save task state: {e}")


def _requires_strict_completion(category: str) -> bool:
    """Return True when local optimistic cache/verification should be ignored."""
    return category in STRICT_COMPLETION_CATEGORIES


def _should_skip_task_via_cache(
    task,
    visited_at_str: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Allow verified-completion cache skips within the cache window."""
    now = now or datetime.now()
    try:
        visited_at = datetime.fromisoformat(visited_at_str)
    except ValueError:
        return False
    return now - visited_at < TASK_CACHE_WINDOW


def _should_cache_task_completion(task, verified_complete: bool) -> bool:
    """Only admit verified task completions into local cache."""
    return bool(verified_complete and getattr(task, "id", ""))


def _build_earn_card_cache_key(href: str = "", selector: str = "") -> str:
    """Build a stable cache key without collapsing distinct Bing search cards."""
    href = (href or "").strip()
    selector = (selector or "").strip()
    if not href:
        return selector

    try:
        parsed = urlsplit(href)
    except Exception:
        return href[:240]

    if "bing.com" in parsed.netloc.lower() and parsed.path.lower() == "/search":
        query = parse_qs(parsed.query).get("q", [""])[0].strip()
        if query:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?q={query}"[:240]

    return href[:240]


def _should_skip_earn_card_via_cache(
    cache_key: str,
    visited_cards: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Skip earn cards only when an exact recent cache entry exists."""
    if not cache_key:
        return False

    visited_at_str = visited_cards.get(cache_key)
    if not visited_at_str:
        return False

    now = now or datetime.now()
    try:
        visited_at = datetime.fromisoformat(visited_at_str)
    except ValueError:
        return False
    return now - visited_at < TASK_CACHE_WINDOW


def _should_cache_earn_card_visit(cache_key: str, proven_complete: bool) -> bool:
    """Only admit earn-card visits into cache when a concrete action succeeded."""
    return bool(cache_key and proven_complete)


def _normalized_offer_text_for_matching(*parts: str) -> str:
    """Normalize Rewards offer text for lightweight rule-based matching."""
    raw = " ".join(part for part in parts if part)
    lowered = raw.replace("\u200b", " ").replace("\xa0", " ").lower()
    return " ".join(lowered.split())


def get_deferred_offer_reason(task) -> str | None:
    """Return a reason when a task is visible but not realistically completable in-session."""
    task_category = getattr(task, "category", "")
    offer_text = _normalized_offer_text_for_matching(
        getattr(task, "title", ""),
        getattr(task, "description", ""),
    )

    if "streak bonus" in offer_text:
        return "streak_bonus_non_actionable"

    if task_category != "more_promo":
        return None

    destination_url = str(getattr(task, "destination_url", "") or "").strip().lower()
    offer_text = _normalized_offer_text_for_matching(
        getattr(task, "title", ""),
        getattr(task, "description", ""),
    )

    search_bar_offer = (
        "searchbar" in destination_url
        or any(token in offer_text for token in ("thanh tìm kiếm", "search bar", "search box"))
    )
    if search_bar_offer and any(
        token in offer_text
        for token in ("3 day", "3 days", "3 ngày", "three day")
    ):
        return "multi_day_search_bar"

    if any(
        token in offer_text
        for token in (
            "turn referrals into rewards",
            "referrals",
            "referral",
            "share an invite",
            "invite friends",
            "friends search on bing",
            "earn 7,500 points when friends search on bing",
        )
    ):
        return "external_referral"

    if "streak bonus" in offer_text:
        return "streak_bonus_non_actionable"

    if "upcoming events near me" in offer_text:
        return "location_dependent_offer"

    return None


# ─── Task Data Classes ─────────────────────────────────────────────────────

@dataclass
class RewardsTask:
    """Represents a single task from the Rewards API."""

    id: str = ""
    title: str = ""
    description: str = ""
    destination_url: str = ""
    category: str = ""           # "daily_set", "punch_card", "more_promo", "streak"
    task_type: str = "visit"     # "visit", "quiz", "poll", "search", "time_gated", "unknown"
    points: int = 0
    points_max: int = 0
    is_complete: bool = False
    is_locked: bool = False
    offer_id: str = ""
    # Punch card specific
    parent_id: str = ""          # Punch card parent ID
    child_index: int = 0         # Sub-task index within punch card
    # Raw API data for AI analysis
    raw_data: dict = field(default_factory=dict)


# ─── Universal Task Scanner ───────────────────────────────────────────────

class UniversalTaskScanner:
    """
    Scans and executes ALL Rewards tasks from a single entry point.

    Usage:
        scanner = UniversalTaskScanner(humanizer, ai_agent)
        result = await scanner.scan_and_complete(page, account_email)
    """

    def __init__(
        self,
        humanizer: Humanizer,
        ai_agent=None,
        on_log: Optional[Callable] = None,
        settings: Optional[dict] = None,
        challenge_handler=None,
    ):
        self.humanizer = humanizer
        self.ai_agent = ai_agent
        self.settings = settings or {}
        self.challenge_handler = challenge_handler
        self._active_account_email = ""
        self._daily_set_bulk_attempted_titles: set[str] = set()
        self.daily_set_execution_proofs: dict[str, dict] = {}
        self._session_completed_categories: set[str] = set()
        self._session_daily_set_titles: set[str] = set()
        self.daily_set_execution_proofs: dict[str, dict] = {}
        self._log = on_log or (lambda level, msg: logger.info(msg))
        self._macro_player = None
        try:
            from src.macro_player import MacroPlayer
            self._macro_player = MacroPlayer()
        except Exception as e:
            logger.debug(f"MacroPlayer init skipped: {e}")
        # Page-agent (browser-side LLM automation) — lazy init
        self._page_agent = None
        if self.settings.get("page_agent_enabled", False):
            try:
                from src.page_agent_flow import PageAgentFlow
                self._page_agent = PageAgentFlow(self.settings)
            except Exception as e:
                logger.debug(f"PageAgentFlow init skipped: {e}")

    def _diag(self, message: str, *, level: str = "info", scope: str = "tasks", **fields) -> None:
        """Emit diagnostic logs for complex task-routing and verification decisions."""
        emit_diagnostic_log(
            self._log,
            self.settings,
            message,
            level=level,
            scope=scope,
            **fields,
        )

    @staticmethod
    def _task_diag_payload(task: RewardsTask) -> dict:
        """Return a compact diagnostic payload for one Rewards task."""
        return {
            "id": task.id,
            "title": task.title[:80],
            "category": task.category,
            "task_type": task.task_type,
            "is_complete": task.is_complete,
            "is_locked": task.is_locked,
            "points": task.points,
            "points_max": task.points_max,
            "destination_url": task.destination_url[:160],
        }

    # ─── Main Entry Point ──────────────────────────────────────────────

    async def scan_and_complete(
        self,
        page: Page,
        account_email: str = "",
        skip_categories: Optional[list] = None,
    ) -> dict:
        """
        Scan ALL tasks and complete what's possible.

        Returns:
            {
                "total": int,
                "completed": int,
                "deferred": int,
                "skipped_locked": int,
                "skipped_done": int,
                "failed": int,
                "tasks": [...]
            }
        """
        skip_categories = skip_categories or []
        stats = {
            "total": 0, "completed": 0,
            "deferred": 0,
            "skipped_locked": 0, "skipped_done": 0,
            "failed": 0, "tasks": [],
            "by_category": {},
            "session_proofs": {},
        }
        self._active_account_email = account_email
        self._daily_set_bulk_attempted_titles.clear()
        self.daily_set_execution_proofs.clear()
        self._session_completed_categories.clear()
        self._session_daily_set_titles.clear()
        self.daily_set_execution_proofs.clear()

        # 1. Fetch all tasks from API
        self._log("info", "🔍 Scanning all Rewards tasks...")
        await self._ensure_no_manual_challenge(page, "Rewards task scan")
        tasks = await self._fetch_all_tasks(page)
        stats["total"] = len(tasks)
        self._log("info", f"Found {len(tasks)} total tasks")
        self._diag(
            "Fetched Rewards task inventory",
            scope="task-scan",
            account=account_email,
            total=len(tasks),
            categories={
                category: sum(1 for task in tasks if task.category == category)
                for category in sorted({task.category for task in tasks})
            },
        )
        self._session_daily_set_titles.update(
            (task.title or "").strip()
            for task in tasks
            if task.category == "daily_set" and (task.title or "").strip()
        )

        # 1b. DOM Cross-Validation — override stale API completion status
        dom_completed_ids = await self._dom_verify_task_status(page, tasks)
        if dom_completed_ids:
            self._log("info", f"👁️ DOM detected {len(dom_completed_ids)} additional completed tasks")
            self._diag(
                "DOM cross-check found additional completed task ids",
                scope="task-scan",
                dom_completed_ids=sorted(dom_completed_ids),
            )
            for task in tasks:
                if not task.is_complete and task.id in dom_completed_ids:
                    task.is_complete = True

        live_category_proofs = await self._apply_live_category_completion_proofs(page, tasks)
        if live_category_proofs:
            self._diag(
                "Applied live category completion proofs before filtering",
                scope="task-scan",
                proofs=live_category_proofs,
            )

        # 2. Load state for time-gated task tracking
        state = _load_state()
        account_state = state.get(account_email, {})

        # 3. Filter and sort tasks
        actionable = []
        for task in tasks:
            category_stats = stats["by_category"].setdefault(
                task.category,
                {
                    "total": 0,
                    "completed": 0,
                    "deferred": 0,
                    "skipped_done": 0,
                    "skipped_locked": 0,
                    "failed": 0,
                },
            )
            category_stats["total"] += 1

            if task.category in skip_categories:
                self._diag(
                    "Skipping task because category is excluded",
                    scope="task-filter",
                    **self._task_diag_payload(task),
                )
                continue

            if task.is_complete:
                stats["skipped_done"] += 1
                category_stats["skipped_done"] += 1
                continue

            if task.is_locked:
                stats["skipped_locked"] += 1
                category_stats["skipped_locked"] += 1
                self._log("info", f"🔒 Locked: {task.title[:40]} (time-gated)")
                self._diag(
                    "Task is locked/time-gated",
                    scope="task-filter",
                    **self._task_diag_payload(task),
                )
                # Track for next run
                account_state.setdefault("locked_tasks", {})[task.id] = {
                    "title": task.title,
                    "locked_since": datetime.now().isoformat(),
                }
                continue

            # Local Memory Cache check (skip tasks recently visited to outpace Microsoft API delay)
            visited_tasks = account_state.setdefault("visited_tasks", {})
            if task.id in visited_tasks:
                visited_at_str = visited_tasks[task.id]
                if _should_skip_task_via_cache(task, visited_at_str):
                    stats["skipped_done"] += 1
                    category_stats["skipped_done"] += 1
                    self._log("info", f"✅ Local Cache: Skipping recently completed task: {task.title[:30]}")
                    self._diag(
                        "Skipping task due to recent cache hit",
                        scope="task-filter",
                        visited_at=visited_at_str,
                        **self._task_diag_payload(task),
                    )
                    continue

            deferred_reason = get_deferred_offer_reason(task)
            if deferred_reason:
                stats["deferred"] += 1
                category_stats["deferred"] += 1
                stats["tasks"].append({
                    "title": task.title[:40],
                    "status": "deferred",
                    "type": task.task_type,
                    "reason": deferred_reason,
                })
                self._log(
                    "info",
                    f"⏭️ Deferred offer: {task.title[:40]} "
                    f"({deferred_reason.replace('_', ' ')})",
                )
                self._diag(
                    "Deferred keep-earning offer after classification",
                    scope="task-filter",
                    deferred_reason=deferred_reason,
                    **self._task_diag_payload(task),
                )
                continue

            actionable.append(task)
            self._diag(
                "Task remains actionable after filtering",
                scope="task-filter",
                **self._task_diag_payload(task),
            )

        # Save updated state
        state[account_email] = account_state
        _save_state(state)

        # 4. Prefer a stable order in safe mode so one run finishes faster.
        if self.settings.get("safe_mode", True):
            actionable.sort(key=self._task_sort_key)
        else:
            random.shuffle(actionable)

        self._log("info",
                   f"📋 {len(actionable)} tasks to complete "
                   f"({stats['skipped_done']} done, {stats['skipped_locked']} locked)")
        self._diag(
            "Prepared actionable task queue",
            scope="task-scan",
            actionable_count=len(actionable),
            stats=stats["by_category"],
        )

        # 5. Execute tasks
        failed_tasks = []
        for i, task in enumerate(actionable):
            try:
                if task.category in self._session_completed_categories:
                    stats["skipped_done"] += 1
                    stats["by_category"].setdefault(task.category, {}).setdefault("skipped_done", 0)
                    stats["by_category"][task.category]["skipped_done"] += 1
                    self._log(
                        "info",
                        f"[{i + 1}/{len(actionable)}] {task.category}: session proof already satisfied",
                    )
                    continue

                await self._ensure_no_manual_challenge(page, task.title[:40] or "Rewards task")
                self._log("info",
                           f"[{i + 1}/{len(actionable)}] {task.category}: "
                           f"{task.title[:40]}... ({task.task_type})")
                self._diag(
                    "Starting task execution",
                    scope="task-run",
                    queue_index=i + 1,
                    queue_total=len(actionable),
                    **self._task_diag_payload(task),
                )

                success = await self._execute_task(page, task)
                if success:
                    success = await self._verify_task_completion(page, task)

                if success:
                    stats["completed"] += 1
                    stats["by_category"].setdefault(task.category, {}).setdefault("completed", 0)
                    stats["by_category"][task.category]["completed"] += 1
                    stats["tasks"].append({
                        "title": task.title[:40],
                        "status": "completed",
                        "type": task.task_type,
                    })

                    if _should_cache_task_completion(task, success):
                        # Register completed task into memory cache to outsmart lagging MS APIs
                        state = _load_state()
                        account_state = state.setdefault(account_email, {})
                        account_state.setdefault("visited_tasks", {})[task.id] = datetime.now().isoformat()
                        _save_state(state)
                    self._diag(
                        "Task completed and verified",
                        scope="task-run",
                        **self._task_diag_payload(task),
                    )
                else:
                    stats["failed"] += 1
                    stats["by_category"].setdefault(task.category, {}).setdefault("failed", 0)
                    stats["by_category"][task.category]["failed"] += 1
                    failed_tasks.append(task)
                    stats["tasks"].append({
                        "title": task.title[:40],
                        "status": "failed",
                        "type": task.task_type,
                    })
                    self._diag(
                        "Task failed verification or execution",
                        scope="task-run",
                        **self._task_diag_payload(task),
                    )

                # Clean up tabs
                try:
                    await close_other_tabs(page)
                except Exception as _e:
                    logger.debug(f"Tab cleanup suppressed: {_e}")

                await self.humanizer.short_delay()

            except Exception as e:
                logger.warning(f"Task failed: {task.title[:30]}: {e}")
                self._diag(
                    "Task raised exception during execution",
                    level="error",
                    scope="task-run",
                    error=str(e),
                    **self._task_diag_payload(task),
                )
                stats["failed"] += 1
                failed_tasks.append(task)
                stats["tasks"].append({
                    "title": task.title[:40],
                    "status": f"error: {e}",
                })
                # Capture error screenshot for debugging
                try:
                    ss_dir = Path("data/error_screenshots")
                    ss_dir.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ss_path = ss_dir / f"task_error_{ts}.png"
                    await page.screenshot(path=str(ss_path))
                    logger.debug(f"Error screenshot saved: {ss_path}")
                except Exception as _e:
                    logger.debug(f"Error screenshot suppressed: {_e}")
                # Recovery
                try:
                    await close_other_tabs(page)
                    await page.goto(REWARDS_URL,
                                    wait_until="domcontentloaded", timeout=35000)
                    await asyncio.sleep(2)
                except Exception as _e:
                    logger.debug(f"Recovery navigation suppressed: {_e}")

        # 6. Auto-retry: re-scan API to find still-incomplete tasks
        retry_limit = int(self.settings.get("session_task_retry_limit", 3))
        
        for retry_round in range(retry_limit):
            if not failed_tasks:
                break
                
            self._log("info", f"🔄 Auto-retry (Round {retry_round + 1}/{retry_limit}): {len(failed_tasks)} failed tasks...")
            self._diag(
                "Starting automatic retry round",
                scope="task-retry",
                round=retry_round + 1,
                retry_limit=retry_limit,
                failed_task_ids=[task.id for task in failed_tasks],
            )
            await asyncio.sleep(5)
            retry_tasks = await self._fetch_all_tasks(page)
            
            still_failed = []
            retried = 0
            
            for rt in retry_tasks:
                if rt.is_complete or rt.is_locked:
                    continue
                if rt.category in self._session_completed_categories:
                    continue
                if get_deferred_offer_reason(rt):
                    continue
                # Only retry tasks that failed before
                if not any(ft.id == rt.id for ft in failed_tasks):
                    continue
                    
                try:
                    self._log("info", f"  🔁 Retry: {rt.title[:40]}")
                    self._diag(
                        "Retrying previously failed task",
                        scope="task-retry",
                        **self._task_diag_payload(rt),
                    )
                    success = await self._execute_task(page, rt)
                    if success:
                        success = await self._verify_task_completion(page, rt)
                    
                    if success:
                        retried += 1
                        stats["completed"] += 1
                        stats["failed"] -= 1
                        stats["by_category"].setdefault(rt.category, {}).setdefault("completed", 0)
                        stats["by_category"][rt.category]["completed"] += 1
                        
                        if stats["by_category"].setdefault(rt.category, {}).setdefault("failed", 0) > 0:
                            stats["by_category"][rt.category]["failed"] -= 1
                    else:
                        still_failed.append(rt)
                        
                    await close_other_tabs(page)
                except Exception:
                    still_failed.append(rt)
                    
            if retried > 0:
                self._log("info", f"  ✅ Retried successfully this round: {retried}")
                self._diag(
                    "Retry round produced recovered tasks",
                    scope="task-retry",
                    recovered=retried,
                    still_failed=[task.id for task in still_failed],
                )
                
            failed_tasks = still_failed

        # ── Quests (AI-driven multi-step cards) ──
        if "quests" not in skip_categories and self.ai_agent and self.ai_agent.enabled:
            try:
                self._log("info", "🗺️ AI Smart Scan – checking /earn page for Quests...")
                quest_res = await self.ai_agent.complete_quests(page)
                if quest_res and quest_res.get("success"):
                    stats["completed"] += 1
            except Exception as e:
                self._log("debug", f"Quests scan softly failed: {e}")

        # ── Explore on Bing (DOM-scraped from /earn page) ──
        if "explore" not in skip_categories:
            try:
                explore_done = await self._scan_explore_on_bing(page)
                stats["completed"] += explore_done
            except Exception as e:
                self._log("info", f"Explore on Bing scan skipped: {e}")

        self._log("info",
                   f"✅ Tasks: {stats['completed']}/{stats['total']} completed, "
                   f"{stats['skipped_locked']} locked, {stats['failed']} failed")
        stats["session_proofs"] = {
            "daily_set_complete": "daily_set" in self._session_completed_categories,
            "daily_set_titles": sorted(self._session_daily_set_titles),
        }
        self._diag(
            "Task scan completed",
            scope="task-scan",
            totals={
                "total": stats["total"],
                "completed": stats["completed"],
                "deferred": stats["deferred"],
                "skipped_done": stats["skipped_done"],
                "skipped_locked": stats["skipped_locked"],
                "failed": stats["failed"],
            },
            session_proofs=stats.get("session_proofs", {}),
        )

        return stats

    @staticmethod
    def _task_sort_key(task: RewardsTask) -> tuple[int, int, str]:
        """Prioritize simpler, immediately-available tasks first."""
        category_order = {
            "streak": 0,      # Edge Streak — priority #1 per spec
            "daily_set": 1,   # Daily Set Streak — priority #2
            "punch_card": 2,  # Punch Cards / Quests — priority #3
            "more_promo": 3,  # Promotions — priority #4
        }
        type_order = {
            "visit": 0,
            "poll": 1,
            "quiz": 2,
            "search": 3,
            "unknown": 4,
        }
        return (
            category_order.get(task.category, 99),
            type_order.get(task.task_type, 99),
            task.title.lower(),
        )

    async def _ensure_no_manual_challenge(self, page: Page, context: str) -> None:
        """Pause for manual verification when a challenge is visible."""
        if not self.challenge_handler:
            return

        resolved = await self.challenge_handler.handle_if_present(
            page,
            account=self._active_account_email,
            context=context,
        )
        if not resolved:
            raise RuntimeError("Manual verification challenge not resolved")

    async def _verify_task_completion(self, page: Page, task: RewardsTask) -> bool:
        """Hybrid 3-layer verification: Memory Cache → DOM visual → API."""
        strict_completion = _requires_strict_completion(task.category)
        offer_text = self._normalized_offer_text(task)
        self._diag(
            "Beginning task verification",
            scope="task-verify",
            strict_completion=strict_completion,
            **self._task_diag_payload(task),
        )

        if task.category == "daily_set":
            execution_proof = self._get_daily_set_execution_proof(task)
            if execution_proof:
                proof_state = execution_proof.get("state", "")
                if proof_state == "category_proven":
                    self._session_completed_categories.add("daily_set")
                    if (task.title or "").strip():
                        self._session_daily_set_titles.add(task.title.strip())
                    for proof_title in execution_proof.get("proof_titles", []):
                        if (proof_title or "").strip():
                            self._session_daily_set_titles.add(proof_title.strip())
                    self._diag(
                        "Daily Set verification satisfied from execution proof carrier",
                        scope="task-verify",
                        proof=execution_proof,
                        **self._task_diag_payload(task),
                    )
                    return True
                if proof_state == "target_proven":
                    self._diag(
                        "Daily Set verification satisfied from task-level execution proof",
                        scope="task-verify",
                        proof=execution_proof,
                        **self._task_diag_payload(task),
                    )
                    return True

        # Layer 1: URL/visit tasks — optimistic pass (they always count)
        if task.task_type in ("urlreward", "visit") and not strict_completion:
            self._log("info", f"  ✅ Optimistically completed URL task (skipping strict API verify)")
            self._diag(
                "Verification short-circuited by optimistic URL policy",
                scope="task-verify",
                **self._task_diag_payload(task),
            )
            return True

        # Layer 2: DOM visual cues — check if the page shows completion
        try:
            dom_done = await self._dom_check_single_task_done(page, task)
            if dom_done:
                self._log("info", f"  ✅ DOM visual confirms task completed")
                self._diag(
                    "Verification passed from DOM cues",
                    scope="task-verify",
                    **self._task_diag_payload(task),
                )
                return True
        except Exception as _e:
            logger.debug(f"DOM visual check suppressed: {_e}")

        # Layer 2b: some Rewards cards only render completion cues on /earn or /dashboard.
        try:
            dom_done_elsewhere = await self._dom_check_task_done_across_rewards_pages(page, task)
            if dom_done_elsewhere:
                self._log("info", f"  ✅ Rewards page DOM confirms task completed")
                self._diag(
                    "Verification passed from candidate Rewards page DOM cues",
                    scope="task-verify",
                    **self._task_diag_payload(task),
                )
                return True
        except Exception as _e:
            logger.debug(f"Cross-page DOM visual check suppressed: {_e}")

        if task.category == "daily_set":
            daily_set_proof = self._get_daily_set_execution_proof(task)
            if daily_set_proof:
                proof_state = daily_set_proof.get("state", "")
                self._diag(
                    "Verification consulted Daily Set execution proof before API polling",
                    scope="task-verify",
                    proof_state=proof_state,
                    proof_source=daily_set_proof.get("source", ""),
                    **self._task_diag_payload(task),
                )
                if proof_state in {"target_proven", "category_proven"}:
                    if proof_state == "category_proven":
                        self._session_completed_categories.add("daily_set")
                    return True

        # Layer 3: API verification (slowest, may lag)
        initial_wait = 5 if task.category == "punch_card" else 2
        max_attempts = 2 if task.category == "punch_card" else 1
        if strict_completion:
            initial_wait = max(initial_wait, 3)
            max_attempts = max(max_attempts, 2)
        if task.category == "more_promo":
            initial_wait = max(initial_wait, 7)
            max_attempts = max(max_attempts, 4)
            if any(
                token in offer_text
                for token in (
                    "turn referrals into rewards",
                    "referrals",
                    "referral",
                    "invite friends",
                    "thanh tìm kiếm",
                    "search box",
                    "search bar",
                    "search with bing",
                )
            ):
                initial_wait = max(initial_wait, 8)

        for attempt in range(max_attempts):
            try:
                await asyncio.sleep(initial_wait if attempt == 0 else 5)
                refreshed_tasks = await self._fetch_all_tasks(page)
                self._diag(
                    "Fetched fresh task inventory for verification attempt",
                    scope="task-verify",
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    inventory_size=len(refreshed_tasks),
                    **self._task_diag_payload(task),
                )
                refreshed = next(
                    (
                        candidate for candidate in refreshed_tasks
                        if candidate.id == task.id
                        and candidate.category == task.category
                        and candidate.parent_id == task.parent_id
                        and candidate.child_index == task.child_index
                    ),
                    None,
                )

                if refreshed is None:
                    if strict_completion:
                        self._log(
                            "info",
                            f"  ⚠️ Task disappeared before strict verification: {task.title[:40]}",
                        )
                        self._diag(
                            "Strict verification failed because task disappeared",
                            scope="task-verify",
                            attempt=attempt + 1,
                            **self._task_diag_payload(task),
                        )
                        return False
                    self._diag(
                        "Non-strict verification tolerated missing task after action",
                        scope="task-verify",
                        attempt=attempt + 1,
                        **self._task_diag_payload(task),
                    )
                    return True

                if refreshed.is_complete:
                    self._diag(
                        "Verification passed via refreshed API state",
                        scope="task-verify",
                        attempt=attempt + 1,
                        refreshed=self._task_diag_payload(refreshed),
                    )
                    return True

                if attempt < max_attempts - 1:
                    self._log(
                        "info",
                        f"  ⏳ Waiting for server to register completion... (retry {attempt + 1})",
                    )
                    self._diag(
                        "Verification still pending after refreshed API state",
                        scope="task-verify",
                        attempt=attempt + 1,
                        refreshed=self._task_diag_payload(refreshed),
                    )
                    continue

                self._log(
                    "info",
                    f"  ⚠️ Task did not verify as complete: {task.title[:40]}",
                )
                self._diag(
                    "Verification exhausted all retries without completion",
                    scope="task-verify",
                    refreshed=self._task_diag_payload(refreshed),
                    **self._task_diag_payload(task),
                )
                return False
            except Exception as e:
                logger.warning(f"Task verification failed for {task.title[:30]}: {e}")
                self._diag(
                    "Verification raised exception",
                    level="error",
                    scope="task-verify",
                    error=str(e),
                    **self._task_diag_payload(task),
                )
                return False
        return False

    async def _dom_check_task_done_across_rewards_pages(self, page: Page, task: RewardsTask) -> bool:
        """Check candidate Rewards surfaces because strict tasks do not always render on the current page."""
        checked_urls = {page.url}
        for rewards_url in self._candidate_rewards_pages(task):
            if not rewards_url or rewards_url in checked_urls:
                continue
            checked_urls.add(rewards_url)
            try:
                await page.goto(
                    rewards_url,
                    wait_until="domcontentloaded",
                    timeout=35000,
                )
                await asyncio.sleep(2)
                if task.category == "daily_set" and "/earn" not in rewards_url:
                    await self._open_daily_set_panel(page)
                    await asyncio.sleep(1)
                if await self._dom_check_single_task_done(page, task):
                    return True
            except Exception:
                continue
        return False

    async def _dom_check_single_task_done(self, page: Page, task: RewardsTask) -> bool:
        """Check if a single task shows visual completion cues on the current page."""
        title_snippet = (task.title or "").strip()
        if not title_snippet:
            return False
        description_snippet = (task.description or "").strip()
        token_list = self._task_title_tokens(task)
        try:
            return await page.evaluate("""
                ({ titleSnippet, descriptionSnippet, tokenList }) => {
                    const normalize = s => (s || '').replace(/[\u200b\u00a0]/g, ' ').trim().toLowerCase();
                    const target = normalize(titleSnippet);
                    const description = normalize(descriptionSnippet);
                    const tokens = Array.isArray(tokenList) ? tokenList.map(normalize).filter(Boolean) : [];
                    if (!target && !tokens.length) return false;

                    const cards = document.querySelectorAll(
                        'mee-card, [class*="card"], [data-bi-area], [class*="earn"]'
                    );

                    let bestCard = null;
                    let bestScore = 0;
                    for (const card of cards) {
                        const text = normalize(card.innerText || card.textContent || '');
                        if (!text) continue;

                        let score = 0;
                        if (target && text.includes(target)) {
                            score += 1000 + target.length;
                        }
                        if (description && text.includes(description)) {
                            score += 600 + description.length;
                        }
                        let tokenHits = 0;
                        for (const token of tokens) {
                            if (token && text.includes(token)) {
                                tokenHits += 1;
                            }
                        }
                        score += tokenHits * 80;
                        if (!score || score <= bestScore) {
                            continue;
                        }
                        bestCard = card;
                        bestScore = score;
                    }

                    if (!bestCard) {
                        return false;
                    }

                    const bestText = normalize(bestCard.innerText || bestCard.textContent || '');
                    if (bestText.includes('completed') || bestText.includes('✓') || bestText.includes('✔')) {
                        return true;
                    }

                    const icons = bestCard.querySelectorAll(
                        'svg, [class*="check"], [class*="complete"], [class*="done"], [aria-label*="complete"]'
                    );
                    if (icons.length > 0) {
                        for (const ic of icons) {
                            const cl = (ic.className || '').toLowerCase();
                            const al = (ic.getAttribute('aria-label') || '').toLowerCase();
                            if (cl.includes('check') || cl.includes('complete') || cl.includes('done')
                                || al.includes('check') || al.includes('complete')) {
                                return true;
                            }
                        }
                    }

                    const style = window.getComputedStyle(bestCard);
                    if (parseFloat(style.opacity) < 0.6) {
                        return true;
                    }
                    return false;
                }
            """, {
                "titleSnippet": title_snippet,
                "descriptionSnippet": description_snippet,
                "tokenList": token_list,
            })
        except Exception:
            return False

    async def _apply_live_category_completion_proofs(self, page: Page, tasks: list[RewardsTask]) -> dict[str, dict]:
        """Use live Rewards overview to short-circuit categories already complete on-page."""
        proofs: dict[str, dict] = {}
        needs_daily_set_proof = any(
            task.category == "daily_set" and not task.is_complete
            for task in tasks
        )
        if not needs_daily_set_proof:
            return proofs

        try:
            from src.streaks import TaskDetector

            overview = await TaskDetector().get_all_tasks(page)
        except Exception as e:
            logger.debug(f"Live category proof check failed: {e}")
            return proofs

        daily_set = overview.get("daily_set", {})
        daily_completed = int(daily_set.get("completed", 0) or 0)
        daily_total = int(daily_set.get("total", 0) or 0)
        if daily_total <= 0 or daily_completed < daily_total:
            return proofs

        for task in tasks:
            if task.category == "daily_set":
                task.is_complete = True
        self._session_completed_categories.add("daily_set")
        proofs["daily_set"] = {
            "completed": daily_completed,
            "total": daily_total,
            "source": "live_rewards_overview",
        }
        self._log(
            "info",
            f"👁️ Live Rewards overview confirms Daily Set already complete ({daily_completed}/{daily_total})",
        )
        return proofs

    async def _dom_verify_task_status(self, page: Page, tasks: list) -> set:
        """Cross-validate API tasks against DOM visual cues on the earn page.

        Returns a set of task IDs that DOM shows as completed but API says incomplete.
        """
        completed_ids = set()
        if not tasks:
            return completed_ids

        try:
            # Navigate to /earn to get the full rendered card layout
            if "rewards.bing.com/" not in page.url:
                await page.goto(
                    "https://rewards.bing.com/",
                    wait_until="domcontentloaded",
                    timeout=35000,
                )
                await asyncio.sleep(3)

            # Scrape all visible card completion status from the DOM
            dom_cards = await page.evaluate("""
                () => {
                    const normalize = s => (s || '').replace(/[\u200b\u00a0]/g, ' ').trim().toLowerCase();
                    const results = [];

                    const cards = document.querySelectorAll(
                        'mee-card, [class*="card"], [data-bi-area], [class*="earn"], [class*="promo"]'
                    );

                    for (const card of cards) {
                        const rect = card.getBoundingClientRect();
                        if (rect.width < 10 || rect.height < 10) continue;

                        const text = normalize(card.innerText || card.textContent || '');
                        if (!text || text.length < 5) continue;

                        let isCompleted = false;

                        // Check text cues
                        if (text.includes('completed') || text.includes('✓') || text.includes('✔')) {
                            isCompleted = true;
                        }

                        // Check for checkmark elements  
                        if (!isCompleted) {
                            const icons = card.querySelectorAll(
                                '[class*="check"], [class*="complete"], [class*="done"], '
                                + '[aria-label*="complete"], [aria-label*="done"]'
                            );
                            if (icons.length > 0) isCompleted = true;
                        }

                        // Check opacity
                        if (!isCompleted) {
                            const style = window.getComputedStyle(card);
                            if (parseFloat(style.opacity) < 0.6) isCompleted = true;
                        }

                        results.push({ text: text.substring(0, 200), completed: isCompleted });
                    }
                    return results;
                }
            """)

            if not dom_cards:
                return completed_ids

            # Match DOM cards to API tasks by title fuzzy match
            completed_dom_texts = [
                c["text"] for c in dom_cards if c.get("completed")
            ]

            for task in tasks:
                if task.is_complete:
                    continue
                task_title_lower = (task.title or "").lower().strip()[:40]
                if not task_title_lower or len(task_title_lower) < 5:
                    continue

                for dom_text in completed_dom_texts:
                    if task_title_lower in dom_text:
                        completed_ids.add(task.id)
                        break

        except Exception as e:
            logger.debug(f"DOM cross-validation failed: {e}")

        return completed_ids

    # ─── API Data Fetch ────────────────────────────────────────────────

    async def _fetch_all_tasks(self, page: Page) -> list[RewardsTask]:
        """Fetch all tasks from Rewards API and classify them."""
        tasks: list[RewardsTask] = []

        try:
            # Navigate to rewards page first
            if "rewards.bing.com" not in page.url:
                await page.goto(REWARDS_URL,
                                wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(3)

            # Fetch tasks via the pure DOM visual scraper
            from src.dashboard_scraper import scan_dashboard_dom
            dom_tasks = await scan_dashboard_dom(page)
            
            for dt in dom_tasks:
                task = RewardsTask()
                # We use the title as ID since it's the only unique identifier we extract from DOM
                task.id = dt["title"] 
                task.title = dt["title"]
                task.description = dt["description"]
                task.points = 0
                task.points_max = dt["points"]
                task.destination_url = dt["url"]
                task.is_complete = False # Dashboard DOM only yields incomplete tasks
                task.category = dt["category"] # "unknown" typically
                task.task_type = "quiz" if dt["is_quiz"] else "unknown"
                task.offer_id = "" # Deprecated via DOM layer
                
                # We stash the DOM index here so we can natively click it later without CSS selectors
                task.raw_data = {"element_index": dt["element_index"]}
                
                tasks.append(task)
                
        except Exception as e:
            logger.error(f"Failed to fetch visual tasks from DOM: {e}")

        return tasks

    def _parse_task(self, data: dict, category: str) -> RewardsTask:
        """Parse raw API data into a RewardsTask."""
        task = RewardsTask()
        task.id = data.get("offerId", "") or data.get("name", "")
        task.title = data.get("title", "") or data.get("name", "")
        task.description = data.get("description", "")
        task.destination_url = data.get("destinationUrl", "")
        task.category = category
        task.points = data.get("pointProgress", 0)
        task.points_max = data.get("pointProgressMax", 0)
        task.offer_id = data.get("offerId", "")
        task.raw_data = data

        # Completion status
        task.is_complete = (
            data.get("complete", False)
            or (task.points >= task.points_max and task.points_max > 0)
        )

        # Locked status (time-gated punch card tasks)
        task.is_locked = data.get("isLocked", False)

        # Auto-classify task type
        task.task_type = self._classify_task(data)

        # Make URL absolute
        if task.destination_url and task.destination_url.startswith("/"):
            task.destination_url = f"https://rewards.bing.com{task.destination_url}"

        return task

    def _classify_task(self, data: dict) -> str:
        """Auto-classify task type from API attributes."""
        attributes = data.get("attributes", {})
        dest = data.get("destinationUrl", "")
        title = (data.get("title", "") or "").lower()
        desc = (data.get("description", "") or "").lower()

        # Type from attributes
        attr_type = attributes.get("type", "")
        if attr_type:
            if "quiz" in attr_type.lower():
                return "quiz"
            if "poll" in attr_type.lower():
                return "poll"
            if "survey" in attr_type.lower():
                return "poll"
            if "search" in attr_type.lower():
                return "search"
            if "urlreward" in attr_type.lower():
                return "visit"

        # Classify by title/description keywords
        quiz_keywords = ["quiz", "test your knowledge", "trivia",
                         "do you know", "challenge yourself", "puzzle"]
        poll_keywords = ["poll", "vote", "which do you", "opinion"]

        for kw in quiz_keywords:
            if kw in title or kw in desc:
                return "quiz"

        for kw in poll_keywords:
            if kw in title or kw in desc:
                return "poll"

        # If it has a destination URL, it's probably a visit task
        if dest:
            return "visit"

        return "unknown"

    async def _execute_task(self, page: Page, task: RewardsTask) -> bool:
        """Execute a task by clicking it on the Rewards page DOM.

        MS Rewards requires the click to originate FROM the rewards page
        to register the activity as completed. Navigating to destinationUrl
        directly does NOT count.
        """
        try:
            daily_set_fallback_allowed = False
            clicked = False
            pages_before = len(page.context.pages)
            await self._ensure_no_manual_challenge(page, task.title[:40] or "Rewards task")

            if task.category == "daily_set":
                from src.daily_set import DailySetCompleter

                self._log("info", "  🎯 Trying Daily Set executor first")
                daily_set = DailySetCompleter(
                    self.humanizer,
                    settings=self.settings,
                    ai_agent=self.ai_agent,
                )
                daily_stats = await daily_set.complete_daily_set(
                    page,
                    expected_title=task.title,
                )
                proof = self._store_daily_set_execution_proof(
                    task,
                    daily_stats,
                    source="daily_set_completer",
                )
                if proof.get("state") == "category_proven":
                    self._session_completed_categories.add("daily_set")
                    if (task.title or "").strip():
                        self._session_daily_set_titles.add(task.title.strip())
                    for proof_title in proof.get("proof_titles", []):
                        if (proof_title or "").strip():
                            self._session_daily_set_titles.add(proof_title.strip())
                if proof.get("state") in {"target_proven", "category_proven"}:
                    try:
                        await page.goto(
                            REWARDS_URL,
                            wait_until="domcontentloaded",
                            timeout=35000,
                        )
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    return True
                if proof.get("state") in {"attempted_only", "panel_control_failed"}:
                    self._log("info", f"  ↪️ Daily Set executor yielded {proof.get('state')}, allowing bounded generic fallback")
                else:
                    self._log("info", "  ⚠️ Daily Set executor did not establish proof")
                    return False

            # 1. Search the page(s) where the current Rewards UI renders this task.
            for rewards_url in self._candidate_rewards_pages(task):
                if page.url != rewards_url:
                    try:
                        await page.goto(
                            rewards_url,
                            wait_until="domcontentloaded",
                            timeout=35000,
                        )
                        await asyncio.sleep(3)
                    except Exception:
                        continue

                clicked = await self._click_task_on_current_page(page, task)
                if clicked:
                    break

            if not clicked:
                if task.category == "daily_set":
                    if daily_set_fallback_allowed:
                        self._log("info", "  ⚠️ Daily Set generic fallback could not locate the current task")
                    else:
                        self._log("info", "  ⚠️ Daily Set proof not established; generic fallback not allowed")
                    return False

                if self.ai_agent and self.ai_agent.enabled:
                    try:
                        self._log("info", "  🤖 Trying AI Rewards-page fallback")
                        ai_result = await self.ai_agent.complete_task_on_page(
                            page,
                            "On Microsoft Rewards, locate and complete this task if possible. "
                            f"Title: {task.title}. "
                            f"Description: {task.description}. "
                            f"Category: {task.category}. "
                            f"Points: {task.points or task.points_max}.",
                        )
                        if ai_result.get("success"):
                            try:
                                await page.goto(
                                    REWARDS_URL,
                                    wait_until="domcontentloaded",
                                    timeout=35000,
                                )
                                await asyncio.sleep(2)
                            except Exception:
                                pass
                            return True
                    except Exception as e:
                        logger.debug(f"AI Rewards-page fallback failed: {e}")

                # Fallback: try destination URL directly (less reliable)
                if task.destination_url:
                    self._log("info", f"  ⚠️ Could not find on page, trying direct URL")
                    await page.goto(task.destination_url,
                                    wait_until="domcontentloaded", timeout=35000)
                    await asyncio.sleep(3)
                    clicked = True
                else:
                    self._log("info", f"  ❌ Could not find task element or URL")
                    return False

            # 3. Handle new tab (activities often open in a new tab)
            await asyncio.sleep(2)
            working_page = page
            new_tab = None

            if len(page.context.pages) > pages_before:
                new_tab = page.context.pages[-1]
                try:
                    await new_tab.wait_for_load_state("domcontentloaded", timeout=35000)
                    working_page = new_tab
                    self._log("info", "  📑 Switched to new tab")
                except Exception:
                    pass

            # 4. Determine how to complete the activity
            await self._ensure_no_manual_challenge(
                working_page,
                task.title[:40] or "Rewards task",
            )
            promo_type = task.raw_data.get("promotionType", "")
            attr_type = task.raw_data.get("attributes", {}).get("type", "")
            promo_handled = False

            if task.category == "more_promo":
                promo_handled = await self._complete_known_more_promo(working_page, task)

            if promo_handled:
                pass
            elif promo_type == "quiz" or task.task_type == "quiz":
                # Quiz: try to solve
                await self._complete_quiz(working_page, task)
            elif "poll" in (task.title or "").lower() or task.task_type == "poll":
                # Poll: click a random option
                await self._complete_poll(working_page, task)
            elif task.task_type == "unknown":
                
                # Check for existing self-learned Macro first
                macro_ok = False
                if self._macro_player.has_macro(task.title):
                    if await self._macro_player.execute_macro(working_page, task.title):
                        self._log("info", "  ⚡ Task completed via AI Macro (bypassing LLM)")
                        await asyncio.sleep(2)
                        macro_ok = True
                        
                # Try page-agent (browser-side LLM with Rewards knowledge)
                pa_ok = macro_ok
                if not macro_ok and self._page_agent:
                    self._log("info", "  🌐 Trying page-agent on unknown activity")
                    try:
                        pa_result = await self._page_agent.run_single_task(
                            working_page,
                            f"Complete this Microsoft Rewards activity: "
                            f"{task.title}. {task.description}",
                            timeout=120.0,
                        )
                        pa_ok = pa_result.get("success", False)
                        
                        # Save the self-learned macro if solved!
                        if pa_ok and pa_result.get("macro_trace"):
                            self._log("info", "  🪄 Extracting AI reflections to build Macro")
                            self._macro_player.save_macro(task.title, pa_result.get("macro_trace"))
                    except Exception as e:
                        logger.debug(f"Page-agent fallback failed: {e}")

                # Then try Python-side AI agent
                if not pa_ok and self.ai_agent and self.ai_agent.enabled:
                    self._log("info", "  🤖 Trying AI fallback on unknown activity")
                    ai_result = await self.ai_agent.complete_task_on_page(
                        working_page,
                        "Complete this Microsoft Rewards activity. "
                        f"Title: {task.title}. "
                        f"Description: {task.description}. "
                        "Use the current page context to finish the task or reach a completed state.",
                    )
                    if not ai_result.get("success"):
                        self._log("info", "  ⚠️ AI could not finish unknown activity, falling back to visit flow")
                        await self._complete_visit(working_page, task)
                elif not pa_ok:
                    await self._complete_visit(working_page, task)
            else:
                # URL reward / visit: just visit and interact
                await self._complete_visit(working_page, task)

            # 5. Clean up: close new tab and go back to rewards page
            if new_tab:
                try:
                    await new_tab.close()
                except Exception:
                    pass

            # Go back to rewards page
            try:
                await page.goto(REWARDS_URL,
                                wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(2)
            except Exception:
                pass

            return True

        except Exception as e:
            logger.warning(f"Task execution failed: {task.title[:30]}: {e}")
            return False

    def _candidate_rewards_pages(self, task: RewardsTask) -> list[str]:
        """Return likely Rewards pages where the task is rendered."""
        pages: list[str] = []

        if task.category in {"daily_set", "streak"}:
            if task.category == "daily_set":
                pages.append(f"{REWARDS_URL}/earn")
            pages.append(f"{REWARDS_URL}/dashboard")
            pages.append(REWARDS_URL)

        if task.category in {"more_promo", "punch_card"}:
            if task.category == "more_promo":
                pages.append(f"{REWARDS_URL}/earn")
                pages.append(f"{REWARDS_URL}/dashboard")
            pages.append(REWARDS_URL)

        parent_promo = task.raw_data.get("parent_promotion", {})
        parent_offer = parent_promo.get("offerId") or task.parent_id
        if task.category == "punch_card" and parent_offer:
            pages.append(f"https://rewards.bing.com/earn/quest/{parent_offer}")

        parent_destination = parent_promo.get("destinationUrl", "")
        if parent_destination:
            pages.append(parent_destination.replace("/dashboard/", "/earn/quest/"))
            pages.append(parent_destination)

        pages.append(REWARDS_URL)

        unique_pages: list[str] = []
        for url in pages:
            if url and url not in unique_pages:
                unique_pages.append(url)
        return unique_pages

    async def _open_daily_set_panel(self, page: Page) -> bool:
        """Open the Daily Set card/panel so individual activities become clickable."""
        selectors = [
            '#daily-sets mee-card-group mee-card:first-child a',
            '#daily-sets mee-card:first-child a',
            '[data-bi-area="DailySet"] a',
            'mee-card:has-text("Daily Set Streak") a',
            'a:has-text("Daily Set Streak")',
            'a:has-text("Daily Set")',
            'mee-rewards-daily-set-item-content:first-of-type a',
        ]

        visible_activity_selectors = [
            'mee-rewards-daily-set-item-content a',
            '#daily-sets mee-card a[href]',
            '#daily-sets a[href*="bing.com"]',
            '#daily-sets a[href*="rewards"]',
        ]

        for activity_sel in visible_activity_selectors:
            try:
                if await page.locator(activity_sel).count() > 1:
                    return True
            except Exception:
                continue

        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible(timeout=2000):
                    continue
                await el.scroll_into_view_if_needed(timeout=3000)
                await el.click(timeout=5000)
                await asyncio.sleep(2)
                self._log("info", "  ✅ Opened Daily Set panel")
                return True
            except Exception:
                continue

        return False

    def _task_title_variants(self, task: RewardsTask) -> list[str]:
        """Return title variants to match current Rewards cards."""
        clean_title = task.title.replace("\u200b", "").replace("\xa0", " ").strip()
        variants = [clean_title]

        if " - Click to complete" in clean_title:
            variants.append(clean_title.replace(" - Click to complete", "").strip())

        if ":" in clean_title:
            variants.append(clean_title.split(":", 1)[0].strip())

        if "?" in clean_title:
            variants.append(clean_title.split("?", 1)[0].strip())

        return [value for value in variants if value]

    @staticmethod
    def _normalized_task_title_key(value: str) -> str:
        """Normalize a task title for per-run Daily Set retry bookkeeping."""
        normalized = "".join(
            ch.lower() if ch.isalnum() or ch.isspace() else " "
            for ch in (value or "").replace("\u200b", " ").replace("\xa0", " ")
        )
        return " ".join(normalized.split())

    def _store_daily_set_execution_proof(
        self,
        task: RewardsTask,
        proof_result: dict | None,
        *,
        source: str = "daily_set_completer",
    ) -> dict:
        """Persist run-local Daily Set proof using task id, normalized title, and category keys."""
        proof_result = proof_result or {}
        state = str(proof_result.get("state") or "").strip()
        if not state:
            if proof_result.get("category_proven", False):
                state = "category_proven"
            elif proof_result.get("target_proven", False):
                state = "target_proven"
            elif proof_result.get("panel_control_failed", False):
                state = "panel_control_failed"
            elif proof_result.get("attempted_only", False) or proof_result.get("attempted", False):
                state = "attempted_only"
            else:
                state = "panel_control_failed"

        proof_titles = [
            title.strip()
            for title in proof_result.get("proof_titles", [])
            if (title or "").strip()
        ]
        record = {
            "state": state,
            "proof_titles": proof_titles,
            "progress_completed": int(proof_result.get("progress_completed", 0) or 0),
            "progress_total": int(proof_result.get("progress_total", 0) or 0),
            "source": str(proof_result.get("source") or source),
        }

        if task.id:
            self.daily_set_execution_proofs[task.id] = dict(record)
        normalized_title = self._normalized_task_title_key(task.title)
        if normalized_title:
            self.daily_set_execution_proofs[normalized_title] = dict(record)
        self.daily_set_execution_proofs["daily_set"] = dict(record)
        return record

    def _get_daily_set_execution_proof(self, task: RewardsTask) -> dict | None:
        """Resolve the best available Daily Set proof for a task from the run-local carrier."""
        candidate_keys = [task.id, self._normalized_task_title_key(task.title), "daily_set"]
        for key in candidate_keys:
            if key and key in self.daily_set_execution_proofs:
                return self.daily_set_execution_proofs[key]
        return None

    @staticmethod
    def _resolve_daily_set_proof_state(result: dict | None) -> str:
        """Normalize Daily Set executor outcomes into the shared proof state machine."""
        result = result or {}
        state = str(result.get("state", "") or "").strip().lower()
        if state:
            return state
        if result.get("category_proven", False):
            return "category_proven"
        if result.get("target_proven", False):
            return "target_proven"
        if result.get("panel_control_failed", False):
            return "panel_control_failed"
        return "attempted_only"

    def _record_daily_set_execution_proof(self, task: RewardsTask, result: dict | None) -> dict:
        """Store Daily Set execution proof under task, title, and category lookup keys."""
        proof_state = self._resolve_daily_set_proof_state(result)
        proof = {
            "state": proof_state,
            "proof_titles": [
                str(title).strip()
                for title in (result or {}).get("proof_titles", [])
                if str(title).strip()
            ],
            "progress_completed": int((result or {}).get("progress_completed", 0) or 0),
            "progress_total": int((result or {}).get("progress_total", 0) or 0),
            "source": str((result or {}).get("source", "daily_set_completer")),
        }

        keys: list[str] = []
        if task.id:
            keys.append(task.id)
        title_key = self._normalized_task_title_key(task.title or "")
        if title_key:
            keys.append(title_key)
        keys.append("daily_set")

        for key in keys:
            self.daily_set_execution_proofs[key] = dict(proof)

        if proof_state == "category_proven":
            self._session_completed_categories.add("daily_set")
            if (task.title or "").strip():
                self._session_daily_set_titles.add(task.title.strip())
            for proof_title in proof["proof_titles"]:
                self._session_daily_set_titles.add(proof_title)

        return proof

    def _get_daily_set_execution_proof(self, task: RewardsTask) -> dict | None:
        """Look up Daily Set proof, preferring category-level proof when it exists."""
        if task.category != "daily_set":
            return None

        proofs: list[dict] = []
        keys: list[str] = []
        if task.id:
            keys.append(task.id)
        title_key = self._normalized_task_title_key(task.title or "")
        if title_key:
            keys.append(title_key)
        keys.append("daily_set")

        for key in keys:
            proof = self.daily_set_execution_proofs.get(key)
            if not proof:
                continue
            if key == "daily_set" and proof.get("state") != "category_proven":
                continue
            if proof.get("state") == "category_proven":
                return proof
            proofs.append(proof)

        return proofs[0] if proofs else None

    @staticmethod
    def _tokenize_match_text(*values: str) -> list[str]:
        """Return meaningful tokens from task title/description for fuzzy card matching."""
        raw = " ".join(value for value in values if value).replace("\u200b", " ").replace("\xa0", " ")
        normalized = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in raw)
        stop_words = {
            "click", "complete", "to", "the", "and", "for", "with",
            "near", "your", "this", "that", "you", "are", "these",
            "into", "from", "more", "points", "point", "earn",
            "reward", "rewards", "daily", "set",
        }
        tokens = []
        for token in normalized.split():
            if len(token) < 4 or token in stop_words:
                continue
            if token not in tokens:
                tokens.append(token)
        return tokens[:10]

    @classmethod
    def _task_title_tokens(cls, task: RewardsTask) -> list[str]:
        """Return title+description tokens so duplicate offer titles remain distinguishable."""
        return cls._tokenize_match_text(task.title or "", task.description or "")

    async def _click_task_on_current_page(self, page: Page, task: RewardsTask) -> bool:
        """Try to click the current task on the already-open Rewards page."""
        title_variants = self._task_title_variants(task)
        primary_title = title_variants[0] if title_variants else task.title
        self._log("info", f"  🔍 Looking for: '{primary_title}' on {page.url}")

        # Fast-path: use exact DOM index captured during the visual scan phase
        elem_idx = task.raw_data.get("element_index")
        if elem_idx is not None:
            self._log("info", f"  🖱️ Using native visual selector index {elem_idx} for task '{primary_title}'")
            import src.dashboard_scraper as scraper
            clicked = await scraper.click_task_by_index(page, elem_idx)
            if clicked:
                self._log("info", "  ✅ Clicked task card visually via absolute target")
                await asyncio.sleep(3)
                return True

        locators = []

        # Only open Daily Set panel on dashboard-like pages, not on /earn
        if task.category == "daily_set" and "/earn" not in page.url:
            await self._open_daily_set_panel(page)

        if task.destination_url:
            locators.extend(
                [
                    page.locator(f'a[href="{task.destination_url}"]').first,
                    page.locator(f'[href="{task.destination_url}"]').first,
                ]
            )

        for title in title_variants:
            if task.category == "daily_set":
                locators.extend(
                    [
                        page.locator('#daily-sets a', has_text=title).first,
                        page.locator(
                            'mee-rewards-daily-set-item-content a',
                            has_text=title,
                        ).first,
                        page.locator('[data-bi-area="DailySet"] a', has_text=title).first,
                        page.locator('mee-card a', has_text=title).first,
                        # /earn page selectors ─ Daily Set section
                        page.locator('[data-bi-id] a', has_text=title).first,
                        page.locator('mee-rewards-daily-set-item-content:not([complete]) a', has_text=title).first,
                    ]
                )
            locators.extend(
                [
                    page.locator("a", has_text=title).first,
                    page.locator("button", has_text=title).first,
                    page.locator('[role="link"]', has_text=title).first,
                    page.locator('[role="button"]', has_text=title).first,
                    page.locator("div.cursor-pointer", has_text=title).first,
                    page.locator("h3", has_text=title).first,
                    page.locator("p", has_text=title).first,
                ]
            )

        for locator in locators:
            try:
                if await locator.count() == 0 or not await locator.is_visible(timeout=2000):
                    continue
                await locator.scroll_into_view_if_needed(timeout=3000)
                await locator.click(timeout=5000)
                self._log("info", "  ✅ Clicked task card on Rewards page")
                await asyncio.sleep(3)
                return True
            except Exception:
                continue

        dom_target_id = await self._mark_dom_text_candidate(
            page,
            title_variants,
            self._task_title_tokens(task),
        )
        if dom_target_id:
            try:
                target = page.locator(f"#{dom_target_id}").first
                if await target.count() > 0 and await target.is_visible(timeout=2000):
                    await target.scroll_into_view_if_needed(timeout=3000)
                    await target.click(timeout=5000)
                    self._log("info", "  ✅ Clicked text-matched Rewards element")
                    await asyncio.sleep(3)
                    return True
            except Exception:
                pass

        if (
            task.destination_url
            and "rewards.bing.com" in page.url
            and task.category != "daily_set"
        ):
            temp_link_id = await self._inject_reward_link(page, task.destination_url)
            if temp_link_id:
                try:
                    temp_link = page.locator(f"#{temp_link_id}").first
                    if await temp_link.count() > 0:
                        await temp_link.click(timeout=5000)
                        self._log("info", "  ✅ Clicked temporary reward link")
                        await asyncio.sleep(3)
                        return True
                except Exception:
                    pass

        return False

    async def _mark_dom_text_candidate(
        self,
        page: Page,
        title_variants: list[str],
        title_tokens: list[str],
    ) -> str | None:
        """Mark a visible DOM node matching one of the task titles for a real click."""
        try:
            return await page.evaluate(
                """
                ({ titles, tokens }) => {
                    const normalize = (value) => (value || "")
                        .replace(/\\u200b/g, "")
                        .replace(/\\u00a0/g, " ")
                        .replace(/[^\\p{L}\\p{N}\\s]/gu, " ")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
                    const clickableSelector = "a,button,[role='button'],[role='link'],[data-rac],.cursor-pointer";
                    const cardSelector = "mee-card,[class*='card'],[data-bi-area],[class*='earn'],[class*='promo']";

                    for (const old of document.querySelectorAll("[data-codex-target='true']")) {
                        old.removeAttribute("data-codex-target");
                        if ((old.id || "").startsWith("codex-target-")) {
                            old.removeAttribute("id");
                        }
                    }

                    const variants = (titles || []).map(normalize).filter(Boolean);
                    const tokenList = (tokens || []).map(normalize).filter(Boolean);
                    if (!variants.length && !tokenList.length) {
                        return null;
                    }

                    const nodes = Array.from(document.querySelectorAll("a,button,[role='button'],[role='link'],h1,h2,h3,h4,p,span,div"));
                    let best = null;
                    let bestScore = 0;

                    for (const node of nodes) {
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 4 || rect.height < 4) {
                            continue;
                        }

                        const style = window.getComputedStyle(node);
                        if (style.visibility === "hidden" || style.display === "none") {
                            continue;
                        }

                        const text = normalize(node.innerText || node.textContent || "");
                        if (!text) {
                            continue;
                        }

                        let score = 0;

                        for (const variant of variants) {
                            if (variant && text.includes(variant)) {
                                score += 1000 + variant.length;
                            }
                        }

                        if (tokenList.length) {
                            let tokenHits = 0;
                            for (const token of tokenList) {
                                if (token && text.includes(token)) {
                                    tokenHits += 1;
                                }
                            }

                            const minHits = Math.min(3, tokenList.length);
                            if (tokenHits >= minHits) {
                                score += tokenHits * 100 + Math.min(text.length, 200);
                            }
                        }

                        if (!score || score <= bestScore) {
                            continue;
                        }

                        const cardRoot = node.closest(cardSelector);
                        const cardText = normalize(cardRoot ? (cardRoot.innerText || cardRoot.textContent || "") : "");
                        if (cardText.includes("completed")) {
                            score -= 40;
                        }

                        let candidate =
                            node.closest(clickableSelector)
                            || (cardRoot ? cardRoot.querySelector(clickableSelector) : null)
                            || node.querySelector(clickableSelector)
                            || cardRoot
                            || node;
                        if (!candidate.id) {
                            candidate.id = `codex-target-${Math.random().toString(36).slice(2, 10)}`;
                        }
                        candidate.setAttribute("data-codex-target", "true");
                        best = candidate.id;
                        bestScore = score;
                    }

                    return best;
                }
                """,
                {"titles": title_variants, "tokens": title_tokens},
            )
        except Exception:
            return None

    async def _inject_reward_link(self, page: Page, destination_url: str) -> str | None:
        """Inject a temporary reward link that Playwright can click with a real user gesture."""
        try:
            return await page.evaluate(
                """
                (url) => {
                    const existing = document.getElementById("codex-temp-reward-link");
                    if (existing) {
                        existing.remove();
                    }

                    const link = document.createElement("a");
                    link.id = "codex-temp-reward-link";
                    link.href = url;
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                    link.textContent = "Temporary reward link";
                    link.style.position = "fixed";
                    link.style.top = "12px";
                    link.style.right = "12px";
                    link.style.zIndex = "2147483647";
                    link.style.padding = "8px 12px";
                    link.style.background = "#b42318";
                    link.style.color = "#ffffff";
                    link.style.fontSize = "14px";
                    link.style.borderRadius = "8px";
                    document.body.appendChild(link);
                    return link.id;
                }
                """,
                destination_url,
            )
        except Exception:
            return None

    @staticmethod
    def _normalized_offer_text(task: RewardsTask) -> str:
        """Flatten title/description into a lowercase token string for offer matching."""
        raw = " ".join(
            part
            for part in [task.title or "", task.description or ""]
            if part
        )
        normalized = "".join(
            ch.lower() if ch.isalnum() or ch.isspace() else " "
            for ch in raw.replace("\u200b", " ").replace("\xa0", " ")
        )
        return " ".join(normalized.split())

    async def _complete_known_more_promo(self, page: Page, task: RewardsTask) -> bool:
        """Apply targeted interactions for known Keep Earning offers that need more than a visit."""
        if task.category != "more_promo":
            return False

        offer_text = self._normalized_offer_text(task)
        if any(
            token in offer_text
            for token in ("thanh tìm kiếm", "search box", "search bar", "search with bing")
        ):
            self._diag(
                "Matched keep-earning search incentive handler",
                scope="promo-handler",
                handler="search_incentive",
                **self._task_diag_payload(task),
            )
            return await self._complete_search_incentive_offer(page, task)

        if any(
            token in offer_text
            for token in ("turn referrals into rewards", "referrals", "referral", "invite friends")
        ):
            self._diag(
                "Matched keep-earning referral handler",
                scope="promo-handler",
                handler="referral",
                **self._task_diag_payload(task),
            )
            return await self._complete_referral_offer(page, task)

        self._diag(
            "No targeted keep-earning handler matched",
            scope="promo-handler",
            **self._task_diag_payload(task),
        )
        return False

    async def _complete_search_incentive_offer(self, page: Page, task: RewardsTask) -> bool:
        """Submit one real Bing search when a promo explicitly references searching."""
        selectors = [
            "#sb_form_q",
            "input[name='q']",
            "input[type='search']",
            "textarea[name='q']",
        ]
        query = "Microsoft Rewards bonus search"
        candidate_urls = []
        current_url = (page.url or "").lower()
        if "bing.com" not in current_url and task.destination_url:
            candidate_urls.append(task.destination_url)
        if "bing.com" not in current_url or not task.destination_url:
            candidate_urls.append("https://www.bing.com/")

        self._diag(
            "Attempting keep-earning search incentive flow",
            scope="promo-search",
            current_url=current_url,
            candidate_urls=candidate_urls,
            selectors=selectors,
            **self._task_diag_payload(task),
        )

        for candidate_url in candidate_urls:
            try:
                await page.goto(candidate_url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(3)
            except Exception:
                self._diag(
                    "Candidate URL navigation failed during promo search flow",
                    scope="promo-search",
                    candidate_url=candidate_url,
                    **self._task_diag_payload(task),
                )
                continue

            for selector in selectors:
                try:
                    box = page.locator(selector).first
                    if await box.count() == 0 or not await box.is_visible(timeout=2500):
                        continue
                    await box.click(timeout=3000)
                    await box.fill(query)
                    await asyncio.sleep(1)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(5)
                    await self.humanizer.simulate_reading(page, random.uniform(3, 5))
                    self._log("info", "  🔎 Submitted live Bing search for promo activation")
                    self._diag(
                        "Submitted live Bing search for promo activation",
                        scope="promo-search",
                        candidate_url=candidate_url,
                        selector=selector,
                        query=query,
                        **self._task_diag_payload(task),
                    )
                    return True
                except Exception:
                    self._diag(
                        "Selector attempt failed during promo search flow",
                        scope="promo-search",
                        candidate_url=candidate_url,
                        selector=selector,
                        **self._task_diag_payload(task),
                    )
                    continue

        for selector in selectors:
            try:
                box = page.locator(selector).first
                if await box.count() == 0 or not await box.is_visible(timeout=2500):
                    continue
                await box.click(timeout=3000)
                await box.fill(query)
                await asyncio.sleep(1)
                await page.keyboard.press("Enter")
                await asyncio.sleep(5)
                await self.humanizer.simulate_reading(page, random.uniform(3, 5))
                self._log("info", "  🔎 Submitted live Bing search for promo activation")
                self._diag(
                    "Submitted promo activation search on current page",
                    scope="promo-search",
                    selector=selector,
                    query=query,
                    **self._task_diag_payload(task),
                )
                return True
            except Exception:
                self._diag(
                    "Fallback selector attempt failed on current page",
                    scope="promo-search",
                    selector=selector,
                    **self._task_diag_payload(task),
                )
                continue

        self._diag(
            "Promo search incentive flow could not find usable search box",
            scope="promo-search",
            **self._task_diag_payload(task),
        )
        return False

    async def _complete_referral_offer(self, page: Page, task: RewardsTask) -> bool:
        """Trigger a visible referral CTA when Microsoft exposes one on-page."""
        ctas = [
            ("button", "Invite"),
            ("button", "Share"),
            ("button", "Copy"),
            ("a", "Invite"),
            ("a", "Share"),
            ("a", "Copy"),
        ]

        self._diag(
            "Attempting keep-earning referral CTA flow",
            scope="promo-referral",
            ctas=ctas,
            current_url=(page.url or ""),
            **self._task_diag_payload(task),
        )

        for role, text in ctas:
            try:
                locator = page.locator(role, has_text=text).first
                if await locator.count() == 0 or not await locator.is_visible(timeout=2000):
                    self._diag(
                        "Referral CTA not visible for candidate",
                        scope="promo-referral",
                        role=role,
                        text=text,
                        **self._task_diag_payload(task),
                    )
                    continue
                await locator.click(timeout=5000)
                await asyncio.sleep(4)
                self._log("info", f"  📣 Triggered referral CTA: {text}")
                self._diag(
                    "Triggered referral CTA successfully",
                    scope="promo-referral",
                    role=role,
                    text=text,
                    **self._task_diag_payload(task),
                )
                return True
            except Exception:
                self._diag(
                    "Referral CTA attempt raised exception",
                    scope="promo-referral",
                    role=role,
                    text=text,
                    **self._task_diag_payload(task),
                )
                continue

        self._diag(
            "Referral flow finished without finding a usable CTA",
            scope="promo-referral",
            **self._task_diag_payload(task),
        )
        return False

    async def _complete_visit(self, page: Page, task: RewardsTask) -> None:
        """Complete a visit-type task: scroll, read, interact."""
        try:
            # 1. Wait out any mandatory timers (some punch cards require ~10-15 seconds)
            wait_time = random.uniform(12, 18) if task.category == "punch_card" else random.uniform(5, 10)
            
            # 2. Scroll to simulate reading while waiting
            scroll_delay = wait_time / 3
            await asyncio.sleep(scroll_delay)
            try:
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            except Exception:
                pass
            await asyncio.sleep(scroll_delay)
            try:
                await page.evaluate('window.scrollTo(0, 0)')
            except Exception:
                pass
            await asyncio.sleep(scroll_delay)
            
            # 3. Only do a forced search if it's explicitly a "search" task,
            # NOT for "visit" punch cards. Doing a search on a visit task
            # can navigate away from the promo URL before tracking fires.
            if task.task_type == "search":
                try:
                    searchbar = page.locator('#sb_form_q')
                    if await searchbar.count() > 0 and await searchbar.is_visible(timeout=3000):
                        await searchbar.click(timeout=3000)
                        await searchbar.fill(task.title[:30] + " info")
                        await asyncio.sleep(1)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(4)
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Visit completion soft-failed: {e}")

    async def _complete_poll(self, page: Page, task: RewardsTask) -> None:
        """Complete a poll: click a random option."""
        try:
            # Poll options have IDs like btoption0, btoption1
            for option_id in ["btoption0", "btoption1"]:
                try:
                    btn = page.locator(f"#{option_id}")
                    if await btn.count() > 0 and await btn.is_visible(timeout=3000):
                        await btn.click(timeout=5000)
                        self._log("info", "  🗳️ Poll answered")
                        await asyncio.sleep(3)
                        return
                except Exception:
                    continue
            # Fallback: just visit
            await self._complete_visit(page, task)
        except Exception:
            pass

    async def _complete_quiz(self, page: Page, task: RewardsTask) -> None:
        """Complete a quiz using JS quiz info or QuizSolver."""
        try:
            # Click "Start Quiz" if present
            for start_sel in ['#rqStartQuiz', 'input[value="Start playing"]']:
                try:
                    start = page.locator(start_sel).first
                    if await start.count() > 0 and await start.is_visible(timeout=3000):
                        await start.click(timeout=5000)
                        await asyncio.sleep(3)
                        break
                except Exception:
                    continue

            # Try JavaScript quiz info (most reliable, from reference project)
            max_questions = await page.evaluate(
                "() => { try { return _w.rewardsQuizRenderInfo.maxQuestions } catch(e) { return 0 } }"
            )
            num_options = await page.evaluate(
                "() => { try { return _w.rewardsQuizRenderInfo.numberOfOptions } catch(e) { return 0 } }"
            )

            if max_questions > 0:
                self._log("info", f"  🧩 Quiz: {max_questions} questions, {num_options} options")
                for q in range(max_questions + 5):  # safety margin
                    # Check completion
                    done_count = await page.evaluate(
                        "() => { try { return _w.rewardsQuizRenderInfo.CorrectlyAnsweredQuestionCount } catch(e) { return -1 } }"
                    )
                    if done_count >= max_questions:
                        self._log("info", "  ✅ Quiz complete!")
                        break

                    await asyncio.sleep(random.uniform(1, 2))

                    if num_options == 8:
                        # Drag-and-drop / multi-select quiz
                        for i in range(8):
                            is_correct = await page.evaluate(
                                f"() => {{ try {{ return document.getElementById('rqAnswerOption{i}').getAttribute('iscorrectoption') }} catch(e) {{ return null }} }}"
                            )
                            if is_correct and is_correct.lower() == "true":
                                try:
                                    await page.locator(f"#rqAnswerOption{i}").click(timeout=3000)
                                    await asyncio.sleep(random.uniform(0.5, 1.5))
                                except Exception:
                                    pass
                    elif num_options in [2, 3, 4]:
                        # Multiple choice quiz
                        correct_answer = await page.evaluate(
                            "() => { try { return _w.rewardsQuizRenderInfo.correctAnswer } catch(e) { return null } }"
                        )
                        if correct_answer:
                            for i in range(num_options):
                                data_option = await page.evaluate(
                                    f"() => {{ try {{ return document.getElementById('rqAnswerOption{i}').getAttribute('data-option') }} catch(e) {{ return null }} }}"
                                )
                                if data_option == correct_answer:
                                    await page.locator(f"#rqAnswerOption{i}").click(timeout=3000)
                                    await asyncio.sleep(random.uniform(2, 4))
                                    break
                    else:
                        # ABC/This-or-That style
                        await page.locator(f"#rqAnswerOption{random.randint(0, max(num_options - 1, 1))}").click(timeout=3000)
                        await asyncio.sleep(random.uniform(2, 4))

                    await asyncio.sleep(random.uniform(1, 3))
                return

            # Fallback: try QuizSolver
            try:
                from src.quiz import QuizSolver
                qs = QuizSolver(self.humanizer)
                if await qs.detect_and_solve(page):
                    self._log("info", "  🧩 Quiz solved by QuizSolver")
                    return
            except Exception as e:
                logger.debug(f"QuizSolver failed: {e}")

            # Fallback: page-agent (browser-side LLM with quiz knowledge)
            if self._page_agent:
                try:
                    pa_result = await self._page_agent.run_single_task(
                        page,
                        f"Complete this quiz. Answer all questions correctly. "
                        f"Title: {task.title}. "
                        f"Look for quiz answer options (#rqAnswerOption0, #rqAnswerOption1, etc.) "
                        f"and click the correct ones. Check iscorrectoption attribute for answers.",
                        timeout=120.0,
                    )
                    if pa_result.get("success"):
                        self._log("info", "  🌐 Quiz solved by page-agent")
                        return
                except Exception as e:
                    logger.debug(f"Page-agent quiz fallback failed: {e}")

            # Fallback: Python-side AI agent
            if self.ai_agent and self.ai_agent.enabled:
                try:
                    result = await self.ai_agent.complete_task_on_page(
                        page,
                        f"Complete this quiz or activity. "
                        f"Click the correct answers. Title: {task.title}",
                    )
                    if result.get("success"):
                        return
                except Exception:
                    pass

            # Last resort: simulate reading
            await self.humanizer.simulate_reading(page, random.uniform(5, 10))

        except Exception as e:
            logger.warning(f"Quiz attempt failed: {e}")

    # ── AI-Driven Earn Page Smart Scan ────────────────────────────────────

    async def _scan_explore_on_bing(self, page: Page) -> int:
        """Intelligently scan the /earn page for non-API task sections.

        Microsoft keeps adding new sections (e.g. "Explore on Bing",
        "Trending now", seasonal promotions) that are rendered client-side
        via Next.js RSC and are **not** part of the ``getuserinfo`` API.

        Strategy:
          Phase 1 – AI Discovery (if AI agent is available):
            Navigate to /earn, scroll to load everything, then ask the AI
            to read the page snapshot and extract every uncompleted task
            card / link it can find that looks like a points-earning card.
          Phase 2 – Fallback DOM scrape (if AI is off or returns nothing):
            Generic heuristic that looks for ``a[href*="bing.com/search"]``
            links inside card-like containers with point badges (``+10`` etc.).
          Phase 3 – Execute:
            Visit each discovered URL to register task completion.
        """
        self._log("info", "🔎 AI Smart Scan – checking /earn page for extra tasks...")

        # ── Navigate and scroll ────────────────────────────────────────
        try:
            await page.goto(
                "https://rewards.bing.com/",
                wait_until="domcontentloaded",
                timeout=35000,
            )
            await asyncio.sleep(5)
        except Exception:
            self._log("info", "  Could not load /earn page")
            return 0

        # Scroll to trigger lazy rendering of ALL sections
        for _ in range(30):
            await page.evaluate("window.scrollBy(0, 400)")
            await asyncio.sleep(0.25)
        await asyncio.sleep(3)

        # ── Phase 1: AI-powered discovery ──────────────────────────────
        cards: list[dict] = []

        if self.ai_agent and self.ai_agent.enabled:
            cards = await self._ai_discover_earn_cards(page)

        # ── Phase 2: Fallback DOM heuristic ────────────────────────────
        if not cards:
            cards = await self._dom_discover_earn_cards(page)

        if not cards:
            self._log("info", "  No extra earn-page tasks found")
            return 0

        # Deduplicate by href
        seen = set()
        unique = []
        for c in cards:
            href = c.get("href", "")
            selector = c.get("selector", "")
            key = href if href else selector
            
            if key and key not in seen:
                seen.add(key)
                unique.append(c)
        cards = unique

        # Memory Cache filter — skip cards visited recently
        state = _load_state()
        account_state = state.get(self._active_account_email, {})
        visited_cards = account_state.setdefault("visited_cards", {})
        fresh_cards = []
        for c in cards:
            href = c.get("href", "")
            selector = c.get("selector", "")
            if not href and not selector:
                continue

            cache_key = _build_earn_card_cache_key(href, selector)
            if _should_skip_earn_card_via_cache(cache_key, visited_cards):
                self._log("info", f"  ✅ Cache: Skipping recently visited card: {c.get('text', '')[:30]}")
                continue
            fresh_cards.append(c)
        cards = fresh_cards

        self._log("info", f"  ✨ Discovered {len(cards)} earn-page cards to visit")

        # ── Phase 3: Visit each URL ───────────────────────────────────
        completed = 0
        for i, card in enumerate(cards):
            title = card.get("text", "")[:50]
            href = card.get("href", "")
            selector = card.get("selector", "")
            if not href and not selector:
                continue

            self._log("info", f"  [{i + 1}/{len(cards)}] {title}")

            try:
                active_page = page
                navigation_succeeded = False
                search_submitted = False
                if href:
                    await page.goto(href, wait_until="domcontentloaded", timeout=25000)
                    navigation_succeeded = True
                else:
                    pages_before = len(page.context.pages)
                    await page.click(selector, timeout=10000)
                    await asyncio.sleep(3)
                    navigation_succeeded = True
                    pages_after = page.context.pages
                    if len(pages_after) > pages_before:
                        # Grab the newly opened page
                        active_page = pages_after[-1]
                        try:
                            await active_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                            
                await asyncio.sleep(random.uniform(3, 6))

                # If this task explicitly asks to "Search on Bing" (e.g., "Search on Bing for your favorite movie")
                needs_search = False
                try:
                    text_lower = title.lower()
                    needs_search = "search" in text_lower or "tìm" in text_lower or "explore" in text_lower
                    if needs_search:
                        input_box = active_page.locator("input[type='search'], textarea[type='search'], input[name='q'], textarea[name='q']").first
                        if await input_box.is_visible(timeout=3000):
                            self._log("info", "    ⌨️ Detected 'Search' requirement, context-aware query loading...")
                            search_term = None
                            
                            # Ask AI agent to deduce what we should type based on the title
                            if getattr(self, "ai_agent", None) and self.ai_agent.enabled:
                                try:
                                    search_term = await self.ai_agent.generate_search_query(title)
                                except Exception as agent_e:
                                    pass
                            
                            # Fallback if AI fails or disabled
                            if not search_term:
                                if "movie" in text_lower or "phim" in text_lower:
                                    search_term = random.choice(["Titanic movie", "Interstellar", "The Matrix", "Avengers"])
                                elif "book" in text_lower or "sách" in text_lower or "read" in text_lower:
                                    search_term = random.choice(["Harry Potter books", "Lord of the Rings", "Dune book"])
                                elif "translate" in text_lower or "từ" in text_lower:
                                    search_term = random.choice(["translate hello to french", "meaning of serendipity"])
                                elif "loan" in text_lower or "vay" in text_lower or "credit" in text_lower:
                                    search_term = random.choice(["personal loan interest rates", "student loan calculator"])
                                elif "diy" in text_lower or "craft" in text_lower:
                                    search_term = random.choice(["fun DIY kits", "craft supplies near me"])
                                elif "pet" in text_lower or "thú" in text_lower:
                                    search_term = random.choice(["best dog food", "cool cat toys"])
                                else:
                                    search_term = random.choice(["Microsoft surface", "Windows 11 features", "OpenAI ChatGPT"])
                                
                            await input_box.fill(search_term)
                            await input_box.press("Enter")
                            search_submitted = True
                            await asyncio.sleep(random.uniform(5, 8))
                except Exception as e:
                    pass
                
                # Simulate brief reading
                try:
                    await active_page.evaluate("window.scrollBy(0, 300)")
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(1, 2))
                
                try:
                    await close_other_tabs(page)
                except Exception:
                    pass
                    
                # If active_page was original page and it navigated away, go back
                if active_page == page and "rewards" not in page.url:
                    await page.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(2)
                
                completed += 1

                # Register visited card into memory cache
                cache_key = _build_earn_card_cache_key(href, selector)
                card_proven = _should_cache_earn_card_visit(
                    cache_key,
                    navigation_succeeded and (search_submitted or not needs_search or "bing.com/search" in (href or "").lower()),
                )
                if card_proven:
                    state = _load_state()
                    account_state = state.setdefault(self._active_account_email, {})
                    account_state.setdefault("visited_cards", {})[cache_key] = datetime.now().isoformat()
                    _save_state(state)
            except Exception as e:
                self._log("info", f"  ⚠️ Card failed: {e}")

            await asyncio.sleep(random.uniform(1, 3))

        self._log("info", f"  ✅ Earn-page smart scan: {completed}/{len(cards)} cards visited")
        return completed

    # ── AI Discovery ───────────────────────────────────────────────────

    async def _ai_discover_earn_cards(self, page: Page) -> list[dict]:
        """Use AI agent with screenshot + text to discover task cards."""
        try:
            # Build a focused page snapshot for the AI
            snapshot = await self.ai_agent._get_page_snapshot(page, max_text_len=6000)

            # Component 3: Try to capture a screenshot for vision-capable LLMs
            screenshot_b64 = None
            try:
                import base64
                screenshot_bytes = await page.screenshot(full_page=False, type="jpeg", quality=60)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            except Exception:
                pass

            prompt = (
                "You are analyzing the Microsoft Rewards Earn page.\n"
                "I need you to find ALL uncompleted task cards that I can click to earn points.\n"
                "Look for sections like:\n"
                "  - 'Keep earning' (cards with +5, +10, +15 points, e.g. 'Perks of standing', 'Complete this puzzle', 'Do you know the answer?')\n"
                "  - 'Explore on Bing' (cards with +10 points, 'Search on Bing for ...')\n"
                "  - 'Trending now', 'Discover', or any other card section\n"
                "  - Any card with a point badge (+5, +10, +15, +20, +50) that is NOT yet completed\n"
                "  - Cards that say 'Completed' or have a checkmark (✓) should be SKIPPED\n"
                "  - Cards that appear faded/greyed out or have reduced opacity are COMPLETED — SKIP them\n\n"
                "For each card found, extract the URL (href) from the link. If the card has no href but has a clickable element, use a CSS selector.\n\n"
                "Respond with a JSON array of objects, each with 'text' and 'href' (or 'selector' if no href).\n"
                "Example: [{\"text\": \"Perks of standing\", \"href\": \"https://rewards.bing.com/...\"}]\n"
                "If no cards found, respond with an empty array: []\n\n"
                "IMPORTANT: Only return the JSON array, nothing else.\n\n"
                f"{snapshot}"
            )

            # Build messages — include screenshot if available for vision LLMs
            if screenshot_b64:
                messages = [
                    {"role": "system", "content": (
                        "You are a Rewards page analyzer with vision. You can see screenshots "
                        "and read page text to extract task card URLs. Respond ONLY with a valid JSON array."
                    )},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{screenshot_b64}",
                            "detail": "low",
                        }},
                    ]},
                ]
            else:
                messages = [
                    {"role": "system", "content": (
                        "You are a Rewards page analyzer. You extract task card URLs from "
                        "page snapshots. Respond ONLY with a valid JSON array."
                    )},
                    {"role": "user", "content": prompt},
                ]

            result = await self.ai_agent._call_llm(messages)

            if result is None:
                return []

            # The LLM might return a dict with the array, or the array directly
            if isinstance(result, list):
                cards = result
            elif isinstance(result, dict):
                # Try common wrapper keys
                for key in ["cards", "tasks", "items", "data", "results"]:
                    if key in result and isinstance(result[key], list):
                        cards = result[key]
                        break
                else:
                    return []
            else:
                return []

            # Validate
            valid = []
            for c in cards:
                if isinstance(c, dict) and (c.get("href") or c.get("selector")):
                    valid.append({
                        "text": str(c.get("text", ""))[:120],
                        "href": str(c.get("href", "")),
                        "selector": str(c.get("selector", "")),
                    })

            self._log("info", f"  🧠 AI found {len(valid)} task cards" + (" (with vision)" if screenshot_b64 else ""))
            return valid

        except Exception as e:
            self._log("info", f"  AI discovery failed: {e}")
            return []

    # ── DOM Fallback Discovery ─────────────────────────────────────────

    async def _dom_discover_earn_cards(self, page: Page) -> list[dict]:
        """Adaptive DOM discovery: scrape the earn page for task cards using
        multiple flexible strategies that survive Microsoft layout changes."""
        return await page.evaluate("""
            () => {
                const cards = [];
                const seenHrefs = new Set();

                const isCompleted = (text) => {
                    const t = (text || '').toLowerCase();
                    return t.includes('completed') || t.includes('\u2713') || t.includes('\u2714')
                        || t.includes('done') || t.includes('claimed');
                };

                const addCard = (a, contextEl) => {
                    if (!a || !a.href || seenHrefs.has(a.href)) return;
                    const parentText = (contextEl || a.parentElement)?.textContent || '';
                    if (isCompleted(parentText)) return;

                    // Check if card is visually faded (completed)
                    const card = a.closest('[class*="card"]') || a.closest('[data-bi-area]') || a.parentElement;
                    if (card) {
                        const style = window.getComputedStyle(card);
                        if (parseFloat(style.opacity) < 0.6) return;
                    }

                    seenHrefs.add(a.href);
                    cards.push({
                        text: (a.textContent || '').trim().substring(0, 120),
                        href: a.href
                    });
                };

                // Strategy 1: Known section IDs (fast path)
                const knownIds = [
                    'exploreonbing', 'trendingnow', 'discover',
                    'explore-on-bing', 'trending-now', 'discover-on-bing',
                    'moreactivities', 'more-activities', 'quests',
                    'keepearning', 'keep-earning', 'morepromotions', 'more-promotions'
                ];
                for (const sectionId of knownIds) {
                    const section = document.getElementById(sectionId);
                    if (!section) continue;
                    
                    // Capture both a[href] and routable mee-cards
                    section.querySelectorAll('a[href], mee-card').forEach(el => {
                        const href = el.href || el.getAttribute('href') || '';
                        if (href && href.includes('bing.com')) {
                            addCard(el, el.closest('[class*="card"]') || el.parentElement);
                        } else if (el.tagName.toLowerCase() === 'mee-card') {
                            const dataBiId = el.getAttribute('data-bi-id');
                            if (dataBiId) {
                                seenHrefs.add(dataBiId);
                                cards.push({
                                    text: (el.textContent || '').trim().substring(0, 120),
                                    href: '',
                                    selector: `mee-card[data-bi-id="${dataBiId}"]`
                                });
                            }
                        }
                    });
                }

                // Strategy 2: Find sections by heading text (flexible)
                const headingKeywords = [
                    'explore on bing', 'trending now', 'discover on bing',
                    'quests', 'more activities', 'featured', 'earn more',
                    'bonus', 'weekly', 'seasonal', 'special',
                    'keep earning', 'more promotions', 'other activities'
                ];
                const headingEls = document.querySelectorAll('h1, h2, h3, h4, h5, span[class*="title"], span[class*="heading"]');
                for (const el of headingEls) {
                    const headText = (el.textContent || '').trim().toLowerCase();
                    const isMatch = headingKeywords.some(kw => headText.includes(kw));
                    if (!isMatch) continue;

                    // Walk up to find the section container
                    const parent = el.closest('section')
                        || el.closest('[class*="section"]')
                        || el.closest('[data-bi-area]')
                        || el.parentElement?.parentElement?.parentElement
                        || el.parentElement?.parentElement;
                    if (!parent) continue;

                    parent.querySelectorAll('a[href], mee-card').forEach(cb => {
                        const href = cb.href || cb.getAttribute('href') || '';
                        if (href && (href.includes('bing.com') || href.includes('rewards.'))) {
                            addCard(cb, cb.closest('[class*="card"]') || cb.parentElement);
                        } else if (cb.tagName.toLowerCase() === 'mee-card') {
                            const dataBiId = cb.getAttribute('data-bi-id');
                            if (dataBiId) {
                                seenHrefs.add(dataBiId);
                                cards.push({
                                    text: (cb.textContent || '').trim().substring(0, 120),
                                    href: '',
                                    selector: `mee-card[data-bi-id="${dataBiId}"]`
                                });
                            }
                        }
                    });
                }

                // Strategy 3: Structural scan — any link inside card-like containers with point badges
                const cardContainers = document.querySelectorAll(
                    '[class*="card"], [class*="Card"], [data-bi-area], '
                    + '[class*="earn"], [class*="Earn"], [class*="quest"], [class*="Quest"], '
                    + '[class*="promo"], [class*="Promo"], mee-card'
                );
                for (const container of cardContainers) {
                    const text = container.textContent || '';
                    // Must contain a point badge pattern (+5, +10, +15, etc.)
                    if (!/\+\d+/.test(text)) continue;
                    if (isCompleted(text)) continue;

                    container.querySelectorAll('a[href], mee-card').forEach(cb => {
                        const href = cb.href || cb.getAttribute('href') || '';
                        if (href && (href.includes('bing.com') || href.includes('rewards.'))) {
                            addCard(cb, container);
                        } else if (cb.tagName.toLowerCase() === 'mee-card') {
                            const dataBiId = cb.getAttribute('data-bi-id');
                            if (dataBiId && !seenHrefs.has(dataBiId)) {
                                seenHrefs.add(dataBiId);
                                cards.push({
                                    text: (cb.textContent || '').trim().substring(0, 120),
                                    href: '',
                                    selector: `mee-card[data-bi-id="${dataBiId}"]`
                                });
                            }
                        }
                    });
                }

                // Strategy 4: Broad search link scan (last resort)
                if (cards.length === 0) {
                    document.querySelectorAll('a[href*="bing.com/search"], mee-card[data-bi-id]').forEach(cb => {
                        const container = cb.closest('[class*="card"]')
                            || cb.closest('[data-bi-area]')
                            || cb.parentElement;
                        const text = container ? (container.textContent || '') : '';
                        if (/\+\d+/.test(text) && !isCompleted(text)) {
                            const href = cb.href || cb.getAttribute('href') || '';
                            if (href) {
                                addCard(cb, container);
                            } else if (cb.tagName.toLowerCase() === 'mee-card') {
                                const dataBiId = cb.getAttribute('data-bi-id');
                                if (dataBiId && !seenHrefs.has(dataBiId)) {
                                    seenHrefs.add(dataBiId);
                                    cards.push({
                                        text: (cb.textContent || '').trim().substring(0, 120),
                                        href: '',
                                        selector: `mee-card[data-bi-id="${dataBiId}"]`
                                    });
                                }
                            }
                        }
                    });
                }

                return cards;
            }
        """)

