"""
Edge Streak - Pure Native Background Mode.

Chạy Edge ở góc màn hình, KHÔNG chiếm chuột/bàn phím.
Dùng PostMessageW để gửi PageDown thẳng vào cửa sổ Edge mà
không cần SetForegroundWindow. Telemetry Microsoft vẫn đếm
vì Edge đang chạy với đúng profile đăng nhập và đang browse Bing.
"""

import asyncio
import ctypes
import random
import subprocess
import time
from typing import Callable, Optional

from src.utils import get_edge_executable_path, logger, DATA_DIR

user32 = ctypes.windll.user32

# Win32 constants
HWND_NOTOPMOST = -2
SWP_SHOWWINDOW = 0x0040
SW_SHOWNOACTIVATE = 4   # Show nhưng KHÔNG lấy focus
SW_MAXIMIZE = 3         # Maximize cửa sổ
SW_RESTORE = 9

# PostMessage constants  
WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
VK_NEXT    = 0x22   # Page Down
VK_PRIOR   = 0x21   # Page Up
VK_F5      = 0x74   # Refresh

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
    "https://www.bing.com/search?q=best+movies+2025",
    "https://www.bing.com/search?q=cooking+recipes",
    "https://www.bing.com/search?q=travel+destinations",
    "https://news.microsoft.com",
]


class NativeEdgeStreak:
    def __init__(self, account_email: str = ""):
        self._edge_exe = get_edge_executable_path()
        self.edge_process: Optional[subprocess.Popen] = None
        self._edge_hwnd = None
        self._account_email = account_email
        if account_email:
            safe_email = account_email.replace("@", "_at_").replace(".", "_")
            self._profile_dir = DATA_DIR / "edge_runtime" / safe_email
        else:
            self._profile_dir = None

    def _find_edge_hwnd(self) -> Optional[int]:
        """Tìm HWND của cửa sổ Edge theo PID."""
        if not self.edge_process:
            return None
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
        return hwnds[0] if hwnds else None

    def _position_edge_corner(self):
        """Tìm HWND sau khi Edge mở, lưu lại để PostMessage dùng sau."""
        import time as _time
        # Chờ cửa sổ xuất hiện (--start-maximized tự phóng to)
        for _ in range(16):
            hwnd = self._find_edge_hwnd()
            if hwnd:
                break
            _time.sleep(0.5)
        
        hwnd = self._find_edge_hwnd()
        if not hwnd:
            logger.warning("Không tìm thấy cửa sổ Edge")
            return

        self._edge_hwnd = hwnd
        # Maximize cửa sổ Edge full màn hình
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        logger.info("Edge da mo full man hinh (PostMessage mode)")

    def _post_scroll(self):
        """Gửi PageDown vào HWND của Edge mà KHÔNG cần lấy focus."""
        hwnd = self._edge_hwnd
        if not hwnd:
            hwnd = self._find_edge_hwnd()
            if hwnd:
                self._edge_hwnd = hwnd
        if not hwnd:
            return
        # PostMessageW: gửi message vào queue, không block, không steal focus
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_NEXT, 0)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_NEXT, 0)

    async def _kill_all_edge(self):
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "msedge.exe", "/t"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            await asyncio.sleep(2)
        except Exception:
            pass

    async def _launch_edge(self, start_url: str = "https://www.bing.com") -> bool:
        await self._kill_all_edge()
        args = [
            self._edge_exe,
            "--no-first-run",
            "--start-maximized",          # Mở full màn hình
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--disable-features=msEdgeSessionRestore",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        if self._profile_dir:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            args.append(f"--user-data-dir={self._profile_dir}")
        args.append(start_url)

        self.edge_process = subprocess.Popen(args)
        await asyncio.sleep(5)
        self._position_edge_corner()
        return True

    def _open_url_in_edge(self, url: str):
        """Mở URL mới trong Edge (tạo tab mới). KHÔNG chiếm focus."""
        args = [self._edge_exe]
        if self._profile_dir:
            args.append(f"--user-data-dir={self._profile_dir}")
        # --new-tab không có trên Edge, nhưng chỉ cần pass URL là nó mở tab mới
        args.append(url)
        subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)

    async def browse(
        self,
        target_minutes: int,
        on_progress: Callable[[int, int], None],
        start_url: str = "https://www.bing.com",
        diagnostic_log=None,
    ):
        """
        Chạy Edge Streak 30 phút.
        - Edge chạy ở góc màn hình, không chiếm chuột/bàn phím.
        - PostMessageW gửi PageDown ngầm vào cửa sổ Edge.
        - Mỗi 90-120 giây mở một URL Bing mới.
        """
        if target_minutes <= 0:
            return

        logger.info(
            f"[Edge Streak] Bat dau native browsing: target={target_minutes} min"
        )

        if not await self._launch_edge("https://rewards.bing.com/"):
            logger.error("Không thể khởi động Edge")
            return

        start_time = time.time()
        urls_pool = list(BROWSE_URLS)
        random.shuffle(urls_pool)
        url_idx = 0

        try:
            while True:
                elapsed_sec = time.time() - start_time
                elapsed_min = int(elapsed_sec / 60)

                if elapsed_min >= target_minutes:
                    logger.info(
                        f"[OK] Da du {target_minutes} min - ket thuc Edge Streak"
                    )
                    break

                if on_progress:
                    on_progress(min(elapsed_min, target_minutes), target_minutes)

                # Mở URL mới mỗi 90-120 giây
                next_url = urls_pool[url_idx % len(urls_pool)]
                url_idx += 1
                logger.info(f"[Edge Streak] {elapsed_min}/{target_minutes} min → {next_url}")
                self._open_url_in_edge(next_url)

                # Scroll ngầm mỗi 10-15 giây trong 90-120 giây đọc trang
                page_read_time = random.uniform(90, 120)
                page_elapsed = 0
                while page_elapsed < page_read_time:
                    chunk = random.uniform(8, 15)
                    await asyncio.sleep(chunk)
                    page_elapsed += chunk

                    # PostMessageW - không chiếm focus
                    self._post_scroll()

                    # Kiểm tra dừng
                    if (time.time() - start_time) / 60 >= target_minutes:
                        break

        except asyncio.CancelledError:
            logger.info("Edge Streak bi huy")
        except Exception as e:
            logger.error(f"Loi Edge Streak: {e}")
        finally:
            logger.info("Tat Edge...")
            await self._kill_all_edge()
