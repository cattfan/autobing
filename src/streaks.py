"""
Streak automation for Microsoft Rewards.
- Bing App Streak: Check in to Bing App daily (mobile visit)
- Edge Browsing Streak: Browse with Edge for 30 minutes daily
- Task Detection: Read Rewards API to find incomplete tasks
"""

import asyncio
import json
import random
import re
from typing import Optional

from playwright.async_api import Page, BrowserContext

from src.utils import (
    logger,
    BING_HOME_URL,
    REWARDS_URL,
    retry,
    select_active_daily_set_items,
)
from src.humanizer import Humanizer

# ─── Rewards API ──────────────────────────────────────────────────────────

REWARDS_API_URL = "https://rewards.bing.com/api/getuserinfo?type=1"

# Websites for Edge Streak — MUST be bing.com domains (Microsoft tracks these)
BROWSE_SITES = [
    "https://www.bing.com",
    "https://www.bing.com/news",
    "https://www.bing.com/images/trending",
    "https://www.bing.com/videos",
    "https://www.bing.com/maps",
    "https://www.bing.com/travel",
    "https://www.bing.com/shop",
    "https://www.bing.com/search?q=weather+today",
    "https://www.bing.com/search?q=latest+news",
    "https://www.bing.com/search?q=best+recipes",
    "https://www.bing.com/search?q=sports+scores+today",
    "https://www.bing.com/search?q=movie+reviews+2026",
    "https://www.bing.com/search?q=technology+news",
    "https://www.bing.com/search?q=how+to+cook+pasta",
    "https://www.bing.com/search?q=fitness+tips",
    "https://www.bing.com/search?q=travel+destinations",
    "https://www.bing.com/search?q=book+recommendations",
    "https://www.bing.com/search?q=home+improvement+ideas",
]


