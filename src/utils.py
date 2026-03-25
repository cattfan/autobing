"""
Utility functions and retry logic for the Rewards Search Automator.
"""

from __future__ import annotations
import asyncio
import json
import os
import time
import random
import logging
import functools
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional

# ─── Paths ────────────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
DASHBOARD_DIR = ROOT_DIR / "dashboard"

# Ensure directories exist
for d in [CONFIG_DIR, DATA_DIR, PROFILES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Setup and return the root logger with console + file output."""
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )
    bot_logger = logging.getLogger("RewardsBot")
    bot_logger.setLevel(level)

    # Add file handler → data/logs/bot_YYYYMMDD.log
    try:
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        bot_logger.addHandler(fh)
        # Also add to root so all loggers write to file
        logging.getLogger().addHandler(fh)
    except Exception:
        pass  # File logging is best-effort

    return bot_logger


logger = setup_logging()

ENV_SETTING_OVERRIDES = {
    "ai_api_key": "REWARDS_BOT_AI_API_KEY",
    "captcha_api_key": "REWARDS_BOT_CAPTCHA_API_KEY",
    "discord_webhook": "REWARDS_BOT_DISCORD_WEBHOOK",
    "telegram_bot_token": "REWARDS_BOT_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "REWARDS_BOT_TELEGRAM_CHAT_ID",
}

# ─── Settings ─────────────────────────────────────────────────────────────────


def load_settings() -> dict:
    """Load settings from config/settings.json."""
    settings_path = CONFIG_DIR / "settings.json"
    settings = get_default_settings()
    if not settings_path.exists():
        logger.warning("settings.json not found, using defaults")
        return _apply_env_overrides(settings)
    with open(settings_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    settings.update(loaded)
    return _apply_env_overrides(settings)


def save_settings(settings: dict) -> None:
    """Save settings to config/settings.json."""
    settings_path = CONFIG_DIR / "settings.json"
    settings_to_save = get_default_settings()
    settings_to_save.update(settings)

    for key, env_name in ENV_SETTING_OVERRIDES.items():
        if os.getenv(env_name):
            settings_to_save[key] = ""

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings_to_save, f, indent=4, ensure_ascii=False)
    logger.info("Settings saved")


def get_default_settings() -> dict:
    """Return default settings."""
    return {
        "headless": True,
        "browser_type": "chromium",
        "desktop_searches": 34,
        "mobile_searches": 24,
        "edge_searches": 20,
        "delay_min": 3,
        "delay_max": 8,
        "search_delay_min": 1.2,
        "search_delay_max": 4.0,
        "search_break_min": 8,
        "search_break_max": 20,
        "search_break_every_min": 7,
        "search_break_every_max": 11,
        "typing_delay_min": 50,
        "typing_delay_max": 150,
        "block_images": True,
        "auto_close_browser": True,
        "use_stealth": True,
        "safe_mode": True,
        "session_task_retry_limit": 0,
        "use_google_trends": True,
        "discord_webhook": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "schedule_enabled": False,
        "schedule_time": "08:00",
        "auto_redeem": False,
        "auto_redeem_goal": 5000,
        "streak_protection": True,
        "retry_max": 3,
        "retry_delay": 5,
        "dashboard_enabled": False,
        "dashboard_host": "127.0.0.1",
        "dashboard_port": 8080,
        "bootstrap_attach_existing_edge": True,
        "edge_cdp_url": "http://127.0.0.1:9222",
        "native_edge_runtime_enabled": True,
        "native_edge_runtime_port_base": 9322,
        "manual_captcha_handoff": True,
        "manual_captcha_timeout": 900,
        "manual_captcha_poll_interval": 5,
        "manual_captcha_screenshot": True,
        "captcha_provider": "2captcha",
        "captcha_api_key": "",
        "ai_enabled": False,
        "ai_api_key": "",
        "ai_model": "meta-llama/llama-3.3-70b-instruct:free",
        "master_password_hash": "",
    }


def is_sensitive_setting(name: str) -> bool:
    """Return True when a settings key should be masked in UI/API responses."""
    normalized = name.lower()
    return any(
        marker in normalized
        for marker in ("password", "token", "secret", "api_key")
    )


def _apply_env_overrides(settings: dict) -> dict:
    """Overlay secret-like settings from environment variables when provided."""
    resolved = dict(settings)
    for key, env_name in ENV_SETTING_OVERRIDES.items():
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            resolved[key] = env_value
    return resolved


def select_active_daily_set_items(daily_sets: dict) -> list[dict]:
    """Pick the Rewards daily-set bucket that corresponds to the current day."""
    if not daily_sets:
        return []

    parsed_sets = []
    for raw_key, items in daily_sets.items():
        try:
            parsed_date = datetime.strptime(raw_key, "%m/%d/%Y").date()
        except ValueError:
            continue
        parsed_sets.append((parsed_date, raw_key, items))

    if parsed_sets:
        today = datetime.now().date()
        eligible = [entry for entry in parsed_sets if entry[0] <= today]
        chosen = max(eligible or parsed_sets, key=lambda entry: entry[0])
        return chosen[2]

    first_key = next(iter(daily_sets))
    return daily_sets.get(first_key, [])


# ─── Browser Tab Cleanup ─────────────────────────────────────────────────────


async def close_other_tabs(page) -> int:
    """
    Close all browser tabs except the given page's tab.

    Returns number of tabs closed.
    """
    try:
        context = page.context
        pages = context.pages
        closed = 0
        preserve_external_tabs = bool(
            getattr(context, "_codex_preserve_external_tabs", False)
        )
        for p in pages:
            if p != page:
                if preserve_external_tabs and not getattr(p, "_codex_owned", False):
                    try:
                        opener = await p.opener()
                    except Exception:
                        opener = None
                    if opener is None or not getattr(opener, "_codex_owned", False):
                        continue
                try:
                    await p.close()
                    closed += 1
                except Exception:
                    pass
        if closed > 0:
            logger.info(f"🧹 Closed {closed} leftover tab(s)")
        return closed
    except Exception as e:
        logger.debug(f"Tab cleanup error: {e}")
        return 0


# ─── Proxy Rotation ──────────────────────────────────────────────────────────


def load_proxy_pool() -> list[str]:
    """Load proxy pool from config/proxies.txt (one per line).
    
    Format: protocol://user:pass@host:port or host:port
    """
    proxy_file = CONFIG_DIR / "proxies.txt"
    if not proxy_file.exists():
        return []
    with open(proxy_file, "r", encoding="utf-8") as f:
        proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if proxies:
        logger.info(f"Loaded {len(proxies)} proxies from pool")
    return proxies


_proxy_pool: list[str] = []


def get_proxy_for_session(account: dict) -> Optional[dict]:
    """Get a proxy for this session using rotation.
    
    Priority:
    1. Account-specific proxy (if set)
    2. Random proxy from global pool (config/proxies.txt)
    3. None (direct connection)
    """
    global _proxy_pool

    # Account has its own proxy
    if account.get("proxy"):
        proxy_val = account["proxy"]
        # If account has multiple proxies (comma-separated), pick random
        if "," in proxy_val:
            proxy_val = random.choice(proxy_val.split(",")).strip()
        return {"server": proxy_val}

    # Global proxy pool
    if not _proxy_pool:
        _proxy_pool = load_proxy_pool()

    if _proxy_pool:
        proxy = random.choice(_proxy_pool)
        return {"server": proxy}

    return None


# ─── Search Topics ────────────────────────────────────────────────────────────


def load_search_topics() -> list[str]:
    """Load search topics from config/search_topics.txt."""
    topics_path = CONFIG_DIR / "search_topics.txt"
    if not topics_path.exists():
        logger.warning("search_topics.txt not found")
        return ["news today", "weather forecast", "best movies 2026"]
    with open(topics_path, "r", encoding="utf-8") as f:
        topics = [line.strip() for line in f if line.strip()]
    logger.info(f"Loaded {len(topics)} search topics")
    return topics


# ─── Retry Decorator ─────────────────────────────────────────────────────────


def retry(
    max_retries: int = 3,
    delay: float = 5.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
):
    """
    Retry decorator with exponential backoff.

    Args:
        max_retries: Maximum number of retries
        delay: Initial delay between retries in seconds
        backoff: Multiplier for delay after each retry
        exceptions: Tuple of exception types to catch
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        jitter = random.uniform(0, current_delay * 0.3)
                        wait_time = current_delay + jitter
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for "
                            f"{func.__name__}: {e}. Retrying in {wait_time:.1f}s..."
                        )
                        await asyncio.sleep(wait_time)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"All {max_retries + 1} attempts failed for "
                            f"{func.__name__}: {e}"
                        )
            raise last_exception

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        jitter = random.uniform(0, current_delay * 0.3)
                        wait_time = current_delay + jitter
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for "
                            f"{func.__name__}: {e}. Retrying in {wait_time:.1f}s..."
                        )
                        time.sleep(wait_time)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"All {max_retries + 1} attempts failed for "
                            f"{func.__name__}: {e}"
                        )
            raise last_exception

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# ─── Real User Agent Strings (auto-detected from system) ─────────────────────


