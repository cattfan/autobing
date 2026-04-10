"""
Manual captcha / challenge handoff flow.

When a verification gate appears, the bot pauses, captures evidence,
notifies the user, and waits for the challenge to be solved manually
in the open browser window before resuming.
"""

from __future__ import annotations
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from src.notifier import Notifier
from src.utils import DATA_DIR, logger


class ManualCaptchaHandler:
    """Detects verification gates and hands control back to the user."""

    SELECTOR_MARKERS = [
        ("iframe[src*='captcha']", "captcha iframe"),
        ("iframe[src*='recaptcha']", "reCAPTCHA"),
        ("iframe[src*='hcaptcha']", "hCaptcha"),
        ("iframe[src*='funcaptcha']", "FunCaptcha"),
        ("iframe[src*='arkoselabs']", "Arkose"),
        ("#enforcement-frame", "Arkose enforcement"),
        ("iframe[data-e2e='enforcement-frame']", "Arkose enforcement"),
        ("#captcha", "captcha container"),
        (".captcha-container", "captcha container"),
        ("[data-captcha]", "captcha element"),
        ("input[aria-label*='captcha' i]", "captcha input"),
    ]

    TEXT_MARKERS = [
        "unusual traffic",
        "verify you are human",
        "verify you're human",
        "complete the security check",
        "enter the characters you see",
        "detected unusual activity",
        "our systems have detected",
        "help us beat the bots",
        "arkose labs",
        "captcha",
    ]

    def __init__(
        self,
        settings: dict,
        notifier: Optional[Notifier] = None,
        on_log=None,
    ):
        self.settings = settings
        self.enabled = bool(settings.get("manual_captcha_handoff", True))
        self.timeout_seconds = max(
            30,
            int(settings.get("manual_captcha_timeout", 900)),
        )
        self.poll_interval = max(
            1,
            int(settings.get("manual_captcha_poll_interval", 5)),
        )
        self.capture_screenshot = bool(
            settings.get("manual_captcha_screenshot", True)
        )
        self.notifier = notifier or Notifier(settings)
        self._log = on_log or self._default_log

    def _default_log(self, level: str, message: str) -> None:
        getattr(logger, level, logger.info)(message)

    async def detect_challenge(self, page: Page) -> Optional[str]:
        """Return a short reason string when a challenge is present."""
        if page.is_closed():
            return None

        # Only check for captchas on Microsoft domains to avoid false positives
        url = page.url.lower()
        if "bing.com" not in url and "live.com" not in url:
            return None

        try:
            for selector, label in self.SELECTOR_MARKERS:
                locator = page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    return label
        except Exception:
            pass

        try:
            body = page.locator("body")
            body_text = await body.inner_text(timeout=1500)
            normalized = " ".join(body_text.lower().split())
            for marker in self.TEXT_MARKERS:
                if marker in normalized:
                    return marker
        except Exception:
            pass

        return None

    async def handle_if_present(
        self,
        page: Page,
        account: str = "",
        context: str = "browser flow",
    ) -> bool:
        """
        Pause for manual intervention when a challenge is visible.

        Returns True when no challenge is present or the user resolves it in time.
        Returns False when the challenge remains unresolved.
        """
        reason = await self.detect_challenge(page)
        if not reason:
            return True

        account_label = account[:5] + "***" if account else "unknown"
        notify_account = account or "unknown"
        screenshot_path = await self._capture_screenshot(page, account, context)
        if screenshot_path:
            self._log("info", f"Challenge screenshot saved to {screenshot_path}")

        if not self.enabled:
            self._log(
                "warning",
                f"Manual challenge handoff disabled for {account_label} ({context})",
            )
            self.notifier.send_manual_action(
                notify_account,
                context,
                page.url,
                f"Challenge detected ({reason}) but handoff is disabled.",
                screenshot_path=screenshot_path,
            )
            return False

        if self.settings.get("headless", True):
            self._log(
                "warning",
                "Challenge detected but browser is headless. "
                "Set headless=false to solve it manually.",
            )
            self.notifier.send_manual_action(
                notify_account,
                context,
                page.url,
                "Challenge detected, but manual handoff requires headless=false.",
                screenshot_path=screenshot_path,
            )
            return False

        try:
            await page.bring_to_front()
        except Exception:
            pass

        self._log(
            "warning",
            f"Manual verification required for {account_label} at {context}. "
            f"Solve it in the open browser window; the bot will resume automatically.",
        )
        self.notifier.send_manual_action(
            notify_account,
            context,
            page.url,
            (
                f"Challenge detected ({reason}). "
                f"Solve it in the open browser window within {self.timeout_seconds}s."
            ),
            screenshot_path=screenshot_path,
        )

        started_at = datetime.now()
        next_progress_log = self.poll_interval * 6
        elapsed = 0
        while elapsed < self.timeout_seconds:
            await asyncio.sleep(self.poll_interval)
            if page.is_closed():
                self._log(
                    "warning",
                    f"Challenge page was closed before verification finished for {context}.",
                )
                return False
            elapsed = int((datetime.now() - started_at).total_seconds())
            reason = await self.detect_challenge(page)
            if not reason:
                self._log(
                    "info",
                    f"Manual verification cleared after {elapsed}s; resuming {context}.",
                )
                return True

            if elapsed >= next_progress_log:
                self._log(
                    "warning",
                    f"Still waiting for manual verification on {context} ({elapsed}s elapsed).",
                )
                next_progress_log += self.poll_interval * 6

        self._log(
            "warning",
            f"Manual verification timed out after {self.timeout_seconds}s for {context}.",
        )
        self.notifier.send_manual_action(
            notify_account,
            context,
            page.url,
            f"Manual verification timed out after {self.timeout_seconds}s.",
            screenshot_path=screenshot_path,
        )
        return False

    async def _capture_screenshot(
        self,
        page: Page,
        account: str,
        context: str,
    ) -> Optional[str]:
        """Capture a screenshot for challenge triage."""
        if not self.capture_screenshot:
            return None

        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_account = (account or "unknown").replace("@", "_at_").replace(".", "_")
            safe_context = "".join(ch if ch.isalnum() else "_" for ch in context.lower())[:40]
            out_dir = DATA_DIR / "captcha_handoffs"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{safe_account}_{safe_context}_{ts}.png"
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as e:
            logger.debug(f"Could not save challenge screenshot: {e}")
            return None
