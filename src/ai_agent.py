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
DEFAULT_LOCAL_API_URL = "http://localhost:20128/v1/chat/completions"

# System prompt — tells the LLM how to act
SYSTEM_PROMPT = """You are AutoBing Agent — a Microsoft Rewards 2026 automation specialist.
Your mission: maximize points while keeping the account ABSOLUTELY SAFE with 100% human-like behavior.

=== CORE PRINCIPLES ===
1. Safety > Points > Speed
2. Never click too fast or repeat fixed patterns
3. If UI looks changed or suspicious → skip safely
4. Always verify points after important tasks
5. Homepage is https://rewards.bing.com/ — always return there between tasks
6. NEVER navigate to /earn or /pointsbreakdown (404 in 2026)

=== 6-STEP WORKFLOW (follow for EVERY task) ===
1. OBSERVATION — Analyze URL (must be rewards.bing.com), title, mee-cards, notifications
2. CLASSIFICATION — Identify task type: EdgeStreak, DailySet, Quest, Promotion, Search, Quiz, Poll
3. PLANNING — Create 3-8 step plan with timing and humanizer
4. EXECUTION — Execute plan with appropriate delay level
5. VERIFICATION — Check for completion signals (checkmarks, point changes)
6. REPORT — Return structured JSON to runner

You receive a PAGE SNAPSHOT containing:
- URL, title
- Visible text content
- Clickable elements (buttons, links) with their selectors

=== RESPONSE FORMAT (ALWAYS valid JSON, nothing else) ===
{
  "step": "observation|classification|planning|execution|verification|report",
  "task_type": "EdgeStreak|DailySet|Quest|Promotion|Search|Quiz|Poll|Unknown",
  "thought": "Brief chain-of-thought reasoning",
  "plan": ["step 1", "step 2"],
  "action": {
    "type": "click|fill|scroll|wait|navigate|answer|done|skip",
    "target": "CSS_SELECTOR",
    "value": "text to type (for fill)",
    "url": "https://... (for navigate)",
    "index": 0,
    "humanizer_level": "normal|slow|very_slow"
  },
  "status": "in_progress|success|failed|partial",
  "points_earned": 0,
  "next_step": "what to do next or null",
  "reasoning": "concise explanation (max 20 words)"
}

=== ACTION TYPES ===
- click: {"type": "click", "target": "CSS_SELECTOR", "humanizer_level": "normal"}
- fill:  {"type": "fill", "target": "CSS_SELECTOR", "value": "text"}
- scroll: {"type": "scroll", "target": "down"}
- navigate: {"type": "navigate", "url": "https://..."}
- wait: {"type": "wait", "value": "3"}
- answer: {"type": "answer", "index": 0}
- done: {"type": "done"}
- skip: {"type": "skip"}

humanizer_level: "normal" (0.8-3.2s), "slow" (3-6s), "very_slow" (6-10s)

=== TASK-SPECIFIC RULES ===
- Edge Streak: find card on homepage, click to activate offerId, then native browse 30+ min
- Daily Set: complete in order (Discover → Shop → News), +5 pts each, return to homepage after each
- Quests: check Activities, skip 🔒 locked and ✅ done tasks
- Promotions: don't rush, 4-8s between clicks, max 3-4 per session
- Quizzes: call quiz_solver first, analyze carefully, 1.5-3s per answer
- Search: click 2-4 results randomly + scroll 30-60%
- If 403/429 or challenge → {"action": {"type": "skip"}, "reasoning": "safety_stop"}
- If stuck 3 attempts → {"action": {"type": "skip"}, "reasoning": "stuck"}
"""


