"""
Edge Streak via native Edge CDP connection with stealth JS injection.

Microsoft blocks Edge telemetry when webdriver=true.
This module launches Edge as a normal process, uses Win32 to push it to the background
(HWND_BOTTOM) so it doesn't interrupt the user, and connects via Playwright CDP to
inject JS stealth scripts (mocking visibilityState and hasFocus).
"""

from __future__ import annotations
import asyncio
import ctypes
import random
import subprocess
import time
from typing import Any, Callable, Optional

import urllib.request
import json
from playwright.async_api import async_playwright

from src.utils import get_edge_executable_path, logger

user32 = ctypes.windll.user32

HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 4

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
    """Handles the Edge Browsing Streak specifically by running MS Edge fully natively
    via CDP connection. This hides 'webdriver=true' so Microsoft tracking thinks it's a
    real user. We use Win32 to push Edge to the bottom of the screen stack so it
    never interrupts the user, and inject JS stealth scripts to spoof visibility.
    """

    def __init__(self):
        self._edge_exe = get_edge_executable_path()
        self.edge_process: Optional[subprocess.Popen] = None
        self._edge_hwnd = None
        self._cdp_port = 9323

    def _wait_for_cdp(self, timeout=10) -> Optional[str]:
        """Poll the CDP endpoint until it responds or timeout."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self._cdp_port}/json/version")
                with urllib.request.urlopen(req) as response:
                    data = json.loads(response.read())
                    return data["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(1)
        return None

    def _push_edge_to_background(self):
        """Find the launched Edge window and push it to the bottom Z-order."""
        if not self.edge_process:
            return

        hwnds = []
        def callback(hwnd, extra):
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.edge_process.pid and user32.IsWindowVisible(hwnd):
                hwnds.append(hwnd)
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(callback), 0)
        
        if hwnds:
            self._edge_hwnd = hwnds[0]
            # Push to bottom immediately without stealing focus
            user32.SetWindowPos(self._edge_hwnd, HWND_BOTTOM, 0, 0, 0, 0, 
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            user32.ShowWindow(self._edge_hwnd, SW_SHOWNOACTIVATE)
            logger.info("Edge window successfully moved to non-intrusive HWND_BOTTOM.")

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
        """Launch Edge natively with background telemetry flags and CDP."""
        await self._kill_all_edge()

        args = [
            self._edge_exe,
            "--no-first-run",
            "--window-size=900,700",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--disable-features=msEdgeSessionRestore",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            f"--remote-debugging-port={self._cdp_port}",
            start_url,
        ]

        logger.info(f"Launching native Edge (CDP port {self._cdp_port})")
        # CREATE_NO_WINDOW is not used here because Edge still needs a real window for rendering stealth properly
        self.edge_process = subprocess.Popen(args)
        
        # Give it a moment to initialize its GUI, then push to bottom
        await asyncio.sleep(3)
        self._push_edge_to_background()
        return True

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
                # 1. Force visibilityState to 'visible'
                # 2. Force document.hidden to false
                # 3. Force document.hasFocus to true 
                # 4. Suppress blur/mouseleave events
                await context.add_init_script("""
                    Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
                    Object.defineProperty(document, 'hidden', {get: () => false});
                    document.hasFocus = () => true;
                    window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
                    window.addEventListener('blur', e => e.stopImmediatePropagation(), true);
                    window.addEventListener('mouseleave', e => e.stopImmediatePropagation(), true);
                """)
                logger.info("Stealth scripts injected for telemetry spoofing.")
                
                page = context.pages[0] if context.pages else await context.new_page()

                while True:
                    elapsed_min = int((time.time() - start_time) / 60)
                    if elapsed_min >= target_minutes:
                        break

                    if on_progress:
                        on_progress(elapsed_min, target_minutes)

                    if not urls_pool:
                        urls_pool = list(BROWSE_URLS)
                    
                    next_url = random.choice(urls_pool)
                    urls_pool.remove(next_url)

                    try:
                        logger.info(f"[Edge Native Streak] {elapsed_min}/{target_minutes} min - {next_url}")
                        await page.goto(next_url, wait_until="domcontentloaded", timeout=15000)
                        
                        # Read and scroll actively
                        read_time = random.uniform(60, 180)
                        scroll_interval = random.uniform(10, 20)
                        read_elapsed = 0
                        
                        while read_elapsed < read_time:
                            await page.evaluate("window.scrollBy(0, 400)")
                            
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
