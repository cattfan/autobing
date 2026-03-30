"""
Edge Streak via native Edge CDP connection with stealth JS injection.

Microsoft blocks Edge telemetry when webdriver=true.
This module launches Edge as a normal process using the BOT's profile directory
(which already has Microsoft login cookies), uses Win32 to push it to the
background (HWND_BOTTOM) so it doesn't interrupt the user, and connects via
Playwright CDP to inject JS stealth scripts (mocking visibilityState and
hasFocus).

CRITICAL: The Edge instance MUST be logged into the Microsoft account for
browsing minutes to count toward the streak. We use --user-data-dir to point
to the bot's existing profile directory where login cookies are stored.
"""

from __future__ import annotations
import asyncio
import ctypes
import random
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

import urllib.request
import json
from playwright.async_api import async_playwright

from src.utils import get_edge_executable_path, logger, DATA_DIR, PROFILES_DIR

user32 = ctypes.windll.user32

HWND_BOTTOM = 1
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9

# Bing pages to browse (must be bing.com for telemetry tracking)
BROWSE_URLS = [
    "https://www.bing.com",
    "https://www.bing.com/news",
    "https://www.bing.com/maps",
    "https://www.bing.com/videos",
    "https://www.bing.com/images",
    "https://www.bing.com/travel",
    "https://www.bing.com/search?q=weather+today",
    "https://www.bing.com/search?q=technology+news",
    "https://www.bing.com/search?q=sports+scores+today",
    "https://www.bing.com/search?q=financial+planning+advice",
    "https://www.bing.com/search?q=healthy+eating+tips",
    "https://www.bing.com/search?q=home+improvement+ideas",
    "https://www.bing.com/search?q=book+recommendations",
    "https://www.bing.com/search?q=how+to+cook+pasta",
    "https://www.bing.com/search?q=learn+a+new+language",
    "https://www.bing.com/search?q=best+movies+of+all+time",
]

