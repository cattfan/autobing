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

    async def browse(self, target_minutes: int, on_progress: Callable[[int, int], None], start_url: str = "https://www.bing.com", diagnostic_log=None):
        """Run the automated browsing loop over CDP with stealth mocks."""
        if target_minutes <= 0:
            return

        logger.info(f"Starting Edge Browsing Streak natively via CDP for {target_minutes} min")
        
        if not await self._launch_edge(start_url):
            logger.error("Failed to launch Edge Native")
            return
            
        ws_url = self._wait_for_cdp(timeout=15)
        if not ws_url:
            logger.error("Could not get CDP endpoint from native Edge. Aborting streak.")
            if self.edge_process:
                self.edge_process.terminate()
            return
            
        logger.info("Connecting Playwright over CDP...")
        
        start_time = time.time()
        urls_pool = list(BROWSE_URLS)
        
        # We handle exceptions tightly so we always cleanup the Edge process
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(ws_url)
                context = browser.contexts[0]
                
                # INJECT STEALTH SCRIPT TO TRICK TELEMETRY
                await context.add_init_script("""
                    // 1. Force visibilityState to 'visible'
                    Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
                    // 2. Force document.hidden to false
                    Object.defineProperty(document, 'hidden', {get: () => false});
                    // 3. Force document.hasFocus to true 
                    document.hasFocus = () => true;
                    // 4. Suppress blur/mouseleave events
                    window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
                    window.addEventListener('blur', e => e.stopImmediatePropagation(), true);
                    window.addEventListener('mouseleave', e => e.stopImmediatePropagation(), true);
                    // 5. Override navigator.webdriver (belt and suspenders)
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)
                logger.info("Stealth scripts injected for telemetry spoofing.")
                
                page = context.pages[0] if context.pages else await context.new_page()

                # CRITICAL: Verify Edge is logged into Microsoft account
                login_ok = await self._ensure_login(page)
                if not login_ok:
                    logger.warning("⚠️ Edge may not be logged in — streak minutes may not count!")

                # Navigate to Rewards page first to activate telemetry tracking
                try:
                    await page.goto("https://rewards.bing.com/", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                    logger.info("Visited rewards.bing.com to activate tracking")
                except Exception:
                    pass
                # Reset start time (login/setup may have taken time)
                start_time = time.time()
                last_focus_pulse = time.time()
                last_dom_check = time.time() - 170  # Check almost immediately on loop start
                FOCUS_PULSE_INTERVAL = 300  # Pulse focus every 5 minutes

                while True:
                    elapsed_min = int((time.time() - start_time) / 60)
                    # Extend timeout significantly: only fail-safe break if it's hopelessly stuck
                    if elapsed_min >= target_minutes + 25:
                        logger.warning(f"[Edge Native Streak] Reached hard timeout of {elapsed_min} minutes without verifying 30/30. Finishing.")
                        break

                    if on_progress:
                        on_progress(elapsed_min, target_minutes)

                    # Periodic focus pulse to keep telemetry tracking
                    if time.time() - last_focus_pulse >= FOCUS_PULSE_INTERVAL:
                        self._pulse_edge_focus()
                        last_focus_pulse = time.time()
                        logger.debug(f"[Edge Streak] Focus pulse at {elapsed_min} min")

                    # Periodically check REAL progress via the isolated background tab
                    if time.time() - last_dom_check >= 180:
                        last_dom_check = time.time()
                        try:
                            cookies = await context.cookies("https://rewards.bing.com")
                            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                            user_agent = await page.evaluate("navigator.userAgent")
                            headers = {
                                "User-Agent": user_agent,
                                "Cookie": cookie_str,
                                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                            }
                            import httpx
                            async with httpx.AsyncClient() as client:
                                response = await client.get("https://rewards.bing.com/earn", headers=headers, timeout=15.0)
                                page_text = response.text
                            
                            import re
                            edge_match = re.search(
                                r"Edge(?:\s+Browsing(?:\s+Streak)?)?(?:[\s\S]{0,150})Minutes:\s*(\d+)\s*/\s*(\d+)",
                                page_text, re.IGNORECASE
                            )
                            if edge_match:
                                current_min = int(edge_match.group(1))
                                target_min = int(edge_match.group(2))
                                logger.info(f"📊 LIVE PROGRESS (Background HTTP): {current_min}/{target_min} min")
                                if current_min >= target_min:
                                    logger.info("🎉 Microsoft reported target reached! Finishing early.")
                                    break
                            else:
                                logger.info("DOM parsing: Streak card not found right now")
                        except Exception as e:
                            logger.info(f"Background HTTP check failed: {e}")

                    if not urls_pool:
                        urls_pool = list(BROWSE_URLS)
                    
                    next_url = random.choice(urls_pool)
                    urls_pool.remove(next_url)

                    try:
                        logger.info(f"[Edge Native Streak] {elapsed_min}/{target_minutes} min - {next_url}")
                        await page.goto(next_url, wait_until="domcontentloaded", timeout=15000)
                        
                        # Re-inject stealth on each navigation (belt and suspenders)
                        try:
                            await asyncio.wait_for(page.evaluate("""
                                Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
                                Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
                                document.hasFocus = () => true;
                            """), timeout=5.0)
                        except Exception:
                            pass
                        
                        # Read and scroll actively with mouse movements
                        read_time = random.uniform(60, 180)
                        scroll_interval = random.uniform(10, 20)
                        read_elapsed = 0
                        
                        while read_elapsed < read_time:
                            # Scroll down
                            try:
                                await asyncio.wait_for(page.evaluate("window.scrollBy(0, 400)"), timeout=5.0)
                            except Exception:
                                pass

                            # Random mouse movement (makes browsing look natural)
                            if random.random() < 0.4:
                                try:
                                    x = random.randint(50, 450)
                                    y = random.randint(50, 350)
                                    await asyncio.wait_for(page.mouse.move(x, y), timeout=5.0)
                                except Exception:
                                    pass
                            
                            wait_chunk = min(scroll_interval, read_time - read_elapsed)
                            await asyncio.sleep(wait_chunk)
                            read_elapsed += wait_chunk
                            
                            # Break inner loop early if we hit global target
                            if int((time.time() - start_time) / 60) >= target_minutes:
                                break

                    except Exception as e:
                        logger.warning(f"Error during CDP browsing to {next_url}: {e}")
                        await asyncio.sleep(5)

                await browser.close()
                
        except Exception as e:
            logger.error(f"CDP Browser Streak encountered an error: {e}")
        finally:
            logger.info("Terminating native Edge session...")
            if self.edge_process:
                self.edge_process.terminate()
                try:
                    self.edge_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.edge_process.kill()
            await self._kill_all_edge()
