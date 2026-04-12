import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.universal_task import RewardsTask, UniversalTaskScanner


class _FakeLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def is_visible(self, timeout=None):
        return False

    async def click(self, timeout=None):
        return None


class _FakePage:
    def __init__(self):
        self.url = "https://rewards.bing.com/earn"

    def locator(self, _selector):
        return _FakeLocator()

    async def evaluate(self, _script):
        return 0


class UniversalTaskTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_skips_hidden_ai_scans_when_inventory_is_empty(self):
        humanizer = SimpleNamespace(
            simulate_reading=AsyncMock(),
            short_delay=AsyncMock(),
        )
        ai_agent = SimpleNamespace(
            enabled=True,
            complete_quests=AsyncMock(return_value={"success": True}),
        )
        scanner = UniversalTaskScanner(
            humanizer=humanizer,
            settings={},
            ai_agent=ai_agent,
        )
        page = _FakePage()

        scanner._ensure_no_manual_challenge = AsyncMock()
        scanner._fetch_all_tasks = AsyncMock(return_value=[])
        scanner._dom_verify_task_status = AsyncMock(return_value=set())
        scanner._apply_live_category_completion_proofs = AsyncMock(return_value={})
        scanner._scan_explore_on_bing = AsyncMock(return_value=2)

        with patch("src.universal_task._load_state", return_value={}), \
             patch("src.universal_task._save_state"), \
             patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            result = await scanner.scan_and_complete(page, account_email="test@example.com")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["completed"], 0)
        ai_agent.complete_quests.assert_not_awaited()
        scanner._scan_explore_on_bing.assert_not_awaited()

    async def test_verify_task_completion_fails_fast_when_refresh_times_out(self):
        humanizer = SimpleNamespace(simulate_reading=AsyncMock())
        scanner = UniversalTaskScanner(humanizer=humanizer, settings={})
        task = RewardsTask(
            id="offer-1",
            title="Do you know the answer?",
            category="more_promo",
            task_type="quiz",
        )
        page = _FakePage()

        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._dom_check_task_done_across_rewards_pages = AsyncMock(return_value=False)
        async def _timeout(*_args, **_kwargs):
            coro = _args[1]
            coro.close()
            return None

        scanner._run_with_timeout = AsyncMock(side_effect=_timeout)

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            result = await scanner._verify_task_completion(page, task)

        self.assertFalse(result)
        scanner._run_with_timeout.assert_awaited()

    async def test_complete_quiz_returns_true_after_bounded_fallback(self):
        humanizer = SimpleNamespace(simulate_reading=AsyncMock())
        scanner = UniversalTaskScanner(humanizer=humanizer, settings={})
        task = RewardsTask(
            id="offer-2",
            title="Iguana tails",
            category="daily_set",
            task_type="quiz",
        )
        page = _FakePage()

        result = await scanner._complete_quiz(page, task)

        self.assertTrue(result)
        humanizer.simulate_reading.assert_awaited_once()

    async def test_more_promo_verification_accepts_disappearing_offer_after_interaction(self):
        humanizer = SimpleNamespace(simulate_reading=AsyncMock())
        scanner = UniversalTaskScanner(humanizer=humanizer, settings={})
        task = RewardsTask(
            id="offer-3",
            title="New Wallpaper Every Day",
            category="more_promo",
            task_type="unknown",
            destination_url="https://www.bing.com/apps/wallpaper?pc=W317&brand=bing",
            raw_data={"_interaction_fired": True, "_interaction_url": "https://www.bing.com/apps/wallpaper?pc=W317&brand=bing"},
        )
        page = _FakePage()

        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._dom_check_task_done_across_rewards_pages = AsyncMock(return_value=False)
        async def _refresh(*_args, **_kwargs):
            coro = _args[1]
            coro.close()
            return []

        scanner._run_with_timeout = AsyncMock(side_effect=_refresh)

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            result = await scanner._verify_task_completion(page, task)

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
