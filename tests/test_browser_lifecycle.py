import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.browser import (
    BrowserManager,
    _build_mobile_runtime_init_script,
    _build_mobile_runtime_profile,
)


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

    def test_mobile_runtime_init_script_overrides_user_agent_on_current_page(self):
        ua = (
            "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36 EdgA/146.0.3856.109"
        )
        profile = _build_mobile_runtime_profile(ua)

        script = _build_mobile_runtime_init_script(
            profile,
            screen_width=412,
            screen_height=915,
        )

        self.assertIn("define(navigator, 'userAgent'", script)
        self.assertIn("define(navigator, 'appVersion'", script)
        self.assertIn(ua, script)

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

    async def test_toggle_mobile_emulation_reuses_existing_mobile_fingerprint(self):
        manager = BrowserManager({})
        sent = []
        detached = False

        class FakeClient:
            async def send(self, method, params):
                sent.append((method, params))
                return {}

            async def detach(self):
                nonlocal detached
                detached = True
                return None

        context = SimpleNamespace(
            _codex_user_agent="Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36 EdgA/146.0.3856.109",
            _codex_mobile_viewport={"width": 412, "height": 915},
            new_cdp_session=AsyncMock(return_value=FakeClient()),
        )
        page = SimpleNamespace(context=context, evaluate=AsyncMock())

        with patch("src.utils.get_random_mobile_rewards_user_agent", side_effect=AssertionError("should not randomize UA")), \
             patch("src.utils.get_random_mobile_rewards_viewport", side_effect=AssertionError("should not randomize viewport")):
            await manager.toggle_mobile_emulation(page, enable=True)

        ua_override = next(params for method, params in sent if method == "Network.setUserAgentOverride")
        metrics = next(params for method, params in sent if method == "Emulation.setDeviceMetricsOverride")
        self.assertEqual(ua_override["userAgent"], context._codex_user_agent)
        self.assertEqual(metrics["width"], 412)
        self.assertEqual(metrics["height"], 915)
        page.evaluate.assert_awaited()
        self.assertIs(page._codex_mobile_emulation_client.__class__, FakeClient)
        self.assertFalse(detached)

    async def test_toggle_mobile_emulation_disable_detaches_cached_session(self):
        manager = BrowserManager({})
        detached = False

        class FakeClient:
            async def send(self, method, params):
                return {}

            async def detach(self):
                nonlocal detached
                detached = True
                return None

        client = FakeClient()
        context = SimpleNamespace(
            _codex_mode="mobile",
            new_cdp_session=AsyncMock(return_value=client),
        )
        page = SimpleNamespace(context=context, _codex_mobile_emulation_client=client, set_extra_http_headers=AsyncMock())

        await manager.toggle_mobile_emulation(page, enable=False)

        self.assertTrue(detached)
        self.assertFalse(hasattr(page, "_codex_mobile_emulation_client"))
        page.set_extra_http_headers.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
