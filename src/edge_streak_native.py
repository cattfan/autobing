"""
Edge Streak via native Edge plus UI Automation.

Microsoft blocks Edge telemetry when --remote-debugging-port is present.
This module launches Edge as a normal process, finds the real address bar
control via UI Automation, and navigates with real window interaction.
"""

from __future__ import annotations
import asyncio
import ctypes
import ctypes.wintypes
import random
import subprocess
import time
from typing import Any

import win32com.client
from pywinauto import mouse
from pywinauto.application import Application
from pywinauto.keyboard import send_keys

from src.utils import get_edge_executable_path, logger

SW_RESTORE = 9
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


def _get_window_text(hwnd) -> str:
    """Read a top-level window title safely for diagnostics."""
    if not hwnd or not user32.IsWindow(hwnd):
        return ""

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""

    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_window_pid(hwnd) -> int:
    """Return the process id owning a given window handle."""
    if not hwnd or not user32.IsWindow(hwnd):
        return 0

    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _find_edge_window(target_pid: int | None = None):
    """Find Edge's main window handle, optionally bound to a process id."""
    result = []

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd, lparam):
        title = _get_window_text(hwnd)
        if title and ("Edge" in title or "Microsoft Edge" in title):
            if user32.IsWindowVisible(hwnd):
                window_pid = _get_window_pid(hwnd)
                if target_pid is None or window_pid == target_pid:
                    result.append(hwnd)
        return True

    user32.EnumWindows(enum_callback, 0)
    return result[0] if result else None


