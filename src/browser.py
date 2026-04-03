"""
Playwright browser management with advanced stealth and fingerprint spoofing.
- CDP-level detection bypass (webdriver, runtime)
- Canvas/WebGL/AudioContext fingerprint noise
- Realistic viewport, timezone, locale randomization
- playwright-stealth integration
"""

from __future__ import annotations
import os
import random
import json
import socket
import subprocess
import time
from typing import Optional
from pathlib import Path
from hashlib import md5
from urllib.parse import urlparse

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from src.utils import (
    DATA_DIR,
    logger,
    PROFILES_DIR,
    get_edge_executable_path,
    get_random_user_agent,
    get_random_viewport,
)

# Realistic fingerprint pools
LOCALES = ["en-US", "en-GB", "en-CA", "en-AU"]
TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Phoenix", "America/Detroit",
]
WEBGL_RENDERERS = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]
HARDWARE_CONCURRENCY = [4, 8, 12, 16]
DEVICE_MEMORY = [4, 8, 16]
SCREEN_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768), (1536, 864), (1440, 900),
]


class BrowserManager:
    """Manages Playwright browser instances with advanced stealth."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.contexts: list[BrowserContext] = []
        self._attached_via_cdp = False
        self._preserve_browser_defaults = False
        self._managed_edge_process: Optional[subprocess.Popen] = None
        self._managed_page_ids: set[int] = set()
        self._native_runtime_cdp_url: str = ""
        # Generate consistent fingerprint per session
        self._fp = self._gen_fingerprint()

    def _gen_fingerprint(self, seed: str = "") -> dict:
        """Generate a consistent fingerprint. If seed is provided (email),
        the fingerprint is deterministic per account — different accounts
        get different but stable fingerprints.
        
        Fingerprints are PERSISTED to disk so the same account always
        gets the same fingerprint across sessions."""
        if seed:
            # Try to load persisted fingerprint
            fp_file = PROFILES_DIR / f"{seed.replace('@','_at_').replace('.','_')}_fp.json"
            if fp_file.exists():
                try:
                    return json.loads(fp_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            rng = random.Random(seed)
        else:
            rng = random.Random()

        webgl = rng.choice(WEBGL_RENDERERS)
        screen = rng.choice(SCREEN_RESOLUTIONS)
        fp = {
            "locale": rng.choice(LOCALES),
            "timezone": rng.choice(TIMEZONES),
            "hw_concurrency": rng.choice(HARDWARE_CONCURRENCY),
            "device_memory": rng.choice(DEVICE_MEMORY),
            "webgl_vendor": webgl[0],
            "webgl_renderer": webgl[1],
            "screen_w": screen[0],
            "screen_h": screen[1],
            "color_depth": rng.choice([24, 32]),
            "max_touch": 0,
            "platform": "Win32",
            "oscpu": "Windows NT 10.0; Win64; x64",
        }

        # Persist fingerprint for future sessions
        if seed:
            try:
                PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                fp_file.write_text(json.dumps(fp, indent=2), encoding="utf-8")
            except Exception:
                pass

        return fp

    def set_account(self, email: str) -> None:
        """Set fingerprint for a specific account (call before create_context)."""
        self._fp = self._gen_fingerprint(seed=email)
        logger.debug(f"Fingerprint set for {email[:5]}***: tz={self._fp['timezone']}, gpu={self._fp['webgl_renderer'][:30]}")

    def _native_edge_profile_dir(self, account_email: str) -> Path:
        safe_email = (account_email or "default").replace("@", "_at_").replace(".", "_")
        return DATA_DIR / "edge_runtime" / safe_email

    def _native_edge_cdp_url(self, account_email: str) -> str:
        configured = str(self.settings.get("edge_cdp_url", "http://127.0.0.1:9222")).strip()
        parsed = urlparse(configured)
        host = parsed.hostname or "127.0.0.1"
        base_port = int(self.settings.get("native_edge_runtime_port_base", parsed.port or 9322))
        if account_email:
            offset = int(md5(account_email.encode("utf-8")).hexdigest()[:4], 16) % 40
        else:
            offset = 0
        port = base_port + offset
        return f"http://{host}:{port}"

    def _cdp_port(self, cdp_url: str) -> int:
        parsed = urlparse(cdp_url)
        return parsed.port or 9222

    def _is_cdp_port_open(self, cdp_url: str) -> bool:
        parsed = urlparse(cdp_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((host, port)) == 0

    async def start_clean_edge(self) -> None:
        """Start Edge with MINIMAL flags — allows telemetry for Edge Browsing Streak.
        
        Microsoft tracks Edge usage via background telemetry. Normal launch args
        block this (--disable-background-networking, --disable-sync, etc).
        This method launches Edge cleanly so browsing time is reported.
        """
        self.playwright = await async_playwright().start()

        # Minimal flags — only anti-detection, NO telemetry blocking
        clean_args = [

            "--disable-infobars",
            "--no-first-run",
            "--start-maximized",
            "--disable-automation",
        ]

        try:
            self.browser = await self.playwright.chromium.launch(
                headless=self.settings.get("headless", True),
                args=clean_args,
                ignore_default_args=["--enable-automation", "--no-sandbox"],
            )
            self._attached_via_cdp = False
            self._preserve_browser_defaults = False
            logger.info("Browser started (Clean Edge for Streak, telemetry enabled)")
        except Exception as e:
            logger.warning(f"Clean Edge failed ({e}), using standard launch")
            await self.start()  # fallback to normal

    async def start_clean_edge_persistent(
        self,
        account_email: str,
        storage_state: str | dict | None = None,
    ) -> tuple["BrowserContext", "Page"]:
        """Start a PERSISTENT Edge context for Edge Browsing Streak.

        Unlike start_clean_edge(), this preserves Edge telemetry, cookies,
        browsing history, and session data across runs — critical for
        Microsoft to track Edge usage time correctly.

        Returns (context, page) directly since persistent context IS browser+context.
        """
        self.playwright = await async_playwright().start()

        safe_email = account_email.replace("@", "_at_").replace(".", "_")
        profile_dir = str(PROFILES_DIR / f"{safe_email}_edge_streak")
        Path(profile_dir).mkdir(parents=True, exist_ok=True)

        clean_args = [

            "--disable-infobars",
            "--no-first-run",
            "--start-maximized",
            "--disable-automation",
        ]

        ua = get_random_user_agent("edge")
        viewport = get_random_viewport("desktop")

        context_options = {
            "user_agent": ua,
            "locale": self._fp["locale"],
            "timezone_id": self._fp["timezone"],
            "color_scheme": "dark",
            "ignore_https_errors": True,
        }

        if not self.settings.get("headless", True):
            context_options["no_viewport"] = True
        else:
            context_options["viewport"] = viewport

        # NOTE: launch_persistent_context does NOT accept storage_state.
        # The persistent profile manages its own cookies/state via the profile dir.
        # We import cookies separately after context creation if needed.

        try:
            # Close existing browser if any
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass
                self.browser = None

            context = await self.playwright.chromium.launch_persistent_context(
                profile_dir,
                headless=self.settings.get("headless", True),
                args=clean_args,
                ignore_default_args=["--enable-automation", "--no-sandbox"],
                **context_options,
            )

            self._attached_via_cdp = False
            self._preserve_browser_defaults = False

            # Import cookies from storage_state into the persistent profile
            # (only needed on first run; subsequent runs have cookies in profile)
            if storage_state:
                try:
                    state_data = storage_state
                    if isinstance(storage_state, str):
                        with open(storage_state, "r", encoding="utf-8") as f:
                            state_data = json.load(f)
                    cookies = state_data.get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)
                        logger.debug(f"Imported {len(cookies)} cookies into Edge persistent profile")
                except Exception as e:
                    logger.debug(f"Could not import cookies: {e}")

            # The persistent context is also the browser
            self.browser = context.browser
            if context not in self.contexts:
                self.contexts.append(context)

            # Get or create the first page
            page = context.pages[0] if context.pages else await context.new_page()
            self._managed_page_ids.add(id(page))

            logger.info(
                f"Browser started (Persistent Clean Edge, profile={safe_email}_edge_streak)"
            )
            return context, page

        except Exception as e:
            logger.warning(f"Persistent Edge failed ({e}), falling back to clean Edge")
            await self.start_clean_edge()
            ctx = await self.create_context(
                mode="edge",
                account_email=account_email,
                storage_state=storage_state,
                use_persistent_profile=False,
            )
            pg = await self.new_page(ctx)
            return ctx, pg

    async def start_connected_edge(self, cdp_url: str) -> None:
        """Attach to an existing Edge instance exposed via CDP."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
        self._attached_via_cdp = True
        self._preserve_browser_defaults = True
        self._native_runtime_cdp_url = cdp_url
        logger.info(f"Attached to existing Edge via CDP ({cdp_url})")

    async def start_native_edge_runtime(self, account_email: str) -> str:
        """Launch or attach to a dedicated Edge bot profile exposed via CDP."""
        cdp_url = self._native_edge_cdp_url(account_email)

        if self._is_cdp_port_open(cdp_url):
            await self.start_connected_edge(cdp_url)
            return cdp_url

        edge_exe = get_edge_executable_path()
        profile_dir = self._native_edge_profile_dir(account_email)
        profile_dir.mkdir(parents=True, exist_ok=True)
        port = self._cdp_port(cdp_url)

        args = [
            edge_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--start-maximized",
            "about:blank",
        ]
        self._managed_edge_process = subprocess.Popen(args)

        last_error = None
        for _ in range(30):
            time.sleep(0.5)
            try:
                await self.start_connected_edge(cdp_url)
                logger.info(
                    f"Dedicated Edge runtime ready for {account_email[:5]}*** "
                    f"(profile={profile_dir.name}, port={port})"
                )
                return cdp_url
            except Exception as e:
                last_error = e
                if self.playwright:
                    try:
                        await self.playwright.stop()
                    except Exception:
                        pass
                    self.playwright = None
        raise RuntimeError(
            f"Could not start dedicated Edge runtime on {cdp_url}: {last_error}"
        )

    async def start_native_edge_default_profile(self) -> str:
        """Launch Edge with the DEFAULT system profile for Edge Streak telemetry.

        Unlike start_native_edge_runtime(), this does NOT pass --user-data-dir,
        so Edge uses the user's default profile where their MS Account is already
        signed in at the browser level. This is critical for Edge Browsing Streak
        because Microsoft tracks browsing time via Edge's internal telemetry,
        which requires the MS Account to be signed in at the browser (not just
        on rewards.bing.com).

        IMPORTANT: Edge on Windows is single-instance. We must kill ALL existing
        Edge processes before launching with --remote-debugging-port, otherwise
        the flag is ignored and we can't connect via CDP.
        """
        streak_port = 9399
        cdp_url = f"http://127.0.0.1:{streak_port}"

        # If port is already open, just attach
        if self._is_cdp_port_open(cdp_url):
            await self.start_connected_edge(cdp_url)
            return cdp_url

        # Kill ALL existing Edge processes (Windows single-instance requirement)
        # Without this, --remote-debugging-port is ignored because Edge opens
        # a tab in the existing instance instead of starting a new one.
        logger.info("Closing all Edge instances for default profile streak...")
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "msedge.exe"],
                capture_output=True, timeout=10,
            )
            time.sleep(2)  # Wait for processes to fully terminate
        except Exception as e:
            logger.debug(f"taskkill msedge: {e}")

        edge_exe = get_edge_executable_path()

        # Launch Edge WITHOUT --user-data-dir → uses default system profile
        args = [
            edge_exe,
            f"--remote-debugging-port={streak_port}",
            "--no-first-run",
            "--start-maximized",
            "--restore-last-session",
            "https://rewards.bing.com",
        ]
        self._managed_edge_process = subprocess.Popen(args)

        last_error = None
        for _ in range(30):
            time.sleep(0.5)
            try:
                await self.start_connected_edge(cdp_url)
                logger.info(
                    f"Edge Streak runtime ready (DEFAULT profile, port={streak_port})"
                )
                return cdp_url
            except Exception as e:
                last_error = e
                if self.playwright:
                    try:
                        await self.playwright.stop()
                    except Exception:
                        pass
                    self.playwright = None
        raise RuntimeError(
            f"Could not start Edge with default profile on {cdp_url}: {last_error}"
        )

    async def start(self) -> None:
        """Start Playwright and launch browser."""
        self.playwright = await async_playwright().start()

        browser_type = self.settings.get("browser_type", "chromium")
        launcher = getattr(self.playwright, browser_type)

        launch_args = [
            # Core anti-detection (minimal footprint)
            "--disable-infobars",
            "--no-first-run",
            "--start-maximized",
        ]

        # Use REAL Microsoft Edge (installed on system) instead of Chromium
        # This is the #1 anti-detection measure — Edge has genuine fingerprints
        # and the user's saved credentials (no passkey/2FA prompts)
        #
        # IMPORTANT: On Windows, if Edge processes are already running,
        # Playwright cannot launch a new controlled instance — the OS merges
        # it with the existing process tree and it dies immediately.
        # We must kill stale Edge processes first.
        if not self.settings.get("headless", True):
            try:
                import subprocess
                # Only kill if not attached via CDP (preserve user's Edge if we're connecting to it)
                result = subprocess.run(
                    ["taskkill", "/F", "/IM", "msedge.exe"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    logger.info("Killed stale Edge processes before launch")
                    import asyncio
                    await asyncio.sleep(2)  # Give OS time to release locks
            except Exception:
                pass  # No Edge processes running, or taskkill not available

        try:
            self.browser = await self.playwright.chromium.launch(
                channel="msedge",
                headless=self.settings.get("headless", True),
                args=launch_args,
                ignore_default_args=["--enable-automation", "--no-sandbox"],
            )
            self._attached_via_cdp = False
            self._preserve_browser_defaults = False
            logger.info(
                f"Browser started (Real Edge, "
                f"headless={self.settings.get('headless', True)})"
            )
        except Exception as e:
            logger.warning(f"Edge not available ({e}), falling back to Chromium")
            self.browser = await self.playwright.chromium.launch(
                headless=self.settings.get("headless", True),
                args=launch_args,
                ignore_default_args=["--enable-automation", "--no-sandbox"],
            )
            self._attached_via_cdp = False
            self._preserve_browser_defaults = False
            logger.info(
                f"Browser started (Chromium fallback, "
                f"headless={self.settings.get('headless', True)})"
            )

    async def create_context(
        self,
        mode: str = "desktop",
        account_email: str = "",
        proxy: Optional[dict] = None,
        user_agent: Optional[str] = None,
        storage_state: Optional[str | dict] = None,
        use_persistent_profile: bool = True,
    ) -> BrowserContext:
        """Create a browser context with stealth + fingerprinting."""
        if not self.browser:
            raise RuntimeError("Browser not started. Call start() first.")

        if self._attached_via_cdp:
            if not self.browser.contexts:
                raise RuntimeError(
                    "Attached Edge debug session has no browser context available"
                )
            context = self.browser.contexts[0]
            context._codex_mode = mode
            context._codex_user_agent = user_agent or ""
            context._codex_preserve_external_tabs = True
            if context not in self.contexts:
                self.contexts.append(context)
            logger.info("Context attached from existing Edge debug session")
            return context

        ua = user_agent or get_random_user_agent(mode)
        viewport = get_random_viewport(mode)

        # Profile dir per account
        profile_dir = None
        if account_email:
            safe_email = account_email.replace("@", "_at_").replace(".", "_")
            profile_dir = str(PROFILES_DIR / f"{safe_email}_{mode}")

        context_options = {
            "user_agent": ua,
            "viewport": viewport,
            "locale": self._fp["locale"],
            "timezone_id": self._fp["timezone"],
            "color_scheme": "dark",
            "ignore_https_errors": True,
            "screen": {"width": self._fp["screen_w"], "height": self._fp["screen_h"]},
            "has_touch": mode == "mobile",
            "is_mobile": mode == "mobile",
            "device_scale_factor": 2 if mode == "mobile" else 1,
        }

        # When not headless, use no_viewport so --start-maximized works
        if not self.settings.get("headless", True) and mode != "mobile":
            context_options["no_viewport"] = True
            del context_options["viewport"]
            del context_options["screen"]
            del context_options["device_scale_factor"]

        if proxy:
            context_options["proxy"] = proxy

        if storage_state:
            context_options["storage_state"] = storage_state

        # Use persistent context if we have a profile dir
        if profile_dir and use_persistent_profile:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            if self.browser:
                await self.browser.close()

            launch_args = [

                "--disable-infobars",
    
                "--disable-automation",
                "--start-maximized",
            ]

            # launch_persistent_context does NOT accept storage_state.
            # Extract it, launch, then import cookies separately.
            saved_storage_state = context_options.pop("storage_state", None)

            # Use real Edge for persistent context too
            try:
                context = await self.playwright.chromium.launch_persistent_context(
                    profile_dir,
                    channel="msedge",
                    headless=self.settings.get("headless", True),
                    args=launch_args,
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    **context_options,
                )
            except Exception:
                context = await self.playwright.chromium.launch_persistent_context(
                    profile_dir,
                    headless=self.settings.get("headless", True),
                    args=launch_args,
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    **context_options,
                )

            # Import cookies from storage_state into the persistent profile
            if saved_storage_state:
                try:
                    state_data = saved_storage_state
                    if isinstance(saved_storage_state, str):
                        with open(saved_storage_state, "r", encoding="utf-8") as f:
                            state_data = json.load(f)
                    cookies = state_data.get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)
                        logger.info(f"Imported {len(cookies)} cookies into persistent profile ({mode})")
                except Exception as ex:
                    logger.debug(f"Could not import cookies: {ex}")
        else:
            context = await self.browser.new_context(**context_options)

        context._codex_mode = mode
        context._codex_user_agent = ua
        context._codex_preserve_external_tabs = False
        context._codex_viewport = viewport

        # Apply stealth layers
        if self.settings.get("use_stealth", True):
            await self._apply_stealth(context)
            # Also try playwright-stealth library
            await self._apply_stealth_lib(context)

        if mode == "mobile":
            await self._apply_mobile_overrides(context)

        # Block images/fonts
        if self.settings.get("block_images", False):
            await self._block_images(context)

        self.contexts.append(context)
        logger.info(f"Context created (mode={mode}, tz={self._fp['timezone']}, locale={self._fp['locale']})")
        return context

    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Apply comprehensive stealth JavaScript to prevent detection."""
        fp = dict(self._fp)
        mode = getattr(context, "_codex_mode", "desktop")
        user_agent = getattr(context, "_codex_user_agent", "") or ""
        viewport = None
        try:
            viewport = context.viewport_size
        except Exception:
            viewport = None

        if mode == "mobile":
            ua_lower = user_agent.lower()
            fp["max_touch"] = max(5, int(fp.get("max_touch", 0) or 0))
            if viewport:
                fp["screen_w"] = viewport.get("width", fp["screen_w"])
                fp["screen_h"] = viewport.get("height", fp["screen_h"])
            if "iphone" in ua_lower or "ios" in ua_lower:
                fp["platform"] = "iPhone"
                fp["oscpu"] = "iPhone; CPU iPhone OS 17_6_1 like Mac OS X"
            else:
                fp["platform"] = "Linux armv8l"
                fp["oscpu"] = "Linux armv8l"

        stealth_js = """
        () => {
            // ═══ 1. Navigator Overrides ═══
            const nav = navigator;

            // Remove webdriver flag (most important)
            Object.defineProperty(nav, 'webdriver', { get: () => undefined });
            delete nav.__proto__.webdriver;

            // Languages
            Object.defineProperty(nav, 'languages', { get: () => ['%s', 'en'] });
            Object.defineProperty(nav, 'language', { get: () => '%s' });

            // Hardware
            Object.defineProperty(nav, 'hardwareConcurrency', { get: () => %d });
            Object.defineProperty(nav, 'deviceMemory', { get: () => %d });
            Object.defineProperty(nav, 'platform', { get: () => '%s' });
            Object.defineProperty(nav, 'maxTouchPoints', { get: () => %d });
            Object.defineProperty(nav, 'oscpu', { get: () => '%s' });

            // Plugins (realistic Chrome plugins)
            Object.defineProperty(nav, 'plugins', {
                get: () => {
                    const arr = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 }
                    ];
                    arr.item = (i) => arr[i];
                    arr.namedItem = (name) => arr.find(p => p.name === name);
                    arr.refresh = () => {};
                    return arr;
                }
            });
            Object.defineProperty(nav, 'mimeTypes', {
                get: () => {
                    const arr = [
                        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: nav.plugins[0] },
                        { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable', enabledPlugin: nav.plugins[2] },
                        { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable', enabledPlugin: nav.plugins[2] }
                    ];
                    arr.item = (i) => arr[i];
                    arr.namedItem = (name) => arr.find(m => m.type === name);
                    return arr;
                }
            });

            // ═══ 2. Chrome Object ═══
            window.chrome = {
                app: { isInstalled: false, InstallState: { INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
                runtime: { OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }, OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }, PlatformArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }, PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }, PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' }, RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }, connect: function() {}, sendMessage: function() {} },
                loadTimes: function() { return { commitLoadTime: Date.now() / 1000, connectionInfo: 'http/1.1', finishDocumentLoadTime: Date.now() / 1000 + 0.1, finishLoadTime: Date.now() / 1000 + 0.2, firstPaintAfterLoadTime: 0, firstPaintTime: Date.now() / 1000 + 0.05, navigationType: 'Other', npnNegotiatedProtocol: 'unknown', requestTime: Date.now() / 1000 - 0.5, startLoadTime: Date.now() / 1000 - 0.3, wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: false, wasNpnNegotiated: false }; },
                csi: function() { return { onloadT: Date.now(), pageT: Date.now() / 1000, startE: Date.now(), tran: 15 }; }
            };

            // ═══ 3. Permissions API ═══
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (params) => {
                if (params.name === 'notifications') return Promise.resolve({ state: Notification.permission });
                if (params.name === 'midi' || params.name === 'camera' || params.name === 'microphone' || params.name === 'speakers') return Promise.resolve({ state: 'denied' });
                return origQuery.call(window.navigator.permissions, params);
            };

            // ═══ 4. WebGL Fingerprint ═══
            const getParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {
                if (p === 37445) return '%s';   // VENDOR
                if (p === 37446) return '%s';   // RENDERER
                return getParam.call(this, p);
            };
            // WebGL2
            if (typeof WebGL2RenderingContext !== 'undefined') {
                const getParam2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(p) {
                    if (p === 37445) return '%s';
                    if (p === 37446) return '%s';
                    return getParam2.call(this, p);
                };
            }

            // ═══ 5. Canvas Fingerprint Noise ═══
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx && this.width > 16 && this.height > 16) {
                    const shift = { r: Math.floor(Math.random() * 5) - 2, g: Math.floor(Math.random() * 5) - 2, b: Math.floor(Math.random() * 5) - 2 };
                    try {
                        const img = ctx.getImageData(0, 0, Math.min(this.width, 64), Math.min(this.height, 64));
                        for (let i = 0; i < img.data.length; i += 4) {
                            img.data[i] = Math.max(0, Math.min(255, img.data[i] + shift.r));
                            img.data[i+1] = Math.max(0, Math.min(255, img.data[i+1] + shift.g));
                            img.data[i+2] = Math.max(0, Math.min(255, img.data[i+2] + shift.b));
                        }
                        ctx.putImageData(img, 0, 0);
                    } catch(e) {}
                }
                return origToDataURL.apply(this, arguments);
            };

            // ═══ 6. AudioContext Fingerprint ═══
            if (typeof AudioContext !== 'undefined' || typeof webkitAudioContext !== 'undefined') {
                const AC = typeof AudioContext !== 'undefined' ? AudioContext : webkitAudioContext;
                const origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
                AnalyserNode.prototype.getFloatFrequencyData = function(arr) {
                    origGetFloatFreq.call(this, arr);
                    for (let i = 0; i < arr.length; i++) {
                        arr[i] += (Math.random() * 0.1) - 0.05;
                    }
                };
            }

            // ═══ 7. Screen & Window ═══
            Object.defineProperty(screen, 'width', { get: () => %d });
            Object.defineProperty(screen, 'height', { get: () => %d });
            Object.defineProperty(screen, 'availWidth', { get: () => %d });
            Object.defineProperty(screen, 'availHeight', { get: () => %d - 40 });
            Object.defineProperty(screen, 'colorDepth', { get: () => %d });
            Object.defineProperty(screen, 'pixelDepth', { get: () => %d });

            // ═══ 8. Battery API (if available) ═══
            if (navigator.getBattery) {
                navigator.getBattery = () => Promise.resolve({
                    charging: true, chargingTime: 0, dischargingTime: Infinity,
                    level: 1.0, addEventListener: () => {}
                });
            }

            // ═══ 9. Connection API ═══
            if (navigator.connection) {
                Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 + Math.floor(Math.random() * 100) });
                Object.defineProperty(navigator.connection, 'downlink', { get: () => 5 + Math.random() * 15 });
                Object.defineProperty(navigator.connection, 'effectiveType', { get: () => '4g' });
            }

            // ═══ 10. Prevent iframe detection ═══
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                get: function() { return window; }
            });

            // ═══ 11. HeadlessChrome detection bypass ═══
            // Block known headless detection vectors
            Object.defineProperty(window, 'chrome', {
                get: () => window.chrome || true,
                configurable: true,
            });

            // Fix outerWidth/outerHeight (headless browsers set these to 0)
            if (window.outerWidth === 0) {
                Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
            }
            if (window.outerHeight === 0) {
                Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
            }

            // ═══ 12. CDP leak prevention ═══
            // Hide Runtime.enable traces that reveal automation
            const origError = Error;
            const origStack = Object.getOwnPropertyDescriptor(origError.prototype, 'stack');
            if (origStack && origStack.get) {
                Object.defineProperty(origError.prototype, 'stack', {
                    get: function() {
                        const stack = origStack.get.call(this);
                        if (typeof stack === 'string') {
                            // Remove CDP/DevTools protocol traces
                            return stack.split('\n')
                                .filter(line => !line.includes('pptr:') && !line.includes('__puppeteer') && !line.includes('Runtime.evaluate'))
                                .join('\n');
                        }
                        return stack;
                    }
                });
            }

            // ═══ 13. Notification API (deny by default like most users) ═══
            if (typeof Notification !== 'undefined') {
                Object.defineProperty(Notification, 'permission', { get: () => 'default' });
            }

            // ═══ 14. WebRTC Leak Protection ═══
            // Prevent real IP leak through RTCPeerConnection (critical when using proxy)
            if (window.RTCPeerConnection) {
                const origRTC = window.RTCPeerConnection;
                window.RTCPeerConnection = function(config, constraints) {
                    // Strip STUN/TURN servers to prevent IP leak
                    if (config && config.iceServers) {
                        config.iceServers = [];
                    }
                    return new origRTC(config, constraints);
                };
                window.RTCPeerConnection.prototype = origRTC.prototype;
            }
            // Also cover webkit prefix
            if (window.webkitRTCPeerConnection) {
                window.webkitRTCPeerConnection = window.RTCPeerConnection;
            }
        }
        """ % (
            fp["locale"], fp["locale"],
            fp["hw_concurrency"], fp["device_memory"],
            fp["platform"], fp["max_touch"], fp["oscpu"],
            fp["webgl_vendor"], fp["webgl_renderer"],
            fp["webgl_vendor"], fp["webgl_renderer"],
            fp["screen_w"], fp["screen_h"],
            fp["screen_w"], fp["screen_h"],
            fp["color_depth"], fp["color_depth"],
        )

        await context.add_init_script(stealth_js)
        logger.debug("Advanced stealth scripts applied (10 layers)")

    async def _apply_stealth_lib(self, context: BrowserContext) -> None:
        """Try to apply playwright-stealth library (extra layer)."""
        try:
            from playwright_stealth import stealth_async
            for page in context.pages:
                await stealth_async(page)
            # Hook for future pages
            context.on("page", lambda p: p.once("domcontentloaded", lambda: None))
            logger.debug("playwright-stealth library applied")
        except ImportError:
            logger.debug("playwright-stealth not installed, skipping library layer")
        except Exception as e:
            logger.debug(f"playwright-stealth error: {e}")

    async def _block_images(self, context: BrowserContext) -> None:
        """Block heavy resources for speed."""
        await context.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,webp,bmp,avif}",
            lambda route: route.abort(),
        )
        await context.route(
            "**/*.{woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )
        logger.debug("Image/font blocking enabled")

    async def _apply_mobile_overrides(self, context: BrowserContext) -> None:
        """Force mobile-facing navigator and screen values for emulated mobile contexts."""
        user_agent = getattr(context, "_codex_user_agent", "") or ""
        viewport = getattr(context, "_codex_viewport", None) or {}
        width = int(viewport.get("width", 390))
        height = int(viewport.get("height", 844))
        ua_lower = user_agent.lower()
        is_ios = "iphone" in ua_lower or "ios" in ua_lower
        platform = "iPhone" if is_ios else "Linux armv8l"
        oscpu = "iPhone; CPU iPhone OS 17_6_1 like Mac OS X" if is_ios else "Linux armv8l"
        ua_platform = "iOS" if is_ios else "Android"

        script = f"""
        (() => {{
            const overrideGetter = (obj, prop, value) => {{
                try {{
                    Object.defineProperty(obj, prop, {{
                        get: () => value,
                        configurable: true,
                    }});
                }} catch (e) {{}}
            }};

            overrideGetter(navigator, 'platform', {platform!r});
            overrideGetter(navigator, 'oscpu', {oscpu!r});
            overrideGetter(navigator, 'maxTouchPoints', 5);
            overrideGetter(screen, 'width', {width});
            overrideGetter(screen, 'height', {height});
            overrideGetter(screen, 'availWidth', {width});
            overrideGetter(screen, 'availHeight', {max(height - 40, 0)});

            try {{
                Object.defineProperty(window, 'orientation', {{
                    get: () => 0,
                    configurable: true,
                }});
            }} catch (e) {{}}

            try {{
                window.ontouchstart = null;
            }} catch (e) {{}}

            if (navigator.userAgentData) {{
                const original = navigator.userAgentData;
                const mobileData = {{
                    brands: original.brands || [],
                    mobile: true,
                    platform: {ua_platform!r},
                    getHighEntropyValues: async (hints) => {{
                        const values = await original.getHighEntropyValues(hints);
                        return Object.assign({{}}, values, {{
                            mobile: true,
                            platform: {ua_platform!r},
                        }});
                    }},
                    toJSON: () => ({{
                        brands: original.brands || [],
                        mobile: true,
                        platform: {ua_platform!r},
                    }}),
                }};
                overrideGetter(navigator, 'userAgentData', mobileData);
            }}
        }})();
        """

        await context.add_init_script(script)
        logger.debug("Mobile emulation overrides applied")

    def _client_hints_headers(self, context: BrowserContext) -> dict[str, str]:
        """Build mode-aware client hints so mobile contexts do not leak desktop signals."""
        from src.utils import _EDGE_VERSION

        edge_major = _EDGE_VERSION.split(".")[0]
        mode = getattr(context, "_codex_mode", "desktop")
        user_agent = (getattr(context, "_codex_user_agent", "") or "").lower()

        platform = "Windows"
        mobile_flag = "?0"
        if mode == "mobile":
            mobile_flag = "?1"
            if "iphone" in user_agent or "ios" in user_agent:
                platform = "iOS"
            else:
                platform = "Android"

        return {
            "sec-ch-ua": f'"Microsoft Edge";v="{edge_major}", "Chromium";v="{edge_major}", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": mobile_flag,
            "sec-ch-ua-platform": f'"{platform}"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "accept-language": f"{self._fp['locale']},en;q=0.9",
        }

    async def toggle_mobile_emulation(self, page, enable: bool = True) -> None:
        """Toggle mobile device emulation on an EXISTING page via CDP.

        Replicates the Rewards Search Automator extension's simulate()/detach()
        approach which is proven to earn mobile credits.

        Args:
            page: The existing Playwright page to apply emulation to.
            enable: True to activate mobile emulation, False to clear it.
        """
        from src.utils import get_random_user_agent, get_random_viewport, _EDGE_VERSION

        context = page.context

        try:
            client = await context.new_cdp_session(page)

            if enable:
                mobile_ua = get_random_user_agent("mobile")
                mobile_vp = get_random_viewport("mobile")
                edge_major = _EDGE_VERSION.split(".")[0]

                ua_lower = mobile_ua.lower()
                is_ios = "iphone" in ua_lower or "ios" in ua_lower

                # ── Step 1: Clear existing device metrics first (extension does this) ──
                try:
                    await client.send("Emulation.clearDeviceMetricsOverride", {})
                except Exception:
                    pass

                # ── Step 2: Set device metrics with fitWindow (extension pattern) ──
                await client.send("Emulation.setDeviceMetricsOverride", {
                    "mobile": True,
                    "fitWindow": True,
                    "width": mobile_vp["width"],
                    "height": mobile_vp["height"],
                    "deviceScaleFactor": 3,  # Extension uses 3, not 2
                })

                # ── Step 3: Set UA override WITH userAgentMetadata (Client Hints) ──
                # This is the KEY — Bing checks Sec-CH-UA-* headers
                ua_override = {
                    "userAgent": mobile_ua,
                }

                # Only set userAgentMetadata for Android (not iOS Safari)
                if not is_ios:
                    # Extract Chrome/Edge version from UA
                    import re
                    chrome_match = re.search(r'Chrome/(\d+)', mobile_ua)
                    edge_match = re.search(r'Edg[A]?/(\d+)', mobile_ua)
                    version = edge_match.group(1) if edge_match else (
                        chrome_match.group(1) if chrome_match else edge_major
                    )

                    ua_override["userAgentMetadata"] = {
                        "brands": [
                            {"brand": "Not_A Brand", "version": "8"},
                            {"brand": "Chromium", "version": version},
                            {"brand": "Microsoft Edge", "version": version},
                        ],
                        "fullVersion": f"{version}.0.0.0",
                        "platform": "Android",
                        "platformVersion": "15.0.0",
                        "architecture": "arm64",
                        "model": "SM-S928B",
                        "mobile": True,
                    }

                await client.send("Network.setUserAgentOverride", ua_override)

                # ── Step 4: Bypass service workers (extension does this) ──
                await client.send("Network.setBypassServiceWorker", {
                    "bypass": True,
                })

                # ── Step 5: Enable touch emulation with maxTouchPoints=10 ──
                max_touch = 5 if is_ios else 10
                await client.send("Emulation.setTouchEmulationEnabled", {
                    "enabled": True,
                    "maxTouchPoints": max_touch,
                    "configuration": "mobile",
                })

                # ── Step 6: Emit touch events for mouse (extension does this) ──
                await client.send("Emulation.setEmitTouchEventsForMouse", {
                    "enabled": True,
                    "configuration": "mobile",
                })

                # ── Step 7: Inject anti-fingerprint script via CDP ──
                # CRITICAL: Playwright's add_init_script does NOT fire on CDP-navigated pages!
                # Inject the extension's content.js anti-fingerprint directly via CDP.
                anti_fingerprint_js = """(function() {
                    // 1. Mask navigator.webdriver (critical bot detection vector)
                    try {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined, configurable: true
                        });
                    } catch(e) {}

                    // 2. Spoof navigator properties
                    try {
                        Object.defineProperty(navigator, 'plugins', {
                            get: () => ({length: 5}), configurable: true
                        });
                        Object.defineProperty(navigator, 'languages', {
                            get: () => ['en-US', 'en'], configurable: true
                        });
                        Object.defineProperty(navigator, 'deviceMemory', {
                            get: () => 8, configurable: true
                        });
                        Object.defineProperty(navigator, 'hardwareConcurrency', {
                            get: () => 8, configurable: true
                        });
                    } catch(e) {}

                    // 3. Canvas fingerprint noise
                    try {
                        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
                        HTMLCanvasElement.prototype.toDataURL = function(type) {
                            const ctx = this.getContext('2d');
                            if (ctx) {
                                const imgData = ctx.getImageData(0, 0, this.width, this.height);
                                for (let i = 0; i < 10; i++) {
                                    const idx = Math.floor(Math.random() * imgData.data.length / 4) * 4;
                                    imgData.data[idx] = (imgData.data[idx] + 1) % 256;
                                }
                                ctx.putImageData(imgData, 0, 0);
                            }
                            return origToDataURL.apply(this, arguments);
                        };
                    } catch(e) {}

                    // 4. WebGL fingerprint
                    try {
                        const getParamProto = WebGLRenderingContext.prototype.getParameter;
                        WebGLRenderingContext.prototype.getParameter = function(p) {
                            if (p === 37445) return 'Google Inc. (Intel)';
                            if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
                            return getParamProto.apply(this, arguments);
                        };
                        if (typeof WebGL2RenderingContext !== 'undefined') {
                            const getParam2Proto = WebGL2RenderingContext.prototype.getParameter;
                            WebGL2RenderingContext.prototype.getParameter = function(p) {
                                if (p === 37445) return 'Google Inc. (Intel)';
                                if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
                                return getParam2Proto.apply(this, arguments);
                            };
                        }
                    } catch(e) {}
                })()"""

                await client.send("Page.addScriptToEvaluateOnNewDocument", {
                    "source": anti_fingerprint_js,
                })
                logger.info("📱 Anti-fingerprint script injected via CDP")

                context._codex_mode = "mobile"
                context._codex_user_agent = mobile_ua
                logger.info(f"📱 Mobile emulation ON (UA={mobile_ua[:60]}...)")

            else:
                # ── Clear all emulation — restore desktop mode ──
                # Match extension's detach() reset commands
                reset_commands = [
                    ("Emulation.clearDeviceMetricsOverride", {}),
                    ("Network.setUserAgentOverride", {"userAgent": ""}),
                    ("Network.setBypassServiceWorker", {"bypass": False}),
                    ("Emulation.setTouchEmulationEnabled", {"enabled": False}),
                    ("Emulation.setEmitTouchEventsForMouse", {"enabled": False}),
                ]
                for cmd, params in reset_commands:
                    try:
                        await client.send(cmd, params)
                    except Exception:
                        continue

                # Restore desktop headers
                await page.set_extra_http_headers(self._client_hints_headers(context))

                context._codex_mode = "desktop"
                logger.info("🖥️ Mobile emulation OFF (restored desktop)")

            await client.detach()

        except Exception as e:
            logger.warning(f"toggle_mobile_emulation failed: {e}")

    async def create_mobile_patchright(self, cookies: list = None):
        """Create a separate patchright Chromium browser for mobile searches.

        patchright is a patched Playwright fork that removes automation
        detection flags at compile-time (navigator.webdriver, --enable-automation,
        CDP leaks). This approach is used by TheNetsky/Microsoft-Rewards-Script
        and other successful MS Rewards bots.

        Returns:
            tuple: (pw_instance, browser, context, page)
        """
        from patchright.async_api import async_playwright as patchright_async

        from src.utils import get_random_user_agent, get_random_viewport, _EDGE_VERSION

        mobile_ua = get_random_user_agent("mobile")
        mobile_vp = get_random_viewport("mobile")
        edge_major = _EDGE_VERSION.split(".")[0]

        ua_lower = mobile_ua.lower()
        is_ios = "iphone" in ua_lower or "ios" in ua_lower
        platform_name = "iOS" if is_ios else "Android"

        logger.info(f"📱 Launching patchright Edge for mobile search...")
        logger.info(f"   UA: {mobile_ua[:60]}...")

        pw = await patchright_async().start()

        # Use system Edge (channel='msedge') instead of standalone Chromium.
        # Bing server-side checks browser identity — only Edge gets mobile credits.
        # patchright still removes automation flags (webdriver, enable-automation).
        browser = await pw.chromium.launch(
            channel="msedge",
            headless=False,
            args=[
                "--no-sandbox",

                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
            ],
        )

        context = await browser.new_context(
            user_agent=mobile_ua,
            viewport={"width": mobile_vp["width"], "height": mobile_vp["height"]},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            locale=self._fp.get("locale", "en-US"),
            timezone_id=self._fp.get("timezone", "America/New_York"),
            extra_http_headers={
                "sec-ch-ua": f'"Microsoft Edge";v="{edge_major}", "Chromium";v="{edge_major}", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": f'"{platform_name}"',
                "sec-ch-ua-platform-version": '"14.0"' if not is_ios else '"17.6.1"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
                "accept-language": f"{self._fp.get('locale', 'en-US')},en;q=0.9",
            },
        )

        # Import cookies from desktop Edge session
        if cookies:
            # Filter to Bing/Microsoft domains and adapt format
            valid_cookies = []
            for c in cookies:
                cookie = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                }
                if c.get("expires"):
                    cookie["expires"] = c["expires"]
                if c.get("httpOnly"):
                    cookie["httpOnly"] = c["httpOnly"]
                if c.get("secure"):
                    cookie["secure"] = c["secure"]
                if c.get("sameSite"):
                    cookie["sameSite"] = c["sameSite"]

                # Only import Bing/Microsoft cookies for login
                domain = cookie["domain"].lower()
                if any(d in domain for d in [
                    "bing.com", "microsoft.com", "live.com",
                    "microsoftonline.com", "login.live.com",
                ]):
                    valid_cookies.append(cookie)

            if valid_cookies:
                await context.add_cookies(valid_cookies)
                logger.info(f"   Imported {len(valid_cookies)} cookies from desktop Edge session")

        # Tag context for mode tracking
        context._codex_mode = "mobile"
        context._codex_user_agent = mobile_ua

        page = await context.new_page()

        # ── Inject comprehensive mobile fingerprint overrides ──
        # Bing server-side checks more than just UA: navigator.platform,
        # maxTouchPoints, screen dimensions, connection type, battery, etc.
        # This must run BEFORE any page loads (addInitScript).
        mobile_platform = "iPhone" if is_ios else "Linux armv81"
        mobile_touch_pts = 1 if is_ios else 5
        mobile_dev_mem = 4
        mobile_hw_conc = 4 if is_ios else 8
        screen_w = mobile_vp["width"]
        screen_h = mobile_vp["height"]

        fingerprint_js = f"""
        (() => {{
            // Override navigator properties
            const platform = '{mobile_platform}';
            const touchPoints = {mobile_touch_pts};
            const ua = navigator.userAgent;  // Keep the context's UA

            Object.defineProperty(navigator, 'platform', {{
                get: () => platform,
                configurable: true,
            }});
            Object.defineProperty(navigator, 'maxTouchPoints', {{
                get: () => touchPoints,
                configurable: true,
            }});
            Object.defineProperty(navigator, 'hardwareConcurrency', {{
                get: () => {mobile_hw_conc},
                configurable: true,
            }});
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: () => {mobile_dev_mem},
                configurable: true,
            }});
            Object.defineProperty(navigator, 'vendor', {{
                get: () => 'Google Inc.',
                configurable: true,
            }});

            // Override screen to match mobile viewport
            const screenW = {screen_w};
            const screenH = {screen_h};
            Object.defineProperty(screen, 'width', {{ get: () => screenW }});
            Object.defineProperty(screen, 'height', {{ get: () => screenH }});
            Object.defineProperty(screen, 'availWidth', {{ get: () => screenW }});
            Object.defineProperty(screen, 'availHeight', {{ get: () => screenH }});
            Object.defineProperty(screen, 'colorDepth', {{ get: () => 24 }});
            Object.defineProperty(screen, 'pixelDepth', {{ get: () => 24 }});

            // Override window.screen.orientation for mobile
            if (screen.orientation) {{
                Object.defineProperty(screen.orientation, 'type', {{
                    get: () => screenH > screenW ? 'portrait-primary' : 'landscape-primary',
                }});
                Object.defineProperty(screen.orientation, 'angle', {{
                    get: () => 0,
                }});
            }}

            // Override connection to appear mobile
            if (navigator.connection) {{
                Object.defineProperty(navigator.connection, 'effectiveType', {{
                    get: () => '4g',
                    configurable: true,
                }});
                Object.defineProperty(navigator.connection, 'type', {{
                    get: () => 'cellular',
                    configurable: true,
                }});
                Object.defineProperty(navigator.connection, 'downlink', {{
                    get: () => 10,
                    configurable: true,
                }});
                Object.defineProperty(navigator.connection, 'rtt', {{
                    get: () => 50,
                    configurable: true,
                }});
                Object.defineProperty(navigator.connection, 'saveData', {{
                    get: () => false,
                    configurable: true,
                }});
            }}

            // Override battery API with mobile-like values
            if (navigator.getBattery) {{
                const batteryMock = {{
                    charging: Math.random() > 0.5,
                    chargingTime: Infinity,
                    dischargingTime: 3600 + Math.floor(Math.random() * 7200),
                    level: 0.3 + Math.random() * 0.6,
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                }};
                navigator.getBattery = () => Promise.resolve(batteryMock);
            }}

            // Ensure window dimensions match mobile
            Object.defineProperty(window, 'innerWidth', {{
                get: () => screenW,
                configurable: true,
            }});
            Object.defineProperty(window, 'innerHeight', {{
                get: () => screenH,
                configurable: true,
            }});
            Object.defineProperty(window, 'outerWidth', {{
                get: () => screenW,
                configurable: true,
            }});
            Object.defineProperty(window, 'outerHeight', {{
                get: () => screenH,
                configurable: true,
            }});

            // Ensure ontouchstart exists (mobile signal)
            if (!('ontouchstart' in window)) {{
                window.ontouchstart = null;
            }}

            // Override permissions to look mobile
            const originalQuery = navigator.permissions?.query;
            if (originalQuery) {{
                navigator.permissions.query = (params) => {{
                    if (params.name === 'notifications') {{
                        return Promise.resolve({{ state: 'prompt', onchange: null }});
                    }}
                    return originalQuery.call(navigator.permissions, params);
                }};
            }}
        }})();
        """

        await page.add_init_script(fingerprint_js)

        logger.info(f"📱 Patchright mobile browser ready (viewport {mobile_vp['width']}x{mobile_vp['height']}, "
                     f"platform={mobile_platform}, touch={mobile_touch_pts})")
        return pw, browser, context, page

    async def new_page(self, context: BrowserContext) -> Page:
        """Create a new page with Edge-like headers. Closes old tabs after."""
        if self._attached_via_cdp:
            page = await context.new_page()
            page._codex_owned = True
            self._managed_page_ids.add(id(page))
            return page

        existing_pages = context.pages

        # For persistent contexts: reuse the existing blank page if available
        if len(existing_pages) == 1 and existing_pages[0].url in ("about:blank", "chrome://newtab/", "edge://newtab/"):
            page = existing_pages[0]
        else:
            # Create new page first, then close old ones
            page = await context.new_page()
            page._codex_owned = True
            self._managed_page_ids.add(id(page))
            for old_page in existing_pages:
                try:
                    await old_page.close()
                except Exception:
                    pass
        if len(existing_pages) == 1 and existing_pages[0].url in ("about:blank", "chrome://newtab/", "edge://newtab/"):
            page._codex_owned = True
            self._managed_page_ids.add(id(page))

        await page.set_extra_http_headers(self._client_hints_headers(context))

        # CDP-level webdriver flag removal
        try:
            client = await context.new_cdp_session(page)
            await client.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })
        except Exception:
            pass

        return page

    async def close_managed_tabs(self, context: BrowserContext, keep: Page | None = None) -> int:
        """Close only tabs that were created by this manager."""
        closed = 0
        for page in list(context.pages):
            if keep is not None and page == keep:
                continue
            if not getattr(page, "_codex_owned", False):
                continue
            try:
                await page.close()
                self._managed_page_ids.discard(id(page))
                closed += 1
            except Exception:
                pass
        return closed

    async def close_context(self, context: BrowserContext) -> None:
        """Close a browser context."""
        if context in self.contexts:
            self.contexts.remove(context)
        if self._attached_via_cdp:
            return
        for page in list(context.pages):
            self._managed_page_ids.discard(id(page))
        await context.close()

    async def close(self) -> None:
        """Close all contexts and browser."""
        if self._attached_via_cdp:
            self.contexts.clear()
            self.browser = None
            self._attached_via_cdp = False
            self._preserve_browser_defaults = False
            self._managed_page_ids.clear()
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            # Kill the native Edge subprocess if we started one
            self._kill_managed_edge()
            logger.info("Detached from existing Edge browser")
            return

        for context in self.contexts[:]:
            try:
                await context.close()
            except Exception:
                pass
        self.contexts.clear()
        self._managed_page_ids.clear()

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

        # Kill the native Edge subprocess if we started one
        self._kill_managed_edge()

        logger.info("Browser closed")

    def _kill_managed_edge(self) -> None:
        """Terminate the native Edge subprocess if it was started by us."""
        proc = getattr(self, "_managed_edge_process", None)
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                logger.info("Terminated native Edge subprocess")
            except Exception:
                pass
            self._managed_edge_process = None
