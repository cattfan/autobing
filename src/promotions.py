"""
Promotions + Earn page task collection for Microsoft Rewards.
Handles both the dashboard promotions and the /earn page tasks.
"""

import asyncio
import random

from playwright.async_api import Page

from src.utils import logger, REWARDS_URL, retry
from src.humanizer import Humanizer
from src.quiz_solver import auto_answer_quiz

EARN_URL = "https://rewards.bing.com/earn"


class PromotionCompleter:
    """Completes Promotion tasks + Earn page tasks on Microsoft Rewards."""

    def __init__(self, humanizer: Humanizer, settings: dict = None, ai_agent=None):
        self.humanizer = humanizer
        self.settings = settings or {}
        self.ai_agent = ai_agent

    @retry(max_retries=2, delay=3)
    async def complete_promotions(self, page: Page) -> dict:
        """Complete all available Promotions (dashboard + earn page)."""
        logger.info("Starting Promotions completion...")
        stats = {"completed": 0, "total": 0}

        try:
            # ── Step 1: Dashboard promotions ──
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            promos = page.locator(
                '[data-bi-area="MoreActivities"] .ds-card-sec:not([class*="punchCard"]), '
                'mee-card[data-bi-area="MorePromotions"], '
                '.more-activities .c-card-content, '
                '[class*="promo-card"]'
            )

            count = await promos.count()
            stats["total"] += count

            for i in range(count):
                try:
                    promo = promos.nth(i)
                    done = promo.locator(
                        '.mee-icon-AddMedium, .checkmark, [class*="complete"]'
                    )
                    if await done.count() > 0:
                        stats["completed"] += 1
                        continue

                    await promo.click()
                    await asyncio.sleep(3)
                    await self._handle_opened_task(page)

                    stats["completed"] += 1
                    await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)
                    await self.humanizer.short_delay()

                except Exception as e:
                    logger.warning(f"Promotion {i + 1} failed: {e}")
                    try:
                        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass

            # ── Step 2: Earn page tasks ──
            # Try AI agent first (smarter), fallback to CSS selectors
            if self.ai_agent and self.ai_agent.enabled:
                logger.info("🤖 Using AI Agent for earn page tasks...")
                ai_result = await self.ai_agent.complete_earn_page(page)
                if ai_result.get("success"):
                    stats["completed"] += ai_result.get("steps", 0)
                    stats["total"] += ai_result.get("steps", 0)
                else:
                    # AI failed, fallback to CSS
                    logger.info("AI fallback → CSS selectors for earn page")
                    earn_stats = await self._complete_earn_page(page)
                    stats["total"] += earn_stats["total"]
                    stats["completed"] += earn_stats["completed"]
            else:
                earn_stats = await self._complete_earn_page(page)
                stats["total"] += earn_stats["total"]
                stats["completed"] += earn_stats["completed"]

        except Exception as e:
            logger.error(f"Promotions error: {e}")

        logger.info(f"Promotions: {stats['completed']}/{stats['total']} completed")
        return stats

    async def _complete_earn_page(self, page: Page) -> dict:
        """Complete tasks on the /earn page (Keep earning section)."""
        stats = {"completed": 0, "total": 0}

        try:
            logger.info("Checking Earn page tasks...")
            await page.goto(EARN_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            # Find earn cards — these are the +5 point tasks
            # The earn page has cards with point badges like "+5"
            earn_cards = page.locator(
                # Modern Rewards earn page selectors
                'mee-card.ng-scope, '
                'mee-card[data-bi-area], '
                '.earn-set-item, '
                '.c-card-content, '
                # Card links that have points badge
                'a[href*="/earn/"] .ds-card-sec, '
                'a[class*="card"][href*="/earn"], '
                # Generic card containers
                '.card-container a, '
                '.rewards-card-container a'
            )

            count = await earn_cards.count()
            if count == 0:
                # Broader fallback selectors
                earn_cards = page.locator(
                    'a[data-bi-area="EarnMore"], '
                    'a[data-bi-area="KeepEarning"], '
                    # Any link with a points badge inside
                    'a:has(.pointsBadge), '
                    # Section-level links
                    'section a[href*="bing.com"]'
                )
                count = await earn_cards.count()

            stats["total"] = count
            logger.info(f"Found {count} Earn page tasks")

            if count == 0:
                return stats

            for i in range(count):
                try:
                    card = earn_cards.nth(i)

                    # Check if already completed
                    done_indicator = card.locator(
                        '.mee-icon-AddMedium, .checkmark, '
                        '[class*="complete"], [class*="checked"], '
                        '.cardComplete'
                    )
                    if await done_indicator.count() > 0:
                        stats["completed"] += 1
                        continue

                    # Get card text for logging
                    card_text = ""
                    try:
                        card_text = (await card.text_content() or "")[:50].strip()
                    except Exception:
                        pass

                    logger.debug(f"Earn task {i + 1}/{count}: {card_text}")

                    # Click the card
                    try:
                        await card.click(timeout=5000)
                    except Exception:
                        try:
                            await card.scroll_into_view_if_needed()
                            await asyncio.sleep(0.5)
                            await card.click(timeout=5000)
                        except Exception:
                            continue

                    await asyncio.sleep(3)

                    # Handle whatever opened
                    await self._handle_opened_task(page)

                    stats["completed"] += 1
                    logger.info(f"Earn task {i + 1}/{count} done: {card_text}")

                    # Go back to earn page
                    await page.goto(EARN_URL, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)
                    await self.humanizer.short_delay()

                except Exception as e:
                    logger.debug(f"Earn task {i + 1} error: {e}")
                    try:
                        await page.goto(EARN_URL, wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass

        except Exception as e:
            logger.warning(f"Earn page error: {e}")

        return stats

    async def _handle_opened_task(self, page: Page) -> None:
        """Handle an opened task: quiz, poll, puzzle, or article."""
        await asyncio.sleep(2)

        # Check for quiz
        quiz = await page.query_selector(
            '#currentQuestionContainer, .rqQuestion, '
            '[id*="rqQuestionState"], #quizComplete498498498'
        )
        if quiz:
            await self._handle_quiz_promo(page)
            return

        # Check for poll
        poll = await page.query_selector(
            '.bt_poll, #btoption, [class*="poll"], [data-bi-type="poll"]'
        )
        if poll:
            await self._handle_poll(page)
            return

        # Check for "This or That"
        tot = await page.query_selector(
            '#currentQuestionContainer .btOptions, '
            '.rq_tooltip, [class*="thisOrThat"]'
        )
        if tot:
            await self._handle_this_or_that(page)
            return

        # Default: article/link visit — simulate reading
        await self.humanizer.simulate_reading(page, random.uniform(3, 8))

    async def _handle_quiz_promo(self, page: Page) -> None:
        """Handle a quiz with auto-answer."""
        for _ in range(20):
            try:
                await asyncio.sleep(2)

                complete = await page.query_selector(
                    '#quizCompleteContainer, [class*="quizComplete"]'
                )
                if complete:
                    break

                # Read question
                question_text = ""
                q_el = await page.query_selector(
                    '.rqQuestion .textContainer, '
                    '#currentQuestionContainer .rqQuestion, '
                    '.wk_questionText, .rq_questionText'
                )
                if q_el:
                    question_text = (await q_el.text_content() or "").strip()

                options = page.locator(
                    '.rqOption, [id*="rqAnswerOption"], '
                    '.wk_choicesInstContainer .rq_button'
                )
                count = await options.count()
                if count == 0:
                    break

                # Get option texts
                option_texts = []
                for j in range(count):
                    text = (await options.nth(j).text_content() or "").strip()
                    if not text:
                        text = await options.nth(j).get_attribute("data-option") or ""
                    option_texts.append(text)

                # Auto-answer
                if question_text and any(option_texts):
                    idx = await auto_answer_quiz(page, question_text, option_texts)
                else:
                    idx = random.randint(0, count - 1)

                await options.nth(idx).click()
                await asyncio.sleep(2)

                next_btn = page.locator('#nextQuestionbtn')
                if await next_btn.count() > 0:
                    await next_btn.click()
                    await asyncio.sleep(1)

            except Exception:
                break

    async def _handle_poll(self, page: Page) -> None:
        """Handle a poll task."""
        try:
            options = page.locator(
                '#btoption0, #btoption1, .bt_poll .btOption, [id*="btoption"]'
            )
            count = await options.count()
            if count > 0:
                idx = random.randint(0, count - 1)
                await options.nth(idx).click()
                await asyncio.sleep(3)
                logger.debug("Poll answered")
        except Exception as e:
            logger.debug(f"Poll error: {e}")

    async def _handle_this_or_that(self, page: Page) -> None:
        """Handle 'This or That' with auto-answer."""
        for _ in range(15):
            try:
                await asyncio.sleep(2)

                complete = await page.query_selector(
                    '#quizCompleteContainer, [class*="quizComplete"]'
                )
                if complete:
                    break

                question_text = ""
                q_el = await page.query_selector(
                    '.rqQuestion, .btQuestionText, '
                    '#currentQuestionContainer .textContainer'
                )
                if q_el:
                    question_text = (await q_el.text_content() or "").strip()

                options = page.locator(
                    '#currentQuestionContainer .btOptionCard, '
                    '.rq_tooltip .btOption, [class*="btOption"]'
                )
                count = await options.count()
                if count >= 2:
                    option_texts = []
                    for j in range(count):
                        text = (await options.nth(j).text_content() or "").strip()
                        option_texts.append(text)

                    if question_text and any(option_texts):
                        idx = await auto_answer_quiz(page, question_text, option_texts)
                    else:
                        idx = random.randint(0, 1)

                    await options.nth(idx).click()
                    await asyncio.sleep(2)
                elif count == 0:
                    break
            except Exception:
                break