def _detect_edge_version() -> str:
    """Read actual Edge version from Windows registry."""
    import subprocess
    try:
        result = subprocess.run(
            ["reg", "query",
             r"HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}",
             "/v", "pv"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "pv" in line and "REG_SZ" in line:
                return line.split("REG_SZ")[-1].strip()
    except Exception:
        pass
    # Fallback: try HKLM without WOW6432Node
    try:
        result = subprocess.run(
            ["reg", "query",
             r"HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}",
             "/v", "pv"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "pv" in line and "REG_SZ" in line:
                return line.split("REG_SZ")[-1].strip()
    except Exception:
        pass
    return "131.0.2903.86"  # safe fallback


def get_edge_executable_path() -> str:
    """Return the installed Edge executable path when available."""
    candidates = [
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for candidate in candidates:
        if candidate and str(candidate) and candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Microsoft Edge executable not found")


# Auto-detect once at import time
_EDGE_VERSION = _detect_edge_version()
_CHROME_MAJOR = _EDGE_VERSION.split(".")[0]  # Edge and Chrome share major version
logger.info(f"Detected Edge version: {_EDGE_VERSION} (Chrome {_CHROME_MAJOR})")


def _build_desktop_ua() -> list[str]:
    """Build desktop UAs matching the real installed Edge."""
    v = _EDGE_VERSION
    cm = _CHROME_MAJOR
    return [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Safari/537.36 Edg/{v}",
    ]


def _build_mobile_ua() -> list[str]:
    """Build mobile UAs with real device models + matching Edge version."""
    v = _EDGE_VERSION
    cm = _CHROME_MAJOR
    # Real device models with real Edge Android/iOS version
    return [
        # Samsung Galaxy S24 Ultra (Android 14)
        f"Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Mobile Safari/537.36 EdgA/{v}",
        # Samsung Galaxy S23 (Android 14)
        f"Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Mobile Safari/537.36 EdgA/{v}",
        # Google Pixel 8 Pro (Android 14)
        f"Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Mobile Safari/537.36 EdgA/{v}",
        # Google Pixel 7 (Android 14)
        f"Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Mobile Safari/537.36 EdgA/{v}",
        # iPhone 15 Pro Max (iOS 17.6.1)
        f"Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 EdgiOS/{v} Mobile/15E148 Safari/604.1",
        # iPhone 16 (iOS 18.1)
        f"Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 EdgiOS/{v} Mobile/15E148 Safari/604.1",
        # Samsung Galaxy A55 (Android 14)
        f"Mozilla/5.0 (Linux; Android 14; SM-A556B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Mobile Safari/537.36 EdgA/{v}",
        # Xiaomi 14 (Android 14)
        f"Mozilla/5.0 (Linux; Android 14; 2311DRK48C) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Mobile Safari/537.36 EdgA/{v}",
    ]


def _build_edge_ua() -> list[str]:
    """Build Edge-specific UAs (for Edge bonus points)."""
    v = _EDGE_VERSION
    cm = _CHROME_MAJOR
    return [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cm}.0.0.0 Safari/537.36 Edg/{v}",
    ]


DESKTOP_USER_AGENTS = _build_desktop_ua()
MOBILE_USER_AGENTS = _build_mobile_ua()
EDGE_USER_AGENTS = _build_edge_ua()

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
    {"width": 2560, "height": 1440},
]

MOBILE_VIEWPORTS = [
    {"width": 412, "height": 915},   # Galaxy S24 Ultra
    {"width": 393, "height": 852},   # Pixel 8 Pro
    {"width": 360, "height": 800},   # Galaxy A55
    {"width": 430, "height": 932},   # iPhone 15 Pro Max
    {"width": 402, "height": 874},   # iPhone 16
    {"width": 384, "height": 854},   # Xiaomi 14
]


def get_random_user_agent(mode: str = "desktop") -> str:
    """Get a random user agent string for the given mode."""
    if mode == "mobile":
        return random.choice(MOBILE_USER_AGENTS)
    elif mode == "edge":
        return random.choice(EDGE_USER_AGENTS)
    return random.choice(DESKTOP_USER_AGENTS)


def get_random_viewport(mode: str = "desktop") -> dict:
    """Get a random viewport size."""
    if mode == "mobile":
        return random.choice(MOBILE_VIEWPORTS)
    return random.choice(VIEWPORTS)


# ─── Timestamps ───────────────────────────────────────────────────────────────


def now_str() -> str:
    """Return current timestamp as string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    """Return today's date as string."""
    return datetime.now().strftime("%Y-%m-%d")


# ─── Bing URLs ────────────────────────────────────────────────────────────────

BING_SEARCH_URL = "https://www.bing.com/search"
BING_HOME_URL = "https://www.bing.com"
REWARDS_URL = "https://rewards.bing.com"
REWARDS_DASHBOARD_URL = "https://rewards.bing.com/pointsbreakdown"
LOGIN_URL = "https://login.live.com"
