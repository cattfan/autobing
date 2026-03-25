"""
Microsoft account auto-login with state-machine architecture.
Based on TheNetsky/Microsoft-Rewards-Script v3 selectors & flow.
Handles 15+ login states: email, password, 2FA, passkey, KMSI, locked, etc.
"""

from __future__ import annotations
import asyncio
from typing import Optional
from enum import Enum

from playwright.async_api import Page

from src.utils import logger, REWARDS_URL, BING_HOME_URL, LOGIN_URL
from src.humanizer import Humanizer


class LoginState(Enum):
    """All possible states during the Microsoft login flow."""
    EMAIL_INPUT = "EMAIL_INPUT"
    PASSWORD_INPUT = "PASSWORD_INPUT"
    KMSI_PROMPT = "KMSI_PROMPT"
    PASSKEY_VIDEO = "PASSKEY_VIDEO"
    PASSKEY_ERROR = "PASSKEY_ERROR"
    SIGN_IN_ANOTHER_WAY = "SIGN_IN_ANOTHER_WAY"
    TOTP_2FA = "TOTP_2FA"
    OTP_CODE_ENTRY = "OTP_CODE_ENTRY"
    RECOVERY_EMAIL = "RECOVERY_EMAIL"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ERROR_ALERT = "ERROR_ALERT"
    PASSWORDLESS = "PASSWORDLESS"
    LOGGED_IN = "LOGGED_IN"
    UNKNOWN = "UNKNOWN"


# ─── Modern Selectors (Microsoft 2025 login redesign) ─────────────────────

SELECTORS = {
    # Login form elements
    "email_input": 'input[type="email"], input#usernameEntry',
    "password_input": 'input[type="password"], [data-testid="passwordEntry"]',
    "submit_button": 'button[type="submit"]',
    "primary_button": 'button[data-testid="primaryButton"]',
    "secondary_button": 'button[data-testid="secondaryButton"]',

    # State detection
    "account_locked": "#serviceAbuseLandingTitle",
    "error_alert": 'div[role="alert"]',
    "kmsi_video": '[data-testid="kmsiVideo"]',
    "passkey_video": '[data-testid="biometricVideo"]',
    "passkey_error": '[data-testid="registrationImg"]',
    "passwordless_check": '[data-testid="deviceShieldCheckmarkVideo"]',
    "password_icon": '[data-testid="tile"]:has(svg path[d*="M11.78 10.22a.75.75"])',
    "identity_banner": '[data-testid="identityBanner"]',
    "view_footer": '[data-testid="viewFooter"] >> [role="button"]',
    "other_ways": '[data-testid="viewFooter"] span[role="button"]',
    "back_button": "#back-button",

    # FIDO / passkey error page selectors
    "fido_cancel": 'a[data-testid="cancelLink"], #CancelNo, #idBtn_Back',
    "fido_sign_in_another_way": 'a:has-text("Sign in another way"), a:has-text("Other ways to sign in")',
    "fido_use_password": 'a:has-text("Use your password"), a:has-text("Use password"), a:has-text("Use my password")',

    # 2FA / OTP
    "totp_input": 'input[name="otc"]',
    "totp_input_old": 'form[name="OneTimeCodeViewForm"]',
    "otp_code_entry": '[data-testid="codeEntry"]',

    # Recovery
    "recovery_email": '[data-testid="proof-confirmation"]',

    # Fallback old selectors (some regions still use old UI)
    "email_input_old": 'input[name="loginfmt"]',
    "password_input_old": 'input[name="passwd"]',
    "submit_old": "#idSIButton9",
    "email_error_old": "#usernameError",
    "password_error_old": "#passwordError",

    # Logged in verification
    "bing_profile": "#id_n",
    "rewards_points": "#id_rc",
}

# Dismiss buttons for popups/overlays
DISMISS_BUTTONS = [
    ("#acceptButton", "AcceptButton"),
    ('#wcpConsentBannerCtrl > * > button:first-child', "Bing Cookies Accept"),
    (".ext-secondary.ext-button", "Skip for now"),
    ("#iLandingViewAction", "iLandingViewAction"),
    ("#iShowSkip", "iShowSkip"),
    ("#iNext", "iNext"),
    ("#iLooksGood", "iLooksGood"),
    ("#idSIButton9", "idSIButton9"),
    (".ms-Button.ms-Button--primary", "Primary Button"),
    (".c-glyph.glyph-cancel", "Mobile Welcome"),
    (".maybe-later", "Mobile Rewards Banner"),
    ("#bnp_btn_accept", "Bing Cookie Banner"),
    ("#reward_pivot_earn", "Reward Coupon"),
]


