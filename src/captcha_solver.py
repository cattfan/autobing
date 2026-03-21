"""
Captcha Solver integration for Microsoft Rewards.
Supports 2captcha and anticaptcha APIs.

Usage:
    solver = CaptchaSolver(settings)
    solved = await solver.solve_if_present(page)
"""

import asyncio
import random
from typing import Optional

from playwright.async_api import Page

from src.utils import logger


class CaptchaSolver:
    """Auto-solve captchas using 2captcha or anticaptcha API."""

    PROVIDERS = {
        "2captcha": "https://2captcha.com/in.php",
        "anticaptcha": "https://api.anti-captcha.com",
    }

    def __init__(self, settings: dict):
        self.api_key = settings.get("captcha_api_key", "")
        self.provider = settings.get("captcha_provider", "2captcha")
        self.enabled = bool(self.api_key)

        if not self.enabled:
            logger.debug("Captcha solver: no API key, will skip captchas")

    async def solve_if_present(self, page: Page) -> bool:
        """Detect and solve captcha if present on page.

        Returns True if captcha was solved or not present, False if failed.
        """
        if not self.enabled:
            # Check if captcha is blocking, log warning
            has_captcha = await self._detect_captcha(page)
            if has_captcha:
                logger.warning(
                    "⚠️ Captcha detected but no API key configured! "
                    "Set captcha_api_key in settings to auto-solve."
                )
                return False
            return True

        captcha_type = await self._detect_captcha_type(page)
        if not captcha_type:
            return True  # No captcha present

        logger.info(f"🔐 Captcha detected: {captcha_type}")

        try:
            if captcha_type == "funcaptcha":
                return await self._solve_funcaptcha(page)
            elif captcha_type == "recaptcha":
                return await self._solve_recaptcha(page)
            elif captcha_type == "hcaptcha":
                return await self._solve_hcaptcha(page)
            elif captcha_type == "image":
                return await self._solve_image_captcha(page)
            else:
                logger.warning(f"Unknown captcha type: {captcha_type}")
                return False
        except Exception as e:
            logger.error(f"❌ Captcha solving failed: {e}")
            return False

    async def _detect_captcha(self, page: Page) -> bool:
        """Quick check if any captcha is present."""
        selectors = [
            "iframe[src*='captcha']",
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='funcaptcha']",
            "iframe[src*='arkoselabs']",
            "#captcha",
            ".captcha-container",
            "[data-captcha]",
            "#enforcement-frame",
        ]
        for sel in selectors:
            if await page.locator(sel).count() > 0:
                return True
        return False

    async def _detect_captcha_type(self, page: Page) -> Optional[str]:
        """Detect specific captcha type."""
        # FunCaptcha (used by Microsoft most often)
        funcaptcha_selectors = [
            "iframe[src*='funcaptcha']",
            "iframe[src*='arkoselabs']",
            "#enforcement-frame",
            "iframe[data-e2e='enforcement-frame']",
        ]
        for sel in funcaptcha_selectors:
            if await page.locator(sel).count() > 0:
                return "funcaptcha"

        # reCaptcha
        if await page.locator("iframe[src*='recaptcha']").count() > 0:
            return "recaptcha"

        # hCaptcha
        if await page.locator("iframe[src*='hcaptcha']").count() > 0:
            return "hcaptcha"

        # Image captcha
        if await page.locator("#captchaImage, img[id*='captcha']").count() > 0:
            return "image"

        return None

    # ─── 2captcha API Methods ─────────────────────────────────

    async def _api_submit(self, payload: dict) -> Optional[str]:
        """Submit captcha to 2captcha and get task ID."""
        import aiohttp

        if self.provider == "2captcha":
            payload["key"] = self.api_key
            payload["json"] = 1

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://2captcha.com/in.php", data=payload
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == 1:
                        return data["request"]
                    logger.error(f"2captcha submit error: {data}")
                    return None

        elif self.provider == "anticaptcha":
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anti-captcha.com/createTask",
                    json={"clientKey": self.api_key, "task": payload},
                ) as resp:
                    data = await resp.json()
                    if data.get("errorId") == 0:
                        return str(data["taskId"])
                    logger.error(f"anticaptcha submit error: {data}")
                    return None

        return None

    async def _api_result(self, task_id: str, timeout: int = 120) -> Optional[str]:
        """Poll for captcha solution."""
        import aiohttp

        for _ in range(timeout // 5):
            await asyncio.sleep(5)

            async with aiohttp.ClientSession() as session:
                if self.provider == "2captcha":
                    url = f"https://2captcha.com/res.php?key={self.api_key}&action=get&id={task_id}&json=1"
                    async with session.get(url) as resp:
                        data = await resp.json()
                        if data.get("status") == 1:
                            return data["request"]
                        if data.get("request") != "CAPCHA_NOT_READY":
                            logger.error(f"2captcha error: {data}")
                            return None

                elif self.provider == "anticaptcha":
                    async with session.post(
                        "https://api.anti-captcha.com/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": int(task_id)},
                    ) as resp:
                        data = await resp.json()
                        if data.get("status") == "ready":
                            return data["solution"].get("token") or data["solution"].get("text")
                        if data.get("status") != "processing":
                            logger.error(f"anticaptcha error: {data}")
                            return None

        logger.error("Captcha solving timed out")
        return None

    # ─── Solver Implementations ───────────────────────────────

    async def _solve_funcaptcha(self, page: Page) -> bool:
        """Solve FunCaptcha (Arkose Labs) — most common on Microsoft."""
        logger.info("🔐 Solving FunCaptcha via API...")

        try:
            # Get public key from iframe src
            iframe = page.locator(
                "iframe[src*='funcaptcha'], iframe[src*='arkoselabs'], "
                "#enforcement-frame"
            )
            src = await iframe.get_attribute("src") or ""

            public_key = ""
            if "pk=" in src:
                public_key = src.split("pk=")[1].split("&")[0]
            elif "public_key=" in src:
                public_key = src.split("public_key=")[1].split("&")[0]

            if not public_key:
                # Common Microsoft FunCaptcha key
                public_key = "B7D8911C-5CC8-A9A3-35B0-554ACEE604DA"

            page_url = page.url

            if self.provider == "2captcha":
                task_id = await self._api_submit({
                    "method": "funcaptcha",
                    "publickey": public_key,
                    "surl": "https://client-api.arkoselabs.com",
                    "pageurl": page_url,
                })
            else:
                task_id = await self._api_submit({
                    "type": "FunCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websitePublicKey": public_key,
                    "funcaptchaApiJSSubdomain": "https://client-api.arkoselabs.com",
                })

            if not task_id:
                return False

            solution = await self._api_result(task_id)
            if not solution:
                return False

            # Inject solution
            await page.evaluate(f"""
                (token) => {{
                    // Try common callback methods
                    if (window.parent && window.parent.fc) {{
                        window.parent.fc.setCallbackValue(token);
                    }}
                    if (typeof enforcementCallback === 'function') {{
                        enforcementCallback(token);
                    }}
                    // Try form submission
                    const input = document.querySelector('input[name="fc-token"]');
                    if (input) {{
                        input.value = token;
                        const form = input.closest('form');
                        if (form) form.submit();
                    }}
                }}
            """, solution)

            await asyncio.sleep(3)
            logger.info("✅ FunCaptcha solved!")
            return True

        except Exception as e:
            logger.error(f"FunCaptcha solving error: {e}")
            return False

    async def _solve_recaptcha(self, page: Page) -> bool:
        """Solve reCAPTCHA v2."""
        logger.info("🔐 Solving reCAPTCHA via API...")

        try:
            sitekey = await page.evaluate("""
                () => {
                    const el = document.querySelector('[data-sitekey]');
                    return el ? el.getAttribute('data-sitekey') : null;
                }
            """)

            if not sitekey:
                logger.warning("Could not find reCAPTCHA sitekey")
                return False

            if self.provider == "2captcha":
                task_id = await self._api_submit({
                    "method": "userrecaptcha",
                    "googlekey": sitekey,
                    "pageurl": page.url,
                })
            else:
                task_id = await self._api_submit({
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page.url,
                    "websiteKey": sitekey,
                })

            if not task_id:
                return False

            solution = await self._api_result(task_id)
            if not solution:
                return False

            await page.evaluate(f"""
                (token) => {{
                    document.querySelector('#g-recaptcha-response').value = token;
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.keys(___grecaptcha_cfg.clients).forEach(key => {{
                            const client = ___grecaptcha_cfg.clients[key];
                            if (client && client.callback) client.callback(token);
                        }});
                    }}
                }}
            """, solution)

            await asyncio.sleep(3)
            logger.info("✅ reCAPTCHA solved!")
            return True

        except Exception as e:
            logger.error(f"reCAPTCHA solving error: {e}")
            return False

    async def _solve_hcaptcha(self, page: Page) -> bool:
        """Solve hCaptcha."""
        logger.info("🔐 Solving hCaptcha via API...")

        try:
            sitekey = await page.evaluate("""
                () => {
                    const el = document.querySelector('[data-sitekey]');
                    return el ? el.getAttribute('data-sitekey') : null;
                }
            """)

            if not sitekey:
                return False

            if self.provider == "2captcha":
                task_id = await self._api_submit({
                    "method": "hcaptcha",
                    "sitekey": sitekey,
                    "pageurl": page.url,
                })
            else:
                task_id = await self._api_submit({
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": page.url,
                    "websiteKey": sitekey,
                })

            if not task_id:
                return False

            solution = await self._api_result(task_id)
            if not solution:
                return False

            await page.evaluate(f"""
                (token) => {{
                    const textarea = document.querySelector('[name="h-captcha-response"]');
                    if (textarea) textarea.value = token;
                    const cb = document.querySelector('[data-hcaptcha-response]');
                    if (cb) cb.value = token;
                }}
            """, solution)

            await asyncio.sleep(3)
            logger.info("✅ hCaptcha solved!")
            return True

        except Exception as e:
            logger.error(f"hCaptcha solving error: {e}")
            return False

    async def _solve_image_captcha(self, page: Page) -> bool:
        """Solve simple image captcha."""
        logger.info("🔐 Solving image captcha via API...")

        try:
            import base64

            img = page.locator("#captchaImage, img[id*='captcha']")
            if await img.count() == 0:
                return False

            screenshot = await img.screenshot()
            b64 = base64.b64encode(screenshot).decode()

            if self.provider == "2captcha":
                task_id = await self._api_submit({
                    "method": "base64",
                    "body": b64,
                })
            else:
                task_id = await self._api_submit({
                    "type": "ImageToTextTask",
                    "body": b64,
                })

            if not task_id:
                return False

            solution = await self._api_result(task_id, timeout=60)
            if not solution:
                return False

            # Type solution into input
            captcha_input = page.locator(
                "input[name*='captcha'], input[id*='captcha'], "
                "input[aria-label*='captcha']"
            )
            if await captcha_input.count() > 0:
                await captcha_input.fill(solution)
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)

            logger.info("✅ Image captcha solved!")
            return True

        except Exception as e:
            logger.error(f"Image captcha solving error: {e}")
            return False
