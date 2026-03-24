"""
Edge Streak via NATIVE subprocess — NO CDP, NO Playwright, NO Selenium.

Microsoft blocks Edge telemetry when --remote-debugging-port is present.
This module launches Edge as a completely normal process and uses Win32 API
(SendInput, FindWindow) to simulate keyboard navigation for browsing.

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
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_F5 = 0x74
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_DOWN = 0x28
VK_UP = 0x26
VK_END = 0x47
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

INPUT_KEYBOARD = 1

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

# ═══ Win32 Structures ═══

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("_input",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("_input", INPUT_UNION)]


user32 = ctypes.windll.user32


def _send_key(vk: int, extended: bool = False):
    """Send a single key press and release via Win32 SendInput."""
    flags = 0
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    
    # Key down
    inp_down = INPUT()
    inp_down.type = INPUT_KEYBOARD
    inp_down.ki.wVk = vk
    inp_down.ki.dwFlags = flags
    
    # Key up
    inp_up = INPUT()
    inp_up.type = INPUT_KEYBOARD
    inp_up.ki.wVk = vk
    inp_up.ki.dwFlags = flags | KEYEVENTF_KEYUP
    
    user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))


def _send_char(char: str):
    """Send a single character via Win32 SendInput (Unicode)."""
    inp_down = INPUT()
    inp_down.type = INPUT_KEYBOARD
    inp_down.ki.wScan = ord(char)
    inp_down.ki.dwFlags = 0x0004  # KEYEVENTF_UNICODE
    
    inp_up = INPUT()
    inp_up.type = INPUT_KEYBOARD
    inp_up.ki.wScan = ord(char)
    inp_up.ki.dwFlags = 0x0004 | KEYEVENTF_KEYUP
    
    user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
    time.sleep(0.02)
    user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))


def _type_text(text: str, delay: float = 0.03):
    """Type text character by character using SendInput."""
    for ch in text:
        _send_char(ch)
        time.sleep(delay + random.uniform(0, 0.02))


def _ctrl_key(vk: int):
    """Send Ctrl+key combination."""
    VK_CONTROL = 0x11
    # Ctrl down
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = VK_CONTROL
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    # Key press
    _send_key(vk)
    time.sleep(0.05)
    # Ctrl up
    inp2 = INPUT()
    inp2.type = INPUT_KEYBOARD
    inp2.ki.wVk = VK_CONTROL
    inp2.ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(INPUT))


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
            # Edge window titles end with "- Microsoft Edge" or similar
            if "Edge" in title or "Microsoft\u200b Edge" in title:
                if user32.IsWindowVisible(hwnd):
                    result.append(hwnd)
        return True
    
    user32.EnumWindows(enum_callback, 0)
    return result[0] if result else None


def _focus_edge(hwnd):
    """Bring Edge window to foreground."""
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def _navigate_to_url(url: str):
    """Navigate Edge to a URL using Ctrl+L → type URL → Enter."""
    # Ctrl+L to focus address bar
    _ctrl_key(0x4C)  # L
    time.sleep(0.3)
    
    # Select all existing text
    _ctrl_key(0x41)  # A
    time.sleep(0.1)
    
    # Type the URL
    _type_text(url, delay=0.02)
    time.sleep(0.2)
    
    # Press Enter
    _send_key(VK_RETURN)


def _scroll_page():
    """Simulate page scrolling with random amounts."""
    for _ in range(random.randint(3, 7)):
        if random.random() < 0.7:
            _send_key(VK_DOWN, extended=True)
        else:
            _send_key(VK_SPACE)
        time.sleep(random.uniform(0.5, 1.5))


class NativeEdgeStreak:
    """Complete Edge Browsing Streak using native Edge + Win32 keyboard simulation.
    
    This is the ONLY approach that works because:
    1. No --remote-debugging-port → telemetry is NOT blocked
    2. No Playwright/CDP → no automation detection
    3. Uses the user's DEFAULT Edge profile → MS Account signed in at browser level
    """
    
    def __init__(self):
        self.edge_process = None
        self._edge_hwnd = None
    
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
        edge_exe = get_edge_executable_path()
        
        # CRITICAL: NO --remote-debugging-port, NO --user-data-dir
        # Edge runs 100% normally using the default system profile
        args = [
            edge_exe,
            "--no-first-run",
            "--start-maximized",
            "--disable-features=msEdgeSidebarV2",
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
                _focus_edge(hwnd)
                logger.info(f"Edge window found (hwnd: {hwnd:#x})")
                return True
        
        logger.warning("Edge window not found after 30 seconds")
        return False
    
    async def _browse_page(self, url: str):
        """Navigate to a URL and simulate reading behavior."""
        if self._edge_hwnd:
            _focus_edge(self._edge_hwnd)
        
        _navigate_to_url(url)
        
        # Wait for page to load
        await asyncio.sleep(random.uniform(3, 5))
        
        # Simulate reading behavior — scroll through page
        _scroll_page()
        
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
