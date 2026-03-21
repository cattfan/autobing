"""
Quiz Auto-Answer for Microsoft Rewards.
Searches Bing to find correct answers instead of random clicking.
"""

import asyncio
import random
import re
from typing import Optional

from playwright.async_api import Page

from src.utils import logger


class QuizSolver:
    """Auto-answers Rewards quizzes by searching Bing for answers."""

    async def find_answer(self, page: Page, question: str, options: list[str]) -> int:
        """Search Bing for the question and find the best matching option.

        Args:
            page: Browser page (used for in-page fetch)
            question: The quiz question text
            options: List of answer option texts

        Returns:
            Index of the best answer, or -1 if no confident match
        """
        if not question or not options:
            return -1

        try:
            # Clean question text
            question = self._clean_text(question)
            logger.info(f"🧠 Quiz question: \"{question}\"")
            logger.info(f"   Options: {options}")

            # Search Bing for the answer
            search_text = await self._search_bing(page, question)

            if not search_text:
                logger.debug("No search results, falling back to random")
                return -1

            # Score each option against search results
            scores = []
            for i, opt in enumerate(options):
                opt_clean = self._clean_text(opt)
                score = self._score_option(opt_clean, search_text, question)
                scores.append(score)
                logger.debug(f"   Option {i}: \"{opt_clean}\" → score={score:.2f}")

            # Pick best answer if confident enough
            max_score = max(scores)
            if max_score > 0:
                best_idx = scores.index(max_score)
                logger.info(f"✅ Best answer: [{best_idx}] \"{options[best_idx]}\" (score={max_score:.2f})")
                return best_idx

            logger.debug("No confident match found")
            return -1

        except Exception as e:
            logger.debug(f"Quiz solver error: {e}")
            return -1

    async def _search_bing(self, page: Page, question: str) -> str:
        """Search Bing using httpx (backend-side) to avoid disrupting browser state."""
        import urllib.parse
        import httpx

        encoded = urllib.parse.quote_plus(question)
        url = f"https://www.bing.com/search?q={encoded}"

        try:
            # Get cookies from browser to authenticate the search
            cookies = await page.context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                                   if "bing.com" in c.get("domain", ""))

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
                        ),
                        "Cookie": cookie_str,
                        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
                    },
                )

                if resp.status_code != 200:
                    return ""

                html = resp.text

                # Parse search results from HTML using regex (no browser needed)
                parts = []

                # Snippets
                for m in re.finditer(r'<p[^>]*class="[^"]*b_(?:caption|algoSlug|snippetBigText)[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL):
                    parts.append(re.sub(r'<[^>]+>', '', m.group(1)))

                # Titles
                for m in re.finditer(r'<h2[^>]*><a[^>]*>(.*?)</a></h2>', html, re.DOTALL):
                    parts.append(re.sub(r'<[^>]+>', '', m.group(1)))

                # Knowledge panel
                for m in re.finditer(r'<div[^>]*class="[^"]*b_(?:entityTP|factrow|ans|focusTextLarge)[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL):
                    parts.append(re.sub(r'<[^>]+>', '', m.group(1)))

                return " \n ".join(parts)[:5000]

        except Exception as e:
            logger.debug(f"Bing search httpx error: {e}")
            return ""

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text for comparison."""
        text = re.sub(r'<[^>]+>', '', text)  # Remove HTML tags
        text = re.sub(r'\s+', ' ', text).strip()  # Normalize whitespace
        return text.lower()

    def _score_option(self, option: str, search_text: str, question: str) -> float:
        """Score an option based on how well it matches search results.

        Uses multiple signals:
        1. Exact match in search text
        2. Word overlap
        3. Proximity to question keywords in search text
        """
        search_lower = search_text.lower()
        option_lower = option.lower()
        score = 0.0

        # 1. Exact match (strongest signal)
        if option_lower in search_lower:
            score += 5.0
            # Count occurrences — more = more confident
            count = search_lower.count(option_lower)
            score += min(count * 0.5, 3.0)

        # 2. Word overlap
        option_words = set(self._get_meaningful_words(option_lower))
        search_words = set(self._get_meaningful_words(search_lower))

        if option_words:
            overlap = len(option_words & search_words)
            ratio = overlap / len(option_words)
            score += ratio * 3.0

        # 3. Proximity: option words near question words in search text
        question_words = set(self._get_meaningful_words(question.lower()))
        if option_words and question_words:
            # Check if option words appear near question words
            for o_word in option_words:
                if o_word in search_lower:
                    for q_word in question_words:
                        if q_word in search_lower:
                            # Find distance between them
                            o_pos = search_lower.find(o_word)
                            q_pos = search_lower.find(q_word)
                            distance = abs(o_pos - q_pos)
                            if distance < 200:
                                score += 1.0
                            elif distance < 500:
                                score += 0.3

        # 4. Number matching (for numeric answers)
        option_numbers = set(re.findall(r'\d+', option_lower))
        search_numbers = set(re.findall(r'\d+', search_lower))
        if option_numbers:
            number_overlap = len(option_numbers & search_numbers)
            score += number_overlap * 2.0

        return score

    def _get_meaningful_words(self, text: str) -> list[str]:
        """Get meaningful words (skip stop words)."""
        stop_words = {
            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
            'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
            'would', 'could', 'should', 'may', 'might', 'shall',
            'and', 'but', 'or', 'nor', 'not', 'no', 'so', 'yet',
            'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
            'from', 'as', 'into', 'about', 'after', 'before',
            'it', 'its', 'this', 'that', 'these', 'those',
            'i', 'me', 'my', 'he', 'him', 'his', 'she', 'her',
            'we', 'us', 'our', 'they', 'them', 'their',
            'what', 'which', 'who', 'whom', 'how', 'when', 'where', 'why',
        }
        words = re.findall(r'\w+', text)
        return [w for w in words if len(w) > 1 and w not in stop_words]


# ─── Convenience function ────────────────────────────────────

async def auto_answer_quiz(page: Page, question: str, options: list[str]) -> int:
    """Quick function to find the best answer for a quiz question.

    Returns the index of the best answer, or a random index if unsure.
    """
    solver = QuizSolver()
    idx = await solver.find_answer(page, question, options)

    if idx >= 0:
        return idx

    # Fallback: random answer
    fallback_idx = random.randint(0, len(options) - 1)
    logger.info(f"🎲 No confident answer, random pick: [{fallback_idx}] \"{options[fallback_idx]}\"")
    return fallback_idx
