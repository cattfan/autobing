"""
Edge Streak via NATIVE subprocess — NO CDP, NO Playwright, NO Selenium.

Microsoft blocks Edge telemetry when --remote-debugging-port is present.
This module launches Edge as a completely normal process and uses
window-targeted PostMessage to simulate keyboard navigation.

CRITICAL: All keyboard events are sent via PostMessage to the Edge window
handle — they NEVER leak to Chrome, chat windows, or any other app.

This is the ONLY way to get Edge Browsing Streak working because:
1. Edge telemetry requires MS Account signed in at browser level
2. --remote-debugging-port disables telemetry even with correct profile
3. Playwright/Selenium/CDP all trigger automation detection
"""

import asyncio
import ctypes
import ctypes.wintypes
import os
import random
import subprocess
import time
from pathlib import Path

from src.utils import logger, get_edge_executable_path

# Win32 constants
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
VK_RETURN = 0x0D
VK_DOWN = 0x28
VK_UP = 0x26
VK_SPACE = 0x20
VK_NEXT = 0x22   # Page Down
VK_CONTROL = 0x11
VK_BACK = 0x08   # Backspace

# Bing pages to browse (must be bing.com for telemetry tracking)
BROWSE_URLS = [
    "https://www.bing.com",
    "https://www.bing.com/news",
    "https://www.bing.com/images/trending",
    "https://www.bing.com/videos",
    "https://www.bing.com/maps",
    "https://www.bing.com/travel",
    "https://www.bing.com/shop",
    "https://www.bing.com/search?q=weather+today",
    "https://www.bing.com/search?q=latest+news",
    "https://www.bing.com/search?q=best+recipes",
    "https://www.bing.com/search?q=sports+scores+today",
    "https://www.bing.com/search?q=technology+news",
    "https://www.bing.com/search?q=how+to+cook+pasta",
    "https://www.bing.com/search?q=fitness+tips",
    "https://www.bing.com/search?q=travel+destinations",
    "https://www.bing.com/search?q=book+recommendations",
    "https://www.bing.com/search?q=movie+reviews+2026",
    "https://www.bing.com/search?q=home+improvement+ideas",
    "https://www.bing.com/search?q=healthy+eating+tips",
    "https://www.bing.com/search?q=financial+planning+advice",
]


user32 = ctypes.windll.user32


def _find_edge_window():
    """Find Edge's main window handle."""
    result = []
    
    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd, lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if "Edge" in title or "Microsoft\u200b Edge" in title:
                if user32.IsWindowVisible(hwnd):
                    result.append(hwnd)
        return True
    
    user32.EnumWindows(enum_callback, 0)
    return result[0] if result else None


def _post_key(hwnd, vk: int):
    """Send a single key press+release to a SPECIFIC window via PostMessage.
    
    Unlike SendInput, this targets the window by handle —
    keystrokes NEVER leak to other applications.
    """
    lparam_down = 0x00000001
    lparam_up = 0xC0000001
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, lparam_down)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, lparam_up)


def _post_char(hwnd, char: str):
    """Send a single character to a SPECIFIC window via WM_CHAR.
    
    WM_CHAR is the proper way to type text into a window —
    it handles Unicode and doesn't require virtual key translation.
    """
    user32.PostMessageW(hwnd, WM_CHAR, ord(char), 0)


def _post_ctrl_key(hwnd, vk: int):
    """Send Ctrl+key combination to a SPECIFIC window via PostMessage."""
    # Ctrl down
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_CONTROL, 0x00000001)
    time.sleep(0.03)
    # Key down + up
    _post_key(hwnd, vk)
    time.sleep(0.03)
    # Ctrl up
    user32.PostMessageW(hwnd, WM_KEYUP, VK_CONTROL, 0xC0000001)


def _navigate_same_tab(hwnd, url: str):
    """Navigate Edge's CURRENT TAB to a URL via PostMessage.
    
    Uses Ctrl+L (focus address bar) → Ctrl+A (select all) → type URL → Enter.
    All keystrokes are sent to the Edge window handle only.
    This navigates the SAME tab — critical for Edge telemetry tracking.
    """
    # Ctrl+L to focus address bar
    _post_ctrl_key(hwnd, 0x4C)  # L
    time.sleep(0.3)
    
    # Ctrl+A to select all text in address bar
    _post_ctrl_key(hwnd, 0x41)  # A
    time.sleep(0.1)
    
    # Type the URL character by character via WM_CHAR
    for ch in url:
        _post_char(hwnd, ch)
        time.sleep(0.015 + random.uniform(0, 0.01))
    
    time.sleep(0.2)
    
    # Press Enter to navigate
    _post_key(hwnd, VK_RETURN)


def _scroll_in_window(hwnd):
    """Simulate page scrolling INSIDE a specific Edge window.
    
    Uses PostMessage so scrolling only affects Edge,
    even if user is focused on Chrome or another app.
    """
    for _ in range(random.randint(3, 7)):
        if random.random() < 0.7:
            _post_key(hwnd, VK_DOWN)
        else:
            _post_key(hwnd, VK_NEXT)
        time.sleep(random.uniform(0.5, 1.5))


