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

from playwright.async_api import Page, BrowserContext

from src.utils import (
    logger,
    REWARDS_URL,
    close_other_tabs,
    select_active_daily_set_items,
)
from src.humanizer import Humanizer

# ─── Task State Persistence ────────────────────────────────────────────────

STATE_FILE = Path("data/task_state.json")


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
        self._daily_set_bulk_attempted = False
        self._log = on_log or (lambda level, msg: logger.info(msg))

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
                "skipped_locked": int,
                "skipped_done": int,
                "failed": int,
                "tasks": [...]
            }
        """
        skip_categories = skip_categories or []
        stats = {
            "total": 0, "completed": 0,
            "skipped_locked": 0, "skipped_done": 0,
            "failed": 0, "tasks": [],
            "by_category": {},
        }
        self._active_account_email = account_email
        self._daily_set_bulk_attempted = False

        # 1. Fetch all tasks from API
        self._log("info", "🔍 Scanning all Rewards tasks...")
        await self._ensure_no_manual_challenge(page, "Rewards task scan")
        tasks = await self._fetch_all_tasks(page)
        stats["total"] = len(tasks)
        self._log("info", f"Found {len(tasks)} total tasks")

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
                    "skipped_done": 0,
                    "skipped_locked": 0,
                    "failed": 0,
                },
            )
            category_stats["total"] += 1

            if task.category in skip_categories:
                continue

            if task.is_complete:
                stats["skipped_done"] += 1
                category_stats["skipped_done"] += 1
                continue

            if task.is_locked:
                stats["skipped_locked"] += 1
                category_stats["skipped_locked"] += 1
                self._log("info", f"🔒 Locked: {task.title[:40]} (time-gated)")
                # Track for next run
                account_state.setdefault("locked_tasks", {})[task.id] = {
                    "title": task.title,
                    "locked_since": datetime.now().isoformat(),
                }
                continue

            actionable.append(task)

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

        # 5. Execute tasks
        failed_tasks = []
        for i, task in enumerate(actionable):
            try:
                await self._ensure_no_manual_challenge(page, task.title[:40] or "Rewards task")
                self._log("info",
                           f"[{i + 1}/{len(actionable)}] {task.category}: "
                           f"{task.title[:40]}... ({task.task_type})")

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

                # Clean up tabs
                try:
                    await close_other_tabs(page)
                except Exception:
                    pass

                await self.humanizer.short_delay()

            except Exception as e:
                logger.warning(f"Task failed: {task.title[:30]}: {e}")
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
                except Exception:
                    pass
                # Recovery
                try:
                    await close_other_tabs(page)
                    await page.goto(REWARDS_URL,
                                    wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)
                except Exception:
                    pass

        # 6. Auto-retry: re-scan API to find still-incomplete tasks
        if int(self.settings.get("session_task_retry_limit", 0)) <= 0:
            failed_tasks = []

        if failed_tasks:
            self._log("info", f"🔄 Auto-retry: {len(failed_tasks)} failed tasks...")
            await asyncio.sleep(3)
            retry_tasks = await self._fetch_all_tasks(page)
            retried = 0
            for rt in retry_tasks:
                if rt.is_complete or rt.is_locked:
                    continue
                # Only retry tasks that failed before
                if not any(ft.id == rt.id for ft in failed_tasks):
                    continue
                try:
                    self._log("info", f"  🔁 Retry: {rt.title[:40]}")
                    success = await self._execute_task(page, rt)
                    if success:
                        success = await self._verify_task_completion(page, rt)
                    if success:
                        retried += 1
                        stats["completed"] += 1
                        stats["failed"] -= 1
                        stats["by_category"].setdefault(rt.category, {}).setdefault("completed", 0)
                        stats["by_category"][rt.category]["completed"] += 1
                        stats["by_category"].setdefault(rt.category, {}).setdefault("failed", 0)
                        if stats["by_category"][rt.category]["failed"] > 0:
                            stats["by_category"][rt.category]["failed"] -= 1
                    await close_other_tabs(page)
                except Exception:
                    pass
            if retried > 0:
                self._log("info", f"  ✅ Retried successfully: {retried}")

        self._log("info",
                   f"✅ Tasks: {stats['completed']}/{stats['total']} completed, "
                   f"{stats['skipped_locked']} locked, {stats['failed']} failed")

        return stats

    @staticmethod
    def _task_sort_key(task: RewardsTask) -> tuple[int, int, str]:
        """Prioritize simpler, immediately-available tasks first."""
        category_order = {
            "daily_set": 0,
            "more_promo": 1,
            "punch_card": 2,
            "streak": 3,
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
        """Re-check the Rewards API so we only count tasks that actually completed."""
        # Punch cards need more time for server-side processing
        initial_wait = 5 if task.category == "punch_card" else 2
        max_attempts = 2 if task.category == "punch_card" else 1

        for attempt in range(max_attempts):
            try:
                await asyncio.sleep(initial_wait if attempt == 0 else 5)
                refreshed_tasks = await self._fetch_all_tasks(page)
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
                    return True

                if refreshed.is_complete:
                    return True

                if attempt < max_attempts - 1:
                    self._log(
                        "info",
                        f"  ⏳ Waiting for server to register completion... (retry {attempt + 1})",
                    )
                    continue

                self._log(
                    "info",
                    f"  ⚠️ Task did not verify as complete: {task.title[:40]}",
                )
                return False
            except Exception as e:
                logger.warning(f"Task verification failed for {task.title[:30]}: {e}")
                return False
        return False

    # ─── API Data Fetch ────────────────────────────────────────────────

    async def _fetch_all_tasks(self, page: Page) -> list[RewardsTask]:
        """Fetch all tasks from Rewards API and classify them."""
        tasks: list[RewardsTask] = []

        try:
            # Navigate to rewards page first
            if "rewards.bing.com" not in page.url:
                await page.goto(REWARDS_URL,
                                wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)

            # Fetch API data
            api_data = await page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('/api/getuserinfo?type=1', {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'}
                        });
                        return await r.json();
                    } catch(e) { return null; }
                }
            """)

            if not api_data:
                logger.warning("Rewards API returned no data")
                return tasks

            dashboard = api_data.get("dashboard", {})

            # ── Daily Set Promotions ──
            daily_sets = dashboard.get("dailySetPromotions", {})
            for item in select_active_daily_set_items(daily_sets):
                task = self._parse_task(item, "daily_set")
                tasks.append(task)

            # ── More Promotions (Quests, Activities) ──
            more_promos = dashboard.get("morePromotions", [])
            for item in more_promos:
                task = self._parse_task(item, "more_promo")
                tasks.append(task)

            # ── Punch Cards ──
            punch_cards = dashboard.get("punchCards", [])
            for pc in punch_cards:
                parent_promo = pc.get("parentPromotion", {})
                parent_id = parent_promo.get("offerId", "")
                children = pc.get("childPromotions", [])

                # Skip expired punch cards
                parent_expiry = parent_promo.get("expirationDate", "")
                if parent_expiry:
                    try:
                        from datetime import datetime as dt
                        expiry_dt = dt.fromisoformat(
                            parent_expiry.replace("Z", "+00:00")
                        )
                        if expiry_dt < dt.now(expiry_dt.tzinfo):
                            logger.debug(
                                f"Skipping expired punch card: "
                                f"{parent_promo.get('title', '?')}"
                            )
                            continue
                    except Exception:
                        pass

                # Skip if parent is already complete
                if parent_promo.get("complete", False):
                    continue

                for idx, child in enumerate(children):
                    task = self._parse_task(child, "punch_card")
                    task.parent_id = parent_id
                    task.child_index = idx
                    task.raw_data["parent_promotion"] = parent_promo
                    tasks.append(task)

        except Exception as e:
            logger.error(f"Failed to fetch tasks: {e}")

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
            clicked = False
            pages_before = len(page.context.pages)
            await self._ensure_no_manual_challenge(page, task.title[:40] or "Rewards task")

            # 1. Search the page(s) where the current Rewards UI renders this task.
            for rewards_url in self._candidate_rewards_pages(task):
                if page.url != rewards_url:
                    try:
                        await page.goto(
                            rewards_url,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(3)
                    except Exception:
                        continue

                clicked = await self._click_task_on_current_page(page, task)
                if clicked:
                    break

            if not clicked:
                if task.category == "daily_set" and not self._daily_set_bulk_attempted:
                    self._daily_set_bulk_attempted = True
                    try:
                        from src.daily_set import DailySetCompleter

                        self._log("info", "  🎯 Trying Daily Set bulk fallback")
                        daily_set = DailySetCompleter(
                            self.humanizer,
                            settings=self.settings,
                            ai_agent=self.ai_agent,
                        )
                        daily_stats = await daily_set.complete_daily_set(page)
                        if daily_stats.get("completed", 0) > 0:
                            try:
                                await page.goto(
                                    REWARDS_URL,
                                    wait_until="domcontentloaded",
                                    timeout=15000,
                                )
                                await asyncio.sleep(2)
                            except Exception:
                                pass
                            return True
                    except Exception as e:
                        logger.debug(f"Daily Set bulk fallback failed: {e}")

                if task.category == "daily_set":
                    self._log("info", "  ⚠️ Daily Set task still not found on Rewards panel")
                    return True

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
                                    timeout=15000,
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
                                    wait_until="domcontentloaded", timeout=15000)
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
                    await new_tab.wait_for_load_state("domcontentloaded", timeout=15000)
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

            if promo_type == "quiz" or task.task_type == "quiz":
                # Quiz: try to solve
                await self._complete_quiz(working_page, task)
            elif "poll" in (task.title or "").lower() or task.task_type == "poll":
                # Poll: click a random option
                await self._complete_poll(working_page, task)
            elif task.task_type == "unknown" and self.ai_agent and self.ai_agent.enabled:
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
                                wait_until="domcontentloaded", timeout=15000)
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
            pages.append(REWARDS_URL)

        if task.category in {"more_promo", "punch_card"}:
            pages.append("https://rewards.bing.com/earn")

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
    def _task_title_tokens(task: RewardsTask) -> list[str]:
        """Return meaningful title tokens for fuzzy card matching."""
        raw = (task.title or "").replace("\u200b", " ").replace("\xa0", " ")
        normalized = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in raw)
        stop_words = {
            "click", "complete", "to", "the", "and", "for", "with",
            "near", "your", "this", "that", "you", "are",
        }
        tokens = []
        for token in normalized.split():
            if len(token) < 4 or token in stop_words:
                continue
            if token not in tokens:
                tokens.append(token)
        return tokens[:8]

    async def _click_task_on_current_page(self, page: Page, task: RewardsTask) -> bool:
        """Try to click the current task on the already-open Rewards page."""
        title_variants = self._task_title_variants(task)
        primary_title = title_variants[0] if title_variants else task.title
        self._log("info", f"  🔍 Looking for: '{primary_title}' on {page.url}")

        locators = []

        if task.category == "daily_set":
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
                                score = Math.max(score, 1000 + variant.length);
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
                                score = Math.max(score, tokenHits * 100 + text.length);
                            }
                        }

                        if (!score || score <= bestScore) {
                            continue;
                        }

                        let candidate = node.closest(clickableSelector) || node;
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

            # Fallback: AI agent
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
