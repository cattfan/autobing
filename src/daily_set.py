"""
Daily Set task automation for Microsoft Rewards.
Includes Quiz Auto-Answer (search Bing for answers) and Captcha solving.
"""

import asyncio
import random

from playwright.async_api import Page

from src.utils import logger, REWARDS_URL, retry
from src.humanizer import Humanizer
from src.quiz_solver import auto_answer_quiz
from src.captcha_solver import CaptchaSolver


class DailySetCompleter:
    """Completes Daily Set tasks on Microsoft Rewards."""

    def __init__(self, humanizer: Humanizer, settings: dict = None, ai_agent=None):
        self.humanizer = humanizer
        self.ai_agent = ai_agent
        self.captcha = CaptchaSolver(settings or {})

    async def _click_daily_set_card(self, page: Page) -> bool:
        """Open the Daily Set container using broad selectors for the current UI."""
        streak_selectors = [
            '#daily-sets mee-card-group mee-card:first-child a',
            '#daily-sets mee-card:first-child a',
            '#daily-sets a',
            '#daily-sets [role="link"]',
            '#daily-sets [role="button"]',
            '#daily-sets button',
            'mee-card:has-text("Daily Set Streak") a',
            'a:has-text("Daily Set Streak")',
            'a:has-text("Daily Set")',
            'mee-card:has-text("Activity") a',
            'mee-card:has-text("Activity") button',
            'mee-card:has-text("Activity") [role="button"]',
            '[data-bi-area="DailySet"] a',
            '[data-bi-name="promotion_item"]:has-text("Daily Set")',
            'mee-rewards-daily-set-item-content:first-of-type a',
        ]

        for sel in streak_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible(timeout=2000):
                    continue
                await el.scroll_into_view_if_needed(timeout=3000)
                await el.click(timeout=5000)
                logger.info(f"Clicked Daily Set Streak card via: {sel}")
                return True
            except Exception:
                continue

        dom_target_id = await page.evaluate(
            """
            () => {
                const normalize = (value) => (value || "")
                    .replace(/\\u200b/g, "")
                    .replace(/\\u00a0/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase();
                const selector = "a,button,[role='button'],[role='link'],[data-rac],.cursor-pointer,div";

                for (const old of document.querySelectorAll("[data-codex-daily='true']")) {
                    old.removeAttribute("data-codex-daily");
                    if ((old.id || "").startsWith("codex-daily-")) {
                        old.removeAttribute("id");
                    }
                }

                for (const node of document.querySelectorAll(selector)) {
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

                    if (!text.includes("daily set") && !text.includes("activity:")) {
                        continue;
                    }

                    const clickable =
                        node.closest("a,button,[role='button'],[role='link']") ||
                        node.querySelector("a,button,[role='button'],[role='link']") ||
                        node;

                    if (!clickable.id) {
                        clickable.id = `codex-daily-${Math.random().toString(36).slice(2, 10)}`;
                    }
                    clickable.setAttribute("data-codex-daily", "true");
                    return clickable.id;
                }

                return null;
            }
            """
        )

        if dom_target_id:
            try:
                target = page.locator(f"#{dom_target_id}").first
                if await target.count() > 0 and await target.is_visible(timeout=2000):
                    await target.scroll_into_view_if_needed(timeout=3000)
                    await target.click(timeout=5000)
                    logger.info("Clicked Daily Set card via DOM text match")
                    return True
            except Exception:
                pass

        return False

    @retry(max_retries=2, delay=3)
    async def complete_daily_set(self, page: Page) -> dict:
        """
        Complete all Daily Set tasks.

        Returns:
            Dict with {completed, total, tasks}
        """
        logger.info("Starting Daily Set completion...")

        stats = {"completed": 0, "total": 0, "tasks": []}

        # Try AI agent first (much better at finding and clicking dynamic UI)
        if self.ai_agent and self.ai_agent.enabled:
            logger.info("🤖 Using AI Agent for Daily Set...")
            ai_result = await self.ai_agent.complete_daily_set(page)
            if ai_result.get("success"):
                steps = ai_result.get("steps", 0)
                return {
                    "completed": steps,
                    "total": steps,
                    "tasks": [{"status": "ai_completed"}],
                }
            else:
                logger.info("AI fallback → CSS selectors for Daily Set")

        try:
            # ═══ Click-based approach using correct Rewards page DOM ═══
            streak_card_clicked = False
            rewards_surfaces = [
                REWARDS_URL,
                f"{REWARDS_URL}/dashboard",
                f"{REWARDS_URL}/earn",
            ]

            for rewards_url in rewards_surfaces:
                try:
                    await page.goto(
                        rewards_url,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    await asyncio.sleep(3)
                else:
                    await asyncio.sleep(2)

                streak_card_clicked = await self._click_daily_set_card(page)
                if streak_card_clicked:
                    break

            if not streak_card_clicked:
                logger.warning("Could not find Daily Set Streak card on #daily-sets")
                return stats

            # Step 2: Wait for modal/flyout to open
            await asyncio.sleep(3)

            # Step 3: Find the sub-activity cards inside the modal
            # After clicking streak card, the modal shows 3 activities
            # Each activity is a mee-card with mee-rewards-daily-set-item-content
            activity_links = None
            activity_selectors = [
                # Inside the streak modal: sub-activity cards
                'mee-rewards-daily-set-item-content a',
                '#daily-sets mee-card a[href]',
                '#daily-sets mee-card-group:not(:first-child) mee-card a',
                # After modal opens, find cards with point badges
                'mee-card a:has-text("+5")',
                'mee-card a:has-text("+10")',
                'mee-rewards-daily-set-item-content [role="link"]',
                'mee-rewards-daily-set-item-content button',
                # Broader: all cards in daily sets section
                '#daily-sets a[href*="bing.com"]',
                '#daily-sets a[href*="rewards"]',
                'mee-card a[href*="bing.com"]',
                'mee-card a[href*="rewards"]',
            ]

            for sel in activity_selectors:
                try:
                    links = page.locator(sel)
                    cnt = await links.count()
                    # Filter: we want 2-4 activity cards (not the streak card itself)
                    if 1 < cnt <= 10:
                        activity_links = links
                        logger.info(f"Found {cnt} Daily Set activities via: {sel}")
                        break
                    elif cnt == 1:
                        # Could be just the streak card, try next selector
                        continue
                except Exception:
                    continue

            if not activity_links:
                # Last resort: try clicking all visible card links
                activity_links = page.locator(
                    'mee-card a[href]:visible'
                )
                cnt = await activity_links.count()
                if cnt <= 1:
                    logger.warning("No Daily Set activities found in modal")
                    return stats
                # Skip first (likely the streak card itself)
                logger.info(f"Found {cnt} card links (using visible cards)")

            count = await activity_links.count()
            stats["total"] = count

            for i in range(count):
                try:
                    # Get activity info before clicking
                    link = activity_links.nth(i)
                    title = ""
                    try:
                        title = (await link.text_content() or "")[:50].strip()
                    except Exception:
                        pass

                    logger.info(f"Daily Set {i + 1}/{count}: {title}")

                    # Remember current pages before click
                    pages_before = len(page.context.pages)

                    # Click the activity
                    await link.click(timeout=5000)
                    await asyncio.sleep(3)

                    # Check if a new tab opened
                    current_pages = page.context.pages
                    if len(current_pages) > pages_before:
                        # Switch to new tab
                        new_tab = current_pages[-1]
                        await new_tab.wait_for_load_state("domcontentloaded", timeout=15000)
                        await asyncio.sleep(2)

                        # Handle the task in the new tab
                        await self.captcha.solve_if_present(new_tab)
                        await self._handle_task(new_tab)

                        # Close the tab and go back to modal
                        await new_tab.close()
                        await asyncio.sleep(1)
                        await page.bring_to_front()
                    else:
                        # Task opened in same page
                        await self.captcha.solve_if_present(page)
                        await self._handle_task(page)

                        # Go back to rewards page and reopen modal
                        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(3)

                        # Re-click streak card to reopen modal
                        if await self._click_daily_set_card(page):
                            await asyncio.sleep(3)

                    stats["completed"] += 1
                    stats["tasks"].append({"index": i + 1, "status": "completed"})
                    logger.info(f"Daily Set {i + 1}/{count} completed")
                    await self.humanizer.short_delay()

                except Exception as e:
                    logger.warning(f"Daily Set activity {i + 1} failed: {e}")
                    stats["tasks"].append({"index": i + 1, "status": f"failed: {e}"})

                    # Try to recover: go back to rewards and reopen modal
                    try:
                        # Close any extra tabs
                        while len(page.context.pages) > 1:
                            extra = page.context.pages[-1]
                            if extra != page:
                                await extra.close()
                            else:
                                break
                        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(3)
                        if await self._click_daily_set_card(page):
                            await asyncio.sleep(3)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Daily Set completion error: {e}")

        logger.info(f"Daily Set: {stats['completed']}/{stats['total']} completed")
        return stats

    async def _handle_task(self, page: Page) -> None:
        """Handle an opened Daily Set task (quiz, poll, link visit)."""
        await asyncio.sleep(3)

        # Check if it's a quiz
        quiz_container = await page.query_selector(
            '#quizComplete498498498, .rqQuestion, [id*="rqQuestionState"], '
            '#currentQuestionContainer'
        )

        if quiz_container:
            await self._handle_quiz(page)
            return

        # Check if it's a poll
        poll_container = await page.query_selector(
            '.bt_poll, #btoption, [class*="poll"], [data-bi-type="poll"]'
        )

        if poll_container:
            await self._handle_poll(page)
            return

        # Check if it's a "This or That" quiz
        tot_container = await page.query_selector(
            '#currentQuestionContainer .btOptions, '
            '.rq_tooltip, [class*="thisOrThat"]'
        )

        if tot_container:
            await self._handle_this_or_that(page)
            return

        # Default: it's just a link visit task - wait and go back
        await self.humanizer.simulate_reading(page, random.uniform(3, 6))

    async def _handle_quiz(self, page: Page) -> None:
        """Handle a quiz task with auto-answer via Bing search."""
        logger.debug("Handling quiz task (auto-answer enabled)...")
        max_questions = 20

        for q in range(max_questions):
            try:
                # Wait for question
                await asyncio.sleep(2)

                # Check if quiz is complete
                complete = await page.query_selector(
                    '#quizCompleteContainer, [class*="quizComplete"], '
                    '.cico.rqDone'
                )
                if complete:
                    logger.debug("Quiz completed!")
                    break

                # Read the question text
                question_text = ""
                q_el = await page.query_selector(
                    '.rqQuestion .textContainer, '
                    '#currentQuestionContainer .rqQuestion, '
                    '.wk_questionText, .rq_questionText'
                )
                if q_el:
                    question_text = (await q_el.text_content() or "").strip()

                # Find answer options
                options = page.locator(
                    '#currentQuestionContainer .rqOption, '
                    '.wk_choicesInst498498498 .rq_button, '
                    '[id*="rqAnswerOption"]'
                )

                count = await options.count()
                if count == 0:
                    break

                # Get option texts for auto-answer
                option_texts = []
                for i in range(count):
                    text = (await options.nth(i).text_content() or "").strip()
                    # Also try data attributes
                    if not text:
                        text = await options.nth(i).get_attribute("data-option") or ""
                    option_texts.append(text)

                # Auto-answer: search Bing for the answer
                if question_text and any(option_texts):
                    idx = await auto_answer_quiz(page, question_text, option_texts)
                else:
                    idx = random.randint(0, count - 1)
                    logger.debug(f"No question text, random pick: {idx}")

                await options.nth(idx).click()
                await asyncio.sleep(2)

                # Check for "next" button
                next_btn = page.locator(
                    '#nextQuestionbtn, [class*="nextQuestion"], '
                    'input[value="Next"]'
                )
                if await next_btn.count() > 0:
                    await next_btn.click()
                    await asyncio.sleep(1)

            except Exception as e:
                logger.debug(f"Quiz question {q + 1} error: {e}")
                break

    async def _handle_poll(self, page: Page) -> None:
        """Handle a poll task."""
        logger.debug("Handling poll task...")
        try:
            options = page.locator(
                '#btoption0, #btoption1, .bt_poll .btOption, '
                '[id*="btoption"]'
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
        """Handle a 'This or That' quiz with auto-answer."""
        logger.debug("Handling This or That quiz (auto-answer)...")
        max_rounds = 15

        for r in range(max_rounds):
            try:
                await asyncio.sleep(2)

                # Check if complete
                complete = await page.query_selector(
                    '#quizCompleteContainer, [class*="quizComplete"]'
                )
                if complete:
                    break

                # Read the question/prompt
                question_text = ""
                q_el = await page.query_selector(
                    '.rqQuestion, .btQuestionText, '
                    '#currentQuestionContainer .textContainer'
                )
                if q_el:
                    question_text = (await q_el.text_content() or "").strip()

                # Find the two options
                options = page.locator(
                    '#currentQuestionContainer .btOptionCard, '
                    '.rq_tooltip .btOption, [class*="btOption"]'
                )
                count = await options.count()
                if count >= 2:
                    # Get option texts
                    option_texts = []
                    for i in range(count):
                        text = (await options.nth(i).text_content() or "").strip()
                        option_texts.append(text)

                    # Auto-answer
                    if question_text and any(option_texts):
                        idx = await auto_answer_quiz(page, question_text, option_texts)
                    else:
                        idx = random.randint(0, min(1, count - 1))

                    await options.nth(idx).click()
                    await asyncio.sleep(2)
                elif count == 0:
                    break

            except Exception as e:
                logger.debug(f"This or That round {r + 1} error: {e}")
                break
