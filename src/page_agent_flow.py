"""
Page-Agent Flow Integration for Autobing.

Injects Alibaba's page-agent (custom IIFE bundle) into Playwright-controlled
browser and provides a Python interface for LLM-powered DOM automation.

Architecture:
  Python (page_agent_flow.py)
    → page.evaluate() injects IIFE bundle
    → window.__pa_createAgent(config) creates agent with Rewards system prompt
    → agent.execute(task) runs observe→think→act loop via LLM tool-calling
    → Python polls for results or waits for completion

Usage:
    from src.page_agent_flow import PageAgentFlow

    pa = PageAgentFlow(settings)
    await pa.inject(page)
    result = await pa.execute_task(page, "Complete the Daily Set quiz")
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from src.utils import logger

VENDOR_DIR = Path(__file__).parent.parent / "vendor"
PAGE_AGENT_BUNDLE = VENDOR_DIR / "page-agent.demo.js"

# ── Rewards Domain Knowledge ────────────────────────────────────────────
# This is injected as `instructions.system` so the page-agent's built-in
# system prompt (DOM observation, tool calling) stays intact, and this
# knowledge is APPENDED as domain-specific context.

REWARDS_INSTRUCTIONS = """\
=== MICROSOFT REWARDS 2026 — DOMAIN KNOWLEDGE ===

You are operating on Microsoft Rewards (rewards.bing.com). Follow these rules:

## SAFETY RULES (Non-negotiable)
- NEVER navigate to /earn or /pointsbreakdown (404 in 2026)
- Homepage is https://rewards.bing.com/ — always return there between tasks
- If you see a CAPTCHA, verification challenge, or "unusual activity" warning → STOP immediately
- If a link opens `microsoft-edge://` protocol → DO NOT click it, skip the task
- If clicking a card opens a new tab, complete the activity in that tab, then close it

## TASK TYPES YOU WILL ENCOUNTER

