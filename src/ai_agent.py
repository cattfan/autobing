"""
AI Agent for Microsoft Rewards — powered by OpenRouter API.

Uses LLM to intelligently navigate and complete rewards tasks:
- Reads page content (text, buttons, links)
- Sends to LLM for analysis
- Executes actions (click, fill, scroll, navigate)
- Loops until task is complete

Supports any OpenRouter model (DeepSeek, Gemini, GPT, Claude, Llama...)
"""

from __future__ import annotations
import asyncio
import json
import random
import re
from typing import Any, Callable, Optional

import httpx
from playwright.async_api import Page

from src.utils import logger

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# System prompt — tells the LLM how to act
SYSTEM_PROMPT = """You are a Microsoft Rewards automation assistant. Your job is to analyze web pages and complete rewards tasks.

You receive a PAGE SNAPSHOT containing:
- URL, title
- Visible text content
- Clickable elements (buttons, links) with their selectors

You must respond with a JSON action. Available actions:

1. Click an element:
   {"action": "click", "selector": "CSS_SELECTOR", "reason": "why"}

2. Fill a text input:
   {"action": "fill", "selector": "CSS_SELECTOR", "text": "text to type", "reason": "why"}

3. Scroll the page:
   {"action": "scroll", "direction": "down", "reason": "why"}

4. Go to a URL:
   {"action": "navigate", "url": "https://...", "reason": "why"}

5. Wait for something:
   {"action": "wait", "seconds": 3, "reason": "why"}

6. Answer a quiz question:
   {"action": "answer", "index": 0, "reason": "why this answer is correct"}

7. Task is complete:
   {"action": "done", "reason": "why task is complete"}

8. Task cannot be completed:
   {"action": "skip", "reason": "why"}

RULES:
- ALWAYS respond with valid JSON only, no other text
- Prefer clicking visible, interactive elements
- For quizzes, analyze the question and pick the CORRECT answer
- If the page looks like rewards dashboard, look for uncompleted tasks
- If you see a checkmark or "completed" indicator, skip that task
- If stuck after 3 attempts, respond with {"action": "skip"}
- Be concise in "reason" fields (max 20 words)
"""


