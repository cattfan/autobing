"""
Edge Streak via NATIVE subprocess — NO CDP, NO Playwright, NO Selenium.

Microsoft blocks Edge telemetry when --remote-debugging-port is present.
This module launches Edge as a NORMAL process and uses Win32 SendInput
for keyboard simulation.

IMPORTANT: Chromium browsers IGNORE PostMessage keyboard events. Only
SendInput (system-level) actually reaches Chromium's input pipeline.
To avoid interfering with user activity, the script:
  1. Saves the currently focused window
  2. Briefly focuses Edge (~1 sec) for URL entry
  3. Restores focus to the user's previous window
Between navigations (1-3 min reading time), user can freely use any app.
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

# ── Win32 types & constants ──────────────────────────────────────────────

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)

# Input types
INPUT_KEYBOARD = 1

# Key event flags
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001

# Virtual keys
VK_RETURN = 0x0D
VK_DOWN = 0x28
VK_SPACE = 0x20
VK_NEXT = 0x22   # Page Down
VK_CONTROL = 0x11

# ── Win32 structures ──────────────────────────────────────────────────────

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class INPUT(ctypes.Structure):
    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 24)]
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", _INPUT_UNION)]

user32 = ctypes.windll.user32

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


# ── Low-level SendInput helpers ──────────────────────────────────────────

def _send_key(vk: int, extended: bool = False):
    """Send a single key press+release via SendInput (system-level)."""
    flags_down = KEYEVENTF_EXTENDEDKEY if extended else 0
    flags_up = flags_down | KEYEVENTF_KEYUP

    inputs = (INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wVk = vk
    inputs[0].union.ki.dwFlags = flags_down
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wVk = vk
    inputs[1].union.ki.dwFlags = flags_up

    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    time.sleep(0.05)


def _send_char(char: str):
    """Send a single unicode character via SendInput."""
    code = ord(char)
    inputs = (INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wScan = code
    inputs[0].union.ki.dwFlags = KEYEVENTF_UNICODE
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wScan = code
    inputs[1].union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP

    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))


def _ctrl_key(vk: int):
    """Send Ctrl+key combo via SendInput."""
    inputs = (INPUT * 4)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wVk = VK_CONTROL
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wVk = vk
    inputs[2].type = INPUT_KEYBOARD
    inputs[2].union.ki.wVk = vk
    inputs[2].union.ki.dwFlags = KEYEVENTF_KEYUP
    inputs[3].type = INPUT_KEYBOARD
    inputs[3].union.ki.wVk = VK_CONTROL
    inputs[3].union.ki.dwFlags = KEYEVENTF_KEYUP

    user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    time.sleep(0.1)


def _type_text(text: str, delay: float = 0.02):
    """Type a string character by character via SendInput."""
    for ch in text:
        _send_char(ch)
        time.sleep(delay + random.uniform(0, 0.01))


# ── Window management ────────────────────────────────────────────────────

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


def _get_foreground_window():
    """Get the currently focused window handle."""
    return user32.GetForegroundWindow()


def _set_foreground_window(hwnd):
    """Bring a window to the foreground."""
    if hwnd:
        user32.SetForegroundWindow(hwnd)


def _navigate_to_url_safe(edge_hwnd, url: str):
    """Navigate Edge to a URL using SendInput, with focus save/restore.

    1. Save the user's currently focused window
    2. Focus Edge briefly (~1 sec)
    3. Ctrl+L → Select All → Type URL → Enter
    4. Restore focus to the user's previous window
    """
    # Save user's current window
    prev_hwnd = _get_foreground_window()

    # Focus Edge
    _set_foreground_window(edge_hwnd)
    time.sleep(0.3)

    # Ctrl+L to focus address bar
    _ctrl_key(0x4C)  # L
    time.sleep(0.2)

    # Ctrl+A to select all existing text
    _ctrl_key(0x41)  # A
    time.sleep(0.1)

    # Type the URL
    _type_text(url, delay=0.015)
    time.sleep(0.15)

    # Press Enter
    _send_key(VK_RETURN)
    time.sleep(0.3)

    # Restore user's previous window focus
    if prev_hwnd and prev_hwnd != edge_hwnd:
        _set_foreground_window(prev_hwnd)


def _scroll_page_safe(edge_hwnd):
    """Scroll the Edge page using SendInput with focus save/restore."""
    prev_hwnd = _get_foreground_window()
    _set_foreground_window(edge_hwnd)
    time.sleep(0.2)

    for _ in range(random.randint(3, 7)):
        if random.random() < 0.7:
            _send_key(VK_DOWN, extended=True)
        else:
            _send_key(VK_SPACE)
        time.sleep(random.uniform(0.5, 1.5))

    # Restore user's window
    if prev_hwnd and prev_hwnd != edge_hwnd:
        _set_foreground_window(prev_hwnd)


# ── Main class ───────────────────────────────────────────────────────────

class NativeEdgeStreak:
    """Complete Edge Browsing Streak using native Edge + SendInput.

    Uses SendInput (ONLY method that works with Chromium) with automatic
    focus save/restore to minimize interference with user activity.

    Between navigations (1-3 min reading time), user can freely use any app.
    Edge focus is only stolen briefly (~1 sec) during URL entry.
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
        args = [
            self._edge_exe,
            "--no-first-run",
            "--start-maximized",
            start_url,
        ]

        self.edge_process = subprocess.Popen(args)
        logger.info(f"Launched native Edge (PID: {self.edge_process.pid})")

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

        # Navigate with focus save/restore (~1 sec focus steal)
        _navigate_to_url_safe(self._edge_hwnd, url)

        # Wait for page to load
        await asyncio.sleep(random.uniform(3, 5))

        # Scroll with focus save/restore (~3 sec focus steal)
        _scroll_page_safe(self._edge_hwnd)

        # Simulate reading time (1-3 min) — user is FREE during this time
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
            start_url: Initial URL to open Edge with
        """
        total_seconds = target_minutes * 60
        total_seconds += 5 * 60  # 5 min buffer

        await self._kill_all_edge()

        if not await self._launch_edge(start_url):
            logger.error("Failed to launch native Edge")
            return

        logger.info(
            f"Native Edge Streak started — will browse for {target_minutes + 5} min "
            f"(target {target_minutes} min + 5 min buffer)"
        )

        # Wait for initial page load
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

            if on_progress:
                on_progress(min(elapsed_min, target_minutes), target_minutes)

            url = urls[url_index % len(urls)]
            url_index += 1

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
