"""
Human-like behavior simulation for stealth automation.
- Bezier curve mouse movements (natural arcs)
- Gaussian typing with variable rhythm (burst + pause)
- Fatigue simulation (slower over time)
- Tab/window focus simulation
- Natural scroll patterns (deceleration)
"""

import random
import math
import asyncio
from typing import Optional

from playwright.async_api import Page

from src.utils import logger


class Humanizer:
    """Simulates human-like behavior with advanced techniques."""

    def __init__(
        self,
        delay_min: float = 3.0,
        delay_max: float = 8.0,
        typing_delay_min: int = 50,
        typing_delay_max: int = 150,
    ):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.typing_delay_min = typing_delay_min
        self.typing_delay_max = typing_delay_max
        self._action_count = 0  # Track actions for fatigue

    # ─── Core Delays ─────────────────────────────────────────

    def _gaussian(self, lo: float, hi: float) -> float:
        mean = (lo + hi) / 2
        std = (hi - lo) / 6
        return max(lo, min(hi, random.gauss(mean, std)))

    def _fatigue_multiplier(self) -> float:
        """Actions slow down over time (fatigue simulation)."""
        base = 1.0
        if self._action_count > 30:
            base += 0.1
        if self._action_count > 60:
            base += 0.2
        if self._action_count > 100:
            base += 0.3
        # Random spike (micro-break)
        if random.random() < 0.03:
            base += random.uniform(0.5, 2.0)
        return base

    async def random_delay(self, lo: Optional[float] = None, hi: Optional[float] = None) -> None:
        lo = lo if lo is not None else self.delay_min
        hi = hi if hi is not None else self.delay_max
        delay = self._gaussian(lo, hi) * self._fatigue_multiplier()
        self._action_count += 1
        await asyncio.sleep(delay)

    async def short_delay(self) -> None:
        await self.random_delay(0.5, 2.0)

    async def micro_delay(self) -> None:
        await self.random_delay(0.1, 0.5)

    # ─── Typing ──────────────────────────────────────────────

    async def type_text(self, page: Page, selector: str, text: str) -> None:
        """Type with variable rhythm: bursts + micro-pauses."""
        el = page.locator(selector)
        await el.click()
        await self.micro_delay()
        await el.fill("")
        await self.micro_delay()
        await self._type_rhythm(page, text)

    async def type_text_direct(self, page: Page, text: str) -> None:
        """Type into focused element with variable rhythm."""
        await self._type_rhythm(page, text)

    async def _type_rhythm(self, page: Page, text: str) -> None:
        """Variable rhythm: random bursts of fast typing + pauses between groups."""
        i = 0
        while i < len(text):
            # Burst length: 2-6 chars typed fast
            burst = random.randint(1, max(1, min(6, len(text) - i)))
            for j in range(burst):
                ch = text[i]
                delay = random.randint(self.typing_delay_min, self.typing_delay_max)
                # Slow down for special chars
                if ch in "!@#$%^&*()_+-=[]{};':\",./<>?":
                    delay = int(delay * 1.5)
                await page.keyboard.type(ch, delay=delay)
                i += 1
                if i >= len(text):
                    break

            # Pause between bursts (like re-reading what you typed)
            if i < len(text):
                pause = random.choice([
                    random.uniform(0.1, 0.3),   # common: brief pause
                    random.uniform(0.1, 0.3),   # common
                    random.uniform(0.4, 0.9),   # occasional: thinking
                    random.uniform(1.0, 2.0),   # rare: longer pause
                ]) if random.random() < 0.4 else random.uniform(0.05, 0.15)
                await asyncio.sleep(pause)

            # Rare typo simulation (2% chance per burst)
            if random.random() < 0.02 and i < len(text):
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                await page.keyboard.type(wrong, delay=random.randint(40, 100))
                await asyncio.sleep(random.uniform(0.2, 0.6))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.1, 0.3))

    # ─── Mouse Movement (Bezier Curves) ──────────────────────

    def _bezier_points(self, x0, y0, x1, y1, n_points=20):
        """Generate points along a cubic Bezier curve for natural mouse motion."""
        # Random control points to create a natural arc
        cp1x = x0 + (x1 - x0) * random.uniform(0.1, 0.4) + random.uniform(-80, 80)
        cp1y = y0 + (y1 - y0) * random.uniform(0.1, 0.4) + random.uniform(-80, 80)
        cp2x = x0 + (x1 - x0) * random.uniform(0.6, 0.9) + random.uniform(-80, 80)
        cp2y = y0 + (y1 - y0) * random.uniform(0.6, 0.9) + random.uniform(-80, 80)

        points = []
        for i in range(n_points + 1):
            t = i / n_points
            inv = 1 - t
            px = inv**3 * x0 + 3 * inv**2 * t * cp1x + 3 * inv * t**2 * cp2x + t**3 * x1
            py = inv**3 * y0 + 3 * inv**2 * t * cp1y + 3 * inv * t**2 * cp2y + t**3 * y1
            # Add micro jitter (hand tremor)
            px += random.uniform(-1, 1)
            py += random.uniform(-1, 1)
            points.append((int(px), int(py)))
        return points

    async def bezier_move(self, page: Page, target_x: int, target_y: int) -> None:
        """Move mouse along a Bezier curve to target (natural arc)."""
        try:
            # Current position (center if unknown)
            vp = page.viewport_size or {"width": 1280, "height": 720}
            cur_x = random.randint(100, vp["width"] - 100)
            cur_y = random.randint(100, vp["height"] - 100)

            points = self._bezier_points(cur_x, cur_y, target_x, target_y)
            for px, py in points:
                await page.mouse.move(px, py)
                await asyncio.sleep(random.uniform(0.005, 0.02))
        except Exception:
            pass

    async def random_mouse_move(self, page: Page) -> None:
        """Move mouse to random position via Bezier curve."""
        try:
            vp = page.viewport_size
            if vp:
                x = random.randint(50, vp["width"] - 50)
                y = random.randint(50, vp["height"] - 50)
                await self.bezier_move(page, x, y)
        except Exception:
            pass

    # ─── Scrolling (with deceleration) ───────────────────────

    async def natural_scroll(self, page: Page, direction: str = "down", distance: int = 0) -> None:
        """Scroll with natural deceleration."""
        if distance == 0:
            distance = random.randint(200, 600)
        if direction == "up":
            distance = -distance

        # Split into decreasing steps (deceleration)
        remaining = abs(distance)
        step_count = random.randint(4, 8)
        sign = 1 if distance > 0 else -1

        for i in range(step_count):
            ratio = 1 - (i / step_count) * 0.6  # Decelerate
            step = int((remaining / (step_count - i)) * ratio)
            step = max(step, 10)
            await page.mouse.wheel(0, step * sign)
            await asyncio.sleep(random.uniform(0.03, 0.08))
            remaining -= step

        await asyncio.sleep(random.uniform(0.2, 0.5))

    async def random_scroll(self, page: Page) -> None:
        direction = random.choice(["down", "down", "down", "up"])  # Bias downward
        await self.natural_scroll(page, direction)
        await self.short_delay()

    # ─── Clicking ────────────────────────────────────────────

    async def human_click(self, page: Page, selector: str) -> None:
        """Click with Bezier mouse approach + random offset."""
        el = page.locator(selector)

        # Move mouse near element first
        if random.random() < 0.4:
            await self.random_mouse_move(page)
            await self.micro_delay()

        try:
            box = await el.bounding_box()
            if box:
                # Bezier approach to element
                target_x = int(box["x"] + box["width"] / 2 + random.uniform(-box["width"] * 0.15, box["width"] * 0.15))
                target_y = int(box["y"] + box["height"] / 2 + random.uniform(-box["height"] * 0.15, box["height"] * 0.15))
                await self.bezier_move(page, target_x, target_y)
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await page.mouse.click(target_x, target_y)
            else:
                await el.click()
        except Exception:
            await el.click()

    # ─── Page Behavior ───────────────────────────────────────

    async def simulate_reading(self, page: Page, duration: float = 0) -> None:
        """Simulate reading with natural patterns."""
        if duration == 0:
            duration = self._gaussian(2, 6)

        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < duration:
            action = random.choices(
                ["scroll", "mouse", "wait", "nothing"],
                weights=[30, 20, 35, 15],
            )[0]
            if action == "scroll":
                await self.natural_scroll(page, "down", random.randint(100, 300))
            elif action == "mouse":
                await self.random_mouse_move(page)
            elif action == "wait":
                await asyncio.sleep(self._gaussian(0.5, 2.0))
            else:
                await asyncio.sleep(self._gaussian(0.3, 0.8))

    async def before_search(self, page: Page) -> None:
        """Natural pre-search behavior."""
        if random.random() < 0.3:
            await self.random_mouse_move(page)
        if random.random() < 0.15:
            await self.natural_scroll(page, random.choice(["up", "down"]))
        # Occasionally hover over a random element
        if random.random() < 0.1:
            try:
                links = page.locator("a")
                cnt = await links.count()
                if cnt > 0:
                    idx = random.randint(0, min(cnt - 1, 5))
                    box = await links.nth(idx).bounding_box()
                    if box:
                        await self.bezier_move(page, int(box["x"] + box["width"]/2), int(box["y"] + box["height"]/2))
            except Exception:
                pass
        await self.short_delay()

    async def after_search(self, page: Page) -> None:
        """Post-search behavior with diverse interactions."""
        # Main reading simulation
        await self.simulate_reading(page, self._gaussian(2, 5))

        # Diverse micro-interactions (realistic user behavior)
        roll = random.random()
        if roll < 0.08:
            # Select some text on page (like copying a snippet)
            try:
                await page.evaluate("""
                    () => {
                        const el = document.querySelector('.b_algo .b_caption p, .b_algo .b_lineclamp2');
                        if (el) {
                            const range = document.createRange();
                            range.selectNodeContents(el);
                            window.getSelection().removeAllRanges();
                            window.getSelection().addRange(range);
                        }
                    }
                """)
                await asyncio.sleep(random.uniform(0.5, 1.5))
                # Deselect
                await page.evaluate("window.getSelection().removeAllRanges()")
            except Exception:
                pass
        elif roll < 0.12:
            # Right-click then dismiss (checking context menu)
            try:
                vp = page.viewport_size
                if vp:
                    x = random.randint(200, vp["width"] - 200)
                    y = random.randint(200, vp["height"] - 200)
                    await page.mouse.click(x, y, button="right")
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await page.keyboard.press("Escape")
            except Exception:
                pass

    async def simulate_tab_switch(self, page: Page) -> None:
        """Simulate losing focus by opening a blank tab, waiting, then returning."""
        try:
            context = page.context
            # Create a new tab (triggers genuine visibilitychange on current page)
            new_page = await context.new_page()
            await asyncio.sleep(random.uniform(2, 8))
            # Close the distraction tab and refocus
            await new_page.close()
            await page.bring_to_front()
            await asyncio.sleep(random.uniform(0.3, 1.0))
        except Exception:
            # Fallback: just wait
            await asyncio.sleep(random.uniform(2, 8))

    async def take_break(self) -> None:
        """Simulate a micro-break (5-30 seconds pause)."""
        pause = random.uniform(5, 30)
        logger.debug(f"Taking a {pause:.0f}s micro-break...")
        await asyncio.sleep(pause)

    async def warm_up_browsing(self, page) -> None:
        """Visit 3-5 random sites before starting tasks.
        
        Builds browsing history and referrer chain to look like
        a real user starting their browser session. Includes
        non-Microsoft sites for realistic diversity.
        """
        warmup_sites = [
            # Microsoft ecosystem (primary)
            "https://www.msn.com",
            "https://www.bing.com",
            "https://outlook.live.com",
            "https://www.bing.com/news",
            "https://www.microsoft.com",
            # Non-Microsoft (diversity — real users visit many domains)
            "https://www.wikipedia.org",
            "https://stackoverflow.com",
            "https://www.reddit.com",
            "https://news.ycombinator.com",
            "https://www.weather.com",
        ]
        
        count = random.randint(3, 5)
        selected = random.sample(warmup_sites, min(count, len(warmup_sites)))
        
        logger.debug(f"Warm-up: visiting {count} sites...")
        for url in selected:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(random.uniform(2, 5))
                # Scroll a bit
                await self.natural_scroll(page, "down", random.randint(100, 400))
                await asyncio.sleep(random.uniform(1, 3))
                # Sometimes click a random link (simulate browsing)
                if random.random() < 0.3:
                    try:
                        links = page.locator("a[href]:visible")
                        link_count = await links.count()
                        if link_count > 3:
                            idx = random.randint(0, min(link_count - 1, 10))
                            await links.nth(idx).click(timeout=3000)
                            await asyncio.sleep(random.uniform(1, 3))
                            await page.go_back(timeout=10000)
                    except Exception:
                        pass
            except Exception:
                pass