class NativeEdgeStreak:
    """Complete Edge Browsing Streak using native Edge.
    
    Navigation: Uses PostMessage Ctrl+L → type URL → Enter (same tab, window-targeted).
    Scrolling: Uses PostMessage to Edge window handle (never leaks to other apps).
    
    This is the ONLY approach that works because:
    1. No --remote-debugging-port → telemetry is NOT blocked
    2. No Playwright/CDP → no automation detection
    3. Uses the user's DEFAULT Edge profile → MS Account signed in at browser level
    4. Navigates same tab (not new tabs) → telemetry tracks correctly
    """
    
    def __init__(self):
        self.edge_process = None
        self._edge_hwnd = None
        self._edge_exe = get_edge_executable_path()
    
    async def _kill_all_edge(self):
        """Kill all existing Edge instances."""
        logger.info("Closing all Edge instances for native streak...")
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "msedge.exe"],
                capture_output=True, timeout=10,
            )
            await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"taskkill msedge: {e}")
    
    async def _launch_edge(self, start_url: str = "https://www.bing.com"):
        """Launch Edge as a normal subprocess with NO automation flags."""
        # CRITICAL: NO --remote-debugging-port, NO --user-data-dir
        # Edge runs 100% normally using the default system profile
        args = [
            self._edge_exe,
            "--no-first-run",
            "--start-maximized",
            start_url,
        ]
        
        self.edge_process = subprocess.Popen(args)
        logger.info(f"Launched native Edge (PID: {self.edge_process.pid})")
        
        # Wait for Edge window to appear
        for attempt in range(30):
            await asyncio.sleep(1)
            hwnd = _find_edge_window()
            if hwnd:
                self._edge_hwnd = hwnd
                logger.info(f"Edge window found (hwnd: {hwnd:#x})")
                return True
        
        logger.warning("Edge window not found after 30 seconds")
        return False
    
    async def _browse_page(self, url: str):
        """Navigate to a URL in the SAME TAB and simulate reading."""
        if not self._edge_hwnd:
            return
        
        # Navigate same tab via PostMessage (Ctrl+L → type → Enter)
        _navigate_same_tab(self._edge_hwnd, url)
        
        # Wait for page to load
        await asyncio.sleep(random.uniform(3, 5))
        
        # Scroll inside Edge window via PostMessage
        _scroll_in_window(self._edge_hwnd)
        
        # Simulate reading time (1-3 minutes per page)
        read_time = random.uniform(60, 180)
        logger.debug(f"Reading {url} for {read_time:.0f}s")
        await asyncio.sleep(read_time)
    
    async def _close_edge(self):
        """Close Edge gracefully."""
        if self.edge_process:
            try:
                self.edge_process.terminate()
                self.edge_process.wait(timeout=5)
            except Exception:
                try:
                    subprocess.run(
                        ["taskkill", "/f", "/im", "msedge.exe"],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass
            self.edge_process = None
        self._edge_hwnd = None
    
    async def browse(
        self,
        target_minutes: int = 30,
        on_progress=None,
        start_url: str = "https://www.bing.com",
    ):
        """Browse Bing pages for the specified duration using native Edge.
        
        Args:
            target_minutes: How many minutes to browse (default 30)
            on_progress: Callback(elapsed_min, target_min)
            start_url: Initial URL to open Edge with (e.g. activation URL)
        """
        total_seconds = target_minutes * 60
        # Add 5 min buffer to ensure telemetry is captured
        total_seconds += 5 * 60
        
        await self._kill_all_edge()
        
        if not await self._launch_edge(start_url):
            logger.error("Failed to launch native Edge")
            return
        
        logger.info(
            f"Native Edge Streak started — will browse for {target_minutes + 5} min "
            f"(target {target_minutes} min + 5 min buffer)"
        )
        
        # Wait initial page load before navigating
        await asyncio.sleep(5)
        
        start_time = time.time()
        urls = list(BROWSE_URLS)
        random.shuffle(urls)
        url_index = 0
        
        while True:
            elapsed = time.time() - start_time
            elapsed_min = int(elapsed / 60)
            
            if elapsed >= total_seconds:
                break
            
            # Report progress
            if on_progress:
                on_progress(min(elapsed_min, target_minutes), target_minutes)
            
            # Navigate to next page
            url = urls[url_index % len(urls)]
            url_index += 1
            
            # Re-shuffle when we've been through all URLs
            if url_index >= len(urls):
                random.shuffle(urls)
                url_index = 0
            
            # Re-find Edge window in case it was recreated
            new_hwnd = _find_edge_window()
            if new_hwnd:
                self._edge_hwnd = new_hwnd
            
            logger.info(
                f"[Edge Native Streak] {elapsed_min}/{target_minutes} min — {url}"
            )
            
            await self._browse_page(url)
            
            # Check if Edge is still running
            if self.edge_process and self.edge_process.poll() is not None:
                logger.warning("Edge process terminated unexpectedly, restarting...")
                if not await self._launch_edge(url):
                    logger.error("Failed to restart Edge")
                    break
        
        elapsed_min = int((time.time() - start_time) / 60)
        logger.info(f"Native Edge Streak completed — browsed for {elapsed_min} min")
        
        if on_progress:
            on_progress(target_minutes, target_minutes)
        
        await self._close_edge()