class LoginManager:
    """State-machine based Microsoft account authentication."""

    PASSKEY_SWITCH_CHOICES = (
        "Sign in another way",
        "Other ways to sign in",
        "Sign-in options",
        "Other ways",
        "I can't use my passkey",
        "Dang nhap theo cach khac",
        "Dang nhap bang cach khac",
        "Cach khac de dang nhap",
    )

    PASSKEY_PASSWORD_CHOICES = (
        "Use your password",
        "Use password",
        "Use my password",
        "Use a password",
        "Password",
        "Su dung mat khau",
        "Dung mat khau",
    )

    PASSKEY_FALLBACK_CHOICES = (
        "Back",
        "Cancel",
        "Skip",
        "Skip for now",
        "Try again",
        "Quay lai",
        "Bo qua",
        "Thu lai",
    )

    PASSKEY_TEXT_CHOICES = (
        "Use your password",
        "Use password",
        "Use my password",
        "Use a password",
        "Sign in another way",
        "Other ways to sign in",
        "Sign-in options",
        "Other ways",
        "Cancel",
        "Skip",
        "Skip for now",
        "I can't use my passkey",
        # Vietnamese
        "Sử dụng mật khẩu",
        "Đăng nhập theo cách khác",
        "Bỏ qua",
    )

    def __init__(self, humanizer: Humanizer, challenge_handler=None):
        self.humanizer = humanizer
        self.challenge_handler = challenge_handler

    async def login(
        self,
        page: Page,
        email: str,
        password: str,
        totp_secret: Optional[str] = None,
        recover_page=None,
    ) -> Page:
        """
        Login to Microsoft with retry support.
        Retries 2 times with 30s delay if login fails (captcha / transient error).
        """
        max_login_retries = 2
        active_page = page
        for attempt in range(max_login_retries + 1):
            try:
                active_page = await self._login_inner(
                    active_page,
                    email,
                    password,
                    totp_secret,
                )
                return active_page
            except RuntimeError as e:
                if "Page closed unexpectedly during login" in str(e):
                    try:
                        active_page = await self._manual_login_handoff(
                            active_page,
                            email,
                            recover_page=recover_page,
                        )
                        return active_page
                    except RuntimeError as manual_error:
                        e = manual_error

                if attempt < max_login_retries:
                    logger.warning(
                        f"Login attempt {attempt + 1} failed: {e}. "
                        f"Retrying in 30s... ({max_login_retries - attempt} left)"
                    )
                    await asyncio.sleep(30)
                    # Reload a stable login entrypoint for the next attempt.
                    try:
                        if active_page.is_closed():
                            active_page = await self._recover_login_page(
                                active_page,
                                recover_page=recover_page,
                            )
                        await self._open_login_entry(active_page)
                    except Exception:
                        pass
                else:
                    raise

        return active_page  # Should not reach here

    async def _login_inner(
        self,
        page: Page,
        email: str,
        password: str,
        totp_secret: Optional[str] = None,
    ) -> Page:
        """
        Login to Microsoft using state-machine approach.
        Starts from a stable Bing/Microsoft sign-in entrypoint, then uses the state machine.
        """
        logger.info(f"Starting login for {email[:5]}***")

        await self._open_login_entry(page)

        # Dismiss any popups
        await self._dismiss_messages(page)

        max_iterations = 25
        previous_state = LoginState.UNKNOWN
        same_state_count = 0
        active_page = page

        for iteration in range(max_iterations):
            if active_page.is_closed():
                raise RuntimeError("Page closed unexpectedly during login")
                # FIDO may have closed the tab — recover a live page
                logger.info(f"Page closed (FIDO?), recovering new page... (attempt {self._fido_recovery_count})")
                try:
                    active_page = await self._recover_login_page(active_page)
                    # Re-setup FIDO interception on the recovered page
                    await self._setup_fido_interception(active_page)
                    await self._disable_fido(active_page)
                    await active_page.goto(
                        "https://login.live.com/login.srf?wa=wsignin1.0"
                        "&wp=MBI_SSL&wreply=https://rewards.bing.com/",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    await asyncio.sleep(3)
                    previous_state = LoginState.UNKNOWN
                    same_state_count = 0
                    continue
                except Exception:
                    raise RuntimeError("Page closed unexpectedly during login")

            state = await self._detect_state(active_page)
            logger.debug(f"Login iteration {iteration + 1}/{max_iterations}: state={state.value}")

            if self.challenge_handler:
                resolved = await self.challenge_handler.handle_if_present(
                    active_page,
                    account=email,
                    context="Microsoft login",
                )
                if not resolved:
                    raise RuntimeError("Manual verification challenge not resolved during login")

            # State transition logging
            if state != previous_state and previous_state != LoginState.UNKNOWN:
                logger.info(f"State: {previous_state.value} → {state.value}")

            # Stuck detection — passkey states are inherently slow, give more time
            if state == previous_state and state not in (LoginState.LOGGED_IN, LoginState.UNKNOWN):
                same_state_count += 1
                # Passkey states need more time (up to 8 loops)
                max_same = 8 if state in (
                    LoginState.PASSKEY_VIDEO, LoginState.PASSKEY_ERROR
                ) else 4
                if same_state_count >= max_same:
                    logger.warning(f"Stuck in state '{state.value}' for {max_same} loops, refreshing...")
                    await active_page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                    same_state_count = 0
                    previous_state = LoginState.UNKNOWN
                    continue
            else:
                same_state_count = 0
            previous_state = state

            # Success
            if state == LoginState.LOGGED_IN:
                logger.info(f"✅ Successfully logged in as {email[:5]}***")
                await self._finalize_login(active_page)
                return active_page

            # Handle current state
            should_continue = await self._handle_state(
                state, active_page, email, password, totp_secret
            )
            if not should_continue:
                raise RuntimeError(f"Login failed at state: {state.value}")

            await asyncio.sleep(1)

        raise RuntimeError(f"Login timeout: exceeded {max_iterations} iterations")

    async def _detect_state(self, page: Page) -> LoginState:
        """Detect current login page state by checking selectors."""
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        url = page.url.lower()

        # Check if on trusted logged-in destinations
        if (
            "rewards.bing.com" in url
            or "account.microsoft.com" in url
            or "account.live.com" in url
        ):
            return LoginState.LOGGED_IN

        # Account locked
        if await self._check(page, SELECTORS["account_locked"]):
            return LoginState.ACCOUNT_LOCKED

        # Error alert (only on login domain, not FIDO pages)
        if await self._check(page, SELECTORS["error_alert"]) and "login.live.com" in url:
            return LoginState.ERROR_ALERT

        # ── FIDO / Passkey redirect detection (MUST be before password check) ──
        # Microsoft redirects to login.microsoft.com/consumers/fido/get...
        # The page first shows "Face, fingerprint, PIN or security key"
        # then after a few seconds switches to "We couldn't sign you in"
        # with a "Sign in another way" link.
        is_fido_url = (
            "login.microsoft.com" in url
            and ("/fido/" in url or "/passkey" in url or "/webauthn" in url)
        )
        if is_fido_url:
            # Check if the error/fallback state has appeared
            if await self._check(page, SELECTORS["passkey_error"]):
                return LoginState.PASSKEY_ERROR
            # Still on the initial passkey animation
            return LoginState.PASSKEY_VIDEO

        # Password entry (must check before email)
        if await self._check(page, SELECTORS["password_input"]) or \
           await self._check(page, SELECTORS["password_input_old"]):
            return LoginState.PASSWORD_INPUT

        # Email entry
        if await self._check(page, SELECTORS["email_input"]) or \
           await self._check(page, SELECTORS["email_input_old"]):
            return LoginState.EMAIL_INPUT

        # KMSI "Stay signed in?" prompt
        if await self._check(page, SELECTORS["kmsi_video"]):
            return LoginState.KMSI_PROMPT

        # Passkey prompts (on login.live.com, not FIDO URL)
        if await self._check(page, SELECTORS["passkey_video"]):
            return LoginState.PASSKEY_VIDEO
        if await self._check(page, SELECTORS["passkey_error"]):
            return LoginState.PASSKEY_ERROR

        # 2FA TOTP
        if await self._check(page, SELECTORS["totp_input"]) or \
           await self._check(page, SELECTORS["totp_input_old"]):
            return LoginState.TOTP_2FA

        # OTP code entry
        if await self._check(page, SELECTORS["otp_code_entry"]):
            return LoginState.OTP_CODE_ENTRY

        # Sign in another way (password option available)
        if await self._check(page, SELECTORS["password_icon"]):
            return LoginState.SIGN_IN_ANOTHER_WAY

        # Recovery email
        if await self._check(page, SELECTORS["recovery_email"]):
            return LoginState.RECOVERY_EMAIL

        # Passwordless
        if await self._check(page, SELECTORS["passwordless_check"]):
            return LoginState.PASSWORDLESS

        # Redirected back to Bing with an authenticated header state.
        if "bing.com" in url:
            sign_in_text = " ".join(
                part.lower()
                for part in (
                    await self._selector_text(page, "#id_s"),
                    await self._selector_text(page, "#id_l"),
                )
                if part
            )
            if "sign in" not in sign_in_text and "dang nhap" not in sign_in_text and "đăng nhập" not in sign_in_text:
                if await self._has_auth_cookie(page):
                    return LoginState.LOGGED_IN

        # Check if redirected to rewards page (alternate check)
        if "bing.com" in url and "/rewards" in url:
            return LoginState.LOGGED_IN

        return LoginState.UNKNOWN

    async def _open_login_entry(self, page: Page) -> None:
        """Open a stable Microsoft sign-in entrypoint without touching Rewards pages."""
        if page.is_closed():
            raise RuntimeError("Page closed unexpectedly during login")

        try:
            await page.goto(
                BING_HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(1)
            await self._dismiss_messages(page)
            clicked = await self._click_selector(
                page,
                "#id_l, #id_s, a[href*='login.live.com'], a[href*='signin']",
            )
            if clicked:
                await asyncio.sleep(2)
        except Exception:
            pass

        if page.is_closed():
            raise RuntimeError("Page closed unexpectedly during login")

        if "login.live.com" not in page.url.lower():
            await page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(1.5)

    async def _manual_login_handoff(self, page: Page, email: str, recover_page=None) -> Page:
        """Pause for user-driven Microsoft login when passkey/FIDO closes the page."""
        challenge_handler = self.challenge_handler
        settings = getattr(challenge_handler, "settings", {}) if challenge_handler else {}
        notifier = getattr(challenge_handler, "notifier", None) if challenge_handler else None
        enabled = bool(settings.get("manual_captcha_handoff", True)) if settings else False
        if not enabled:
            raise RuntimeError("Page closed unexpectedly during login")
        if settings.get("headless", True):
            raise RuntimeError("Manual Microsoft login requires headless=false")

        active_page = await self._recover_login_page(page, recover_page=recover_page)

        try:
            await active_page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception:
            pass

        try:
            await active_page.bring_to_front()
        except Exception:
            pass

        timeout_seconds = max(60, int(settings.get("manual_captcha_timeout", 900)))
        poll_interval = max(2, int(settings.get("manual_captcha_poll_interval", 5)))
        logger.warning(
            f"Microsoft sign-in requires manual completion for {email[:5]}***. "
            "If Microsoft shows passkey, choose 'Use password' or 'Other ways to sign in'; "
            "the bot will resume automatically after you finish."
        )
        if notifier:
            notifier.send_manual_action(
                email,
                "Microsoft login",
                active_page.url,
                (
                    "Microsoft redirected to a passkey/FIDO flow that closed the automated tab. "
                    "Choose 'Use password' or 'Other ways to sign in' if needed, then "
                    f"complete sign-in manually within {timeout_seconds}s."
                ),
            )

        started = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - started) < timeout_seconds:
            await asyncio.sleep(poll_interval)
            if active_page.is_closed():
                try:
                    active_page = await self._recover_login_page(
                        active_page,
                        recover_page=recover_page,
                    )
                except RuntimeError:
                    continue

            if await self.is_logged_in(active_page, allow_navigation=False):
                logger.info(f"✅ Manual login completed for {email[:5]}***")
                return active_page

        raise RuntimeError(
            f"Manual Microsoft login timed out after {timeout_seconds}s"
        )

    async def _recover_login_page(self, page: Page, recover_page=None) -> Page:
        """Recover a usable page when Microsoft closes the current sign-in tab/context."""
        context = page.context

        try:
            pages = context.pages
        except Exception:
            pages = []

        live_pages = [candidate for candidate in pages if not candidate.is_closed()]
        if live_pages:
            return live_pages[-1]

        try:
            return await context.new_page()
        except Exception:
            if recover_page is not None:
                return await recover_page()
            browser = getattr(context, "browser", None)
            if browser is None:
                raise RuntimeError(
                    "Microsoft closed the sign-in context and no browser recovery path is available"
                )
            try:
                return await browser.new_page()
            except Exception as e:
                raise RuntimeError(
                    "Microsoft closed the sign-in context before a new page could be opened"
                ) from e

    async def _check(self, page: Page, selector: str) -> bool:
        """Check if a selector is visible on page (200ms timeout)."""
        try:
            await page.wait_for_selector(selector, state="visible", timeout=200)
            return True
        except Exception:
            return False

    async def _handle_state(
        self,
        state: LoginState,
        page: Page,
        email: str,
        password: str,
        totp_secret: Optional[str],
    ) -> bool:
        """Handle a detected login state. Returns True to continue loop."""

        if state == LoginState.ACCOUNT_LOCKED:
            logger.error("❌ Account has been locked!")
            raise RuntimeError("Account locked by Microsoft. Check your account.")

        elif state == LoginState.ERROR_ALERT:
            error_el = page.locator(SELECTORS["error_alert"])
            error_msg = await error_el.inner_text() if await error_el.count() > 0 else "Unknown error"
            logger.error(f"Login error: {error_msg}")
            raise RuntimeError(f"Microsoft login error: {error_msg}")

        elif state == LoginState.EMAIL_INPUT:
            logger.info("Entering email...")
            await self._enter_email(page, email)
            return True

        elif state == LoginState.PASSWORD_INPUT:
            logger.info("Entering password...")
            await self._enter_password(page, password)
            return True

        elif state == LoginState.KMSI_PROMPT:
            logger.info("Accepting 'Stay signed in' prompt...")
            await self._click_primary_button(page)
            return True

        elif state in (LoginState.PASSKEY_VIDEO, LoginState.PASSKEY_ERROR):
            logger.info("Handling passkey/FIDO redirect...")
            await self._handle_passkey_to_password(page)
            return True

        elif state == LoginState.TOTP_2FA:
            logger.info("Entering 2FA TOTP code...")
            await self._enter_totp(page, totp_secret)
            return True

        elif state == LoginState.OTP_CODE_ENTRY:
            logger.info("OTP code entry detected, trying to switch to password...")
            # Try "Other ways to sign in" footer
            clicked = await self._click_selector(page, SELECTORS["view_footer"])
            if not clicked:
                await self._click_selector(page, SELECTORS["back_button"])
            return True

        elif state == LoginState.SIGN_IN_ANOTHER_WAY:
            logger.info("Selecting 'Use my password' option...")
            await self._click_selector(page, SELECTORS["password_icon"])
            return True

        elif state == LoginState.RECOVERY_EMAIL:
            logger.warning("Recovery email verification required — cannot auto-handle")
            raise RuntimeError("Recovery email verification required. Please verify manually.")

        elif state == LoginState.PASSWORDLESS:
            logger.info("Passwordless login detected, waiting...")
            await asyncio.sleep(5)
            return True

        elif state == LoginState.UNKNOWN:
            if await self._try_switch_to_password_flow(page):
                return True
            logger.debug(f"Unknown state at {page.url}, waiting...")
            await asyncio.sleep(2)
            return True

        return True

    async def _enter_email(self, page: Page, email: str) -> None:
        """Enter email and click submit."""
        # Try modern selector first, then fallback
        for selector in [SELECTORS["email_input"], SELECTORS["email_input_old"]]:
            try:
                el = await page.wait_for_selector(selector, state="visible", timeout=1000)
                if el:
                    await asyncio.sleep(0.5)

                    # Check if email is pre-filled
                    prefilled = await page.query_selector("#userDisplayName")
                    if not prefilled:
                        await page.fill(selector, "")
                        await asyncio.sleep(0.3)
                        await page.fill(selector, email)
                        await asyncio.sleep(0.5)
                    else:
                        logger.info("Email already pre-filled")

                    # Open a backup tab BEFORE submitting — FIDO may close the current tab.
                    # The backup keeps the persistent context alive.
                    try:
                        backup = await page.context.new_page()
                        await backup.goto("about:blank")
                        logger.debug("Backup tab opened (FIDO safety net)")
                    except Exception:
                        pass

                    # Click submit
                    await self._click_submit(page)
                    # Wait and handle FIDO redirect — page may close
                    await self._wait_after_email_submit(page)
                    return
            except Exception:
                continue

        logger.warning("Could not find email field")

    async def _enter_password(self, page: Page, password: str) -> None:
        """Enter password and click submit."""
        for selector in [SELECTORS["password_input"], SELECTORS["password_input_old"]]:
            try:
                el = await page.wait_for_selector(selector, state="visible", timeout=1000)
                if el:
                    await asyncio.sleep(0.5)
                    await page.fill(selector, "")
                    await asyncio.sleep(0.3)
                    await page.fill(selector, password)
                    await asyncio.sleep(0.5)
                    await self._click_submit(page)
                    await asyncio.sleep(3)
                    return
            except Exception:
                continue

        logger.warning("Could not find password field")

    async def _enter_totp(self, page: Page, totp_secret: Optional[str]) -> None:
        """Enter TOTP 2FA code."""
        if not totp_secret:
            raise RuntimeError(
                "2FA required but no TOTP secret configured. "
                "Add totp_secret to your account."
            )

        try:
            import pyotp
            totp = pyotp.TOTP(totp_secret)
            code = totp.now()
            logger.info(f"Entering 2FA code: {code[:2]}****")

            for selector in [SELECTORS["totp_input"], 'input[name="otc"]']:
                try:
                    el = await page.wait_for_selector(selector, state="visible", timeout=1000)
                    if el:
                        await page.fill(selector, code)
                        await asyncio.sleep(0.5)
                        await self._click_submit(page)
                        await asyncio.sleep(3)
                        return
                except Exception:
                    continue

        except ImportError:
            raise RuntimeError("pyotp not installed. Run: pip install pyotp")

    async def _click_submit(self, page: Page) -> None:
        """Click the submit/next button."""
        for selector in [
            SELECTORS["submit_button"],
            SELECTORS["primary_button"],
            SELECTORS["submit_old"],
        ]:
            try:
                btn = await page.wait_for_selector(selector, state="visible", timeout=1000)
                if btn:
                    await btn.click()
                    return
            except Exception:
                continue
        logger.warning("Could not find submit button, pressing Enter")
        await page.keyboard.press("Enter")

    async def _click_primary_button(self, page: Page) -> None:
        """Click the primary action button."""
        await self._click_selector(page, SELECTORS["primary_button"])
        if not await self._check(page, SELECTORS["primary_button"]):
            await self._click_selector(page, SELECTORS["submit_old"])

    async def _click_selector(self, page: Page, selector: str) -> bool:
        """Try to click a selector. Returns True if clicked."""
        try:
            el = await page.wait_for_selector(selector, state="visible", timeout=2000)
            if el:
                await el.click()
                await asyncio.sleep(1)
                return True
        except Exception:
            pass
        return False

    async def _dismiss_messages(self, page: Page) -> None:
        """Dismiss all popup messages, cookie banners, overlays."""
        for selector, label in DISMISS_BUTTONS:
            try:
                el = page.locator(selector)
                if await el.is_visible():
                    await el.click()
                    logger.debug(f"Dismissed: {label}")
                    await asyncio.sleep(0.3)
            except Exception:
                continue

        # Bing overlay
        try:
            overlay = await page.query_selector("#bnp_overlay_wrapper")
            if overlay:
                reject = page.locator('#bnp_btn_reject, button[aria-label*="Reject" i]')
                if await reject.count() > 0:
                    await reject.first.click()
                else:
                    accept = page.locator("#bnp_btn_accept")
                    if await accept.count() > 0:
                        await accept.first.click()
        except Exception:
            pass

    async def _setup_fido_interception(self, page: Page) -> None:
        """Intercept FIDO/passkey redirects at the network level.
        
        When Microsoft redirects to login.microsoft.com/consumers/fido/*,
        we intercept and serve a lightweight HTML page that immediately
        redirects back to login.live.com via JavaScript. This prevents the
        FIDO page from loading and keeps the browser context alive.
        """
        redirect_html = """<!DOCTYPE html>
<html><head><title>Redirecting...</title></head>
<body><script>
window.location.replace(
    "https://login.live.com/login.srf?wa=wsignin1.0&wp=MBI_SSL&wreply=https://rewards.bing.com/"
);
</script></body></html>"""

        async def _intercept_fido(route):
            url = route.request.url.lower()
            logger.info(f"🚫 Blocked FIDO redirect: {url[:80]}")
            # Serve an HTML page that JS-redirects to login.live.com
            # (302 fulfills crash persistent contexts)
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=redirect_html,
            )

        try:
            # Use context-level routing to catch all pages (including new tabs)
            ctx = page.context
            await ctx.route("**/consumers/fido/**", _intercept_fido)
            await ctx.route("**/consumers/passkey/**", _intercept_fido)
            await ctx.route("**/fido/get**", _intercept_fido)
            logger.debug("FIDO route interception set up (context-level)")
        except Exception as e:
            logger.debug(f"Could not set up FIDO interception: {e}")

    async def _disable_fido(self, page: Page) -> None:
        """Suppress WebAuthn/Fido2 by adding a virtual authenticator with no credentials.
        
        This makes the browser report 'no authenticator available' to the website,
        which forces Microsoft to show the 'Sign in another way' error immediately
        instead of waiting for a real passkey device.
        """
        try:
            client = await page.context.new_cdp_session(page)
            # Enable WebAuthn interception
            await client.send("WebAuthn.enable", {"enableUI": False})
            # Add a virtual authenticator with NO credentials registered.
            # This makes FIDO assertions fail immediately → "can't sign you in" error.
            await client.send("WebAuthn.addVirtualAuthenticator", {
                "options": {
                    "protocol": "ctap2",
                    "transport": "internal",
                    "hasResidentKey": True,
                    "hasUserVerification": True,
                    "isUserVerified": True,
                }
            })
            logger.debug("Virtual authenticator added (FIDO will fail fast)")
        except Exception as e:
            logger.debug(f"Could not set up virtual authenticator: {e}")
            # Fallback: try the simpler disable
            try:
                client = await page.context.new_cdp_session(page)
                await client.send("WebAuthn.disable")
                logger.debug("WebAuthn disabled via CDP (fallback)")
            except Exception:
                pass

    async def _wait_after_email_submit(self, page: Page) -> None:
        """Wait for passkey/FIDO redirect and navigate back to password flow.
        
        After email submit, Microsoft may redirect to:
        - Password page (normal) → return immediately
        - FIDO passkey page (login.microsoft.com/consumers/fido/...) → wait for
          "Sign in another way" to appear (5-10 seconds), click it, then wait
          for password field.
        - FIDO may CLOSE the original tab — we handle this gracefully by
          just returning (the state machine in _login_inner will recover).
        """
        deadline = asyncio.get_event_loop().time() + 15.0  # 15s (passkey page is slow)
        fido_detected = False

        while asyncio.get_event_loop().time() < deadline:
            if page.is_closed():
                # Page closed — likely by FIDO redirect. The state machine in
                # _login_inner will handle recovery via _recover_login_page.
                logger.info("Page closed during FIDO wait, returning to state machine for recovery")
                return

            url = page.url.lower()

            # ── Already on password page? Done. ──
            if await self._check(page, SELECTORS["password_input"]) or \
               await self._check(page, SELECTORS["password_input_old"]):
                return

            # ── Already navigated away from FIDO? Done. ──
            if fido_detected and "login.microsoft.com" not in url:
                logger.info(f"Left FIDO page, now at: {url[:60]}")
                return

            # ── FIDO / passkey redirect detected ──
            if "login.microsoft.com" in url and ("/fido/" in url or "/passkey" in url):
                if not fido_detected:
                    logger.info("FIDO passkey page detected, clicking 'Sign in another way'...")
                    fido_detected = True
                    # Give the FIDO error page just 1s to render (virtual authenticator
                    # makes it fail fast). Page auto-closes after ~5s, so no time to waste.
                    await asyncio.sleep(1)

                # Try to find and click the password/cancel links
                if await self._handle_passkey_to_password(page):
                    # Successfully clicked or navigated away
                    await asyncio.sleep(2)
                    return

                await asyncio.sleep(0.3)  # Poll every 300ms — page may close soon
                continue

            # ── Other passkey-related URLs ──
            if any(m in url for m in ("passkey", "passwordless", "webauthn")):
                if await self._try_switch_to_password_flow(page):
                    await asyncio.sleep(1)
                    return

            await asyncio.sleep(0.3)

    async def _handle_passkey_to_password(self, page: Page) -> bool:
        """Handle FIDO/passkey page: click 'Sign in another way' or 'Back' FAST.
        
        The FIDO page auto-closes in ~3s on persistent contexts, so we must
        complete the click within 1-2 seconds. Uses a single JavaScript call
        to find and click the right element, which is much faster than
        multiple Playwright locator calls.
        """
        if page.is_closed():
            return False

        # ── Fast JS click: find and click 'Sign in another way' or 'Back' in one shot ──
        try:
            clicked = await page.evaluate("""
                () => {
                    // Priority targets from the FIDO error page
                    const targets = [
                        'sign in another way',
                        'use your password',
                        'use a password',
                        'other ways to sign in',
                        'back',
                        'cancel',
                        'skip',
                    ];
                    
                    const clickables = document.querySelectorAll('a, button, [role="button"], input[type="button"], input[type="submit"]');
                    for (const target of targets) {
                        for (const el of clickables) {
                            const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                            if (text.includes(target)) {
                                el.click();
                                return target;
                            }
                        }
                    }
                    return null;
                }
            """)
            if clicked:
                logger.info(f"Clicked FIDO element via JS: '{clicked}'")
                await asyncio.sleep(1.5)
                return True
        except Exception as e:
            logger.debug(f"JS click on FIDO page failed: {e}")

        # ── Fallback: Playwright locator for 'Sign in another way' ──
        try:
            link = page.locator("a:has-text('Sign in another way')").first
            if await link.count() > 0:
                await link.click(timeout=2000)
                logger.info("Clicked 'Sign in another way' via Playwright")
                await asyncio.sleep(1.5)
                return True
        except Exception:
            pass

        # ── Fallback: 'Back' button ──
        try:
            back = page.locator("button:has-text('Back'), #idBtn_Back").first
            if await back.count() > 0:
                await back.click(timeout=2000)
                logger.info("Clicked 'Back' on FIDO page")
                await asyncio.sleep(1.5)
                return True
        except Exception:
            pass

        # ── Last resort: go_back() ──
        try:
            logger.info("No FIDO elements found, trying browser back...")
            await page.go_back(wait_until="domcontentloaded", timeout=5000)
            await asyncio.sleep(1)
            if "login.microsoft.com" not in page.url.lower():
                return True
        except Exception:
            pass

        return False

    async def _try_switch_to_password_flow(self, page: Page) -> bool:
        """Attempt to switch passkey/passwordless prompts back to password sign-in."""
        if page.is_closed():
            return False

        url = page.url.lower()
        # Expanded URL check — includes login.microsoft.com
        markers = ("fido/", "passkey", "passwordless", "webauthn", "login.microsoft.com")
        if not any(marker in url for marker in markers):
            # Also check page content if URL doesn't match
            # (some passkey flows stay on login.live.com)
            has_passkey_element = (
                await self._check(page, SELECTORS["passkey_video"])
                or await self._check(page, SELECTORS["passkey_error"])
            )
            if not has_passkey_element:
                return False

        return await self._handle_passkey_to_password(page)

    async def _finalize_login(self, page: Page) -> None:
        """Finalize login on Bing home so we avoid Rewards pages that may self-close."""
        try:
            if page.is_closed():
                return
            await page.goto(
                BING_HOME_URL,
                wait_until="domcontentloaded",
                timeout=10000,
            )
            await asyncio.sleep(1)
            await self._dismiss_messages(page)
        except Exception as e:
            logger.debug(f"Finalize login warning: {e}")

    async def _has_auth_cookie(self, page: Page) -> bool:
        """Check for Microsoft auth cookies without touching Rewards pages."""
        try:
            cookies = await page.context.cookies(
                [BING_HOME_URL, REWARDS_URL, "https://login.live.com"]
            )
            names = {cookie.get("name", "") for cookie in cookies}
            return any(name in names for name in ("_U", "MSPAuth", "MSPProf"))
        except Exception:
            return False

    async def _selector_text(self, page: Page, selector: str) -> str:
        """Return selector text or an empty string when unavailable."""
        try:
            locator = page.locator(selector)
            if await locator.count() == 0:
                return ""
            return (await locator.first.inner_text(timeout=1000)).strip()
        except Exception:
            return ""

    async def is_logged_in(self, page: Page, *, allow_navigation: bool = True) -> bool:
        """Quick check if already logged in.

        When allow_navigation is False, keep the current page untouched so
        manual sign-in flows are not interrupted by background probes.
        """
        try:
            if page.is_closed():
                return False

            current_url = page.url.lower()
            if allow_navigation and "bing.com" not in current_url:
                await page.goto(BING_HOME_URL, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1.5)
            else:
                await asyncio.sleep(0.5 if allow_navigation else 0.2)

            if page.is_closed():
                return False

            url = page.url.lower()
            on_login_domain = any(
                host in url
                for host in (
                    "login.live.com",
                    "login.microsoftonline.com",
                    "login.microsoft.com",
                )
            )

            if not on_login_domain:
                await self._dismiss_messages(page)
                if page.is_closed():
                    return False

            if on_login_domain:
                return await self._has_auth_cookie(page)

            if "rewards.bing.com" in url:
                return True

            sign_in_text = " ".join(
                part.lower()
                for part in (
                    await self._selector_text(page, "#id_s"),
                    await self._selector_text(page, "#id_l"),
                )
                if part
            )
            if "sign in" in sign_in_text or "dang nhap" in sign_in_text or "đăng nhập" in sign_in_text:
                return False

            if await self._has_auth_cookie(page):
                return True

            if await self._check(page, SELECTORS["rewards_points"]):
                return True

            profile_text = await self._selector_text(page, SELECTORS["bing_profile"])
            if profile_text and "sign in" not in profile_text.lower():
                return True

            return False
        except Exception as e:
            logger.debug(f"is_logged_in probe failed: {e}")
            return False

    async def detect_account_issues(self, page: Page) -> Optional[str]:
        """Detect if account is locked/suspended."""
        if await self._check(page, SELECTORS["account_locked"]):
            return "Account locked by Microsoft"
        return None

    async def _body_text(self, page: Page) -> str:
        """Return page body text or an empty string."""
        try:
            return await page.locator("body").inner_text(timeout=1000)
        except Exception:
            return ""

    async def _click_first_matching_text(self, page: Page, texts: tuple[str, ...]) -> bool:
        """Click the first visible clickable element whose text matches a target."""
        if page.is_closed():
            return False

        normalized_targets = [text.strip().lower() for text in texts if text.strip()]
        if not normalized_targets:
            return False

        try:
            clicked = await page.evaluate(
                """
                (targets) => {
                    const normalize = (value) =>
                        (value || "")
                            .toLowerCase()
                            .normalize("NFD")
                            .replace(/[\\u0300-\\u036f]/g, "")
                            .replace(/\\s+/g, " ")
                            .trim();
                    const selector = "a, button, [role='button'], input[type='button'], input[type='submit'], span[role='button']";
                    const nodes = Array.from(document.querySelectorAll(selector));
                    const wanted = targets.map(normalize);

                    for (const target of wanted) {
                        for (const node of nodes) {
                            const style = window.getComputedStyle(node);
                            if (style.display === "none" || style.visibility === "hidden") {
                                continue;
                            }
                            const text = normalize(node.innerText || node.textContent || node.getAttribute("aria-label"));
                            if (!text) {
                                continue;
                            }
                            if (text.includes(target)) {
                                node.click();
                                return text;
                            }
                        }
                    }

                    return null;
                }
                """,
                normalized_targets,
            )
            if clicked:
                logger.info(f"Clicked Microsoft option: {clicked}")
                return True
        except Exception:
            pass

        return False

    async def _open_login_entry(self, page: Page) -> None:
        """Open the Microsoft sign-in page directly."""
        if page.is_closed():
            raise RuntimeError("Page closed unexpectedly during login")

        await page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await asyncio.sleep(1.5)
        await self._dismiss_messages(page)

    async def _login_inner(
        self,
        page: Page,
        email: str,
        password: str,
        totp_secret: Optional[str] = None,
    ) -> Page:
        """Run a single, deterministic Microsoft sign-in flow."""
        logger.info(f"Starting login for {email[:5]}***")

        await self._open_login_entry(page)
        await self._dismiss_messages(page)

        max_iterations = 25
        previous_state = LoginState.UNKNOWN
        same_state_count = 0
        active_page = page

        for iteration in range(max_iterations):
            if active_page.is_closed():
                raise RuntimeError("Page closed unexpectedly during login")

            state = await self._detect_state(active_page)
            logger.debug(f"Login iteration {iteration + 1}/{max_iterations}: state={state.value}")

            if self.challenge_handler:
                resolved = await self.challenge_handler.handle_if_present(
                    active_page,
                    account=email,
                    context="Microsoft login",
                )
                if not resolved:
                    raise RuntimeError("Manual verification challenge not resolved during login")

            if state != previous_state and previous_state != LoginState.UNKNOWN:
                logger.info(f"State: {previous_state.value} -> {state.value}")

            if state == previous_state and state not in (LoginState.LOGGED_IN, LoginState.UNKNOWN):
                same_state_count += 1
                max_same = 8 if state in (
                    LoginState.PASSKEY_VIDEO,
                    LoginState.PASSKEY_ERROR,
                    LoginState.SIGN_IN_ANOTHER_WAY,
                ) else 4
                if same_state_count >= max_same:
                    logger.warning(f"Stuck in state '{state.value}' for {max_same} loops, retrying state recovery...")
                    if state in (
                        LoginState.PASSKEY_VIDEO,
                        LoginState.PASSKEY_ERROR,
                        LoginState.SIGN_IN_ANOTHER_WAY,
                    ):
                        await self._handle_passkey_to_password(active_page)
                    else:
                        await active_page.reload(wait_until="domcontentloaded")
                        await asyncio.sleep(3)
                    same_state_count = 0
                    previous_state = LoginState.UNKNOWN
                    continue
            else:
                same_state_count = 0

            previous_state = state

            if state == LoginState.LOGGED_IN:
                logger.info(f"Successfully logged in as {email[:5]}***")
                await self._finalize_login(active_page)
                return active_page

            should_continue = await self._handle_state(
                state,
                active_page,
                email,
                password,
                totp_secret,
            )
            if not should_continue:
                raise RuntimeError(f"Login failed at state: {state.value}")

            await asyncio.sleep(1)

        raise RuntimeError(f"Login timeout: exceeded {max_iterations} iterations")

    async def _detect_state(self, page: Page) -> LoginState:
        """Detect the current Microsoft login surface."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass

        url = page.url.lower()
        body_text = (await self._body_text(page)).lower()

        if any(host in url for host in ("rewards.bing.com", "account.microsoft.com", "account.live.com")):
            return LoginState.LOGGED_IN

        if await self._check(page, SELECTORS["account_locked"]):
            return LoginState.ACCOUNT_LOCKED

        if (
            "login.microsoft.com" in url
            and any(marker in url for marker in ("/fido/", "/passkey", "/webauthn"))
        ):
            if await self._check(page, SELECTORS["passkey_error"]) or "sign in another way" in body_text:
                return LoginState.PASSKEY_ERROR
            return LoginState.PASSKEY_VIDEO

        if await self._check(page, SELECTORS["password_input"]) or \
           await self._check(page, SELECTORS["password_input_old"]):
            return LoginState.PASSWORD_INPUT

        if await self._check(page, SELECTORS["email_input"]) or \
           await self._check(page, SELECTORS["email_input_old"]):
            return LoginState.EMAIL_INPUT

        if await self._check(page, SELECTORS["kmsi_video"]) or "stay signed in" in body_text:
            return LoginState.KMSI_PROMPT

        if await self._check(page, SELECTORS["passkey_video"]):
            return LoginState.PASSKEY_VIDEO

        if await self._check(page, SELECTORS["passkey_error"]):
            return LoginState.PASSKEY_ERROR

        if await self._check(page, SELECTORS["totp_input"]) or \
           await self._check(page, SELECTORS["totp_input_old"]):
            return LoginState.TOTP_2FA

        if await self._check(page, SELECTORS["otp_code_entry"]):
            return LoginState.OTP_CODE_ENTRY

        if (
            await self._check(page, SELECTORS["password_icon"])
            or await self._check(page, SELECTORS["other_ways"])
            or await self._check(page, SELECTORS["fido_sign_in_another_way"])
        ):
            return LoginState.SIGN_IN_ANOTHER_WAY

        if await self._check(page, SELECTORS["recovery_email"]):
            return LoginState.RECOVERY_EMAIL

        if await self._check(page, SELECTORS["passwordless_check"]):
            return LoginState.PASSWORDLESS

        if await self._check(page, SELECTORS["error_alert"]) and "login.live.com" in url:
            return LoginState.ERROR_ALERT

        if "bing.com" in url:
            sign_in_text = " ".join(
                part.lower()
                for part in (
                    await self._selector_text(page, "#id_s"),
                    await self._selector_text(page, "#id_l"),
                )
                if part
            )
            if "sign in" not in sign_in_text and "dang nhap" not in sign_in_text:
                if await self._has_auth_cookie(page):
                    return LoginState.LOGGED_IN

        return LoginState.UNKNOWN

    async def _handle_state(
        self,
        state: LoginState,
        page: Page,
        email: str,
        password: str,
        totp_secret: Optional[str],
    ) -> bool:
        """Handle one login state. Return True to continue the loop."""
        if state == LoginState.ACCOUNT_LOCKED:
            raise RuntimeError("Account locked by Microsoft. Check your account.")

        if state == LoginState.ERROR_ALERT:
            error_el = page.locator(SELECTORS["error_alert"])
            error_msg = await error_el.inner_text() if await error_el.count() > 0 else "Unknown error"
            raise RuntimeError(f"Microsoft login error: {error_msg}")

        if state == LoginState.EMAIL_INPUT:
            logger.info("Entering email...")
            await self._enter_email(page, email)
            return True

        if state == LoginState.PASSWORD_INPUT:
            logger.info("Entering password...")
            await self._enter_password(page, password)
            return True

        if state == LoginState.KMSI_PROMPT:
            logger.info("Accepting 'Stay signed in' prompt...")
            await self._click_primary_button(page)
            return True

        if state in (
            LoginState.PASSKEY_VIDEO,
            LoginState.PASSKEY_ERROR,
            LoginState.SIGN_IN_ANOTHER_WAY,
        ):
            logger.info("Trying to switch Microsoft back to password sign-in...")
            await self._handle_passkey_to_password(page)
            return True

        if state == LoginState.TOTP_2FA:
            logger.info("Entering 2FA TOTP code...")
            await self._enter_totp(page, totp_secret)
            return True

        if state == LoginState.OTP_CODE_ENTRY:
            logger.info("OTP code entry detected, trying to switch to password...")
            if not await self._try_switch_to_password_flow(page):
                await self._click_selector(page, SELECTORS["back_button"])
            return True

        if state == LoginState.RECOVERY_EMAIL:
            raise RuntimeError("Recovery email verification required. Please verify manually.")

        if state == LoginState.PASSWORDLESS:
            await self._try_switch_to_password_flow(page)
            await asyncio.sleep(2)
            return True

        if state == LoginState.UNKNOWN:
            if await self._try_switch_to_password_flow(page):
                return True
            logger.debug(f"Unknown login state at {page.url}, waiting...")
            await asyncio.sleep(2)
            return True

        return True

    async def _enter_email(self, page: Page, email: str) -> None:
        """Enter email and submit."""
        for selector in [SELECTORS["email_input"], SELECTORS["email_input_old"]]:
            try:
                el = await page.wait_for_selector(selector, state="visible", timeout=1500)
            except Exception:
                continue

            if not el:
                continue

            prefilled = await page.query_selector("#userDisplayName")
            if not prefilled:
                await page.fill(selector, "")
                await asyncio.sleep(0.3)
                await page.fill(selector, email)
                await asyncio.sleep(0.5)
            else:
                logger.info("Email already pre-filled")

            await self._click_submit(page)
            await self._wait_after_email_submit(page)
            return

        logger.warning("Could not find email field")

    async def _wait_after_email_submit(self, page: Page) -> None:
        """Wait until Microsoft shows the next stable step after email submit."""
        deadline = asyncio.get_event_loop().time() + 18.0

        while asyncio.get_event_loop().time() < deadline:
            if page.is_closed():
                raise RuntimeError("Page closed unexpectedly during login")

            if await self._check(page, SELECTORS["password_input"]) or \
               await self._check(page, SELECTORS["password_input_old"]):
                return

            state = await self._detect_state(page)
            if state in (
                LoginState.LOGGED_IN,
                LoginState.KMSI_PROMPT,
                LoginState.PASSWORD_INPUT,
                LoginState.TOTP_2FA,
                LoginState.OTP_CODE_ENTRY,
            ):
                return

            if state in (
                LoginState.PASSKEY_VIDEO,
                LoginState.PASSKEY_ERROR,
                LoginState.SIGN_IN_ANOTHER_WAY,
                LoginState.UNKNOWN,
            ):
                if await self._try_switch_to_password_flow(page):
                    await asyncio.sleep(1)
                    continue

            await asyncio.sleep(0.5)

    async def _handle_passkey_to_password(self, page: Page) -> bool:
        """Switch a passkey/FIDO screen back to password sign-in."""
        if page.is_closed():
            return False

        if await self._check(page, SELECTORS["password_input"]) or \
           await self._check(page, SELECTORS["password_input_old"]):
            return True

        if await self._click_selector(page, SELECTORS["password_icon"]):
            await asyncio.sleep(1)
            return True

        if await self._click_selector(page, SELECTORS["fido_sign_in_another_way"]):
            await asyncio.sleep(1.2)
            if await self._check(page, SELECTORS["password_input"]) or \
               await self._check(page, SELECTORS["password_input_old"]):
                return True

        if await self._click_first_matching_text(page, self.PASSKEY_SWITCH_CHOICES):
            await asyncio.sleep(1.2)
            if await self._check(page, SELECTORS["password_input"]) or \
               await self._check(page, SELECTORS["password_input_old"]):
                return True

        if await self._click_selector(page, SELECTORS["fido_use_password"]):
            await asyncio.sleep(1.2)
            return True

        if await self._click_first_matching_text(page, self.PASSKEY_PASSWORD_CHOICES):
            await asyncio.sleep(1.2)
            return True

        if await self._click_selector(page, SELECTORS["other_ways"]):
            await asyncio.sleep(1)
            return True

        if await self._click_selector(page, SELECTORS["view_footer"]):
            await asyncio.sleep(1)
            return True

        if await self._click_selector(page, SELECTORS["fido_cancel"]):
            await asyncio.sleep(1)
            return True

        if await self._click_first_matching_text(page, self.PASSKEY_FALLBACK_CHOICES):
            await asyncio.sleep(1)
            return True

        try:
            await page.go_back(wait_until="domcontentloaded", timeout=5000)
            await asyncio.sleep(1)
            if "login.microsoft.com" not in page.url.lower():
                return True
        except Exception:
            pass

        return False

    async def _try_switch_to_password_flow(self, page: Page) -> bool:
        """Attempt to switch passkey/passwordless prompts back to password sign-in."""
        if page.is_closed():
            return False

        url = page.url.lower()
        body_text = (await self._body_text(page)).lower()
        markers = ("fido/", "passkey", "passwordless", "webauthn", "login.microsoft.com")
        cues = (
            "passkey",
            "fingerprint",
            "security key",
            "pin",
            "sign in another way",
            "other ways to sign in",
            "use password",
            "use your password",
        )

        if not any(marker in url for marker in markers):
            if not any(cue in body_text for cue in cues):
                if not await self._check(page, SELECTORS["password_icon"]):
                    return False

        return await self._handle_passkey_to_password(page)
