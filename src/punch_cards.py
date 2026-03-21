"""
Punch Cards completion for Microsoft Rewards.
"""

import asyncio
import random

from playwright.async_api import Page

from src.utils import logger, REWARDS_URL, retry
from src.humanizer import Humanizer


class PunchCardCompleter:
    """Completes Punch Card tasks on Microsoft Rewards."""

    def __init__(self, humanizer: Humanizer):
        self.humanizer = humanizer

    @retry(max_retries=2, delay=3)
    async def complete_punch_cards(self, page: Page) -> dict:
        """
        Complete all available Punch Cards.

        Returns:
            Dict with {completed, total, cards}
        """
        logger.info("Starting Punch Cards completion...")

        stats = {"completed": 0, "total": 0, "cards": []}

        try:
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            # Find Punch Card section
            punch_cards = page.locator(
                '[data-bi-area="MoreActivities"] .ds-card-sec, '
                'mee-card[data-bi-area="PunchCards"], '
                '.punchcard-row .c-card-content, '
                '[class*="punchCard"] a'
            )

            count = await punch_cards.count()
            stats["total"] = count
            logger.info(f"Found {count} Punch Cards")

            for i in range(count):
                try:
                    card = punch_cards.nth(i)
                    card_text = await card.inner_text()

                    # Check if already completed
                    completed = card.locator(
                        '.mee-icon-AddMedium, .checkmark, '
                        '[class*="complete"]'
                    )
                    if await completed.count() > 0:
                        logger.debug(f"Punch Card {i + 1} already done")
                        stats["completed"] += 1
                        stats["cards"].append(
                            {"index": i + 1, "status": "already_done", "text": card_text[:50]}
                        )
                        continue

                    # Click on punch card
                    await card.click()
                    await asyncio.sleep(3)

                    # Handle punch card activities
                    await self._handle_punch_card(page)

                    stats["completed"] += 1
                    stats["cards"].append(
                        {"index": i + 1, "status": "completed", "text": card_text[:50]}
                    )

                    # Go back to rewards
                    await page.goto(
                        REWARDS_URL,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    await asyncio.sleep(2)
                    await self.humanizer.short_delay()

                except Exception as e:
                    logger.warning(f"Punch Card {i + 1} failed: {e}")
                    stats["cards"].append(
                        {"index": i + 1, "status": f"failed: {e}", "text": ""}
                    )
                    try:
                        await page.goto(
                            REWARDS_URL,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(2)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Punch Cards error: {e}")

        logger.info(f"Punch Cards: {stats['completed']}/{stats['total']} completed")
        return stats

    async def _handle_punch_card(self, page: Page) -> None:
        """Handle activities within a Punch Card."""
        await asyncio.sleep(3)

        # Check for sub-tasks within the punch card
        sub_tasks = page.locator(
            '.punchcard-child-card, .offer-task, '
            '[class*="punchCardChild"], [class*="offer-cta"]'
        )
        sub_count = await sub_tasks.count()

        if sub_count > 0:
            for j in range(sub_count):
                try:
                    task = sub_tasks.nth(j)

                    # Check if sub-task already done
                    done = task.locator('[class*="complete"], .checkmark')
                    if await done.count() > 0:
                        continue

                    await task.click()
                    await asyncio.sleep(3)

                    # Wait on the page (simulate reading/interaction)
                    await self.humanizer.simulate_reading(
                        page, random.uniform(3, 6)
                    )

                    # Go back
                    await page.go_back()
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.debug(f"Punch Card sub-task error: {e}")
        else:
            # Simple punch card - just visit and wait
            await self.humanizer.simulate_reading(page, random.uniform(3, 6))
