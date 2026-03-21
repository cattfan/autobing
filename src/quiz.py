"""
Quiz solver for Microsoft Rewards quizzes.
Handles: This or That, Polls, Lightspeed Quiz, Supersonic, Warpspeed.
"""

import asyncio
import random

from playwright.async_api import Page

from src.utils import logger, retry
from src.humanizer import Humanizer


class QuizSolver:
    """Solves various Microsoft Rewards quiz types."""

    def __init__(self, humanizer: Humanizer):
        self.humanizer = humanizer

    async def detect_and_solve(self, page: Page) -> bool:
        """
        Detect quiz type and solve it.

        Returns:
            True if quiz was detected and solved
        """
        await asyncio.sleep(2)

        quiz_type = await self._detect_quiz_type(page)

        if quiz_type == "this_or_that":
            await self.solve_this_or_that(page)
            return True
        elif quiz_type == "poll":
            await self.solve_poll(page)
            return True
        elif quiz_type == "lightspeed":
            await self.solve_lightspeed(page)
            return True
        elif quiz_type == "supersonic":
            await self.solve_supersonic(page)
            return True
        elif quiz_type == "multiple_choice":
            await self.solve_multiple_choice(page)
            return True
        elif quiz_type == "text_input":
            await self.solve_text_input(page)
            return True

        return False

    async def _detect_quiz_type(self, page: Page) -> str:
        """Detect which type of quiz is on the current page."""
        # This or That
        tot = await page.query_selector(
            '.btOptionCard, [class*="thisOrThat"], '
            '#currentQuestionContainer .btOptions'
        )
        if tot:
            return "this_or_that"

        # Poll
        poll = await page.query_selector(
            '#btoption0, .bt_poll, [data-bi-type="poll"]'
        )
        if poll:
            return "poll"

        # Lightspeed quiz (timed)
        lightspeed = await page.query_selector(
            '.rqQuestionState .countdown, [class*="lightspeed"], '
            '.rq_timer'
        )
        if lightspeed:
            return "lightspeed"

        # Supersonic quiz
        supersonic = await page.query_selector(
            '[class*="supersonic"], .wk_choicesInstContainer'
        )
        if supersonic:
            return "supersonic"

        # Multiple choice
        mc = await page.query_selector(
            '.rqOption, [id*="rqAnswerOption"], '
            '#currentQuestionContainer .rq_button'
        )
        if mc:
            return "multiple_choice"

        # Text input
        text_input = await page.query_selector(
            'input[type="text"][class*="rq"], '
            '#currentQuestionContainer input[type="text"]'
        )
        if text_input:
            return "text_input"

        return "unknown"

    async def solve_this_or_that(self, page: Page, max_rounds: int = 15) -> None:
        """Solve a This or That quiz (uses Bing search for best answer)."""
        logger.info("Solving This or That quiz...")

        for r in range(max_rounds):
            try:
                await asyncio.sleep(2)

                if await self._is_quiz_complete(page):
                    logger.info("This or That complete!")
                    break

                options = page.locator(
                    '.btOptionCard, #currentQuestionContainer .btOption, '
                    '[class*="btOptionCard"]'
                )
                count = await options.count()

                if count >= 2:
                    # Try to use Bing search scoring for smarter answer
                    idx = await self._smart_pick(page, options, count)
                    await options.nth(idx).click()
                    await asyncio.sleep(random.uniform(1.5, 3))
                elif count == 0:
                    break

            except Exception as e:
                logger.debug(f"This or That round {r + 1}: {e}")
                break

    async def solve_poll(self, page: Page) -> None:
        """Solve a poll by clicking a random option."""
        logger.info("Solving poll...")
        try:
            options = page.locator(
                '#btoption0, #btoption1, #btoption2, #btoption3, '
                '.bt_poll .btOption'
            )
            count = await options.count()
            if count > 0:
                idx = random.randint(0, count - 1)
                await self.humanizer.short_delay()
                await options.nth(idx).click()
                await asyncio.sleep(2)
                logger.info("Poll answered")
        except Exception as e:
            logger.warning(f"Poll error: {e}")

    async def solve_lightspeed(self, page: Page, max_questions: int = 5) -> None:
        """Solve a Lightspeed quiz (timed, need quick answers)."""
        logger.info("Solving Lightspeed quiz...")

        for q in range(max_questions):
            try:
                await asyncio.sleep(1)

                if await self._is_quiz_complete(page):
                    break

                options = page.locator(
                    '.rqOption, [id*="rqAnswerOption"], .rq_button'
                )
                count = await options.count()

                if count > 0:
                    # Try all options until correct one found
                    for attempt in range(count):
                        try:
                            await options.nth(attempt).click()
                            await asyncio.sleep(1)

                            # Check if correct (moved to next question or completed)
                            incorrect = await page.query_selector(
                                '.rqOptionStateIncorrect, [class*="incorrect"]'
                            )
                            if not incorrect:
                                break
                        except Exception:
                            continue

                    await asyncio.sleep(1)
                else:
                    break

            except Exception as e:
                logger.debug(f"Lightspeed question {q + 1}: {e}")
                break

    async def solve_supersonic(self, page: Page, max_questions: int = 10) -> None:
        """Solve a Supersonic quiz."""
        logger.info("Solving Supersonic quiz...")

        for q in range(max_questions):
            try:
                await asyncio.sleep(2)

                if await self._is_quiz_complete(page):
                    break

                options = page.locator(
                    '.wk_choicesInstContainer .rq_button, '
                    '.rqOption, [id*="rqAnswerOption"]'
                )
                count = await options.count()

                if count > 0:
                    idx = random.randint(0, count - 1)
                    await options.nth(idx).click()
                    await asyncio.sleep(2)

                    next_btn = page.locator('#nextQuestionbtn')
                    if await next_btn.count() > 0:
                        await next_btn.click()
                        await asyncio.sleep(1)
                else:
                    break

            except Exception as e:
                logger.debug(f"Supersonic question {q + 1}: {e}")
                break

    async def solve_multiple_choice(self, page: Page, max_questions: int = 20) -> None:
        """Solve a generic multiple choice quiz (uses Bing search scoring)."""
        logger.info("Solving multiple choice quiz...")

        for q in range(max_questions):
            try:
                await asyncio.sleep(2)

                if await self._is_quiz_complete(page):
                    break

                options = page.locator(
                    '.rqOption, [id*="rqAnswerOption"], '
                    '#currentQuestionContainer .rq_button'
                )
                count = await options.count()

                if count > 0:
                    # Try smart pick with Bing search
                    idx = await self._smart_pick(page, options, count)
                    await options.nth(idx).click()
                    await asyncio.sleep(2)

                    next_btn = page.locator('#nextQuestionbtn')
                    if await next_btn.count() > 0:
                        await next_btn.click()
                        await asyncio.sleep(1)
                else:
                    break

            except Exception as e:
                logger.debug(f"MC question {q + 1}: {e}")
                break

    async def solve_text_input(self, page: Page) -> None:
        """Handle a text input quiz (just type a random answer)."""
        logger.info("Solving text input quiz...")
        try:
            text_input = page.locator(
                'input[type="text"][class*="rq"], '
                '#currentQuestionContainer input[type="text"]'
            )
            if await text_input.count() > 0:
                await self.humanizer.type_text(
                    page,
                    'input[type="text"]',
                    random.choice(["answer", "yes", "true", "correct"]),
                )
                await asyncio.sleep(1)

                submit = page.locator(
                    'input[type="submit"], #nextQuestionbtn, button[type="submit"]'
                )
                if await submit.count() > 0:
                    await submit.click()
                    await asyncio.sleep(2)

        except Exception as e:
            logger.warning(f"Text input quiz error: {e}")

    async def _is_quiz_complete(self, page: Page) -> bool:
        """Check if the current quiz is complete."""
        complete = await page.query_selector(
            '#quizCompleteContainer, [class*="quizComplete"], '
            '.cico.rqDone, .quizCompleteContainer'
        )
        return complete is not None

    async def _smart_pick(self, page, options_locator, count: int) -> int:
        """Use quiz_solver's Bing search scoring for smarter answer selection.
        
        Falls back to random if scoring fails.
        """
        try:
            from src.quiz_solver import QuizSolver as BingQuizSolver

            # Extract question text
            question_el = await page.query_selector(
                '#currentQuestionContainer .title, '
                '.rqQuestion, .btQuestionText, '
                '#QuestionPane .textSmall, '
                '.wk_questionText'
            )
            question = ""
            if question_el:
                question = await question_el.inner_text()

            if not question:
                return random.randint(0, count - 1)

            # Extract option texts
            option_texts = []
            for i in range(count):
                try:
                    text = await options_locator.nth(i).inner_text()
                    option_texts.append(text.strip())
                except Exception:
                    option_texts.append("")

            # Use Bing search scoring
            solver = BingQuizSolver()
            best_idx = await solver.find_answer(page, question, option_texts)
            if best_idx >= 0:
                return best_idx

        except Exception as e:
            logger.debug(f"Smart pick failed: {e}")

        # Fallback: random
        return random.randint(0, count - 1)