class NativeEdgeStreak:
    """Handles the Edge Browsing Streak specifically by running MS Edge fully
    natively via CDP connection. This hides 'webdriver=true' so Microsoft
    tracking thinks it's a real user. We use Win32 to push Edge to the bottom
    of the screen stack so it never interrupts the user, and inject JS stealth
    scripts to spoof visibility.

    IMPORTANT: Must be initialized with account_email so we can use the correct
    logged-in profile directory. Without this, Edge opens with default profile
    and Microsoft doesn't know who is browsing → 0/30 minutes.
    """

    def __init__(self, account_email: str = "", storage_state_path: Optional[Path] = None):
        self._edge_exe = get_edge_executable_path()
        self.edge_process: Optional[subprocess.Popen] = None
        self._edge_hwnd = None
        self._cdp_port = 9323
        self._account_email = account_email
        self._storage_state_path = storage_state_path
        self._diagnostic_log = None  # Set by browse() for Web Dashboard logging
        # Build the profile directory path matching browser.py's convention
        if account_email:
            safe_email = account_email.replace("@", "_at_").replace(".", "_")
            self._profile_dir = DATA_DIR / "edge_runtime" / safe_email
        else:
            self._profile_dir = None

    def _wait_for_cdp(self, timeout=15) -> Optional[str]:
        """Poll the CDP endpoint until it responds or timeout."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self._cdp_port}/json/version")
                with urllib.request.urlopen(req, timeout=3) as response:
                    data = json.loads(response.read())
                    return data["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(1)
        return None

    def _position_edge_corner(self):
        """Position Edge as a small window at the bottom-right corner of the screen.
        
        CRITICAL: Do NOT use HWND_BOTTOM. Microsoft Edge telemetry uses native Win32
        focus signals to track browsing time. Pushing Edge to HWND_BOTTOM causes the
        timer to stop counting. Instead, we keep Edge in normal Z-order but make it
        small and position it at the screen corner so it's non-intrusive.
        """
        if not self.edge_process:
            return

        # Wait a moment for Edge windows to appear
        import time as _time
        max_wait = 8
        start = _time.time()
        hwnds = []
        while _time.time() - start < max_wait:
            hwnds = []
            def callback(hwnd, extra):
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == self.edge_process.pid and user32.IsWindowVisible(hwnd):
                    hwnds.append(hwnd)
                return True
            user32.EnumWindows(
                ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(callback), 0
            )
            if hwnds:
                break
            _time.sleep(0.5)

        if not hwnds:
            logger.warning("Could not find Edge window to reposition")
            return

        self._edge_hwnd = hwnds[0]

        # Get screen dimensions
        screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        screen_h = user32.GetSystemMetrics(1)  # SM_CYSCREEN

        # Small window at bottom-right corner (non-intrusive but still "visible" to OS)
        win_w = 500
        win_h = 400
        pos_x = screen_w - win_w - 10
        pos_y = screen_h - win_h - 50  # 50px above taskbar

        # Restore window if minimized and reposition
        user32.ShowWindow(self._edge_hwnd, SW_RESTORE)
        # Place at corner WITHOUT SWP_NOACTIVATE — let it be "active" 
        user32.SetWindowPos(
            self._edge_hwnd, HWND_NOTOPMOST,
            pos_x, pos_y, win_w, win_h,
            SWP_SHOWWINDOW,
        )
        logger.info(
            f"Edge window positioned at bottom-right corner ({pos_x},{pos_y} {win_w}x{win_h}) — "
            f"staying in normal Z-order for telemetry tracking."
        )

    def _pulse_edge_focus(self):
        """Briefly bring Edge window to foreground to refresh telemetry tracking.
        
        Microsoft telemetry may stop counting if Edge loses foreground status.
        This method brings Edge briefly to foreground then releases it.
        """
        if not self._edge_hwnd:
            return
        try:
            # Restore and bring to foreground
            user32.ShowWindow(self._edge_hwnd, SW_RESTORE)
            user32.SetForegroundWindow(self._edge_hwnd)
        except Exception:
            pass

    async def _kill_all_edge(self):
        """Ensure no lingering instances prevent our clean launch."""
        try:
            logger.debug("Killing all existing msedge.exe processes...")
            subprocess.run(["taskkill", "/f", "/im", "msedge.exe", "/t"], 
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"taskkill msedge: {e}")

    async def _launch_edge(self, start_url: str = "https://www.bing.com") -> bool:
        """Launch Edge natively with the BOT's profile (logged-in cookies)."""
        await self._kill_all_edge()

        args = [
            self._edge_exe,
            "--no-first-run",
            "--window-size=500,400",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--disable-features=msEdgeSessionRestore",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # CRITICAL: Suppress navigator.webdriver = true
            "--disable-blink-features=AutomationControlled",
            f"--remote-debugging-port={self._cdp_port}",
        ]

        # CRITICAL: Use the bot's profile directory where login cookies exist
        if self._profile_dir:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            args.append(f"--user-data-dir={self._profile_dir}")
            logger.info(f"Using bot profile: {self._profile_dir.name}")
        else:
            logger.warning("No profile directory specified — Edge will use default profile (may not be logged in!)")

        args.append(start_url)

        logger.info(f"Launching native Edge (CDP port {self._cdp_port})")
        self.edge_process = subprocess.Popen(args)
        
        # Give it a moment to initialize its GUI, then push to bottom
        await asyncio.sleep(3)
        self._position_edge_corner()
        return True

    async def _ensure_login(self, page) -> bool:
        """Verify the Edge session is logged into Microsoft. 
        If not, import cookies from storage_state and reload."""
        try:
            # Navigate to bing.com to check login status
            await page.goto("https://www.bing.com", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            # Check if logged in by looking for the profile element
            logged_in = await page.evaluate("""
                () => {
                    const profileEl = document.querySelector('#id_n');
                    const signInEl = document.querySelector('#id_s, #id_l');
                    if (profileEl) return true;
                    if (signInEl) {
                        const text = (signInEl.textContent || '').toLowerCase();
                        return !text.includes('sign in') && !text.includes('đăng nhập');
                    }
                    return false;
                }
            """)

            if logged_in:
                logger.info("Edge session is logged in ✅")
                return True

            logger.warning("Edge session NOT logged in — importing cookies from storage state...")
            
            # Try to import cookies from the bot's storage state file
            if self._storage_state_path and self._storage_state_path.exists():
                try:
                    with open(self._storage_state_path, "r", encoding="utf-8") as f:
                        state_data = json.load(f)
                    
                    cookies = state_data.get("cookies", [])
                    if cookies:
                        # Convert Playwright cookies format to CDP format
                        cdp_cookies = []
                        for c in cookies:
                            cdp_cookie = {
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c["domain"],
                                "path": c.get("path", "/"),
                            }
                            if c.get("expires", -1) > 0:
                                cdp_cookie["expires"] = c["expires"]
                            if c.get("httpOnly"):
                                cdp_cookie["httpOnly"] = True
                            if c.get("secure"):
                                cdp_cookie["secure"] = True
                            if c.get("sameSite"):
                                cdp_cookie["sameSite"] = c["sameSite"]
                            cdp_cookies.append(cdp_cookie)

                        await page.context.add_cookies(cdp_cookies)
                        logger.info(f"Imported {len(cdp_cookies)} cookies into Edge streak session")
                        
                        # Reload to apply cookies
                        await page.goto("https://www.bing.com", wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(3)
                        
                        # Verify again
                        logged_in_2 = await page.evaluate("""
                            () => {
                                const profileEl = document.querySelector('#id_n');
                                return !!profileEl;
                            }
                        """)
                        if logged_in_2:
                            logger.info("Edge session logged in after cookie import ✅")
                            return True
                        else:
                            logger.warning("Cookie import didn't establish login — trying login flow")
                except Exception as e:
                    logger.warning(f"Cookie import failed: {e}")

            # Last resort: navigate to rewards login URL which may auto-login with existing cookies
            try:
                await page.goto(
                    "https://login.live.com/login.srf?wa=wsignin1.0&wp=MBI_SSL&wreply=https://rewards.bing.com/",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await asyncio.sleep(5)
                
                if "rewards.bing.com" in page.url or "bing.com" in page.url:
                    logger.info("Auto-login via redirect succeeded ✅")
                    return True
            except Exception as e:
                logger.warning(f"Auto-login redirect failed: {e}")

            logger.error("Could not establish login for Edge Streak — minutes may not count!")
            return False

        except Exception as e:
            logger.warning(f"Login check failed: {e}")
            return False

    def _log(self, level: str, message: str):
        """Log to both logger and diagnostic_log callback (for Web Dashboard)."""
        if level == "warning":
            logger.warning(message)
        elif level == "debug":
            logger.debug(message)
        elif level == "error":
            logger.error(message)
        else:
            logger.info(message)
        if self._diagnostic_log:
            try:
                self._diagnostic_log(level, message)
            except Exception:
                pass

    async def _check_current_streak_progress(self, context) -> int:
        """Silently query the Rewards API in a background tab to check current tracking progress.
        
        FIX #1: Uses rewards.bing.com directly (no bing.com navigation needed)
        to minimize latency and network overhead.
        """
        progress_min = 0
        bg_page = None
        try:
            bg_page = await context.new_page()
            # Navigate directly to rewards — cookies are shared across the context
            await bg_page.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=10000)
            data = await bg_page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('/api/getuserinfo?type=1', {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'}
                        });
                        return await r.json();
                    } catch(e) { return null; }
                }
            """)
            if data:
                dashboard = data.get("dashboard", {})
                
                # Check promotions for edge streak
                for _ds_key, _ds_items in dashboard.get("dailySetPromotions", {}).items():
                    if isinstance(_ds_items, list):
                        for item in _ds_items:
                            title = (item.get("title", "") or item.get("name", "")).lower()
                            if "edge" in title and ("brows" in title or "streak" in title or "minute" in title):
                                return item.get("pointProgress", 0)
                                
                for promo in dashboard.get("morePromotions", []):
                    title = (promo.get("title", "") or promo.get("name", "")).lower()
                    if "edge" in title and ("brows" in title or "streak" in title or "minute" in title):
                        return promo.get("pointProgress", 0)
                        
                for pc in dashboard.get("punchCards", []):
                    for child in pc.get("childPromotions", []):
                        title = (child.get("title", "") or child.get("name", "")).lower()
                        if "edge" in title and ("brows" in title or "streak" in title or "minute" in title):
                            return child.get("pointProgress", 0)

        except Exception as e:
            logger.debug(f"Failed to check streak progress: {e}")
        finally:
            if bg_page:
                try:
                    await bg_page.close()
                except Exception:
                    pass
        return progress_min

    async def _activate_edge_streak_card(self, page) -> bool:
        """Click the Edge Browsing Streak card on rewards.bing.com to activate tracking.
        
        FIX #3: Microsoft may require clicking the card to start the timer.
        This mirrors the logic in streaks.py EdgeBrowsingStreak.
        """
        activation_urls = [
            "https://rewards.bing.com/pointsbreakdown",
            "https://rewards.bing.com/earn",
            "https://rewards.bing.com/",
        ]
        for act_url in activation_urls:
            try:
                await page.goto(act_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
                
                # JS-based card finder (works regardless of DOM structure)
                activated = await page.evaluate("""
                    () => {
                        // Search all links and clickable elements for Edge-related text
                        const allElements = document.querySelectorAll('a, button, [role="link"], [role="button"], mee-card a');
                        for (const el of allElements) {
                            const text = (el.textContent || '').toLowerCase();
                            const href = (el.href || '').toLowerCase();
                            if ((text.includes('edge') && (text.includes('brows') || text.includes('streak') || text.includes('minute')))
                                || (href.includes('edge') && href.includes('streak'))) {
                                el.click();
                                return true;
                            }
                        }
                        // Try shadow DOM elements (mee-card components)
                        const cards = document.querySelectorAll('mee-card, mee-rewards-more-activities-card-item');
                        for (const card of cards) {
                            const text = (card.textContent || '').toLowerCase();
                            if (text.includes('edge') && (text.includes('brows') || text.includes('streak'))) {
                                const link = card.querySelector('a');
                                if (link) { link.click(); return true; }
                                card.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if activated:
                    self._log("info", f"✅ Edge Streak card activated on {act_url}")
                    await asyncio.sleep(random.uniform(3, 5))
                    return True
            except Exception:
                continue
        
        self._log("warning", "⚠️ Could not click Edge Streak card — tracking may not activate")
        return False

    async def browse(self, target_minutes: int, on_progress: Callable[[int, int], None], start_url: str = "https://www.bing.com", diagnostic_log=None):
        """Run the automated browsing loop over CDP with stealth mocks and dynamic API validation.
        
        FIX #2: diagnostic_log is now used for all important messages.
        FIX #4: Max 5 restart attempts to prevent infinite loops.
        """
        if target_minutes <= 0:
            return

        # FIX #2: Store diagnostic_log for use by self._log()
        self._diagnostic_log = diagnostic_log

        baseline_minutes = 0
        overall_start_time = time.time()
        restart_count = 0
        MAX_RESTARTS = 5  # FIX #4: Prevent infinite restart loops
        
        while baseline_minutes < target_minutes and (time.time() - overall_start_time) < 10800:
            # FIX #4: Check restart limit
            if restart_count >= MAX_RESTARTS:
                self._log("error", f"❌ Edge Streak: Gave up after {MAX_RESTARTS} restart attempts. "
                          f"Progress: {baseline_minutes}/{target_minutes} min. "
                          f"Profile may need re-authentication.")
                break

            attempt_label = f" (attempt {restart_count + 1})" if restart_count > 0 else ""
            self._log("info", f"🚀 Starting Edge Browsing Streak via CDP{attempt_label} "
                      f"(Target: {target_minutes} min, current: {baseline_minutes} min)")
            
            if not await self._launch_edge(start_url):
                self._log("error", "Failed to launch Edge Native")
                return
                
            ws_url = self._wait_for_cdp(timeout=15)
            if not ws_url:
                self._log("error", "Could not get CDP endpoint from native Edge. Aborting streak.")
                if self.edge_process:
                    self.edge_process.terminate()
                return
                
            self._log("info", "Connecting Playwright over CDP...")
            
            # FIX #5: Reset HWND on each restart (handled by _launch_edge → _position_edge_corner)
            last_focus_pulse = time.time()
            last_progress_check = time.time()
            last_increase_time = time.time()
            
            FOCUS_PULSE_INTERVAL = 300  # Pulse focus every 5 minutes
            PROGRESS_CHECK_INTERVAL = 360  # Check progress every ~6 minutes
            STUCK_TIMEOUT = 540  # Consider stuck if no progress for 9 minutes
            
            urls_pool = list(BROWSE_URLS)
            tracking_stuck = False
            
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.connect_over_cdp(ws_url)
                    context = browser.contexts[0]
                    
                    # INJECT STEALTH SCRIPT TO TRICK TELEMETRY
                    await context.add_init_script("""
                        Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
                        Object.defineProperty(document, 'hidden', {get: () => false});
                        document.hasFocus = () => true;
                        window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
                        window.addEventListener('blur', e => e.stopImmediatePropagation(), true);
                        window.addEventListener('mouseleave', e => e.stopImmediatePropagation(), true);
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    """)
                    self._log("info", "Stealth scripts injected for telemetry spoofing.")
                    
                    page = context.pages[0] if context.pages else await context.new_page()

                    # CRITICAL: Verify Edge is logged into Microsoft account
                    login_ok = await self._ensure_login(page)
                    if not login_ok:
                        self._log("warning", "⚠️ Edge may not be logged in — streak minutes may not count!")

                    # FIX #3: Click Edge Streak card to activate tracking
                    await self._activate_edge_streak_card(page)

                    # Fetch initial baseline minutes for this session
                    current_prog = await self._check_current_streak_progress(context)
                    if current_prog > baseline_minutes:
                        baseline_minutes = current_prog
                    self._log("info", f"📊 Initial API progress: {baseline_minutes}/{target_minutes} min")

                    if baseline_minutes >= target_minutes:
                        self._log("info", f"✅ Edge Streak already complete ({baseline_minutes}/{target_minutes} min)")
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        break
                    
                    while not tracking_stuck:
                        if baseline_minutes >= target_minutes:
                            break

                        # Update UI
                        if on_progress:
                            on_progress(baseline_minutes, target_minutes)

                        # Periodic focus pulse
                        if time.time() - last_focus_pulse >= FOCUS_PULSE_INTERVAL:
                            self._pulse_edge_focus()
                            last_focus_pulse = time.time()
                            logger.debug(f"[Edge Streak] Focus pulse executed")

                        # Periodic progress validation
                        if time.time() - last_progress_check >= PROGRESS_CHECK_INTERVAL:
                            self._log("info", "🔍 Verifying tracking progress via API...")
                            api_val = await self._check_current_streak_progress(context)
                            self._log("info", f"📊 API progress: {api_val}/{target_minutes} min")
                            
                            if api_val > baseline_minutes:
                                # We made progress! Telemetry is alive.
                                baseline_minutes = api_val
                                last_increase_time = time.time()
                                self._log("info", f"✅ Tracking confirmed alive — {baseline_minutes}/{target_minutes} min")
                            elif time.time() - last_increase_time > STUCK_TIMEOUT:
                                # Oh no, we've been browsing for > 9 minutes with 0 progress.
                                self._log("warning", f"⚠️ Tracking stuck at {baseline_minutes}/{target_minutes} min "
                                          f"for over {STUCK_TIMEOUT // 60} min! Restarting Edge...")
                                tracking_stuck = True
                                break
                            last_progress_check = time.time()

                        if not urls_pool:
                            urls_pool = list(BROWSE_URLS)
                        
                        next_url = random.choice(urls_pool)
                        urls_pool.remove(next_url)

                        try:
                            logger.info(f"[Edge Native Streak] {baseline_minutes}/{target_minutes} min - {next_url}")
                            await page.goto(next_url, wait_until="domcontentloaded", timeout=15000)
                            
                            # Read and scroll actively
                            read_time = random.uniform(60, 150)
                            scroll_interval = random.uniform(10, 20)
                            read_elapsed = 0
                            
                            while read_elapsed < read_time:
                                try:
                                    await page.evaluate("window.scrollBy(0, 400)")
                                except Exception:
                                    pass

                                if random.random() < 0.4:
                                    try:
                                        x = random.randint(50, 450)
                                        y = random.randint(50, 350)
                                        await page.mouse.move(x, y)
                                    except Exception:
                                        pass
                                
                                wait_chunk = min(scroll_interval, read_time - read_elapsed)
                                await asyncio.sleep(wait_chunk)
                                read_elapsed += wait_chunk
                                
                                if time.time() - last_progress_check >= PROGRESS_CHECK_INTERVAL:
                                    break # jump out to do progress check

                        except Exception as e:
                            logger.warning(f"Error during CDP browsing to {next_url}: {e}")
                            await asyncio.sleep(5)

                    try:
                        await browser.close()
                    except Exception:
                        pass
                    
            except Exception as e:
                self._log("error", f"CDP Browser Streak error: {e}")
            finally:
                self._log("info", "Terminating native Edge session...")
                if self.edge_process:
                    self.edge_process.terminate()
                    try:
                        self.edge_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.edge_process.kill()
                self._edge_hwnd = None  # FIX #5: Clear stale HWND
                await self._kill_all_edge()
                
            if baseline_minutes >= target_minutes:
                break

            # FIX #4: Increment restart counter
            restart_count += 1
            self._log("info", f"🔄 Restarting Edge session (attempt {restart_count + 1}/{MAX_RESTARTS + 1})...")
            await asyncio.sleep(5)