class AIAgent:
    """AI Agent that uses LLM to navigate and complete rewards tasks."""

    def __init__(self, settings: dict, on_event: Optional[Callable[[str, str, dict], None]] = None, humanizer=None):
        self.api_key = settings.get("ai_api_key", "")
        self.model = settings.get("ai_model", "cx/gpt-5.4")
        self.humanizer = humanizer  # Humanizer instance for human-like actions

        # Support custom API base URL (local LLM)
        custom_url = settings.get("ai_api_url", "").rstrip("/")
        if custom_url:
            self._api_url = (
                custom_url + "/chat/completions"
                if not custom_url.endswith("/chat/completions")
                else custom_url
            )
            self._is_local = True
        else:
            self._api_url = OPENROUTER_API_URL
            self._is_local = False

        # Local endpoints don't require API key
        self.enabled = bool(
            settings.get("ai_enabled", False)
            and (self.api_key or self._is_local)
        )
        self._settings_enabled = bool(settings.get("ai_enabled", False))
        self._conversation: list[dict] = []
        self._max_steps = 25  # Safety limit
        self._on_event = on_event
        # Fallback free models (only used for OpenRouter)
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

        if self.enabled:
            endpoint_label = "Local" if self._is_local else "OpenRouter"
            logger.info(f"[AI] {endpoint_label} endpoint: {self._api_url} | model: {self.model}")

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
        if not self.api_key and not self._is_local:
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
                headers = {"Content-Type": "application/json"}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                if not self._is_local:
                    headers["HTTP-Referer"] = "https://rewards.bing.com"
                response = await client.post(
                    self._api_url,
                    headers=headers,
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
                        self._api_url,
                        headers=headers,
                        json=self._request_payload(messages, self.model),
                    )

                    if response.status_code == 429 and not self._is_local:
                        fallback_response = await self._try_fallback_models(
                            client,
                            messages,
                            exclude_model=self.model,
                        )
                        if fallback_response is not None:
                            response = fallback_response

                if response.status_code != 200:
                    if response.status_code not in (401, 402, 403) and not self._is_local:
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
                    match = re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', content, re.DOTALL)
                    if match:
                        content = match.group(1)

                # Clean up any non-JSON prefix/suffix
                # Support both JSON objects {...} and arrays [...]
                obj_start = content.find('{')
                obj_end = content.rfind('}')
                arr_start = content.find('[')
                arr_end = content.rfind(']')

                if arr_start >= 0 and arr_end >= 0 and (obj_start < 0 or arr_start < obj_start):
                    # Array comes first — parse as array
                    content = content[arr_start:arr_end + 1]
                elif obj_start >= 0 and obj_end >= 0:
                    content = content[obj_start:obj_end + 1]

                action = json.loads(content)
                usage = data.get("usage", {})
                if isinstance(action, dict):
                    logger.debug(
                        f"AI: {action.get('action')} "
                        f"(tokens: {usage.get('prompt_tokens', '?')}/"
                        f"{usage.get('completion_tokens', '?')})"
                    )
                else:
                    logger.debug(
                        f"AI: returned {type(action).__name__}[{len(action)}] "
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
                "mee-rewards-daily-set-item:visible, "
                "mee-rewards-more-activities-card-item:visible, "
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

    @staticmethod
    def _normalize_v2_response(raw: dict) -> dict:
        """Convert V2 nested format to flat format for _execute_action().

        Supports both V1 flat format and V2 nested format:
        V1: {"action": "click", "selector": "...", "reason": "..."}
        V2: {"action": {"type": "click", "target": "..."}, "reasoning": "..."}
        """
        action_field = raw.get("action", {})

        # V1 format: action is a string
        if isinstance(action_field, str):
            return raw

        # V2 format: action is a dict with "type"
        if isinstance(action_field, dict):
            flat = {
                "action": action_field.get("type", "skip"),
                "selector": action_field.get("target", ""),
                "text": action_field.get("value", ""),
                "url": action_field.get("url", ""),
                "index": action_field.get("index", 0),
                "direction": action_field.get("target", "down"),  # scroll uses target for direction
                "seconds": int(action_field.get("value", 3) or 3) if action_field.get("type") == "wait" else 3,
                "humanizer_level": action_field.get("humanizer_level", "normal"),
                "reason": raw.get("reasoning", raw.get("thought", "")),
                # Preserve V2 metadata
                "_v2_step": raw.get("step", ""),
                "_v2_task_type": raw.get("task_type", ""),
                "_v2_status": raw.get("status", ""),
                "_v2_points_earned": raw.get("points_earned", 0),
            }
            return flat

        # Fallback
        return raw

    async def _execute_action(self, page: Page, action: dict) -> bool:
        """Execute an AI-decided action on the page with humanizer enforcement."""
        act = action.get("action", "")
        reason = action.get("reason", "")
        h_level = action.get("humanizer_level", "normal")

        # Humanizer delay ranges based on level
        delay_map = {
            "normal": (0.8, 3.2),
            "slow": (3.0, 6.0),
            "very_slow": (6.0, 10.0),
        }
        delay_lo, delay_hi = delay_map.get(h_level, (0.8, 3.2))

        try:
            if act == "click":
                selector = action.get("selector", "")
                logger.debug(f"AI click: {selector} ({reason})")
                el = page.locator(selector).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    # Use humanizer if available, otherwise fallback
                    if self.humanizer:
                        try:
                            await self.humanizer.human_click(page, selector)
                        except Exception:
                            await el.click(timeout=5000)
                    else:
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        await el.click(timeout=5000)
                    await asyncio.sleep(random.uniform(delay_lo, delay_hi))
                    return True
                else:
                    logger.debug(f"AI: element not found: {selector}")
                    return False

            elif act == "fill":
                selector = action.get("selector", "")
                text = action.get("text", "")
                logger.debug(f"AI fill: {selector} = '{text}' ({reason})")
                if self.humanizer:
                    try:
                        await self.humanizer.type_text(page, selector, text)
                    except Exception:
                        el = page.locator(selector).first
                        await el.fill(text)
                else:
                    el = page.locator(selector).first
                    await el.fill(text)
                await asyncio.sleep(random.uniform(delay_lo, delay_hi))
                return True

            elif act == "scroll":
                direction = action.get("direction", "down")
                if self.humanizer:
                    await self.humanizer.natural_scroll(page, direction, random.randint(200, 400))
                else:
                    delta = 400 if direction == "down" else -400
                    await page.mouse.wheel(0, delta)
                await asyncio.sleep(random.uniform(1, 3))
                return True

            elif act == "navigate":
                url = action.get("url", "")
                logger.debug(f"AI navigate: {url} ({reason})")
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(random.uniform(2, 4))
                return True

            elif act == "wait":
                seconds = min(action.get("seconds", 3), 10)
                await asyncio.sleep(seconds)
                return True

            elif act == "answer":
                # Click a quiz option by index — use slow timing per spec
                idx = action.get("index", 0)
                options = page.locator(
                    ".rqOption, [id*='rqAnswerOption'], "
                    ".wk_choicesInstContainer .rq_button, "
                    ".btOptionCard"
                )
                count = await options.count()
                if 0 <= idx < count:
                    logger.info(f"🧠 AI answer: option [{idx}] ({reason})")
                    # Spec: 1.5-3s per answer, 2-4s between questions
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    if self.humanizer:
                        try:
                            box = await options.nth(idx).bounding_box()
                            if box:
                                await self.humanizer.bezier_move(
                                    page,
                                    int(box["x"] + box["width"] / 2),
                                    int(box["y"] + box["height"] / 2),
                                )
                        except Exception:
                            pass
                    await options.nth(idx).click()
                    await asyncio.sleep(random.uniform(2, 4))
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

        consecutive_failures = 0
        max_failures = 3

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

                # Normalize V2 nested format → flat for _execute_action
                flat_action = self._normalize_v2_response(action)
                action_name = flat_action.get("action", "unknown")
                reason = flat_action.get("reason", "")
                v2_task_type = flat_action.get("_v2_task_type", "")
                task_type_str = f" [{v2_task_type}]" if v2_task_type else ""

                self._emit(
                    "info",
                    f"Bước {step + 1}/{self._max_steps}: {action_name}{task_type_str}"
                    + (f" - {reason}" if reason else ""),
                    active=True,
                    model=self._current_model,
                    task=task_label,
                    step=step + 1,
                    action=action_name,
                )

                # Check if done
                if flat_action.get("action") == "done":
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

                if flat_action.get("action") == "skip":
                    self._emit(
                        "info",
                        f"Bỏ qua: {flat_action.get('reason', '') or 'không xử lý được'}",
                        active=False,
                        model=self._current_model,
                        task=task_label,
                        steps=step + 1,
                        reason="skip",
                    )
                    break

                # Execute action (using normalized flat format)
                success = await self._execute_action(page, flat_action)
                if not success:
                    consecutive_failures += 1
                    self._emit(
                        "warning",
                        f"Action '{action_name}' thất bại, sẽ thử cách khác ({consecutive_failures}/{max_failures}).",
                        active=True,
                        model=self._current_model,
                        task=task_label,
                        step=step + 1,
                        action=action_name,
                        reason="action_failed",
                    )
                    if consecutive_failures >= max_failures:
                        self._emit(
                            "warning",
                            f"Dừng tác vụ AI do thất bại liên tiếp {max_failures} lần.",
                            active=False,
                            model=self._current_model,
                            task=task_label,
                            reason="max_failures_reached"
                        )
                        break
                        
                    # Tell AI the action failed
                    messages.append({
                        "role": "user",
                        "content": "⚠️ Action failed. Element might not exist or is unclickable. Try a completely different approach or skip.",
                    })
                else:
                    consecutive_failures = 0

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

    async def generate_search_query(self, task_description: str) -> str | None:
        """
        Use AI to generate a contextual search query based on the task description.
        If AI is disabled or fails, returns None.
        """
        if not self.enabled:
            return None

        prompt = (
            f"Microsoft Rewards task requires a search:\n\n"
            f"Task: \"{task_description}\"\n\n"
            f"What is a highly relevant, single search query I should type into Bing "
            f"to accurately complete this task? Make it realistic.\n"
            f"Respond with JSON ONLY: {{\"action\": \"search\", \"query\": \"your search term\"}}"
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        action = await self._call_llm(messages)
        if action and action.get("action") in ("search", "fill"):
            query = action.get("query") or action.get("text", "")
            query = query.strip()
            if query:
                logger.info(f"🧠 AI generated contextual search query: \"{query}\"")
                return query

        return None

    async def complete_task_on_page(self, page: Page, task_description: str) -> dict:
        """Compatibility wrapper for task-level Rewards fallbacks."""
        return await self.run_task(page, task_description)

    async def complete_earn_page(self, page: Page) -> dict:
        """Use AI to complete all earn page tasks."""
        return await self.run_task(
            page,
            "Go to https://rewards.bing.com/ and complete ALL uncompleted "
            "tasks. SCROLL DOWN FULLY first — the page has lazy-loaded sections.\n"
            "Look for these sections:\n"
            "  - 'Keep earning' (cards with +5, +10, etc. — quizzes, polls, visits)\n"
            "  - 'Explore on Bing' (12 cards, +10 each, 'Search on Bing for ...')\n"
            "  - 'Trending now', 'Discover', or any other new card section\n"
            "  - 'Quests' (multi-step quest cards with subtasks)\n\n"
            "For each uncompleted task:\n"
            "1. Click the task card\n"
            "2. If it opens a quiz - answer all questions correctly then go back\n"
            "3. If it opens a poll - pick any option then go back\n"
            "4. If it opens an article/search page - wait 5 seconds then go back\n"
            "5. Navigate back to https://rewards.bing.com/\n"
            "6. Click the next uncompleted task\n"
            "7. Skip cards that say 'Completed' or have a checkmark\n"
            "8. When ALL tasks show checkmarks/completed, respond 'done'"
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
            "5. Move to the next available task inside the Quest\n\n"
            "CRITICAL INSTRUCTION FOR MULTIPLE QUEST CARDS:\n"
            "There may be MULTIPLE Quest cards on the page. You MUST check ALL of them! "
            "If you hit a 🔒 locked task inside one Quest card, close its panel/modal and click the NEXT Quest card on the page.\n\n"
            "When ALL available tasks across ALL quest cards are completed (remaining are either ✅ done "
            "or 🔒 locked), ONLY THEN respond 'done'. If only locked tasks remain across all cards, "
            "respond 'done' with a message about waiting."
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
