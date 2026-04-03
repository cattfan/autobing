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

        # 1b. DOM Cross-Validation — override stale API completion status
        dom_completed_ids = await self._dom_verify_task_status(page, tasks)
        if dom_completed_ids:
            self._log("info", f"👁️ DOM detected {len(dom_completed_ids)} additional completed tasks")
            for task in tasks:
                if not task.is_complete and task.id in dom_completed_ids:
                    task.is_complete = True

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

            # Local Memory Cache check (skip tasks recently visited to outpace Microsoft API delay)
            visited_tasks = account_state.setdefault("visited_tasks", {})
            if task.id in visited_tasks:
                visited_at_str = visited_tasks[task.id]
                try:
                    visited_at = datetime.fromisoformat(visited_at_str)
                    if datetime.now() - visited_at < timedelta(hours=12):
                        stats["skipped_done"] += 1
                        category_stats["skipped_done"] += 1
                        self._log("info", f"✅ Local Cache: Skipping recently completed task: {task.title[:30]}")
                        continue
                except ValueError:
                    pass

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

                    # Register completed task into memory cache to outsmart lagging MS APIs
                    state = _load_state()
                    account_state = state.setdefault(account_email, {})
                    account_state.setdefault("visited_tasks", {})[task.id] = datetime.now().isoformat()
                    _save_state(state)
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
                except Exception as _e:
                    logger.debug(f"Tab cleanup suppressed: {_e}")

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
            await asyncio.sleep(5)
            retry_tasks = await self._fetch_all_tasks(page)
            
            still_failed = []
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
                        
                        if stats["by_category"].setdefault(rt.category, {}).setdefault("failed", 0) > 0:
                            stats["by_category"][rt.category]["failed"] -= 1
                    else:
                        still_failed.append(rt)
                        
                    await close_other_tabs(page)
                except Exception:
                    still_failed.append(rt)
                    
            if retried > 0:
                self._log("info", f"  ✅ Retried successfully this round: {retried}")
                
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
        """Hybrid 3-layer verification: Memory Cache → DOM visual → API."""

        # Layer 1: URL/visit tasks — optimistic pass (they always count)
        if task.task_type in ("urlreward", "visit"):
            self._log("info", f"  ✅ Optimistically completed URL task (skipping strict API verify)")
            return True

        # Layer 2: DOM visual cues — check if the page shows completion
        try:
            dom_done = await self._dom_check_single_task_done(page, task)
            if dom_done:
                self._log("info", f"  ✅ DOM visual confirms task completed")
                return True
        except Exception as _e:
            logger.debug(f"DOM visual check suppressed: {_e}")

        # Layer 3: API verification (slowest, may lag)
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

    async def _dom_check_single_task_done(self, page: Page, task: RewardsTask) -> bool:
        """Check if a single task shows visual completion cues on the current page."""
        title_snippet = (task.title or "")[:30]
        if not title_snippet:
            return False
        try:
            return await page.evaluate("""
                (titleSnippet) => {
                    const normalize = s => (s || '').replace(/[\u200b\u00a0]/g, ' ').trim().toLowerCase();
                    const target = normalize(titleSnippet);
                    if (!target) return false;

                    const cards = document.querySelectorAll(
                        'mee-card, [class*="card"], [data-bi-area], [class*="earn"]'
                    );

                    for (const card of cards) {
                        const text = normalize(card.innerText || card.textContent || '');
                        if (!text.includes(target)) continue;

                        // Check visual completion cues
                        if (text.includes('completed') || text.includes('✓') || text.includes('✔')) {
                            return true;
                        }

                        // Check for checkmark icons
                        const icons = card.querySelectorAll(
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

                        // Check for muted/disabled styling
                        const style = window.getComputedStyle(card);
                        if (parseFloat(style.opacity) < 0.6) {
                            return true;
                        }
                    }
                    return false;
                }
            """, title_snippet)
        except Exception:
            return False

    async def _dom_verify_task_status(self, page: Page, tasks: list) -> set:
        """Cross-validate API tasks against DOM visual cues on the earn page.

        Returns a set of task IDs that DOM shows as completed but API says incomplete.
        """
        completed_ids = set()
        if not tasks:
            return completed_ids

        try:
            # Navigate to /earn to get the full rendered card layout
            if "rewards.bing.com/earn" not in page.url:
                await page.goto(
                    "https://rewards.bing.com/earn",
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
                            timeout=35000,
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
                                    timeout=35000,
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
            pages.append(REWARDS_URL)
            # Also try /earn — Daily Set items render reliably there
            if task.category == "daily_set":
                pages.append("https://rewards.bing.com/earn")

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

        # Only open Daily Set panel on the main dashboard, not on /earn
        if task.category == "daily_set" and "rewards.bing.com/earn" not in page.url:
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
                "https://rewards.bing.com/earn",
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
                
            cache_key = (href.split("?")[0][:120]) if href else selector
            if cache_key in visited_cards:
                try:
                    visited_at = datetime.fromisoformat(visited_cards[cache_key])
                    if datetime.now() - visited_at < timedelta(hours=12):
                        self._log("info", f"  ✅ Cache: Skipping recently visited card: {c.get('text', '')[:30]}")
                        continue
                except ValueError:
                    pass
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
                if href:
                    await page.goto(href, wait_until="domcontentloaded", timeout=25000)
                else:
                    pages_before = len(page.context.pages)
                    await page.click(selector, timeout=10000)
                    await asyncio.sleep(3)
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
                try:
                    text_lower = title.lower()
                    if "search" in text_lower or "tìm" in text_lower or "explore" in text_lower:
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
                    await page.goto("https://rewards.bing.com/earn", wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(2)
                
                completed += 1

                # Register visited card into memory cache
                cache_key = (href.split("?")[0][:120]) if href else selector
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
                "  - 'Explore on Bing' (cards with +10 points, 'Search on Bing for ...')\n"
                "  - 'Trending now', 'Discover', or any other card section\n"
                "  - Any card with a point badge (+5, +10, +15, +20, +50) that is NOT yet completed\n"
                "  - Cards that say 'Completed' or have a checkmark should be SKIPPED\n"
                "  - Cards that appear faded/greyed out or have reduced opacity are COMPLETED — SKIP them\n\n"
                "For each card found, extract the URL (href) from the link.\n\n"
                "Respond with a JSON array of objects, each with 'text' and 'href'.\n"
                "Example: [{\"text\": \"Learn song lyrics\", \"href\": \"https://www.bing.com/search?q=...\"}, ...]\n"
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
                if isinstance(c, dict) and c.get("href"):
                    valid.append({
                        "text": str(c.get("text", ""))[:120],
                        "href": str(c["href"]),
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
                    'moreactivities', 'more-activities', 'quests'
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
                    'bonus', 'weekly', 'seasonal', 'special'
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
                    if (!/\\+\\d+/.test(text)) continue;
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
                        if (/\\+\\d+/.test(text) && !isCompleted(text)) {
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

