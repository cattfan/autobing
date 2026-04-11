"""
Bing search automation with advanced anti-detection patterns.
- Variable search methods (URL bar, search box, autocomplete)
- Random result interactions (click, hover, back, scroll)
- Session timing variation (micro-breaks, speed changes)
- Referrer chain simulation
"""

from __future__ import annotations
import asyncio
import random
import re
import urllib.parse
from typing import Optional, Callable

from playwright.async_api import BrowserContext, Page

from src.utils import logger, BING_SEARCH_URL, BING_HOME_URL, retry, close_other_tabs
from src.humanizer import Humanizer
from src.trends import TrendsManager


class SafetyStopError(RuntimeError):
    """Raised when Bing asks for verification or shows abuse warnings."""


class Searcher:
    """Performs Bing searches with human-like stealth patterns."""

    def __init__(
        self,
        humanizer: Humanizer,
        trends: TrendsManager,
        settings: dict,
        challenge_handler=None,
    ):
        self.humanizer = humanizer
        self.trends = trends
        self.settings = settings
        self.challenge_handler = challenge_handler
        self.account_email = ""
        self._current_mode: str = "desktop"
        self.on_progress: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

    async def _safe_navigate(self, page: Page, url: str, timeout: int = 15000) -> None:
        """Navigate to URL safely."""
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    def set_account_context(self, account_email: str) -> None:
        """Set the active account label for manual challenge handoff."""
        self.account_email = account_email

    def _search_delay_bounds(self) -> tuple[float, float]:
        """Return the delay profile used between searches."""
        lo = float(self.settings.get("search_delay_min", 5.0))
        hi = float(self.settings.get("search_delay_max", 12.0))
        if hi < lo:
            hi = lo
        return lo, hi

    def _session_break_interval(self) -> int:
        """Return how many searches to do before taking a mini-break."""
        lo = int(self.settings.get("search_break_every_min", 7))
        hi = int(self.settings.get("search_break_every_max", 11))
        if hi < lo:
            hi = lo
        return random.randint(lo, hi)

    async def _check_safety_signals(self, page: Page) -> None:
        """Stop immediately when Bing requests verification."""
        if self.challenge_handler:
            resolved = await self.challenge_handler.handle_if_present(
                page,
                account=self.account_email,
                context="Bing searches",
            )
            if resolved:
                return
            raise SafetyStopError(
                "Manual verification challenge was not resolved; stopping search session"
            )

        try:
            page_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return

        normalized = " ".join(page_text.lower().split())
        markers = (
            "unusual traffic",
            "verify you are human",
            "verify you're human",
            "complete the security check",
            "enter the characters you see",
            "detected unusual activity",
            "our systems have detected",
            "captcha",
        )
        if any(marker in normalized for marker in markers):
            raise SafetyStopError(
                "Bing requested verification/captcha; stopping to protect the account"
            )

        # Check for HTTP 403/429 rate-limiting signals
        rate_limit_markers = (
            "403 forbidden",
            "429 too many requests",
            "rate limit",
            "too many requests",
            "temporarily blocked",
            "access denied",
        )
        if any(marker in normalized for marker in rate_limit_markers):
            pause_seconds = random.randint(900, 1800)  # 15-30 minutes per spec
            logger.warning(
                f"Rate limiting detected (403/429). Pausing {pause_seconds // 60} min for safety..."
            )
            await asyncio.sleep(pause_seconds)
            # Enable slow_human mode for remaining searches
            self._slow_human_mode = True

    def _add_typo(self, query: str) -> tuple[str, str]:
        """Add a realistic typo to query (20% chance).
        
        Returns (typed_query, correction_query).
        If no typo: both are the same.
        If typo: typed has error, correction is original.
        """
        if self.settings.get("safe_mode", True):
            return query, query

        if random.random() > 0.20 or len(query) < 5:
            return query, query
        
        # Pick random position to typo (not first/last char)
        pos = random.randint(1, len(query) - 2)
        typo_type = random.choice(["swap", "double", "skip", "adjacent"])
        
        chars = list(query)
        if typo_type == "swap" and pos < len(chars) - 1:
            # Swap two adjacent characters
            chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
        elif typo_type == "double":
            # Double a character
            chars.insert(pos, chars[pos])
        elif typo_type == "skip":
            # Skip a character
            chars.pop(pos)
        elif typo_type == "adjacent":
            # Replace with adjacent keyboard key
            keyboard_adj = {
                'a': 'sq', 'b': 'vn', 'c': 'xv', 'd': 'sf', 'e': 'wr',
                'f': 'dg', 'g': 'fh', 'h': 'gj', 'i': 'uo', 'j': 'hk',
                'k': 'jl', 'l': 'k', 'm': 'n', 'n': 'bm', 'o': 'ip',
                'p': 'o', 'q': 'w', 'r': 'et', 's': 'ad', 't': 'ry',
                'u': 'yi', 'v': 'cb', 'w': 'qe', 'x': 'zc', 'y': 'tu',
                'z': 'x',
            }
            c = chars[pos].lower()
            if c in keyboard_adj:
                chars[pos] = random.choice(keyboard_adj[c])
        
        typo_query = "".join(chars)
        return typo_query, query

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Clean duplicated fragments so queries stay readable and distinct."""
        cleaned = " ".join((query or "").split())
        if not cleaned:
            return ""

        parts = cleaned.split()
        deduped: list[str] = []
        for token in parts:
            if deduped and deduped[-1].lower() == token.lower():
                continue
            deduped.append(token)
        parts = deduped

        max_repeat = min(4, len(parts) // 2)
        for size in range(max_repeat, 0, -1):
            if parts[-size:] == parts[-2 * size:-size]:
                parts = parts[:-size]
                break

        normalized = " ".join(parts).strip(" -")
        normalized = re.sub(r"\s+([?.!,])", r"\1", normalized)
        return normalized[:90].strip()

    async def _find_search_box(self, page: Page):
        """Return a visible Bing search box locator when one exists."""
        selectors = [
            "#sb_form_q",
            "input[name='q']",
            "input[type='search']",
            "textarea[name='q']",
            "textarea[type='search']",
            "input[aria-label*='Search' i]",
            "textarea[aria-label*='Search' i]",
        ]

        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() > 0 and await locator.is_visible(timeout=1500):
                    return locator
            except Exception:
                continue

        return None

    async def run_searches(
        self,
        page: Page,
        count: int,
        mode: str = "desktop",
        credit_probe_fn: Optional[Callable] = None,
    ) -> dict:
        """Perform searches with variable timing and patterns.
        
        Args:
            credit_probe_fn: Optional async callable that returns current credit
                points (int). Called after search #3 to detect if the platform
                is crediting our searches. If it returns 0, we abort early.
        """
        logger.info(f"Starting {count} {mode} searches...")
        self._current_mode = mode
        self._slow_human_mode = getattr(self, '_slow_human_mode', False)
        stats = {"completed": 0, "failed": 0, "queries": [], "fatal_error": "", "early_abort": False}
        seen_queries: set[str] = set()
        _consecutive_closed = 0  # Guard: abort if page is persistently closed
        _consecutive_failures = 0  # Recovery: slow down after consecutive failures

        if self.settings.get("use_google_trends", True):
            await self.trends.fetch_trending()

        queries = self.trends.get_batch_queries(count)
        delay_lo, delay_hi = self._search_delay_bounds()
        next_break_at = self._session_break_interval()

        for i, raw_query in enumerate(queries):
            try:
                query = self._normalize_query(raw_query)
                if not query:
                    query = f"bing search topic {i + 1}"

                dedupe_key = query.casefold()
                suffix_idx = 2
                while dedupe_key in seen_queries:
                    query = self._normalize_query(f"{query} update {suffix_idx}")
                    dedupe_key = query.casefold()
                    suffix_idx += 1
                seen_queries.add(dedupe_key)

                # Choose search method
                # Mobile Rewards appears to credit more reliably when the query is
                # submitted through the visible search box instead of direct URL
                # navigation. We still keep URL-direct for desktop as a mixed path.
                if mode == "mobile":
                    method = "searchbox"
                else:
                    method = random.choices(
                        ["searchbox", "url_direct"],
                        weights=[55, 45],
                    )[0]

                if method == "url_direct":
                    success = await self._search_via_url(page, query, i + 1, count)
                else:
                    success = await self._search_via_box(page, query, i + 1, count)

                if success:
                    _consecutive_closed = 0  # Reset on success
                    _consecutive_failures = 0  # Reset on success
                    stats["completed"] += 1
                    stats["queries"].append(query)
                else:
                    stats["failed"] += 1
                    _consecutive_failures += 1
                    # Enable slow_human mode after 2 consecutive failures
                    if _consecutive_failures >= 2 and not self._slow_human_mode:
                        self._slow_human_mode = True
                        logger.warning(
                            f"2+ consecutive search failures — activating slow_human mode (doubling delays)"
                        )

                # Clean up leftover tabs after each search
                await close_other_tabs(page)

                if self.on_progress:
                    self.on_progress(i + 1, count, query)

                # ─── Credit probe after search #3 ─────
                if credit_probe_fn and (i + 1) == 3:
                    try:
                        current_credits = await credit_probe_fn()
                        if current_credits == 0:
                            logger.warning(
                                f" {mode} credits still 0 after 3 searches — "
                                f"platform not crediting, aborting early"
                            )
                            stats["early_abort"] = True
                            break
                        else:
                            logger.info(
                                f" {mode} credit probe OK: {current_credits} pts after 3 searches"
                            )
                    except Exception as e:
                        logger.debug(f"Credit probe failed: {e}")

                # ─── Variable delay between searches ─────
                # Apply slow_human multiplier if active (doubles all delays)
                slow_mult = 2.0 if self._slow_human_mode else 1.0
                if i < count - 1:
                    if (i + 1) >= next_break_at:
                        await self._session_break(page)
                        next_break_at = (i + 1) + self._session_break_interval()
                    else:
                        await self.humanizer.random_delay(delay_lo * slow_mult, delay_hi * slow_mult)

                    # Occasionally simulate tab-switch
                    if random.random() < 0.08:
                        await self.humanizer.simulate_tab_switch(page)

            except SafetyStopError as e:
                stats["fatal_error"] = str(e)
                logger.warning(stats["fatal_error"])
                if self.on_error:
                    self.on_error(stats["fatal_error"])
                break
            except Exception as e:
                err_str = str(e).lower()
                # Detect persistent "page closed" zombie loop and abort early
                is_closed_err = any(kw in err_str for kw in (
                    "target page, context or browser has been closed",
                    "execution context was destroyed",
                    "target closed",
                    "session closed",
                ))
                if is_closed_err:
                    _consecutive_closed += 1
                    if _consecutive_closed >= 5:
                        msg = (
                            f"Aborting {mode} searches: browser context closed "
                            f"for 5+ consecutive searches (page is gone). "
                            f"Completed: {stats['completed']}/{count}"
                        )
                        logger.error(msg)
                        stats["fatal_error"] = msg
                        if self.on_error:
                            self.on_error(msg)
                        break
                else:
                    _consecutive_closed = 0  # Reset on non-closed error

                logger.error(f"Search {i + 1}/{count} failed: {e}")
                stats["failed"] += 1
                if self.on_error:
                    self.on_error(str(e))
                await self.humanizer.short_delay()

        logger.info(f"Searches done: {stats['completed']}/{count} OK, {stats['failed']} failed")
        if stats["fatal_error"]:
            return stats

        # Skip deficit retry for mobile (navigating to rewards.bing.com breaks emulation)
        if mode == "mobile":
            return stats

        return await self._finalize_search_stats(
            page,
            mode,
            stats,
            delay_lo,
            delay_hi,
        )

    async def _finalize_search_stats(
        self,
        page: Page,
        mode: str,
        stats: dict,
        delay_lo: float,
        delay_hi: float,
    ) -> dict:
        """Retry small deficits so a single run is more likely to finish credits."""
        try:
            # Increased to 15 to accommodate slow Microsoft tracking updates and ensure full points
            max_rounds = max(1, int(self.settings.get("search_deficit_rounds", 15)))
            max_extra_per_round = max(
                3,
                int(self.settings.get("search_deficit_max_extra", 15)),
            )

            for round_idx in range(max_rounds):
                logger.info(f"Checking for any missing points (Attempt {round_idx + 1}/{max_rounds})...")
                await asyncio.sleep(8)
                status_after = await self.get_search_points_status(page)

                if mode == "desktop":
                    current = status_after.get("pc_current", 0)
                    maximum = status_after.get("pc_max", 0)
                elif mode == "mobile":
                    current = status_after.get("mobile_current", 0)
                    maximum = status_after.get("mobile_max", 0)
                else:
                    current = maximum = 0

                deficit_pts = max(0, maximum - current)
                deficit_searches = (deficit_pts + 2) // 3
                if deficit_searches <= 0:
                    break

                batch_size = min(deficit_searches, max_extra_per_round)
                logger.info(
                    f"Search deficit: {current}/{maximum} pts "
                    f"({deficit_searches} more needed, round {round_idx + 1}/{max_rounds})"
                )

                extra_queries = self.trends.get_batch_queries(batch_size + 3)
                done_extra = 0
                for eq in extra_queries:
                    if done_extra >= batch_size:
                        break

                    eq = self._normalize_query(eq)
                    if not eq:
                        continue

                    try:
                        success = await self._search_via_url(
                            page,
                            eq,
                            done_extra + 1,
                            batch_size,
                        )
                        if success:
                            done_extra += 1
                            stats["completed"] += 1
                        await self.humanizer.random_delay(delay_lo, delay_hi)
                    except SafetyStopError as e:
                        stats["fatal_error"] = str(e)
                        logger.warning(stats["fatal_error"])
                        return stats
                    except Exception:
                        pass

                logger.info(f"Supplementary searches: {done_extra}/{batch_size}")
        except Exception as e:
            logger.debug(f"Deficit check failed: {e}")

        return stats

    @retry(max_retries=2, delay=2)
    async def _search_via_box(self, page: Page, query: str, cur: int, total: int) -> bool:
        """Search by typing into Bing's search box."""
        logger.info(f"[{cur}/{total}] Search (box): \"{query}\"")

        try:
            if "bing.com" not in page.url.lower():
                await self._safe_navigate(page, BING_HOME_URL)
                await self.humanizer.short_delay()
            else:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.3, 0.8))

            await self._check_safety_signals(page)
            await self.humanizer.before_search(page)

            # Find search box
            sb = await self._find_search_box(page)
            if sb is None:
                await self._safe_navigate(page, BING_HOME_URL)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                await self._check_safety_signals(page)
                sb = await self._find_search_box(page)

            if sb is None:
                logger.info("Search box not available, falling back to direct URL search")
                return await self._search_via_url(page, query, cur, total)

            try:
                await sb.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            try:
                await sb.focus()
            except Exception:
                try:
                    await sb.evaluate("(el) => { el.focus(); if (el.select) el.select(); }")
                except Exception:
                    logger.info("Search box focus failed, falling back to direct URL search")
                    return await self._search_via_url(page, query, cur, total)

            try:
                await sb.fill("")
            except Exception:
                try:
                    await page.keyboard.press("Control+a")
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                    await page.keyboard.press("Backspace")
                except Exception:
                    logger.info("Search box clear failed, falling back to direct URL search")
                    return await self._search_via_url(page, query, cur, total)
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Type query (with possible typo simulation)
            typo_query, correct_query = self._add_typo(query)
            await self.humanizer.type_text_direct(page, typo_query)
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # If we made a typo, pause, select all, retype correct
            if typo_query != correct_query:
                await asyncio.sleep(random.uniform(0.5, 1.0))  # "notice" the typo
                await page.keyboard.press("Control+a")
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await self.humanizer.type_text_direct(page, correct_query)
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # Submit: mostly Enter (95%) — button is often hidden
            if random.random() < 0.95:
                await page.keyboard.press("Enter")
            else:
                try:
                    btn = page.locator('#sb_form_go:visible, #search_icon:visible')
                    if await btn.count() > 0:
                        await btn.first.click(timeout=3000)
                    else:
                        await page.keyboard.press("Enter")
                except Exception:
                    await page.keyboard.press("Enter")

            await page.wait_for_load_state("domcontentloaded", timeout=35000)
            await asyncio.sleep(1)
            await self._check_safety_signals(page)

            # Post-search reading
            await self.humanizer.after_search(page)

            # Interact with results sometimes
            await self._maybe_interact(page)

            return True

        except Exception as e:
            logger.warning(f"Box search failed: {e}")
            try:
                logger.info("Falling back to direct URL search after box interaction failure")
                return await self._search_via_url(page, query, cur, total)
            except Exception:
                raise

    @retry(max_retries=2, delay=2)
    async def _search_via_url(self, page: Page, query: str, cur: int, total: int) -> bool:
        """Search by navigating directly to search URL (like typing in address bar)."""
        logger.info(f"[{cur}/{total}] Search (url): \"{query}\"")

        try:
            encoded = urllib.parse.quote_plus(query)
            # Use platform-specific form params so Bing credits mobile vs desktop
            if self._current_mode == "mobile":
                suffix = random.choice([
                    "&form=EDGEAR",   # Edge Android search
                    "&form=EDGSPA",   # Edge search page Android
                    "&form=EDGEAR&qs=n",
                    "&form=EDGSPA&qs=AS",
                ])
            else:
                suffix = random.choice(["", "&form=QBLH", "&qs=n", "&form=QBRE"])
            url = f"{BING_SEARCH_URL}?q={encoded}{suffix}"

            await self._safe_navigate(page, url)
            await asyncio.sleep(random.uniform(1, 2))
            await self._check_safety_signals(page)

            # Read results
            await self.humanizer.after_search(page)
            await self._maybe_interact(page)

            return True

        except Exception as e:
            logger.warning(f"URL search failed: {e}")
            raise

    async def _maybe_interact(self, page: Page) -> None:
        """Random interactions with search results — realistic rates.
        
        Real users interact with results ~70% of the time:
        - ~35% click a result, read, come back
        - ~15% scroll & hover results (reading snippets)
        - ~10% click related searches
        - ~5% go to page 2
        - ~5% just deep scroll
        - ~30% just skim and move on
        """
        roll = random.random()

        if roll < 0.35:
            # Click a result, spend time reading, go back
            await self._click_random_result(page)
        elif roll < 0.50:
            # Hover over results (reading snippets without clicking)
            hover_count = random.randint(1, 3)
            for _ in range(hover_count):
                await self._hover_result(page)
                await asyncio.sleep(random.uniform(0.5, 1.5))
        elif roll < 0.60:
            # Click related searches (natural exploration)
            await self._click_related(page)
        elif roll < 0.65:
            # Scroll to page 2 of results
            await self._browse_page2(page)
        elif roll < 0.70:
            # Deep scroll through results
            for _ in range(random.randint(2, 4)):
                await self.humanizer.natural_scroll(page, "down", random.randint(300, 600))
                await asyncio.sleep(random.uniform(1, 3))
            # Sometimes scroll back up
            if random.random() < 0.3:
                await self.humanizer.natural_scroll(page, "up", random.randint(200, 400))

    async def _browse_page2(self, page: Page) -> None:
        """Navigate to page 2 of search results (natural curiosity)."""
        try:
            next_btn = page.locator('a.sb_pagN, a[title="Next page"], nav[aria-label="pagination"] a')
            cnt = await next_btn.count()
            if cnt > 0:
                await next_btn.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(random.uniform(2, 5))
                await self.humanizer.natural_scroll(page, "down", random.randint(200, 500))
                await asyncio.sleep(random.uniform(1, 3))
                # Go back to page 1
                await page.go_back()
                await asyncio.sleep(random.uniform(0.5, 1.5))
        except Exception:
            pass

    async def _click_random_result(self, page: Page) -> None:
        """Click on a search result, spend realistic time reading, then go back."""
        try:
            results = page.locator("#b_results .b_algo h2 a")
            count = await results.count()
            if count > 0:
                # Prefer top results (more natural)
                weights = [max(1, 10 - i * 2) for i in range(count)]
                idx = random.choices(range(count), weights=weights[:count])[0]

                # Bezier mouse approach
                box = await results.nth(idx).bounding_box()
                if box:
                    await self.humanizer.bezier_move(
                        page,
                        int(box["x"] + box["width"] / 2),
                        int(box["y"] + box["height"] / 2),
                    )
                    await asyncio.sleep(random.uniform(0.1, 0.3))

                await results.nth(idx).click()

                # Simulate realistic reading time
                read_time = random.choices(
                    [random.uniform(3, 8),     # Quick skim
                     random.uniform(8, 15),    # Normal read
                     random.uniform(15, 25)],  # Deep read (article)
                    weights=[50, 35, 15],
                )[0]

                # Scroll through the page naturally
                scroll_count = random.randint(1, 4)
                per_scroll_time = read_time / (scroll_count + 1)
                for _ in range(scroll_count):
                    await asyncio.sleep(per_scroll_time)
                    await self.humanizer.natural_scroll(page, "down", random.randint(150, 500))
                    # Random mouse movement while "reading"
                    if random.random() < 0.3:
                        await self.humanizer.random_mouse_move(page)

                await asyncio.sleep(per_scroll_time)  # Final reading pause

                # Go back to search results
                await page.go_back()
                await asyncio.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass

    async def _hover_result(self, page: Page) -> None:
        """Hover over a result without clicking (reading snippets)."""
        try:
            results = page.locator("#b_results .b_algo")
            cnt = await results.count()
            if cnt > 0:
                idx = random.randint(0, min(cnt - 1, 3))
                box = await results.nth(idx).bounding_box()
                if box:
                    await self.humanizer.bezier_move(
                        page,
                        int(box["x"] + box["width"] / 2),
                        int(box["y"] + box["height"] / 4),
                    )
                    await asyncio.sleep(random.uniform(0.5, 2))
        except Exception:
            pass

    async def _click_related(self, page: Page) -> None:
        """Sometimes click on 'related searches' for natural browsing."""
        try:
            related = page.locator(".b_rs a, .b_ans .b_suggestionList a")
            cnt = await related.count()
            if cnt > 0:
                idx = random.randint(0, min(cnt - 1, 3))
                await related.nth(idx).click()
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(random.uniform(2, 5))
                await self.humanizer.natural_scroll(page, "down")
                await asyncio.sleep(random.uniform(1, 2))
        except Exception:
            pass

    async def _session_break(self, page: Page) -> None:
        """Take a realistic session break — visit other pages like a real user."""
        break_type = random.choices(
            ["bing_news", "bing_weather", "bing_images", "just_wait", "homepage"],
            weights=[25, 15, 15, 30, 15],
        )[0]

        min_break = float(self.settings.get("search_break_min", 8))
        max_break = float(self.settings.get("search_break_max", 20))
        if max_break < min_break:
            max_break = min_break
        break_duration = random.uniform(min_break, max_break)
        logger.debug(f"Session break ({break_type}, {break_duration:.0f}s)")

        try:
            if break_type == "bing_news":
                await page.goto("https://www.bing.com/news", wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(random.uniform(3, 8))
                await self._check_safety_signals(page)
                await self.humanizer.natural_scroll(page, "down", random.randint(200, 500))
                await asyncio.sleep(random.uniform(2, 5))
            elif break_type == "bing_weather":
                await page.goto("https://www.bing.com/search?q=weather", wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(random.uniform(3, 10))
                await self._check_safety_signals(page)
            elif break_type == "bing_images":
                await page.goto("https://www.bing.com/images/trending", wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(random.uniform(3, 8))
                await self._check_safety_signals(page)
                await self.humanizer.natural_scroll(page, "down", random.randint(300, 600))
            elif break_type == "homepage":
                await page.goto(BING_HOME_URL, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(random.uniform(2, 5))
                await self._check_safety_signals(page)
            else:  # just_wait
                pass

            # Random mouse movements during break
            if random.random() < 0.5:
                await self.humanizer.random_mouse_move(page)

            remaining = max(0, break_duration - 10)
            if remaining > 0:
                await asyncio.sleep(remaining)

        except SafetyStopError:
            raise
        except Exception:
            await asyncio.sleep(break_duration)

    async def get_search_points_status(self, page: Page) -> dict:
        """Read search points via in-page fetch across multiple Rewards surfaces."""
        try:
            rewards_surfaces: list[str] = []
            current_url = str(getattr(page, "url", "") or "")
            if "rewards.bing.com" in current_url:
                rewards_surfaces.append(current_url)
            rewards_surfaces.extend([
                "https://rewards.bing.com/dashboard",
                "https://rewards.bing.com/earn",
                "https://rewards.bing.com/",
                "https://rewards.bing.com/about",
            ])

            merged_status = self._empty_status() | {"total_points": 0}
            seen_urls: set[str] = set()

            for rewards_url in rewards_surfaces:
                if not rewards_url or rewards_url in seen_urls:
                    continue
                seen_urls.add(rewards_url)

                max_nav_retries = 3
                for nav_attempt in range(max_nav_retries):
                    try:
                        if page.url != rewards_url:
                            await page.goto(
                                rewards_url,
                                wait_until="domcontentloaded",
                                timeout=20000,
                            )
                            await asyncio.sleep(2)
                        if "rewards.bing.com" in page.url:
                            break
                    except Exception as nav_err:
                        logger.debug(
                            f"Rewards nav attempt {nav_attempt + 1}/{max_nav_retries} failed: {nav_err}"
                        )
                        if nav_attempt < max_nav_retries - 1:
                            await asyncio.sleep(2)
                        else:
                            raise

                data = None
                for attempt in range(3):
                    try:
                        data = await page.evaluate("""
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
                        if data:
                            break
                    except Exception as exc:
                        if attempt < 2 and (
                            "Execution context was destroyed" in str(exc)
                            or "navigat" in str(exc).lower()
                        ):
                            logger.debug(
                                f"API fetch attempt {attempt + 1}/3 failed: {exc}, retrying..."
                            )
                            try:
                                await page.wait_for_load_state(
                                    "domcontentloaded",
                                    timeout=8000,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                            continue
                        raise

                if not data:
                    logger.debug(f"In-page API fetch returned null on {rewards_url}")
                    continue

                surface_status = self._status_from_rewards_payload(data)
                merged_status = self._merge_search_status(merged_status, surface_status)

                if self._has_resolved_search_counter(merged_status):
                    # Keep probing other surfaces only if a track is still unresolved.
                    if (
                        merged_status.get("pc_max", 0) > 0
                        and merged_status.get("mobile_max", 0) > 0
                    ) or rewards_url.endswith("/about"):
                        break

            logger.info(f"Search points: {merged_status}")
            return merged_status

        except Exception as e:
            logger.warning(f"Points status check failed: {e}")

        return self._empty_status()

    @classmethod
    def _status_from_rewards_payload(cls, data: dict) -> dict:
        """Extract search counters from a Rewards API payload."""
        dashboard = data.get("dashboard", {})
        user_status = dashboard.get("userStatus", {})
        counters = user_status.get("counters", {})

        counter_keys = list(counters.keys())
        logger.debug(f"RAW counter keys: {counter_keys}")
        for ck in counter_keys:
            cv = counters[ck]
            if isinstance(cv, list) and cv:
                cv = cv[0]
            if isinstance(cv, dict):
                logger.debug(f"  counter[{ck}] = {cv.get('pointProgress', 0)}/{cv.get('pointProgressMax', 0)}")

        status = {
            "pc_current": 0,
            "pc_max": 0,
            "mobile_current": 0,
            "mobile_max": 0,
            "edge_current": 0,
            "edge_max": 0,
            "total_points": user_status.get("availablePoints", 0),
        }
        status["pc_current"], status["pc_max"] = cls._extract_counter_progress(
            counters,
            exact_keys=("pcSearch", "desktopSearch"),
            required_tokens=("search", "pc"),
        )
        status["mobile_current"], status["mobile_max"] = cls._extract_counter_progress(
            counters,
            exact_keys=("mobileSearch",),
            required_tokens=("search", "mobile"),
        )
        status["edge_current"], status["edge_max"] = cls._extract_counter_progress(
            counters,
            exact_keys=("edgeSearch", "edgeBonusSearch", "edgeSearchBonus"),
            required_tokens=("search", "edge"),
        )
        return status

    @staticmethod
    def _merge_search_status(base_status: dict, candidate_status: dict) -> dict:
        """Merge two search status snapshots, keeping the strongest evidence per track."""
        merged = dict(base_status or {})
        for current_key, max_key in (
            ("pc_current", "pc_max"),
            ("mobile_current", "mobile_max"),
            ("edge_current", "edge_max"),
        ):
            base_pair = (
                int(merged.get(current_key, 0) or 0),
                int(merged.get(max_key, 0) or 0),
            )
            candidate_pair = (
                int(candidate_status.get(current_key, 0) or 0),
                int(candidate_status.get(max_key, 0) or 0),
            )
            if candidate_pair[1] > base_pair[1] or (
                candidate_pair[1] == base_pair[1]
                and candidate_pair[0] > base_pair[0]
            ):
                merged[current_key], merged[max_key] = candidate_pair
        merged["total_points"] = max(
            int(merged.get("total_points", 0) or 0),
            int(candidate_status.get("total_points", 0) or 0),
        )
        return merged

    @staticmethod
    def _has_resolved_search_counter(status: dict) -> bool:
        """Return True when at least one search track exposes a non-zero maximum."""
        return any(
            int(status.get(key, 0) or 0) > 0
            for key in ("pc_max", "mobile_max", "edge_max")
        )

    @staticmethod
    def _extract_counter_progress(
        counters: dict,
        exact_keys: tuple[str, ...],
        required_tokens: tuple[str, ...],
    ) -> tuple[int, int]:
        """Read a counter from the Rewards API using exact keys first, then fuzzy key matching."""
        for key in exact_keys:
            progress = Searcher._normalize_counter(counters.get(key))
            if progress is not None:
                return progress

        for key, value in counters.items():
            normalized_key = key.lower()
            if all(token in normalized_key for token in required_tokens):
                progress = Searcher._normalize_counter(value)
                if progress is not None:
                    return progress

        return 0, 0

    @staticmethod
    def _normalize_counter(counter_value) -> Optional[tuple[int, int]]:
        """Normalize Rewards API counter payloads that may arrive as dicts or single-item lists."""
        if isinstance(counter_value, list):
            counter_value = counter_value[0] if counter_value else None

        if not isinstance(counter_value, dict):
            return None

        return (
            counter_value.get("pointProgress", 0),
            counter_value.get("pointProgressMax", 0),
        )

    @staticmethod
    def _empty_status() -> dict:
        return {
            "pc_current": 0, "pc_max": 0,
            "mobile_current": 0, "mobile_max": 0,
            "edge_current": 0, "edge_max": 0,
        }