class AIAgent:
    """AI Agent that uses LLM to navigate and complete rewards tasks."""

    def __init__(self, settings: dict, on_event: Optional[Callable[[str, str, dict], None]] = None):
        self.api_key = settings.get("ai_api_key", "")
        self.model = settings.get("ai_model", "meta-llama/llama-3.3-70b-instruct:free")
        self.enabled = bool(
            settings.get("ai_enabled", False) and self.api_key
        )
        self._settings_enabled = bool(settings.get("ai_enabled", False))
        self._conversation: list[dict] = []
        self._max_steps = 25  # Safety limit
        self._on_event = on_event
        # Fallback free models (tried in order if primary model is rate-limited)
        self._fallback_models = [
            "openrouter/auto",
            "openrouter/free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "meta-llama/llama-3.3-70b-instruct",
            "meta-llama/llama-4-maverick",
            "meta-llama/llama-4-scout",
            "qwen/qwen3-4b:free",
            "qwen/qwen3-coder:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "qwen/qwen3.5-27b",
            "qwen/qwen3.5-35b-a3b",
            "qwen/qwen3.5-9b",
            "google/gemma-3-27b-it:free",
            "google/gemini-2.5-flash-lite",
            "google/gemini-2.5-flash",
            "deepseek/deepseek-chat",
            "deepseek/deepseek-chat-v3.1",
            "mistralai/mistral-small-3.1-24b-instruct:free",
            "mistralai/mistral-small-2603",
            "anthropic/claude-haiku-4.5",
        ]
        # Smart rotation: remember last model that worked
        self._last_working_model: Optional[str] = None
        self._current_model = self.model

    def _emit(self, level: str, message: str, **meta: Any) -> None:
        """Write AI logs to the app logger and optional dashboard callback."""
        tagged = f"[AI] {message}"

        if self._on_event:
            try:
                self._on_event(level, message, meta)
                return
            except Exception as e:
                logger.debug(f"[AI] Event callback failed: {e}")

        if level == "warning":
            logger.warning(tagged)
        elif level == "debug":
            logger.debug(tagged)
        else:
            logger.info(tagged)

    @staticmethod
    def _short_task_label(task_description: str, limit: int = 80) -> str:
        """Keep task labels compact for logs and dashboard status."""
        cleaned = " ".join((task_description or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3] + "..."

    def _request_models(self) -> list[str]:
        """Return a de-duplicated model chain for OpenRouter fallback routing."""
        ordered: list[str] = []
        for model in [self.model, self._last_working_model, *self._fallback_models]:
            if model and model not in ordered:
                ordered.append(model)
        return ordered

    def _request_payload(self, messages: list[dict], model: str | None = None) -> dict:
        """Build the OpenRouter payload with explicit fallback routing."""
        selected_model = model or self.model
        routed_models = []
        for fallback_model in [self._last_working_model, *self._fallback_models]:
            if fallback_model and fallback_model != selected_model and fallback_model not in routed_models:
                routed_models.append(fallback_model)
        return {
            "model": selected_model,
            "models": routed_models[:3],
            "messages": messages,
            "max_tokens": 300,
            "temperature": 0.1,
        }

    async def _try_fallback_models(
        self,
        client: httpx.AsyncClient,
        messages: list[dict],
        *,
        exclude_model: str = "",
    ) -> httpx.Response | None:
        """Try additional fallback models manually when the primary request fails."""
        ordered_fallbacks = list(self._fallback_models)
        if self._last_working_model and self._last_working_model in ordered_fallbacks:
            ordered_fallbacks.remove(self._last_working_model)
            ordered_fallbacks.insert(0, self._last_working_model)

        for fb_model in ordered_fallbacks:
            if not fb_model or fb_model == exclude_model:
                continue
            self._emit(
                "info",
                f"Thử fallback model {fb_model}.",
                active=True,
                model=fb_model,
                reason="fallback_model",
            )
            await asyncio.sleep(2)
            try:
                response = await client.post(
                    OPENROUTER_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://rewards.bing.com",
                    },
                    json=self._request_payload(messages, fb_model),
                )
                if response.status_code == 200:
                    try:
                        fb_data = response.json()
                        fb_content = fb_data.get("choices", [{}])[0].get("message", {}).get("content")
                        if fb_content and fb_content.strip():
                            self._last_working_model = fb_model
                            self._current_model = fb_model
                            return response
                        self._emit(
                            "warning",
                            f"Model {fb_model} trả về nội dung rỗng, chuyển model khác.",
                            active=True,
                            model=fb_model,
                            reason="empty_content",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            await asyncio.sleep(2)
        return None

    async def _call_llm(self, messages: list[dict]) -> Optional[dict]:
        """Call OpenRouter API and parse JSON response."""
        if not self.api_key:
            self._emit(
                "warning",
                "Bỏ qua AI vì chưa có API key.",
                active=False,
                reason="missing_api_key",
            )
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                self._current_model = self.model
                response = await client.post(
                    OPENROUTER_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://rewards.bing.com",
                    },
                    json=self._request_payload(messages, self.model),
                )

                if response.status_code == 429:
                    # Quick retry once (5s) before fallback
                    self._emit(
                        "warning",
                        f"Model {self.model} bị rate limit, thử lại sau 5 giây.",
                        active=True,
                        model=self.model,
                        reason="rate_limited",
                    )
                    await asyncio.sleep(5)
                    response = await client.post(
                        OPENROUTER_API_URL,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://rewards.bing.com",
                        },
                        json=self._request_payload(messages, self.model),
                    )

                    if response.status_code == 429:
                        fallback_response = await self._try_fallback_models(
                            client,
                            messages,
                            exclude_model=self.model,
                        )
                        if fallback_response is not None:
                            response = fallback_response

                if response.status_code != 200:
                    if response.status_code not in (401, 402, 403):
                        fallback_response = await self._try_fallback_models(
                            client,
                            messages,
                            exclude_model=self._current_model,
                        )
                        if fallback_response is not None:
                            response = fallback_response

                if response.status_code != 200:
                    self._emit(
                        "warning",
                        f"AI API lỗi {response.status_code}: {response.text[:100]}",
                        active=False,
                        model=self._current_model,
                        status_code=response.status_code,
                    )
                    if response.status_code in (401, 402, 403):
                        self.enabled = False
                        self._emit(
                            "warning",
                            "AI bị tắt cho run này do lỗi auth/billing.",
                            active=False,
                            model=self._current_model,
                            reason="auth_or_billing",
                        )
                    elif response.status_code == 429:
                        self.enabled = False
                        self._emit(
                            "warning",
                            "AI bị tắt cho run này do rate limit kéo dài.",
                            active=False,
                            model=self._current_model,
                            reason="persistent_rate_limit",
                        )
                    return None

                data = response.json()
                raw_content = data.get("choices", [{}])[0].get("message", {}).get("content")
                if not raw_content:
                    logger.warning("AI returned empty content")
                    return None
                content = raw_content.strip()

                # Parse JSON from response (handle markdown code blocks)
                if "```" in content:
                    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
                    if match:
                        content = match.group(1)

                # Clean up any non-JSON prefix/suffix
                start = content.find('{')
                end = content.rfind('}')
                if start >= 0 and end >= 0:
                    content = content[start:end + 1]

                action = json.loads(content)
                usage = data.get("usage", {})
                logger.debug(
                    f"AI: {action.get('action')} "
                    f"(tokens: {usage.get('prompt_tokens', '?')}/"
                    f"{usage.get('completion_tokens', '?')})"
                )
                return action

        except json.JSONDecodeError as e:
            self._emit(
                "warning",
                f"AI trả JSON không hợp lệ: {e}",
                active=True,
                model=self._current_model,
                reason="invalid_json",
            )
            return None
        except Exception as e:
            self._emit(
                "warning",
                f"AI call thất bại: {e}",
                active=True,
                model=self._current_model,
                reason="call_failed",
            )
            return None

    async def _get_page_snapshot(self, page: Page, max_text_len: int = 3000) -> str:
        """Extract page content as a compact text snapshot for the LLM."""
        try:
            url = page.url
            title = await page.title()

            # Get visible text (truncated)
            body_text = await page.inner_text("body")
            body_text = ' '.join(body_text.split())  # normalize whitespace
            if len(body_text) > max_text_len:
                body_text = body_text[:max_text_len] + "..."

            # Get clickable elements
            clickables = []
            elements = await page.query_selector_all(
                "a[href]:visible, button:visible, "
                "[role='button']:visible, input[type='submit']:visible, "
                ".rqOption:visible, [id*='rqAnswerOption']:visible, "
                "mee-card:visible, .ds-card-sec:visible, "
                "[class*='card']:visible"
            )

            for i, el in enumerate(elements[:30]):  # Max 30 elements
                text = (await el.text_content() or "").strip()[:60]
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                el_id = await el.get_attribute("id") or ""
                el_class = await el.get_attribute("class") or ""
                href = await el.get_attribute("href") or ""

                if not text and not el_id:
                    continue

                # Build a selector for this element
                if el_id:
                    selector = f"#{el_id}"
                elif text:
                    selector = f"{tag}:has-text('{text[:30]}')"
                else:
                    selector = f".{el_class.split()[0]}" if el_class else tag

                clickables.append(f"  [{i}] {selector} → \"{text}\"")

            clickables_str = "\n".join(clickables) if clickables else "  (no clickable elements found)"

            # Check for quiz elements
            quiz_info = ""
            question_el = await page.query_selector(
                ".rqQuestion, #currentQuestionContainer .textContainer, "
                ".wk_questionText, .btQuestionText"
            )
            if question_el:
                q_text = (await question_el.text_content() or "").strip()
                quiz_info = f"\n🧩 QUIZ DETECTED: \"{q_text}\"\n"

                # Get options
                options = await page.query_selector_all(
                    ".rqOption, [id*='rqAnswerOption'], "
                    ".wk_choicesInstContainer .rq_button, "
                    ".btOptionCard"
                )
                for j, opt in enumerate(options):
                    opt_text = (await opt.text_content() or "").strip()
                    opt_id = await opt.get_attribute("id") or f"option-{j}"
                    quiz_info += f"  Option [{j}]: #{opt_id} → \"{opt_text}\"\n"

            snapshot = (
                f"📄 PAGE SNAPSHOT\n"
                f"URL: {url}\n"
                f"Title: {title}\n"
                f"{quiz_info}"
                f"\n📝 CONTENT:\n{body_text}\n"
                f"\n🔘 CLICKABLE ELEMENTS:\n{clickables_str}"
            )

            return snapshot

        except Exception as e:
            logger.debug(f"Snapshot error: {e}")
            return f"URL: {page.url}\nError reading page: {e}"

    async def _execute_action(self, page: Page, action: dict) -> bool:
        """Execute an AI-decided action on the page."""
        act = action.get("action", "")
        reason = action.get("reason", "")

        try:
            if act == "click":
                selector = action.get("selector", "")
                logger.debug(f"AI click: {selector} ({reason})")
                el = page.locator(selector).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await el.click(timeout=5000)
                    await asyncio.sleep(random.uniform(1, 3))
                    return True
                else:
                    logger.debug(f"AI: element not found: {selector}")
                    return False

            elif act == "fill":
                selector = action.get("selector", "")
                text = action.get("text", "")
                logger.debug(f"AI fill: {selector} = '{text}' ({reason})")
                el = page.locator(selector).first
                await el.fill(text)
                await asyncio.sleep(random.uniform(0.5, 1))
                return True

            elif act == "scroll":
                direction = action.get("direction", "down")
                delta = 400 if direction == "down" else -400
                await page.mouse.wheel(0, delta)
                await asyncio.sleep(random.uniform(1, 2))
                return True

            elif act == "navigate":
                url = action.get("url", "")
                logger.debug(f"AI navigate: {url} ({reason})")
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                return True

            elif act == "wait":
                seconds = min(action.get("seconds", 3), 10)
                await asyncio.sleep(seconds)
                return True

            elif act == "answer":
                # Click a quiz option by index
                idx = action.get("index", 0)
                options = page.locator(
                    ".rqOption, [id*='rqAnswerOption'], "
                    ".wk_choicesInstContainer .rq_button, "
                    ".btOptionCard"
                )
                count = await options.count()
                if 0 <= idx < count:
                    logger.info(f"🧠 AI answer: option [{idx}] ({reason})")
                    await options.nth(idx).click()
                    await asyncio.sleep(2)
                    return True
                return False

            elif act in ("done", "skip"):
                logger.debug(f"AI {act}: {reason}")
                return True

            else:
                logger.debug(f"AI unknown action: {act}")
                return False

        except Exception as e:
            logger.debug(f"AI action error: {e}")
            return False

    async def run_task(self, page: Page, task_description: str) -> dict:
        """
        Run an AI-driven task loop.

        The AI reads the page, decides what to do, executes it, and repeats
        until the task is done or max steps reached.

        Returns:
            Dict with {success, steps, actions}
        """
        if not self.enabled:
            reason = "disabled_in_settings" if not self._settings_enabled else "missing_api_key"
            self._emit(
                "info",
                "AI đang tắt nên bỏ qua fallback này.",
                active=False,
                model=self.model,
                reason=reason,
                task=self._short_task_label(task_description),
            )
            return {"success": False, "steps": 0, "actions": []}

        task_label = self._short_task_label(task_description)
        self._emit(
            "info",
            f"Bắt đầu xử lý: {task_label}",
            active=True,
            model=self.model,
            task=task_label,
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        result = {"success": False, "steps": 0, "actions": []}

        for step in range(self._max_steps):
            try:
                # Get page snapshot
                snapshot = await self._get_page_snapshot(page)

                # Build user message
                user_msg = (
                    f"TASK: {task_description}\n\n"
                    f"STEP {step + 1}/{self._max_steps}\n\n"
                    f"{snapshot}"
                )

                # Keep conversation short (last 4 exchanges only)
                if len(messages) > 9:
                    messages = [messages[0]] + messages[-8:]

                messages.append({"role": "user", "content": user_msg})

                # Call LLM
                action = await self._call_llm(messages)
                if action is None:
                    self._emit(
                        "warning",
                        "Model không trả action hợp lệ, dừng fallback AI.",
                        active=False,
                        model=self._current_model,
                        task=task_label,
                        reason="no_action",
                    )
                    break

                # Record action in conversation
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(action),
                })

                result["steps"] = step + 1
                result["actions"].append(action)
                action_name = action.get("action", "unknown")
                reason = action.get("reason", "")
                self._emit(
                    "info",
                    f"Bước {step + 1}/{self._max_steps}: {action_name}"
                    + (f" - {reason}" if reason else ""),
                    active=True,
                    model=self._current_model,
                    task=task_label,
                    step=step + 1,
                    action=action_name,
                )

                # Check if done
                if action.get("action") == "done":
                    result["success"] = True
                    self._emit(
                        "info",
                        f"Hoàn tất sau {step + 1} bước.",
                        active=False,
                        model=self._current_model,
                        task=task_label,
                        steps=step + 1,
                    )
                    break

                if action.get("action") == "skip":
                    self._emit(
                        "info",
                        f"Bỏ qua: {action.get('reason', '') or 'không xử lý được'}",
                        active=False,
                        model=self._current_model,
                        task=task_label,
                        steps=step + 1,
                        reason="skip",
                    )
                    break

                # Execute action
                success = await self._execute_action(page, action)
                if not success:
                    self._emit(
                        "warning",
                        f"Action '{action_name}' thất bại, sẽ thử cách khác.",
                        active=True,
                        model=self._current_model,
                        task=task_label,
                        step=step + 1,
                        action=action_name,
                        reason="action_failed",
                    )
                    # Tell AI the action failed
                    messages.append({
                        "role": "user",
                        "content": "⚠️ Action failed. Element might not exist. Try a different approach.",
                    })

                # Small delay between steps
                await asyncio.sleep(random.uniform(0.5, 1.5))

            except Exception as e:
                self._emit(
                    "warning",
                    f"Lỗi ở bước {step + 1}: {e}",
                    active=True,
                    model=self._current_model,
                    task=task_label,
                    step=step + 1,
                    reason="step_error",
                )
                continue

        if not result["success"] and result["steps"] >= self._max_steps:
            self._emit(
                "warning",
                f"Chạm giới hạn {self._max_steps} bước.",
                active=False,
                model=self._current_model,
                task=task_label,
                reason="max_steps",
            )

        return result

    async def solve_quiz(self, page: Page, question: str, options: list[str]) -> int:
        """
        Use AI to answer a quiz question.

        Args:
            page: Current page
            question: The quiz question text
            options: List of option texts

        Returns:
            Index of the best answer (0-based)
        """
        if not self.enabled:
            return random.randint(0, len(options) - 1)

        options_str = "\n".join(f"  [{i}] {opt}" for i, opt in enumerate(options))
        prompt = (
            f"Microsoft Rewards quiz question:\n\n"
            f"Question: {question}\n\n"
            f"Options:\n{options_str}\n\n"
            f"Which option is the CORRECT answer? "
            f"Respond with JSON: {{\"action\": \"answer\", \"index\": <number>, \"reason\": \"...\"}}"
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        action = await self._call_llm(messages)
        if action and action.get("action") == "answer":
            idx = action.get("index", 0)
            if 0 <= idx < len(options):
                logger.info(f"🧠 AI quiz: [{idx}] \"{options[idx]}\" ({action.get('reason', '')})")
                return idx

        # Fallback: random
        return random.randint(0, len(options) - 1)

    async def complete_task_on_page(self, page: Page, task_description: str) -> dict:
        """Compatibility wrapper for task-level Rewards fallbacks."""
        return await self.run_task(page, task_description)

    async def complete_earn_page(self, page: Page) -> dict:
        """Use AI to complete all earn page tasks."""
        return await self.run_task(
            page,
            "Go to https://rewards.bing.com/earn and complete ALL uncompleted "
            "tasks. Look for cards with point badges like '+5', '+10', '+50'. "
            "For each uncompleted task:\n"
            "1. Click the task card\n"
            "2. If it opens a quiz - answer all questions correctly then go back\n"
            "3. If it opens a poll - pick any option then go back\n"
            "4. If it opens an article/page - wait 5 seconds then go back\n"
            "5. Navigate back to https://rewards.bing.com/earn\n"
            "6. Click the next uncompleted task\n"
            "7. When ALL tasks show checkmarks/completed, respond 'done'"
        )

    async def complete_daily_set(self, page: Page) -> dict:
        """Use AI to complete Daily Set Streak activities."""
        return await self.run_task(
            page,
            "Go to https://rewards.bing.com. Find the 'Daily Set Streak' card "
            "showing 'Activity: 0/3' (or similar). Click on it to open the "
            "streak panel/popup. Inside you will see 3 daily activities like:\n"
            "- 'Upcoming comedy events' (+5)\n"
            "- 'Eco-friendly style' (+5)\n"
            "- 'Boxing Legend?' (+5) — this is a quiz\n\n"
            "For EACH activity:\n"
            "1. Click the activity card (it has a small external link icon)\n"
            "2. A new page opens. If it's a quiz, answer all questions correctly. "
            "If it's an article, wait 5 seconds to simulate reading.\n"
            "3. Go back to https://rewards.bing.com\n"
            "4. Click the Daily Set Streak card again to re-open the panel\n"
            "5. Click the next uncompleted activity\n"
            "6. When Activity shows 3/3, respond 'done'\n\n"
            "IMPORTANT: Each activity opens in a new tab or navigates away. "
            "Always navigate back to rewards.bing.com after completing each one."
        )

    async def complete_quests(self, page: Page) -> dict:
        """Use AI to complete Quests (multi-step quest cards)."""
        return await self.run_task(
            page,
            "Go to https://rewards.bing.com. Find the 'Quests' section. "
            "You will see quest cards like:\n"
            "- 'Your March guide: Explore global trips...' (+50, 1/4 tasks)\n\n"
            "Click on a quest card to open it. Inside you'll see 'Activities' "
            "with a list of tasks. IMPORTANT: Some tasks are TIME-GATED:\n\n"
            "- Tasks with a ✅ green checkmark = ALREADY COMPLETED, skip these\n"
            "- Tasks with a 🔒 lock icon = TIME-LOCKED (text says 'wait 24 hours "
            "or more after completing Day X'), you CANNOT complete these now\n"
            "- Tasks WITHOUT a lock/checkmark = AVAILABLE to complete\n\n"
            "For each AVAILABLE (unlocked, uncompleted) task:\n"
            "1. Click on the task title or its action button (e.g. 'See updates', "
            "'Plan your visit', 'Try these tools')\n"
            "2. The task may open a new page or tab — wait 5 seconds\n"
            "3. Navigate back to https://rewards.bing.com\n"
            "4. Click the quest card again to check progress\n"
            "5. Move to next available task\n\n"
            "When ALL available tasks are completed (remaining are either ✅ done "
            "or 🔒 locked), respond 'done'. If only locked tasks remain, that's "
            "fine — respond 'done' with message about waiting.\n\n"
            "NOTE: Do NOT try to click locked tasks. They will not unlock until "
            "24+ hours after the previous day's task was completed."
        )

    async def complete_all_rewards(self, page: Page) -> dict:
        """Complete all rewards tasks in sequence: Daily Set → Quests → Earn."""
        results = {"daily_set": {}, "quests": {}, "earn": {}}

        # 1. Daily Set Streak
        logger.info("🤖 AI: Starting Daily Set Streak...")
        results["daily_set"] = await self.complete_daily_set(page)

        # 2. Quests
        logger.info("🤖 AI: Starting Quests...")
        results["quests"] = await self.complete_quests(page)

        # 3. Earn page
        logger.info("🤖 AI: Starting Earn page tasks...")
        results["earn"] = await self.complete_earn_page(page)

        total_steps = sum(r.get("steps", 0) for r in results.values())
        any_success = any(r.get("success", False) for r in results.values())

        logger.info(f"🤖 AI completed all tasks ({total_steps} total steps)")
        return {
            "success": any_success,
            "steps": total_steps,
            "details": results,
        }