class NativeEdgeStreak:
    """Complete Edge Browsing Streak using native Edge plus UI Automation."""

    def __init__(self):
        self.edge_process = None
        self._edge_hwnd = None
        self._edge_exe = get_edge_executable_path()
        self._diagnostic_log = None
        self._shell = win32com.client.Dispatch("WScript.Shell")

    def _emit_diagnostic(self, level: str, message: str):
        """Mirror navigation diagnostics into the dashboard log when available."""
        if self._diagnostic_log:
            try:
                self._diagnostic_log(level, message)
                return
            except Exception as e:
                logger.debug(f"Diagnostic callback failed: {e}")

        if level == "warning":
            logger.warning(message)
        elif level == "debug":
            logger.debug(message)
        else:
            logger.info(message)

    def _refresh_edge_window(self):
        """Re-resolve the Edge hwnd against the launched process."""
        target_pid = self.edge_process.pid if self.edge_process else None
        hwnd = _find_edge_window(target_pid)
        if hwnd:
            self._edge_hwnd = hwnd
        return self._edge_hwnd

    def _window_snapshot(self) -> dict:
        """Capture hwnd, pid, title, and foreground status for diagnostics."""
        hwnd = self._edge_hwnd
        fg = user32.GetForegroundWindow()
        return {
            "hwnd": f"{int(hwnd):#x}" if hwnd else "None",
            "pid": _get_window_pid(hwnd),
            "title": _get_window_text(hwnd),
            "foreground": bool(hwnd and fg == hwnd),
        }

    def _activate_edge(self) -> bool:
        """Bring the launched Edge window to the foreground."""
        if not self._refresh_edge_window():
            return False

        user32.ShowWindow(self._edge_hwnd, SW_RESTORE)
        time.sleep(0.1)
        self._shell.AppActivate(self.edge_process.pid)
        time.sleep(0.4)
        return True

    def _uia_window(self):
        """Connect to the running Edge top window via UI Automation."""
        if not self.edge_process:
            return None
        app = Application(backend="uia").connect(process=self.edge_process.pid)
        return app.top_window()

    def _uia_windows(self):
        """Return all UIA windows owned by the launched Edge process."""
        if not self.edge_process:
            return []
        app = Application(backend="uia").connect(process=self.edge_process.pid)
        try:
            return app.windows()
        except Exception:
            top = app.top_window()
            return [top] if top is not None else []

    @staticmethod
    def _address_bar_score(ctrl) -> int:
        """Rank Edit controls to find the real Edge address bar reliably."""
        try:
            rect = ctrl.rectangle()
            name = (ctrl.element_info.name or "").strip()
            text = (ctrl.window_text() or "").strip()
            visible = ctrl.is_visible()
        except Exception:
            return -1

        if not visible or rect.width() <= 0 or rect.height() <= 0:
            return -1

        lowered_name = name.lower()
        lowered_text = text.lower()
        score = 0

        if "address and search bar" in lowered_name:
            score += 1000
        if "search or enter web address" in lowered_name:
            score += 900
        if "address" in lowered_name and "search" in lowered_name:
            score += 800
        if lowered_text.startswith("http://") or lowered_text.startswith("https://"):
            score += 700
        if "bing.com" in lowered_text:
            score += 500
        if 20 <= rect.top <= 110:
            score += 250
        if rect.width() >= 250:
            score += min(rect.width(), 1200)
        if rect.height() >= 18:
            score += min(rect.height() * 2, 120)
        return score

    def _describe_edit_candidates(self, windows) -> list[dict[str, Any]]:
        """Return a compact snapshot of visible Edit controls for diagnostics."""
        candidates = []
        for index, window in enumerate(windows):
            try:
                edits = window.descendants(control_type="Edit")
            except Exception:
                continue
            for ctrl in edits:
                try:
                    rect = ctrl.rectangle()
                    candidates.append(
                        {
                            "window": index,
                            "title": window.window_text(),
                            "name": ctrl.element_info.name or "",
                            "text": ctrl.window_text() or "",
                            "rect": [rect.left, rect.top, rect.right, rect.bottom],
                            "score": self._address_bar_score(ctrl),
                        }
                    )
                except Exception:
                    continue
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[:6]

    def _find_address_bar(self, window=None, required: bool = True):
        """Locate the visible Edge address bar Edit control."""
        last_snapshot = []

        for _attempt in range(5):
            windows = [window] if window is not None else self._uia_windows()
            best_ctrl = None
            best_score = -1

            for candidate_window in windows:
                if candidate_window is None:
                    continue
                try:
                    edits = candidate_window.descendants(control_type="Edit")
                except Exception:
                    continue

                for ctrl in edits:
                    score = self._address_bar_score(ctrl)
                    if score > best_score:
                        best_ctrl = ctrl
                        best_score = score

            if best_ctrl is not None and best_score >= 450:
                return best_ctrl

            last_snapshot = self._describe_edit_candidates(windows)
            time.sleep(0.3)
            window = None

        if required:
            raise RuntimeError(f"Edge address bar not found; candidates={last_snapshot}")
        return None

    def _read_address_bar(self, required: bool = False) -> str:
        """Return the current address bar value for precise navigation diagnostics."""
        try:
            address_bar = self._find_address_bar(required=required)
            if address_bar is None:
                return ""
            return address_bar.window_text()
        except Exception:
            if required:
                raise
            return ""

    def _navigate_to_url(self, url: str) -> str:
        """Navigate Edge by editing the real address bar control."""
        self._refresh_edge_window()
        self._activate_edge()
        address_bar = self._find_address_bar(required=True)
        address_bar.click_input()
        time.sleep(0.2)
        address_bar.set_edit_text(url)
        address_bar.type_keys("{ENTER}", pause=0.05)
        time.sleep(0.2)
        return self._read_address_bar()

    def _scroll_page(self):
        """Scroll within the page without clicking links or interactive content."""
        if not self._activate_edge():
            return

        window = self._uia_window()
        if window is None:
            return

        rect = window.rectangle()
        scroll_x = rect.right - min(90, max(40, int(rect.width() * 0.08)))
        scroll_y = rect.top + int(rect.height() * 0.45)

        try:
            mouse.move(coords=(scroll_x, scroll_y))
        except Exception:
            pass

        for _ in range(random.randint(3, 6)):
            mouse.scroll(coords=(scroll_x, scroll_y), wheel_dist=-random.randint(2, 5))
            time.sleep(random.uniform(0.6, 1.4))

        if random.random() < 0.25:
            mouse.scroll(coords=(scroll_x, scroll_y), wheel_dist=random.randint(1, 2))
            time.sleep(random.uniform(0.4, 0.9))

    async def _kill_all_edge(self):
        """Close existing Edge instances, avoiding a crash-restore bubble when possible."""
        logger.info("Closing all Edge instances for native streak...")
        try:
            subprocess.run(
                ["taskkill", "/im", "msedge.exe", "/t"],
                capture_output=True,
                timeout=10,
            )
            await asyncio.sleep(3)
            check = subprocess.run(
                ["tasklist", "/fi", "imagename eq msedge.exe"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if "msedge.exe" in check.stdout.lower():
                logger.info("Some Edge processes remained after graceful close; forcing shutdown...")
                subprocess.run(
                    ["taskkill", "/f", "/im", "msedge.exe", "/t"],
                    capture_output=True,
                    timeout=10,
                )
                await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"taskkill msedge: {e}")

    async def _launch_edge(self, start_url: str = "https://www.bing.com"):
        """Launch Edge as a normal subprocess with no automation flags."""
        args = [
            self._edge_exe,
            "--no-first-run",
            "--start-maximized",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--disable-features=msEdgeSessionRestore",
            start_url,
        ]

        self.edge_process = subprocess.Popen(args)
        logger.info(f"Launched native Edge (PID: {self.edge_process.pid})")

        for _attempt in range(30):
            await asyncio.sleep(1)
            hwnd = _find_edge_window(self.edge_process.pid)
            if hwnd:
                self._edge_hwnd = hwnd
                logger.info(
                    f"Edge window found (hwnd: {hwnd:#x}, pid={_get_window_pid(hwnd)})"
                )
                return True

        logger.warning("Edge window not found after 30 seconds")
        return False

    async def _browse_page(self, url: str):
        """Navigate to a URL in the same tab and simulate reading."""
        if not self._refresh_edge_window():
            return

        before_snapshot = self._window_snapshot()
        before_url = self._read_address_bar()
        self._emit_diagnostic(
            "info",
            f"[EdgeNav] Before navigate: target={url}, current_url='{before_url}', "
            f"snapshot={before_snapshot}",
        )

        typed_value = self._navigate_to_url(url)
        self._emit_diagnostic(
            "debug",
            f"[EdgeNav] Typed into address bar: target={url}, typed_value='{typed_value}'",
        )

        await asyncio.sleep(2)
        self._refresh_edge_window()
        after_short = self._window_snapshot()
        short_url = self._read_address_bar()
        self._emit_diagnostic(
            "info",
            f"[EdgeNav] +2s after navigate: target={url}, current_url='{short_url}', "
            f"snapshot={after_short}",
        )

        await asyncio.sleep(random.uniform(1, 3))
        self._refresh_edge_window()
        after_load = self._window_snapshot()
        load_url = self._read_address_bar()
        url_changed = load_url != before_url
        self._emit_diagnostic(
            "info" if url_changed else "warning",
            f"[EdgeNav] Post-load: target={url}, current_url='{load_url}', "
            f"url_changed={url_changed}, before_url='{before_url}', "
            f"before_title='{before_snapshot['title']}', after_title='{after_load['title']}'",
        )

        self._scroll_page()

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
                        ["taskkill", "/f", "/im", "msedge.exe", "/t"],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
            self.edge_process = None
        self._edge_hwnd = None
        self._diagnostic_log = None

    async def browse(
        self,
        target_minutes: int = 30,
        on_progress=None,
        start_url: str = "https://www.bing.com",
        diagnostic_log=None,
    ):
        """Browse Bing pages for the specified duration using native Edge."""
        self._diagnostic_log = diagnostic_log
        total_seconds = target_minutes * 60
        total_seconds += 5 * 60

        await self._kill_all_edge()

        if not await self._launch_edge(start_url):
            logger.error("Failed to launch native Edge")
            return

        logger.info(
            f"Native Edge Streak started - will browse for {target_minutes + 5} min "
            f"(target {target_minutes} min + 5 min buffer)"
        )

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

            self._refresh_edge_window()
            logger.info(f"[Edge Native Streak] {elapsed_min}/{target_minutes} min - {url}")

            await self._browse_page(url)

            if self.edge_process and self.edge_process.poll() is not None:
                logger.warning("Edge process terminated unexpectedly, restarting...")
                if not await self._launch_edge(url):
                    logger.error("Failed to restart Edge")
                    break

        elapsed_min = int((time.time() - start_time) / 60)
        logger.info(f"Native Edge Streak completed - browsed for {elapsed_min} min")

        if on_progress:
            on_progress(target_minutes, target_minutes)

        await self._close_edge()
