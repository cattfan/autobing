import os
import json
import asyncio
import hashlib
from pathlib import Path
from playwright.async_api import Page
from src.utils import logger

MACROS_DIR = Path("data/ai_macros")

class MacroPlayer:
    """
    Handles saving and replaying Page-Agent extracted traces as static JSON macros.
    This eliminates the need to call LLM API for tasks that have already been solved once.
    """
    def __init__(self):
        MACROS_DIR.mkdir(parents=True, exist_ok=True)
    
    def _get_macro_path(self, task_title: str) -> Path:
        # Generate a safe filename hash from the task title
        safe_title = "".join([c if c.isalnum() else "_" for c in task_title.strip()]).strip("_")
        short_hash = hashlib.md5(task_title.encode("utf-8")).hexdigest()[:8]
        return MACROS_DIR / f"macro_{safe_title[:30]}_{short_hash}.json"

    def has_macro(self, task_title: str) -> bool:
        """Check if a macro exists for this task title."""
        if not task_title: return False
        return self._get_macro_path(task_title).exists()

    def save_macro(self, task_title: str, macro_trace: list) -> bool:
        """
        Convert raw macro trace from page-agent into a static JSON macro.
        trace items: {"type": "click"/"fill", "xpath": "...", "attributes": {}, "tag": "...", "value": "..."}
        """
        if not task_title or not macro_trace:
            return False
            
        steps = []
        for step in macro_trace:
            xpath = step.get("xpath")
            if not xpath:
                continue
                
            # If the element has an ID, we prioritize ID selector for robustness
            attrs = step.get("attributes", {})
            el_id = attrs.get("id")
            robust_selector = f"#{el_id}" if el_id else f"xpath={xpath}"
            
            if step["type"] == "click":
                steps.append({"action": "click", "selector": robust_selector, "tag": step.get("tag", "")})
            elif step["type"] == "fill":
                steps.append({"action": "fill", "selector": robust_selector, "value": step.get("value", "")})

        if not steps:
            return False
            
        macro_data = {
            "task_title": task_title,
            "steps": steps
        }
        
        path = self._get_macro_path(task_title)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(macro_data, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Saved learned macro with {len(steps)} steps for: {task_title}")
            return True
        except Exception as e:
            logger.error(f"Failed to save macro: {e}")
            return False

    async def execute_macro(self, page: Page, task_title: str) -> bool:
        """
        Load and execute a macro using standard Playwright actions.
        Returns True if successful, False if failed.
        """
        path = self._get_macro_path(task_title)
        if not path.exists():
            return False
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                macro = json.load(f)
                
            steps = macro.get("steps", [])
            logger.info(f"🤖 Replaying macro ({len(steps)} steps) for: {task_title}")
            
            for index, step in enumerate(steps):
                action = step.get("action")
                selector = step.get("selector")
                
                # Dynamic wait - check if element exists before interacting
                try:
                    locator = page.locator(selector).first
                    await locator.wait_for(state="attached", timeout=15000)
                except Exception:
                    logger.warning(f"Macro step {index+1}: Selector not found '{selector}'")
                    return False
                    
                # Small humanized delay
                await asyncio.sleep(1.5)
                
                if action == "click":
                    await locator.scroll_into_view_if_needed()
                    await locator.click(timeout=5000)
                elif action == "fill":
                    val = step.get("value", "")
                    await locator.scroll_into_view_if_needed()
                    await locator.fill(val, timeout=5000)
                
                # Small wait after interaction
                await asyncio.sleep(1.0)
                
            logger.info(f"✅ Macro executed successfully for: {task_title}")
            return True
        except Exception as e:
            logger.error(f"❌ Macro execution failed: {e}")
            return False
