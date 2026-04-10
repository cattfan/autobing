import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.browser import BrowserManager, _build_mobile_runtime_profile


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_mobile_runtime_profile_extracts_android_model_and_touch_profile(self):
        profile = _build_mobile_runtime_profile(
            "Mozilla/5.0 (Linux; Android 14; SM-A556B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36 EdgA/146.0.3856.109"
        )

        self.assertFalse(profile["is_ios"])
        self.assertEqual(profile["platform_name"], "Android")
        self.assertEqual(profile["navigator_platform"], "Linux armv81")
        self.assertEqual(profile["max_touch_points"], 5)
        self.assertEqual(profile["model"], "SM-A556B")
        self.assertEqual(profile["platform_version"], "14.0.0")
        self.assertEqual(profile["brands"][2]["brand"], "Microsoft Edge")

    def test_mobile_runtime_profile_falls_back_to_default_android_model(self):
        profile = _build_mobile_runtime_profile(
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36 EdgA/146.0.3856.109"
        )

        self.assertEqual(profile["model"], "SM-S928B")

    async def test_disconnect_attached_browser_preserves_external_runtime(self):
        manager = BrowserManager({})
        browser = SimpleNamespace(close=AsyncMock())
        playwright = SimpleNamespace(stop=AsyncMock())
        manager.browser = browser
        manager.playwright = playwright
        manager.contexts = [object()]
        manager._attached_via_cdp = True
        manager._owns_browser_process = False

        await manager.disconnect_attached_browser()

        browser.close.assert_not_awaited()
        playwright.stop.assert_awaited_once()
        self.assertFalse(manager._attached_via_cdp)
        self.assertIsNone(manager.browser)

    async def test_close_does_not_kill_unowned_attached_runtime(self):
        manager = BrowserManager({})
        playwright = SimpleNamespace(stop=AsyncMock())
        manager.playwright = playwright
        manager._attached_via_cdp = True
        manager._owns_browser_process = False

        with patch.object(manager, "_kill_managed_edge") as kill_managed_edge:
            await manager.close()

        kill_managed_edge.assert_not_called()
        playwright.stop.assert_awaited_once()

    async def test_close_kills_owned_attached_runtime(self):
        manager = BrowserManager({})
        playwright = SimpleNamespace(stop=AsyncMock())
        manager.playwright = playwright
        manager._attached_via_cdp = True
        manager._owns_browser_process = True

        with patch.object(manager, "_kill_managed_edge") as kill_managed_edge:
            await manager.close()

        kill_managed_edge.assert_called_once()
        playwright.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