class TaskDetector:
    """Reads Rewards dashboard API to detect incomplete tasks."""

    @staticmethod
    def _emit_debug(debug_log, level: str, message: str) -> None:
        """Send optional diagnostics to the dashboard log without breaking callers."""
        if debug_log:
            try:
                debug_log(level, message)
                return
            except Exception as e:
                logger.debug(f"Debug callback failed: {e}")

        if level == "warning":
            logger.warning(message)
        elif level == "debug":
            logger.debug(message)
        else:
            logger.info(message)

    @staticmethod
    def _summarize_edge_promo(promo: dict) -> dict:
        """Keep raw Edge-related promo diagnostics compact and predictable."""
        attributes = promo.get("attributes", {}) or {}
        return {
            "title": promo.get("title", ""),
            "name": promo.get("name", ""),
            "offerId": promo.get("offerId", ""),
            "hash": promo.get("hash", ""),
            "destinationUrl": promo.get("destinationUrl", ""),
            "complete": promo.get("complete", False),
            "pointProgress": promo.get("pointProgress", 0),
            "pointProgressMax": promo.get("pointProgressMax", 0),
            "attributesType": attributes.get("type", ""),
        }

    @staticmethod
    def _looks_like_edge_promo(promo: dict) -> bool:
        """Capture promos that mention Edge even if the streak heuristic misses them."""
        haystack = " ".join([
            promo.get("title", "") or "",
            promo.get("name", "") or "",
            promo.get("destinationUrl", "") or "",
        ]).lower()
        return "edge" in haystack

    @staticmethod
    def _extract_edge_card_excerpt(page_text: str) -> str:
        """Pull a small excerpt around the first Edge-related line from rendered DOM text."""
        if not page_text:
            return ""

        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        for idx, line in enumerate(lines):
            lowered = line.lower()
            if "edge" in lowered and (
                "brows" in lowered or "streak" in lowered or "minute" in lowered
            ):
                start = max(0, idx - 2)
                end = min(len(lines), idx + 3)
                excerpt = " | ".join(lines[start:end])
                return excerpt[:500]
        return ""

    @staticmethod
    def _parse_card_progress(page_text: str) -> dict[str, tuple[int, int] | None]:
        """Extract streak and daily-set progress from rendered card text."""
        if not page_text:
            return {"daily_set": None, "edge": None, "bing_app": None}

        edge_match = re.search(
            r"Edge(?:\s+Browsing(?:\s+Streak)?)?.*?Minutes:\s*(\d+)\s*/\s*(\d+)",
            page_text,
            re.IGNORECASE | re.DOTALL,
        )
        bing_app_match = re.search(
            r"(?:Mobile\s+App|Bing\s+App(?:\s+Streak)?).*?Check-?in:\s*(\d+)\s*/\s*(\d+)",
            page_text,
            re.IGNORECASE | re.DOTALL,
        )
        daily_set_match = re.search(
            r"Daily\s+Set(?:\s+Streak)?.*?Activit(?:y|ies):\s*(\d+)\s*/\s*(\d+)",
            page_text,
            re.IGNORECASE | re.DOTALL,
        )

        def _pair(match):
            if not match:
                return None
            return int(match.group(1)), int(match.group(2))

        return {
            "daily_set": _pair(daily_set_match),
            "edge": _pair(edge_match),
            "bing_app": _pair(bing_app_match),
        }

    @staticmethod
    async def get_all_tasks(
        page: Page,
        *,
        edge_debug_label: str = "",
        debug_log=None,
        include_edge_diagnostics: bool = False,
    ) -> dict:
        """
        Fetch complete task status from Rewards API.

        Returns dict with:
            - searches: {pc_current, pc_max, mobile_current, mobile_max}
            - daily_set: {completed, total}
            - streaks: {bing_app: {current, done}, edge: {minutes, done}}
            - more_activities: {completed, total}
            - total_points: int
            - level: str
        """
        if edge_debug_label:
            include_edge_diagnostics = True

        result = {
            "searches": {
                "pc_current": 0, "pc_max": 0,
                "mobile_current": 0, "mobile_max": 0,
                "edge_current": 0, "edge_max": 0,
            },
            "daily_set": {"completed": 0, "total": 0},
            "streaks": {
                "bing_app": {"current": 0, "done": False},
                "edge": {
                    "minutes": 0,
                    "target": 30,
                    "done": False,
                    "offerId": "",
                    "hash": "",
                    "name": "",
                    "destinationUrl": "",
                    "debugPromos": [],
                    "domSnapshots": [],
                },
            },
            "more_activities": {"completed": 0, "total": 0},
            "total_points": 0,
            "level": "",
        }

        try:
            # Visit Rewards page normally first (natural navigation)
            current_url = page.url
            if "rewards.bing.com" not in current_url:
                await page.goto(
                    "https://rewards.bing.com/",
                    wait_until="domcontentloaded", timeout=15000,
                )
                await asyncio.sleep(2)

            # Call API from within page context (same as page's own JS)
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

            if not data:
                logger.warning("API returned no data")
                return result

            dashboard = data.get("dashboard", {})
            user_status = dashboard.get("userStatus", {})

            # ── Total Points & Level ──
            result["total_points"] = user_status.get("availablePoints", 0)
            result["level"] = user_status.get("levelInfo", {}).get("activeLevel", "")

            # ── Search Counters ──
            counters = user_status.get("counters", {})

            if "pcSearch" in counters and counters["pcSearch"]:
                pc = counters["pcSearch"][0]
                result["searches"]["pc_current"] = pc.get("pointProgress", 0)
                result["searches"]["pc_max"] = pc.get("pointProgressMax", 0)

            if "mobileSearch" in counters and counters["mobileSearch"]:
                mob = counters["mobileSearch"][0]
                result["searches"]["mobile_current"] = mob.get("pointProgress", 0)
                result["searches"]["mobile_max"] = mob.get("pointProgressMax", 0)

            # ── Daily Set ──
            daily_sets = dashboard.get("dailySetPromotions", {})
            for item in select_active_daily_set_items(daily_sets):
                result["daily_set"]["total"] += 1
                if item.get("complete", False) or item.get("pointProgress", 0) >= item.get("pointProgressMax", 1):
                    result["daily_set"]["completed"] += 1

            # ── More Activities / Promotions ──
            more_promos = dashboard.get("morePromotions", [])
            for promo in more_promos:
                result["more_activities"]["total"] += 1
                if promo.get("complete", False) or promo.get("pointProgress", 0) >= promo.get("pointProgressMax", 1):
                    result["more_activities"]["completed"] += 1

                # Detect streak tasks
                title = (promo.get("title", "") or promo.get("name", "")).lower()
                attributes = promo.get("attributes", {})

                if TaskDetector._looks_like_edge_promo(promo):
                    result["streaks"]["edge"]["debugPromos"].append(
                        TaskDetector._summarize_edge_promo(promo)
                    )

                # Bing App Streak
                if "bing" in title and ("app" in title or "streak" in title or "check" in title):
                    progress = promo.get("pointProgress", 0)
                    max_progress = promo.get("pointProgressMax", 1)
                    result["streaks"]["bing_app"]["current"] = progress
                    result["streaks"]["bing_app"]["done"] = progress >= max_progress or promo.get("complete", False)

                # Edge Browsing Streak
                if "edge" in title and ("brows" in title or "minute" in title or "streak" in title):
                    progress = promo.get("pointProgress", 0)
                    max_progress = promo.get("pointProgressMax", 1)
                    result["streaks"]["edge"]["minutes"] = progress
                    result["streaks"]["edge"]["target"] = max_progress if max_progress > 0 else 30
                    result["streaks"]["edge"]["done"] = progress >= max_progress or promo.get("complete", False)
                    # Store promo identifiers for API-based approach
                    result["streaks"]["edge"]["offerId"] = promo.get("offerId", "")
                    result["streaks"]["edge"]["hash"] = promo.get("hash", "")
                    result["streaks"]["edge"]["name"] = promo.get("name", "")
                    result["streaks"]["edge"]["destinationUrl"] = promo.get("destinationUrl", "")
                    # Log full promo data for debugging
                    logger.info(
                        f"Edge Streak promo: offerId='{promo.get('offerId', '')}', "
                        f"hash='{promo.get('hash', '')}', name='{promo.get('name', '')}', "
                        f"progress={progress}/{max_progress}, "
                        f"attributes={attributes}, "
                        f"destUrl='{promo.get('destinationUrl', '')}'"
                    )

            # New Rewards UI exposes some progress only in rendered cards.
            need_dom_progress = (
                result["daily_set"]["total"] == 0
                or (
                    not result["streaks"]["edge"]["done"]
                    and result["streaks"]["edge"]["minutes"] == 0
                )
                or (
                    not result["streaks"]["bing_app"]["done"]
                    and result["streaks"]["bing_app"]["current"] == 0
                )
            )
            if need_dom_progress or include_edge_diagnostics:
                pages_to_probe = []
                for rewards_url in [
                    page.url,
                    "https://rewards.bing.com/pointsbreakdown",
                    "https://rewards.bing.com/earn",
                ]:
                    if rewards_url not in pages_to_probe:
                        pages_to_probe.append(rewards_url)

                for rewards_url in pages_to_probe:
                    try:
                        if page.url != rewards_url:
                            await page.goto(
                                rewards_url,
                                wait_until="domcontentloaded",
                                timeout=15000,
                            )
                            await asyncio.sleep(2)
                        page_text = await page.locator("body").inner_text(timeout=5000)
                    except Exception:
                        continue

                    card_progress = TaskDetector._parse_card_progress(page_text)
                    edge_excerpt = TaskDetector._extract_edge_card_excerpt(page_text)

                    if include_edge_diagnostics:
                        edge_progress = card_progress.get("edge")
                        edge_progress_payload = (
                            {
                                "minutes": edge_progress[0],
                                "target": edge_progress[1],
                            }
                            if edge_progress else None
                        )
                        result["streaks"]["edge"]["domSnapshots"].append({
                            "url": rewards_url,
                            "progress": edge_progress_payload,
                            "excerpt": edge_excerpt,
                        })

                    daily_set_progress = card_progress.get("daily_set")
                    if daily_set_progress:
                        current, total = daily_set_progress
                        result["daily_set"]["completed"] = max(
                            result["daily_set"]["completed"], current
                        )
                        result["daily_set"]["total"] = max(
                            result["daily_set"]["total"], total
                        )

                    edge_progress = card_progress.get("edge")
                    if edge_progress:
                        minutes, target = edge_progress
                        result["streaks"]["edge"]["minutes"] = max(
                            result["streaks"]["edge"]["minutes"], minutes
                        )
                        result["streaks"]["edge"]["target"] = max(
                            result["streaks"]["edge"]["target"], target
                        )
                        result["streaks"]["edge"]["done"] = (
                            result["streaks"]["edge"]["minutes"]
                            >= result["streaks"]["edge"]["target"]
                        )

                    bing_app_progress = card_progress.get("bing_app")
                    if bing_app_progress:
                        current, target = bing_app_progress
                        result["streaks"]["bing_app"]["current"] = max(
                            result["streaks"]["bing_app"]["current"], current
                        )
                        result["streaks"]["bing_app"]["done"] = (
                            result["streaks"]["bing_app"]["current"] >= target
                        )

                    if (
                        result["daily_set"]["total"] > 0
                        and (
                            result["streaks"]["edge"]["done"]
                            or result["streaks"]["edge"]["minutes"] > 0
                        )
                        and (
                            result["streaks"]["bing_app"]["done"]
                            or result["streaks"]["bing_app"]["current"] > 0
                        )
                    ):
                        break

            if include_edge_diagnostics:
                label = edge_debug_label or "snapshot"
                edge_state = result["streaks"]["edge"]
                promos_json = json.dumps(
                    edge_state.get("debugPromos", []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                TaskDetector._emit_debug(
                    debug_log,
                    "info",
                    f"[EdgeDiag:{label}] API edge promos: {promos_json}",
                )
                TaskDetector._emit_debug(
                    debug_log,
                    "info",
                    f"[EdgeDiag:{label}] Edge state: "
                    f"minutes={edge_state.get('minutes', 0)}/"
                    f"{edge_state.get('target', 30)}, "
                    f"done={edge_state.get('done', False)}, "
                    f"offerId='{edge_state.get('offerId', '')}', "
                    f"hash='{edge_state.get('hash', '')}', "
                    f"destinationUrl='{edge_state.get('destinationUrl', '')}'",
                )
                for snapshot in edge_state.get("domSnapshots", []):
                    excerpt = snapshot.get("excerpt", "") or "<no edge card excerpt>"
                    TaskDetector._emit_debug(
                        debug_log,
                        "info",
                        f"[EdgeDiag:{label}] DOM {snapshot.get('url')}: "
                        f"progress={snapshot.get('progress')}, excerpt={excerpt}",
                    )

            logger.info(
                f"Tasks detected — PC: {result['searches']['pc_current']}/{result['searches']['pc_max']}, "
                f"Mobile: {result['searches']['mobile_current']}/{result['searches']['mobile_max']}, "
                f"Daily: {result['daily_set']['completed']}/{result['daily_set']['total']}, "
                f"BingApp: {'✅' if result['streaks']['bing_app']['done'] else '❌'}, "
                f"Edge: {'✅' if result['streaks']['edge']['done'] else '❌'}"
            )

        except Exception as e:
            logger.warning(f"Task detection failed: {e}")

        return result

class BingAppStreak:
    """
    Bing App Streak completion.

    How it works: Microsoft checks visits from the Bing mobile app.
    The real Bing App uses the "BingSapphire" identifier in User-Agent.
    We simulate this by visiting bing.com with a real Bing App UA,
    then visiting the rewards activity page to register the check-in.
    """

    # Real Bing App user-agents (Android & iOS) — must use "BingSapphire"
    BING_APP_UA = [
        # Android Bing App (real package: com.microsoft.bing)
        "Mozilla/5.0 (Linux; Android 14; SM-S928B Build/UP1A.231005.007) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.0.0 "
        "Mobile Safari/537.36 BingSapphire/25.3.410526303",
        # Android Bing App — Pixel
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro Build/UD1A.231105.004) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.0.0 "
        "Mobile Safari/537.36 BingSapphire/25.3.410526303",
        # iOS Bing App
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1 BingSapphire/25.3.410526303",
    ]

    def __init__(self, humanizer: Humanizer):
        self.humanizer = humanizer

    async def check_in(self, page: Page) -> bool:
        """
        Perform Bing App check-in.

        Steps:
        1. Visit bing.com with Bing App UA (triggers app detection)
        2. Do a search (reinforces app activity)
        3. Visit rewards page and click "Bing App Streak" card directly
        4. Confirm via API
        """
        logger.info("🔥 Starting Bing App Streak check-in...")

        try:
            # 1. Visit Bing homepage (triggers check-in cookie)
            await page.goto(BING_HOME_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(random.uniform(3, 5))

            # 2. Do a quick search (reinforces activity)
            query = random.choice([
                "weather today", "news headlines", "sports scores",
                "recipe ideas", "movie showtimes", "stock market today",
            ])
            sb = page.locator('#sb_form_q, input[name="q"]')
            if await sb.count() > 0:
                await sb.click()
                await sb.fill(query)
                await asyncio.sleep(random.uniform(0.5, 1.0))
                await page.keyboard.press("Enter")
                await asyncio.sleep(random.uniform(3, 6))

            # 3. Visit rewards page and click the Bing App Streak card
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(random.uniform(3, 5))

            # Try to click the Bing App Streak card directly
            streak_clicked = False
            streak_selectors = [
                # Correct selectors from Rewards page DOM (#more-activities section)
                '#more-activities mee-card:has-text("Bing") a',
                '#more-activities mee-card:has-text("Mobile App") a',
                '#more-activities mee-card:has-text("Check-in") a',
                'mee-rewards-more-activities-card-item:has-text("Bing") a',
                # Text-based fallback
                'a:has-text("Bing App Streak")',
                'a:has-text("Bing App")',
                'a:has-text("Mobile App")',
                'a:has-text("Check-in")',
                # Data attribute based
                '[data-bi-id*="BingApp"]',
                '[data-bi-id*="AppStreak"]',
                '[data-bi-name="promotion_item"]:has-text("Bing")',
                # Card structure
                'mee-card:has-text("Bing App") a',
                'mee-card:has-text("Bing Streak") a',
                'mee-card:has-text("Mobile App") a',
            ]
            for sel in streak_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        pages_before = len(page.context.pages)
                        await el.click(timeout=5000)
                        streak_clicked = True
                        logger.debug(f"Clicked Bing App card via: {sel}")
                        await asyncio.sleep(random.uniform(3, 5))

                        current_pages = page.context.pages
                        if len(current_pages) > pages_before:
                            popup = current_pages[-1]
                            try:
                                await popup.wait_for_load_state(
                                    "domcontentloaded",
                                    timeout=15000,
                                )
                                await asyncio.sleep(random.uniform(4, 7))
                                await popup.bring_to_front()
                            except Exception:
                                pass
                        break
                except Exception:
                    continue

            if not streak_clicked:
                logger.debug("Could not find Bing App Streak card, using API fallback")

            # 4. Hit the rewards activity API to confirm check-in
            try:
                await page.evaluate("""
                    async () => {
                        try {
                            await fetch('https://rewards.bing.com/api/getuserinfo?type=1', {
                                credentials: 'include'
                            });
                        } catch(e) {}
                    }
                """)
            except Exception:
                pass

            await asyncio.sleep(2)
            for other_page in list(page.context.pages):
                if other_page is page:
                    continue
                try:
                    await other_page.close()
                except Exception:
                    pass

            # 5. Visit bing.com once more (double-tap for safety)
            await page.goto(BING_HOME_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(random.uniform(2, 4))

            detector = TaskDetector()
            bing_state = {"current": 0, "done": False}
            for attempt in range(3):
                status = await detector.get_all_tasks(page)
                bing_state = status.get("streaks", {}).get("bing_app", {})
                if bing_state.get("done", False):
                    logger.info("✅ Bing App Streak check-in verified")
                    return True

                if attempt < 2:
                    try:
                        await page.goto(
                            "https://rewards.bing.com/earn",
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(random.uniform(3, 5))
                    except Exception:
                        pass

            logger.warning(
                f"⚠️ Bing App Streak not verified "
                f"({bing_state.get('current', 0)}/1)"
            )
            return False

        except Exception as e:
            logger.error(f"❌ Bing App check-in failed: {e}")
            return False


class EdgeBrowsingStreak:
    """
    Edge Browsing Streak completion.

    Browse with Edge for 30 minutes daily. Microsoft tracks this through:
    1. Edge telemetry (browsing activity reported to Microsoft)
    2. Bing.com cookies tracking activity duration
    3. Rewards API heartbeat checks

    KEY: Must periodically visit rewards.bing.com to register browsing heartbeat.
    """

    def __init__(self, humanizer: Humanizer):
        self.humanizer = humanizer

    async def _read_verified_progress(self, page: Page) -> tuple[int, int, bool]:
        """Read Edge streak progress from Rewards after a browsing heartbeat."""
        tasks = await TaskDetector.get_all_tasks(page)
        edge = tasks.get("streaks", {}).get("edge", {})
        minutes = edge.get("minutes", 0)
        target = edge.get("target", 30) or 30
        done = edge.get("done", False) or minutes >= target
        return minutes, target, done

    async def browse(
        self,
        page: Page,
        target_minutes: int = 30,
        on_progress: Optional[callable] = None,
        initial_minutes: int = 0,
        hard_cap_minutes: Optional[int] = None,
    ) -> bool:
        """
        Browse various sites for the target duration.
        Clicks Edge Streak card first, then visits bing.com pages with
        periodic heartbeats via rewards API to register browsing time.
        """
        logger.info(f"🌐 Starting Edge Browsing Streak ({target_minutes} min)...")

        elapsed = 0
        verified_minutes = max(0, initial_minutes)
        verified_target = max(1, target_minutes)
        hard_cap_seconds = (
            max(
                verified_target + 15,
                verified_minutes + max(10, (verified_target - verified_minutes) * 2 + 10),
            )
            if hard_cap_minutes is None
            else max(hard_cap_minutes, verified_target)
        ) * 60
        sites = list(BROWSE_SITES)
        random.shuffle(sites)
        site_idx = 0
        last_heartbeat = 0

        # ═══ STEP 1: Activate Edge Browsing Streak tracking ═══
        try:
            # Method 1: Direct activation URL (most reliable)
            activation_urls = [
                "https://rewards.bing.com/pointsbreakdown",
                "https://rewards.bing.com/earn",
                REWARDS_URL,
            ]
            streak_activated = False

            for act_url in activation_urls:
                try:
                    await page.goto(act_url, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)

                    # Method 2: JS-based card finder (works regardless of DOM structure)
                    try:
                        activated_via_js = await page.evaluate("""
                            () => {
                                // Search all links and clickable elements for Edge-related text
                                const allElements = document.querySelectorAll('a, button, [role="link"], [role="button"], mee-card a');
                                for (const el of allElements) {
                                    const text = (el.textContent || '').toLowerCase();
                                    const href = (el.href || '').toLowerCase();
                                    if ((text.includes('edge') && (text.includes('brows') || text.includes('streak') || text.includes('minute')))
                                        || href.includes('edge') && href.includes('streak')) {
                                        el.click();
                                        return true;
                                    }
                                }
                                // Try shadow DOM elements (mee-card components)
                                const cards = document.querySelectorAll('mee-card, mee-rewards-more-activities-card-item');
                                for (const card of cards) {
                                    const shadow = card.shadowRoot;
                                    const text = (card.textContent || '').toLowerCase();
                                    if (text.includes('edge') && (text.includes('brows') || text.includes('streak'))) {
                                        const link = card.querySelector('a') || (shadow && shadow.querySelector('a'));
                                        if (link) { link.click(); return true; }
                                        card.click();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        if activated_via_js:
                            streak_activated = True
                            logger.info("Activated Edge Streak via JS card finder")
                            await asyncio.sleep(random.uniform(3, 5))
                            break
                    except Exception:
                        pass

                    # Method 3: Playwright selector fallback
                    edge_selectors = [
                        '#more-activities mee-card:has-text("Edge") a',
                        '#more-activities mee-card:has-text("Browsing") a',
                        'mee-rewards-more-activities-card-item:has-text("Edge") a',
                        'a:has-text("Edge Browsing Streak")',
                        'a:has-text("Edge Browsing")',
                        '[data-bi-id*="EdgeBrowsing"]',
                        '[data-bi-id*="EdgeStreak"]',
                        '[data-bi-name="promotion_item"]:has-text("Edge")',
                        'mee-card:has-text("Edge") a',
                        'a:has-text("Edge")',
                    ]
                    for sel in edge_selectors:
                        try:
                            el = page.locator(sel).first
                            if await el.count() > 0:
                                await el.click(timeout=5000)
                                streak_activated = True
                                logger.info(f"Activated Edge Streak card via: {sel}")
                                await asyncio.sleep(random.uniform(3, 5))
                                break
                        except Exception:
                            continue

                    if streak_activated:
                        break
                except Exception:
                    continue

            if not streak_activated:
                logger.warning("Could not activate Edge Streak card — browsing may not be tracked")

        except Exception:
            pass

        # ═══ STEP 2: Browse bing.com pages with periodic heartbeats ═══
        zero_progress_retries = 0  # How many times mid-session retry found 0 min
        max_zero_retries = 8  # Bail out after this many (~20 min) — prevents 70 min loop

        try:
            current_minutes, current_target, streak_done = await self._read_verified_progress(page)
            verified_minutes = max(verified_minutes, current_minutes)
            verified_target = max(verified_target, current_target)
            if streak_done:
                logger.info(
                    f"✅ Edge streak already verified ({verified_minutes}/{verified_target} min)"
                )
                return True
        except Exception:
            pass

        while verified_minutes < verified_target and elapsed < hard_cap_seconds:
            try:
                # Pick a site to visit
                url = sites[site_idx % len(sites)]
                site_idx += 1

                logger.debug(f"Edge browse: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)

                # Simulate natural browsing (20s - 60s per site)
                visit_time = random.randint(20, 60)

                # Scroll naturally (with timeout to prevent hang)
                async def _scroll_task():
                    for _ in range(random.randint(1, 3)):
                        await self.humanizer.natural_scroll(
                            page, "down", random.randint(200, 400)
                        )
                        await asyncio.sleep(random.uniform(1, 4))

                try:
                    await asyncio.wait_for(_scroll_task(), timeout=15)
                except (asyncio.TimeoutError, Exception):
                    pass

                # Occasionally click a link on the page (with timeout)
                if random.random() < 0.2:
                    async def _click_task():
                        links = page.locator("a[href]:visible")
                        cnt = await links.count()
                        if cnt > 2:
                            link_idx = random.randint(0, min(cnt - 1, 10))
                            await links.nth(link_idx).click(timeout=5000)
                            await asyncio.sleep(random.uniform(3, 8))

                    try:
                        await asyncio.wait_for(_click_task(), timeout=10)
                    except (asyncio.TimeoutError, Exception):
                        pass

                # Wait remaining visit time
                remaining_visit = max(3, visit_time - 20)
                await asyncio.sleep(remaining_visit)

                elapsed += visit_time

                # ═══ HEARTBEAT: Visit rewards page every 2 minutes ═══
                time_since_heartbeat = elapsed - last_heartbeat
                if time_since_heartbeat >= 120:  # Every 2 minutes
                    try:
                        logger.debug("Edge streak heartbeat → rewards.bing.com")
                        await page.goto(
                            REWARDS_URL,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(random.uniform(3, 6))

                        # Hit the API for heartbeat
                        await page.evaluate("""
                            async () => {
                                try {
                                    await fetch('https://rewards.bing.com/api/getuserinfo?type=1', {
                                        credentials: 'include'
                                    });
                                } catch(e) {}
                            }
                        """)

                        # Re-activate Edge Streak card via JS
                        try:
                            await page.evaluate("""
                                () => {
                                    const allElements = document.querySelectorAll('a, button, [role="link"], mee-card a');
                                    for (const el of allElements) {
                                        const text = (el.textContent || '').toLowerCase();
                                        if (text.includes('edge') && (text.includes('brows') || text.includes('streak') || text.includes('minute'))) {
                                            el.click(); return true;
                                        }
                                    }
                                    const cards = document.querySelectorAll('mee-card, mee-rewards-more-activities-card-item');
                                    for (const card of cards) {
                                        const text = (card.textContent || '').toLowerCase();
                                        if (text.includes('edge') && (text.includes('brows') || text.includes('streak'))) {
                                            const link = card.querySelector('a');
                                            if (link) { link.click(); return true; }
                                            card.click(); return true;
                                        }
                                    }
                                    return false;
                                }
                            """)
                        except Exception:
                            pass

                        await asyncio.sleep(2)
                        try:
                            current_minutes, current_target, streak_done = (
                                await self._read_verified_progress(page)
                            )
                            verified_minutes = max(verified_minutes, current_minutes)
                            verified_target = max(verified_target, current_target)
                            if streak_done:
                                break

                            # Mid-session zero-progress detection:
                            # If >6 min elapsed but API still says 0, try pointsbreakdown page
                            if elapsed > 360 and current_minutes == 0:
                                zero_progress_retries += 1

                                # EARLY BAIL-OUT: If 0 min after multiple retries,
                                # the Edge Browsing Streak is likely NOT available
                                # in the user's region (only US/CA/GB/DE/FR/AU/JP).
                                if zero_progress_retries >= max_zero_retries:
                                    logger.warning(
                                        f"⚠️ Edge Streak: 0 min after {elapsed // 60}+ min "
                                        f"and {zero_progress_retries} retries. "
                                        f"This feature may not be available in your region. "
                                        f"Skipping to save time."
                                    )
                                    break

                                logger.warning(
                                    f"⚠️ 6+ min elapsed but 0 min credited, "
                                    f"retrying via pointsbreakdown... "
                                    f"(attempt {zero_progress_retries}/{max_zero_retries})"
                                )
                                # Try the points breakdown page which has direct streak links
                                for retry_url in [
                                    "https://rewards.bing.com/pointsbreakdown",
                                    "https://rewards.bing.com/earn",
                                    REWARDS_URL,
                                ]:
                                    try:
                                        await page.goto(
                                            retry_url,
                                            wait_until="domcontentloaded",
                                            timeout=15000,
                                        )
                                        await asyncio.sleep(3)
                                        clicked = await page.evaluate("""
                                            () => {
                                                const els = document.querySelectorAll('a, button, mee-card a, [role="link"]');
                                                for (const el of els) {
                                                    const text = (el.textContent || '').toLowerCase();
                                                    if (text.includes('edge') && (text.includes('brows') || text.includes('streak') || text.includes('minute'))) {
                                                        el.click(); return true;
                                                    }
                                                }
                                                return false;
                                            }
                                        """)
                                        if clicked:
                                            logger.info(f"Re-activated Edge Streak via {retry_url}")
                                            await asyncio.sleep(3)
                                            break
                                    except Exception:
                                        continue
                        except Exception:
                            pass
                        last_heartbeat = elapsed
                    except Exception:
                        pass

                if on_progress:
                    on_progress(verified_minutes, verified_target)

                if verified_minutes > 0:
                    logger.info(f"Edge browse: {verified_minutes}/{verified_target} min")

                # Short break every ~10 min
                if elapsed > 0 and elapsed % 600 < visit_time:
                    await asyncio.sleep(random.uniform(5, 15))

            except Exception as e:
                logger.debug(f"Edge browse error: {e}")
                await asyncio.sleep(10)
                elapsed += 10

        # ═══ Final heartbeat ═══
        try:
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)
            await page.evaluate("""
                async () => {
                    try {
                        await fetch('https://rewards.bing.com/api/getuserinfo?type=1', {
                            credentials: 'include'
                        });
                    } catch(e) {}
                }
            """)
            current_minutes, current_target, streak_done = await self._read_verified_progress(page)
            verified_minutes = max(verified_minutes, current_minutes)
            verified_target = max(verified_target, current_target)
        except Exception:
            streak_done = verified_minutes >= verified_target

        logger.info(
            f"✅ Edge browsing session finished "
            f"({verified_minutes}/{verified_target} verified min)"
        )
        return streak_done or verified_minutes >= verified_target