### Daily Set (3 cards per day)
- Located at top of homepage in a "Daily Set" section
- Each card is one of: article visit, quiz, or poll
- Completed cards show a GREEN CHECKMARK (✓ or ✅)
- Click incomplete cards → complete activity → return to homepage
- Quiz: answer questions using on-page clues (JS exposes `_w.rewardsQuizRenderInfo`)
- Poll: click any option (#btoption0 or #btoption1)
- Visit: scroll page, wait 5-10 seconds

### Quizzes (Multiple Subtypes)
- "Start Quiz" button: #rqStartQuiz or input[value="Start playing"]
- Answer options: #rqAnswerOption0, #rqAnswerOption1, etc.
- Correct answer attribute: `iscorrectoption="True"` or `data-option` matching `correctAnswer`
- This-or-That: 10 rounds, pick between 2 images
- Lightspeed: timed quiz, answer fast
- Supersonic: 8 options, click all correct ones
- Multiple Choice: 2-4 text options
- Wait 1.5-3 seconds between answers

### Polls
- Simple choice: click #btoption0 or #btoption1
- No correct answer — any option works
- Wait 2 seconds after clicking

### Keep Earning / More Activities
- Scroll down on homepage to find "Keep earning" or "More activities" section
- Cards with point badges (+5, +10, +20)
- Most are visit tasks: click → spend 5-8 seconds → return
- Some require searching on Bing (search box at bing.com)
- SKIP cards mentioning "referr" or "invite friends" (require external action)
- SKIP cards mentioning "3 days" or "consecutive days" (multi-day, can't complete now)

### Explore on Bing
- Dynamic cards near bottom of homepage
- Usually link to bing.com/search?q=... 
- Visit each link, spend 3-5 seconds, return

## COMPLETION SIGNALS
- Green checkmark ✓ or ✅ on card
- CSS class containing "complete" or "is-complete"
- Point counter increases
- Card disappears from incomplete list

## TIMING GUIDELINES
- Poll answers: 1-2 seconds
- Quiz answers: 1.5-3 seconds between answers
- Visit/article: 5-10 seconds on page
- Punch card visits: 12-18 seconds (longer tracking)
- Between tasks: 2-4 seconds

## IMPORTANT DOM SELECTORS
- Task cards: mee-card, mee-rewards-daily-set-item, .ds-card-sec
- More activities: mee-rewards-more-activities-card-item
- Quiz elements: .rqOption, [id*='rqAnswerOption'], .btOptionCard
- Search box: #sb_form_q, input[name='q']
- Points display: #id_rc (rewards counter)
"""


class PageAgentFlow:
    """Manages page-agent injection and flow execution."""

    def __init__(self, settings: dict):
        self.settings = settings
        self._bundle_source: str | None = None

    # ── helpers ──────────────────────────────────────────────────

    def _resolve_bundle_path(self) -> Path:
        path = Path(
            self.settings.get("page_agent_bundle_path", str(PAGE_AGENT_BUNDLE))
        )
        if not path.is_absolute():
            path = Path(__file__).parent / path
        if not path.exists():
            raise FileNotFoundError(f"Page-agent bundle not found at {path}")
        return path

    def _get_bundle_source(self) -> str:
        """Read the bundle JS source once and cache it."""
        if self._bundle_source is None:
            path = self._resolve_bundle_path()
            self._bundle_source = path.read_text(encoding="utf-8")
            logger.info(f"[PageAgent] Loaded bundle ({len(self._bundle_source)} chars)")
        return self._bundle_source

    def _build_config(self) -> dict:
        """Build the LLM config dict from settings.
        NOTE: baseURL modified to use local Playwright API interceptor to bypass CORS!
        """
        return {
            "model": self.settings.get("page_agent_llm_model", "cx/gpt-5.4"),
            "baseURL": "/__pa_llm_proxy",
            "apiKey": self.settings.get("page_agent_llm_api_key", "dummy"),
            "language": self.settings.get("page_agent_language", "en-US"),
            "enableMask": False,
            "promptForNextTask": False,
            "maxSteps": int(self.settings.get("page_agent_max_steps", 40)),
            "stepDelay": 0.5,
            # Inject Rewards domain knowledge via instructions (additive)
            "instructions": {
                "system": REWARDS_INSTRUCTIONS,
            },
        }

    async def _setup_proxy_route(self, page: Page) -> None:
        """Intercept `__pa_llm_proxy` from browser and forward as Python Request to LLM API to completely bypass CORS."""
        target_base = self.settings.get("page_agent_llm_base_url", "http://localhost:20128/v1")
        if target_base.endswith("/"):
            target_base = target_base[:-1]

        async def route_handler(route, request):
            if request.method == "OPTIONS":
                await route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"})
                return

            relative_path = request.url.split("__pa_llm_proxy")[1]
            actual_url = f"{target_base}{relative_path}"
            
            headers = {k: v for k, v in request.headers.items() 
                       if k.lower() not in ["origin", "referer", "host"]}
            # Must explicitly set auth if the actual URL needs it
            api_key = self.settings.get("page_agent_llm_api_key", "dummy")
            if api_key:
                headers["authorization"] = f"Bearer {api_key}"

            try:
                import httpx
                async with httpx.AsyncClient(timeout=180.0) as client:
                    resp = await client.request(
                        request.method,
                        actual_url,
                        content=request.post_data_buffer,
                        headers=headers
                    )
                    
                    forward_headers = dict(resp.headers)
                    forward_headers["access-control-allow-origin"] = "*"
                    if "content-encoding" in forward_headers:
                        del forward_headers["content-encoding"] # Let playwright handle encoding
                        
                    await route.fulfill(
                        status=resp.status_code,
                        headers=forward_headers,
                        body=resp.content
                    )
            except Exception as e:
                logger.error(f"[PageAgent] LLM proxy error for {actual_url}: {e}")
                await route.abort()

        await page.route("**/__pa_llm_proxy/**", route_handler)

    # ── injection ────────────────────────────────────────────────

    async def inject(self, page: Page) -> bool:
        """Inject page-agent into a Playwright page.

        Steps:
        1. Evaluate the IIFE source (registers window.PageAgent and
           window.__pa_createAgent helper)
        2. Call __pa_createAgent(config) to create the agent instance

        Returns True if injection succeeded.
        """
        config = self._build_config()
        config_json = json.dumps(config)
        bundle_source = self._get_bundle_source()

        # Step 0: Setup LLM Proxy Route to bypass CORS
        await self._setup_proxy_route(page)

        # Step 1: Load the IIFE bundle
        try:
            await page.evaluate(bundle_source)
        except Exception as e:
            logger.error(f"[PageAgent] Failed to load bundle: {e}")
            return False

        # Step 2: Create agent via the helper exposed by our custom build
        init_js = f"window.__pa_createAgent({config_json})"
        try:
            await page.evaluate(init_js)
            logger.info("[PageAgent] Injected and initialized ✅")
            return True
        except Exception as e:
            logger.error(f"[PageAgent] Injection failed: {e}")
            return False

    async def is_ready(self, page: Page) -> bool:
        """Check if page-agent is ready on the page."""
        try:
            return await page.evaluate("!!window.__pa_ready")
        except Exception:
            return False

    # ── execution ────────────────────────────────────────────────

    async def execute_task(
        self,
        page: Page,
        task: str,
        *,
        timeout: float = 180.0,
        auto_inject: bool = True,
    ) -> dict[str, Any]:
        """Execute a natural-language task via page-agent.

        The agent will loop (observe → think → act) until done or
        maxSteps is reached.

        Args:
            page: Playwright Page
            task: NL instruction (e.g. "Complete all Daily Set tasks")
            timeout: Python-side timeout in seconds
            auto_inject: Auto-inject if agent not ready

        Returns:
            dict: {success: bool, data: str, steps: int, history: list, task: str}
        """
        if auto_inject and not await self.is_ready(page):
            ok = await self.inject(page)
            if not ok:
                return {"success": False, "data": "Injection failed", "steps": 0, "history": [], "task": task, "macro_trace": []}

        # Keep trace in Python side to survive page navigations
        macro_trace = []
        
        # Try exposing the binding securely if it hasn't been exposed yet onto the context
        try:
            async def _pa_log_macro_trace(source, data):
                macro_trace.append(data)
                # Expose real-time agent reasoning steps to the CLI and Web Dashboard logger!
                action = data.get("type", "ACT").upper()
                tag = data.get("tag", "ELEM").lower()
                val = data.get("value", "")
                val_str = f" » '{val}'" if val else ""
                
                logger.info(f"    🧠 [AI Agent] {action} <{tag}>{val_str}")
                
            await page.context.expose_binding("__pa_log_macro_trace", _pa_log_macro_trace)
        except Exception:
            pass # Already exposed on this context 

        safe_task = json.dumps(task)

        logger.info(f"[PageAgent] Executing task: {task[:120]}")

        exec_js = f"""
        (async () => {{
            const agent = window.__pa;
            if (!agent) return {{ success: false, data: 'Agent not initialised' }};

            // Patch PageController to extract xpath traces
            if (!agent._is_patched) {{
                agent._is_patched = true;
                const pc = agent.pageController;
                
                // Polyfill xpath generator
                window._paGetXPath = function(el) {{
                    if (!el) return '';
                    if (el.id) return `//*[@id="${{el.id}}"]`;
                    if (el.tagName === 'BODY') return '/html/body';
                    let ix = 0;
                    let siblings = el.parentNode ? el.parentNode.childNodes : [];
                    for (let i = 0; i < siblings.length; i++) {{
                        let sibling = siblings[i];
                        if (sibling === el)
                            return window._paGetXPath(el.parentNode) + '/' + el.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                        if (sibling.nodeType === 1 && sibling.tagName === el.tagName)
                            ix++;
                    }}
                    return '';
                }};
                
                const origClick = pc.clickElement.bind(pc);
                pc.clickElement = async function(index) {{
                    const el = pc.selectorMap.get(index);
                    if (el && window.__pa_log_macro_trace) {{
                        const computedXpath = el.xpath || window._paGetXPath(el.ref);
                        await window.__pa_log_macro_trace({{ type: 'click', xpath: computedXpath, attributes: el.attributes, tag: el.tagName }});
                    }}
                    return await origClick(index);
                }};
                
                const origInput = pc.inputText.bind(pc);
                pc.inputText = async function(index, text) {{
                    const el = pc.selectorMap.get(index);
                    if (el && window.__pa_log_macro_trace) {{
                        const computedXpath = el.xpath || window._paGetXPath(el.ref);
                        await window.__pa_log_macro_trace({{ type: 'fill', xpath: computedXpath, attributes: el.attributes, tag: el.tagName, value: text }});
                    }}
                    return await origInput(index, text);
                }};
            }}

            try {{
                const result = await agent.execute({safe_task});
                return {{
                    success: !!result?.success,
                    data: result?.data ?? '',
                    steps: result?.history?.length ?? 0,
                    history: result?.history || [],
                }};
            }} catch (e) {{
                return {{ success: false, data: e.message || String(e) }};
            }}
        }})()
        """

        try:
            result = await asyncio.wait_for(
                page.evaluate(exec_js),
                timeout=timeout,
            )
            success = result.get("success", False)
            result["task"] = task
            result["macro_trace"] = macro_trace
            level = "info" if success else "warning"
            getattr(logger, level)(
                f"[PageAgent] Task {'OK ✅' if success else 'FAILED ❌'}: "
                f"{result.get('data', '')[:120]}  ({result.get('steps', '?')} steps)"
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"[PageAgent] Task timed out after {timeout}s")
            try:
                await page.evaluate("window.__pa?.stop()")
            except Exception:
                pass
            return {"success": False, "data": f"Timeout ({timeout}s)", "steps": 0, "history": [], "task": task, "macro_trace": macro_trace}
        except Exception as e:
            # If navigation occurs, context is destroyed but we still have the trace!
            logger.error(f"[PageAgent] Task exception (possible navigation): {e}")
            return {"success": True, "data": str(e), "steps": len(macro_trace), "history": [], "task": task, "macro_trace": macro_trace}

    # ── convenience: single-task with full lifecycle ─────────────

    async def run_single_task(
        self,
        page: Page,
        task_instruction: str,
        *,
        timeout: float = 180.0,
        navigate_to: str | None = None,
    ) -> dict[str, Any]:
        """Inject agent, optionally navigate, run one task, dispose.

        This is the recommended entry point for universal_task.py integration.
        """
        try:
            if navigate_to:
                await page.goto(navigate_to, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(3)

            result = await self.execute_task(page, task_instruction, timeout=timeout)
            return result
        except Exception as e:
            logger.error(f"[PageAgent] run_single_task failed: {e}")
            return {"success": False, "data": str(e), "steps": 0}
        finally:
            await self.dispose(page)

    async def stop(self, page: Page) -> None:
        """Stop any running page-agent task."""
        try:
            await page.evaluate("window.__pa?.stop()")
        except Exception:
            pass

    async def dispose(self, page: Page) -> None:
        """Dispose the page-agent instance."""
        try:
            await page.evaluate("""
            if (window.__pa) { window.__pa.dispose(); window.__pa = null; window.__pa_ready = false; }
            """)
        except Exception:
            pass

    # ── flow runner ──────────────────────────────────────────────

    async def run_flow(
        self, page: Page, flow_name: str, *, timeout_per_step: float = 120.0
    ) -> dict[str, Any]:
        """Execute a named flow from config/flows.json.

        Each step is a natural language instruction executed sequentially.

        Returns:
            dict with success, completed, failed, errors, flow_name
        """
        flows_file = Path(__file__).parent.parent / "config" / "flows.json"
        if not flows_file.exists():
            return {"success": False, "error": "flows.json not found", "flow_name": flow_name}

        with open(flows_file, "r", encoding="utf-8") as f:
            flows = json.load(f)

        if flow_name not in flows:
            available = ", ".join(flows.keys())
            return {
                "success": False,
                "error": f"Flow '{flow_name}' not found. Available: {available}",
                "flow_name": flow_name,
            }

        flow_def = flows[flow_name]
        steps = flow_def.get("steps", [])
        completed = 0
        failed = 0
        errors: list[str] = []

        logger.info(f"[PageAgent] Starting flow '{flow_name}' ({len(steps)} steps)")

        for i, step in enumerate(steps, 1):
            logger.info(f"[PageAgent] Flow step {i}/{len(steps)}: {step[:80]}")
            result = await self.execute_task(page, step, timeout=timeout_per_step)
            if result.get("success"):
                completed += 1
            else:
                failed += 1
                errors.append(f"Step {i}: {result.get('data', 'Unknown')}")

        success = failed == 0
        logger.info(
            f"[PageAgent] Flow '{flow_name}' {'DONE ✅' if success else 'PARTIAL ⚠️'}: "
            f"{completed}/{len(steps)} ok, {failed} failed"
        )

        return {
            "success": success,
            "completed": completed,
            "failed": failed,
            "total_steps": len(steps),
            "errors": errors,
            "flow_name": flow_name,
        }

    def list_flows(self) -> list[str]:
        """List available flow names from config/flows.json."""
        flows_file = Path(__file__).parent.parent / "config" / "flows.json"
        if not flows_file.exists():
            return []
        with open(flows_file, "r", encoding="utf-8") as f:
            return list(json.load(f).keys())
