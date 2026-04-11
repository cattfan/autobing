import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import (
    _describe_deferred_items,
    _describe_remaining_items,
    _edge_streak_attempt_allowed,
    _needs_mobile_credit_recheck,
    _reconcile_verification_with_session_proof,
    _read_search_status_with_mobile_recheck,
    _resolve_mobile_search_requirement,
    _run_mobile_search_pass,
)
from src.daily_set import DailySetCompleter
from src.dashboard_scraper import NON_ACTIONABLE_CARD_TITLES, scan_dashboard_dom
from src.humanizer import Humanizer
from src.searcher import Searcher
from src.streaks import TaskDetector
from src.utils import (
    diagnostic_logging_enabled,
    get_random_mobile_rewards_user_agent,
    summarize_search_status,
)
from src.universal_task import (
    RewardsTask,
    UniversalTaskScanner,
    _build_earn_card_cache_key,
    _requires_strict_completion,
    _should_cache_earn_card_visit,
    _should_cache_task_completion,
    _should_skip_earn_card_via_cache,
    _should_skip_task_via_cache,
    get_deferred_offer_reason,
)


class RewardsTrustTests(unittest.TestCase):
    def test_diagnostic_logging_defaults_to_enabled(self):
        self.assertTrue(diagnostic_logging_enabled({}))
        self.assertTrue(diagnostic_logging_enabled(None))
        self.assertFalse(diagnostic_logging_enabled({"diagnostic_logging": False}))

    def test_search_status_summary_is_compact_and_stable(self):
        self.assertEqual(
            summarize_search_status(
                {
                    "pc_current": 90,
                    "pc_max": 90,
                    "mobile_current": 27,
                    "mobile_max": 60,
                    "edge_current": 0,
                    "edge_max": 0,
                    "total_points": 9036,
                }
            ),
            "pc=90/90, mobile=27/60, edge=0/0, total=9036",
        )

    def test_targeted_categories_require_strict_completion(self):
        self.assertTrue(_requires_strict_completion("daily_set"))
        self.assertTrue(_requires_strict_completion("more_promo"))
        self.assertFalse(_requires_strict_completion("punch_card"))

    def test_mobile_rewards_user_agent_stays_on_android_edge(self):
        ua = get_random_mobile_rewards_user_agent()
        self.assertIn("EdgA/", ua)
        self.assertNotIn("EdgiOS/", ua)

    def test_recent_cache_skips_verified_daily_set_task(self):
        task = RewardsTask(id="daily-1", category="daily_set", title="Daily Set")
        recent = datetime.now().isoformat()
        self.assertTrue(_should_skip_task_via_cache(task, recent))

    def test_recent_cache_skips_verified_more_promo_task(self):
        task = RewardsTask(id="promo-1", category="more_promo", title="Promo")
        recent = datetime.now().isoformat()
        self.assertTrue(_should_skip_task_via_cache(task, recent))

    def test_recent_cache_can_skip_non_targeted_task(self):
        task = RewardsTask(id="pc-1", category="punch_card", title="Punch Card")
        recent = datetime.now().isoformat()
        self.assertTrue(_should_skip_task_via_cache(task, recent))

    def test_expired_cache_does_not_skip_non_targeted_task(self):
        task = RewardsTask(id="pc-1", category="punch_card", title="Punch Card")
        old = (datetime.now() - timedelta(hours=13)).isoformat()
        self.assertFalse(_should_skip_task_via_cache(task, old))

    def test_expired_cache_does_not_skip_strict_task(self):
        task = RewardsTask(id="daily-1", category="daily_set", title="Daily Set")
        old = (datetime.now() - timedelta(hours=13)).isoformat()
        self.assertFalse(_should_skip_task_via_cache(task, old))

    def test_task_cache_admission_requires_verified_completion(self):
        task = RewardsTask(id="promo-1", category="more_promo")
        self.assertTrue(_should_cache_task_completion(task, True))
        self.assertFalse(_should_cache_task_completion(task, False))

    def test_earn_card_cache_key_preserves_search_query(self):
        key_a = _build_earn_card_cache_key(
            "https://www.bing.com/search?q=parrot+intelligence&form=ML2BF8"
        )
        key_b = _build_earn_card_cache_key(
            "https://www.bing.com/search?q=gaudi+masterpiece&form=ML2BF8"
        )
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(
            key_a,
            "https://www.bing.com/search?q=parrot intelligence",
        )

    def test_recent_earn_card_cache_skip_requires_exact_key(self):
        cache_key = _build_earn_card_cache_key(
            "https://www.bing.com/search?q=parrot+intelligence&form=ML2BF8"
        )
        visited_cards = {cache_key: datetime.now().isoformat()}
        self.assertTrue(_should_skip_earn_card_via_cache(cache_key, visited_cards))
        self.assertFalse(
            _should_skip_earn_card_via_cache(
                _build_earn_card_cache_key(
                    "https://www.bing.com/search?q=gaudi+masterpiece&form=ML2BF8"
                ),
                visited_cards,
            )
        )

    def test_earn_card_cache_admission_requires_proof(self):
        cache_key = _build_earn_card_cache_key(
            "https://www.bing.com/search?q=parrot+intelligence"
        )
        self.assertTrue(_should_cache_earn_card_visit(cache_key, True))
        self.assertFalse(_should_cache_earn_card_visit(cache_key, False))

    def test_mobile_credit_recheck_only_for_ambiguous_zero_zero(self):
        settings = {"mobile_searches": 60}
        ambiguous = {"mobile_current": 0, "mobile_max": 0}
        resolved = {"mobile_current": 27, "mobile_max": 60}
        self.assertTrue(_needs_mobile_credit_recheck(ambiguous, settings))
        self.assertFalse(_needs_mobile_credit_recheck(resolved, settings))

    def test_edge_streak_attempt_allowed_without_offer_id(self):
        info = {"exists": True, "done": False, "minutes": 5, "target": 30}
        self.assertTrue(_edge_streak_attempt_allowed(info))

    def test_edge_streak_attempt_not_allowed_when_done_or_missing(self):
        self.assertFalse(_edge_streak_attempt_allowed({"exists": False, "done": False, "minutes": 0, "target": 30}))
        self.assertFalse(_edge_streak_attempt_allowed({"exists": True, "done": True, "minutes": 30, "target": 30}))

    def test_final_reporting_keeps_targeted_pending_tasks_visible(self):
        snapshot = {
            "search_status": {},
            "task_overview": {"daily_set": {"completed": 0, "total": 0}, "streaks": {}},
            "pending_tasks": ["Explore on Bing for your favorite movie"],
        }
        self.assertEqual(
            _describe_remaining_items(snapshot),
            ["Task: Explore on Bing for your favorite movie"],
        )

    def test_explore_card_detects_search_requirement_from_description(self):
        scanner = UniversalTaskScanner(Humanizer())
        card = {
            "text": "Plan a quick getaway Search on Bing for a flight to your perfect vacation +10",
        }

        self.assertTrue(scanner._explore_card_requires_search(card))

    def test_explore_card_extracts_requested_query_from_description(self):
        scanner = UniversalTaskScanner(Humanizer())
        card = {
            "text": "Plan a quick getaway Search on Bing for a flight to your perfect vacation +10",
        }

        self.assertEqual(
            scanner._extract_explore_search_query(card),
            "a flight to your perfect vacation",
        )

    def test_explore_card_extracts_query_from_href_when_copy_is_insufficient(self):
        scanner = UniversalTaskScanner(Humanizer())
        card = {
            "text": "Explore on Bing +10",
            "href": "https://www.bing.com/search?q=latest+news&form=ML2X9A",
        }

        self.assertEqual(scanner._extract_explore_search_query(card), "latest news")

    def test_explore_card_without_search_copy_is_visit_only(self):
        scanner = UniversalTaskScanner(Humanizer())
        card = {
            "text": "Trending destinations +10",
        }

        self.assertFalse(scanner._explore_card_requires_search(card))

    def test_final_reporting_marks_unverified_mobile_runtime_separately(self):
        snapshot = {
            "search_status": {
                "pc_current": 90,
                "pc_max": 90,
                "mobile_current": 0,
                "mobile_max": 60,
            },
            "search_verification": {
                "desktop": {"verified": True},
                "mobile": {"verified": False, "reason": "runtime_account_unproven"},
            },
            "task_overview": {"daily_set": {"completed": 0, "total": 0}, "streaks": {}},
            "pending_tasks": [],
        }

        self.assertEqual(
            _describe_remaining_items(snapshot),
            ["Mobile unverified from original runtime"],
        )

    def test_final_reporting_can_ignore_mobile_app_and_edge_streak(self):
        snapshot = {
            "search_status": {},
            "task_overview": {
                "daily_set": {"completed": 3, "total": 3},
                "streaks": {
                    "bing_app": {"exists": True, "done": False, "current": 0},
                    "edge": {"exists": True, "done": False, "minutes": 0, "target": 30},
                },
            },
            "reporting_overrides": {
                "ignore_bing_app_checkin": True,
                "ignore_edge_streak": True,
            },
            "pending_tasks": [],
        }

        self.assertEqual(_describe_remaining_items(snapshot), [])

    def test_reconcile_session_proof_applies_reporting_overrides_without_daily_set_proof(self):
        snapshot = {"task_overview": {}, "pending_tasks": []}
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "ignore_bing_app_checkin": True,
                "ignore_edge_streak": True,
            },
        )

        self.assertTrue(reconciled["reporting_overrides"]["ignore_bing_app_checkin"])
        self.assertTrue(reconciled["reporting_overrides"]["ignore_edge_streak"])

    def test_final_reporting_ignores_nonexistent_streaks(self):
        snapshot = {
            "search_status": {},
            "task_overview": {
                "daily_set": {"completed": 0, "total": 0},
                "streaks": {
                    "bing_app": {"exists": False, "done": False, "current": 0},
                    "edge": {"exists": False, "done": False, "minutes": 0, "target": 30},
                },
            },
            "pending_tasks": [],
        }
        self.assertEqual(_describe_remaining_items(snapshot), [])

    def test_session_proof_reconciles_stale_daily_set_verification(self):
        snapshot = {
            "task_overview": {"daily_set": {"completed": 0, "total": 3}},
            "category_status": {"daily_set": {"completed": 0, "total": 3}},
            "pending_tasks": [
                "Parrot intelligence",
                "Gaudi's Masterpiece?",
                "Do you know the answer?",
            ],
            "pending_by_category": {
                "daily_set": [
                    "Parrot intelligence",
                    "Gaudi's Masterpiece?",
                    "Do you know the answer?",
                ]
            },
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "daily_set_complete": True,
                "daily_set_titles": [
                    "Parrot intelligence",
                    "Gaudi's Masterpiece?",
                    "Do you know the answer?",
                ],
            },
        )
        self.assertEqual(reconciled["task_overview"]["daily_set"]["completed"], 3)
        self.assertEqual(reconciled["category_status"]["daily_set"]["completed"], 3)
        self.assertEqual(reconciled["pending_tasks"], [])

    def test_daily_set_title_matching_is_token_aware(self):
        self.assertTrue(
            DailySetCompleter._titles_match(
                "Gaudí's Masterpiece?",
                "Gaudí's Masterpiece? Test your knowledge",
            )
        )
        self.assertFalse(
            DailySetCompleter._titles_match(
                "Parrot intelligence",
                "Sunny winter in San Diego",
            )
        )


    def test_deferred_offer_reason_detects_external_and_multi_day_promos(self):
        referral_task = RewardsTask(
            id="promo-referral",
            category="more_promo",
            title="Turn referrals into Rewards",
            description="Earn 7,500 points when friends search on Bing. Just share an invite.",
            destination_url="https://rewards.bing.com/referandearn?form=ML2X5V",
        )
        search_bar_task = RewardsTask(
            id="promo-searchbar",
            category="more_promo",
            title="Nhận 100 điểm với thanh tìm kiếm",
            description="Bật thanh tìm kiếm và tìm kiếm bằng thanh này trong 3 ngày để nhận 100 điểm",
            destination_url="microsoft-edge://?ux=searchbar&pc=esb004",
        )

        self.assertEqual(get_deferred_offer_reason(referral_task), "external_referral")
        self.assertEqual(get_deferred_offer_reason(search_bar_task), "multi_day_search_bar")

    def test_deferred_items_are_reported_separately_from_blocking_remaining_items(self):
        snapshot = {
            "search_status": {},
            "task_overview": {},
            "pending_tasks": [],
            "deferred_tasks": [
                {"title": "Turn referrals into Rewards", "reason": "external_referral"},
                {"title": "Nhận 100 điểm với thanh tìm kiếm", "reason": "multi_day_search_bar"},
            ],
        }

        self.assertEqual(_describe_remaining_items(snapshot), [])
        self.assertEqual(
            _describe_deferred_items(snapshot),
            [
                "Deferred: Turn referrals into Rewards (requires friend referral activity)",
                "Deferred: Nhận 100 điểm với thanh tìm kiếm (multi-day search-bar offer)",
            ],
        )

    def test_more_promo_candidate_pages_include_earn_surface(self):
        scanner = UniversalTaskScanner(Humanizer())
        task = RewardsTask(
            id="promo-1",
            category="more_promo",
            title="Perks of standing",
            description="Explore how standing desks can boost your health at work",
        )

        pages = scanner._candidate_rewards_pages(task)

        self.assertIn("https://rewards.bing.com/earn", pages)
        self.assertIn("https://rewards.bing.com/dashboard", pages)
        self.assertIn("https://rewards.bing.com", pages)
        self.assertLess(pages.index("https://rewards.bing.com/earn"), pages.index("https://rewards.bing.com"))

    def test_task_match_tokens_include_description_for_duplicate_titles(self):
        task = RewardsTask(
            id="promo-2",
            category="more_promo",
            title="Do you know the answer?",
            description="Challenge yourself with these trivia questions",
        )

        tokens = UniversalTaskScanner._task_title_tokens(task)

        self.assertIn("answer", tokens)
        self.assertIn("trivia", tokens)
        self.assertIn("questions", tokens)


class RewardsTrustAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_detector_uses_completed_daily_set_cards_when_summary_lags(self):
        class FakeBodyLocator:
            async def inner_text(self, timeout=None):
                return (
                    "Dashboard\n"
                    "Daily set\n"
                    "Earn more\n"
                    "Upcoming sporting events\n10\nCompleted\n"
                    "Going to Saskatoon\n10\nCompleted\n"
                    "Rose-Red City?\n10\nCompleted\n"
                    "Your activity\n"
                    "Daily Set\nActivity: 0/3\n"
                )

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/dashboard"

            async def goto(self, url, **kwargs):
                self.url = url

            async def evaluate(self, script):
                return {
                    "dashboard": {
                        "userStatus": {
                            "availablePoints": 10406,
                            "levelInfo": {"activeLevel": "newLevel3"},
                            "counters": {},
                        },
                        "dailySetPromotions": {
                            "2026-04-10": [
                                {"complete": False, "pointProgress": 0, "pointProgressMax": 1},
                                {"complete": False, "pointProgress": 0, "pointProgressMax": 1},
                                {"complete": False, "pointProgress": 0, "pointProgressMax": 1},
                            ]
                        },
                        "morePromotions": [],
                    }
                }

            def locator(self, selector):
                assert selector == "body"
                return FakeBodyLocator()

        with patch("src.streaks.asyncio.sleep", new=AsyncMock()):
            result = await TaskDetector.get_all_tasks(FakePage())

        self.assertEqual(result["daily_set"]["completed"], 3)
        self.assertEqual(result["daily_set"]["total"], 3)

    async def test_daily_set_execute_routes_to_completer_first_and_records_target_proof(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._ensure_no_manual_challenge = AsyncMock()
        scanner._click_task_on_current_page = AsyncMock(return_value=True)
        scanner._complete_visit = AsyncMock()

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"
                self.context = SimpleNamespace(pages=[self])

            async def goto(self, url, **_kwargs):
                self.url = url

        page = FakePage()
        task = RewardsTask(
            id="daily-1",
            title="Parrot intelligence",
            category="daily_set",
            task_type="visit",
        )

        with patch("src.daily_set.DailySetCompleter.complete_daily_set", new=AsyncMock(return_value={
            "attempted": True,
            "target_proven": True,
            "category_proven": False,
            "attempted_only": False,
            "panel_control_failed": False,
            "proof_titles": ["Parrot intelligence"],
            "progress_completed": 1,
            "progress_total": 3,
            "state": "target_proven",
        })), patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            result = await scanner._execute_task(page, task)

        self.assertTrue(result)
        scanner._click_task_on_current_page.assert_not_awaited()
        self.assertEqual(scanner.daily_set_execution_proofs["daily-1"]["state"], "target_proven")
        self.assertNotIn("daily_set", scanner._session_completed_categories)

    async def test_daily_set_execute_allows_generic_fallback_only_after_attempted_only(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._ensure_no_manual_challenge = AsyncMock()
        scanner._click_task_on_current_page = AsyncMock(return_value=True)
        scanner._complete_visit = AsyncMock()

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"
                self.context = SimpleNamespace(pages=[self])

            async def goto(self, url, **_kwargs):
                self.url = url

        page = FakePage()
        task = RewardsTask(
            id="daily-2",
            title="Gaudi's Masterpiece?",
            category="daily_set",
            task_type="visit",
        )

        with patch("src.daily_set.DailySetCompleter.complete_daily_set", new=AsyncMock(return_value={
            "attempted": True,
            "target_proven": False,
            "category_proven": False,
            "attempted_only": True,
            "panel_control_failed": False,
            "proof_titles": [],
            "progress_completed": 1,
            "progress_total": 3,
            "state": "attempted_only",
        })), patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            result = await scanner._execute_task(page, task)

        self.assertTrue(result)
        scanner._click_task_on_current_page.assert_awaited()
        self.assertEqual(scanner.daily_set_execution_proofs["daily-2"]["state"], "attempted_only")

    async def test_daily_set_execute_allows_generic_fallback_after_panel_control_failure(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._ensure_no_manual_challenge = AsyncMock()
        scanner._click_task_on_current_page = AsyncMock(return_value=True)
        scanner._complete_visit = AsyncMock()

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"
                self.context = SimpleNamespace(pages=[self])

            async def goto(self, url, **_kwargs):
                self.url = url

        page = FakePage()
        task = RewardsTask(
            id="daily-3",
            title="Do you know the answer?",
            category="daily_set",
            task_type="visit",
        )

        with patch("src.daily_set.DailySetCompleter.complete_daily_set", new=AsyncMock(return_value={
            "attempted": False,
            "target_proven": False,
            "category_proven": False,
            "attempted_only": False,
            "panel_control_failed": True,
            "proof_titles": [],
            "progress_completed": 0,
            "progress_total": 3,
            "state": "panel_control_failed",
        })), patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            result = await scanner._execute_task(page, task)

        self.assertTrue(result)
        scanner._click_task_on_current_page.assert_awaited()
        self.assertEqual(scanner.daily_set_execution_proofs["daily-3"]["state"], "panel_control_failed")

    async def test_daily_set_category_proof_verifies_other_items_before_api_polling(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._fetch_all_tasks = AsyncMock(return_value=[])

        first = RewardsTask(id="daily-1", title="Parrot intelligence", category="daily_set", task_type="visit")
        second = RewardsTask(id="daily-2", title="Gaudi's Masterpiece?", category="daily_set", task_type="visit")
        scanner._store_daily_set_execution_proof(
            first,
            {
                "state": "category_proven",
                "proof_titles": ["Parrot intelligence", "Gaudi's Masterpiece?"],
                "progress_completed": 3,
                "progress_total": 3,
            },
        )

        verified = await scanner._verify_task_completion(None, second)

        self.assertTrue(verified)
        self.assertIn("daily_set", scanner._session_completed_categories)
        self.assertIn("Gaudi's Masterpiece?", scanner._session_daily_set_titles)
        scanner._dom_check_single_task_done.assert_not_awaited()
        scanner._fetch_all_tasks.assert_not_awaited()

    async def test_daily_set_target_proof_verifies_current_item_before_api_polling(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._fetch_all_tasks = AsyncMock(return_value=[])
        task = RewardsTask(
            id="daily-1",
            title="Parrot intelligence",
            category="daily_set",
            task_type="visit",
        )
        scanner._store_daily_set_execution_proof(
            task,
            {
                "state": "target_proven",
                "proof_titles": ["Parrot intelligence"],
                "progress_completed": 1,
                "progress_total": 3,
            },
        )

        verified = await scanner._verify_task_completion(None, task)

        self.assertTrue(verified)
        self.assertNotIn("daily_set", scanner._session_completed_categories)
        scanner._dom_check_single_task_done.assert_not_awaited()
        scanner._fetch_all_tasks.assert_not_awaited()

    async def test_dashboard_scraper_classifies_keep_earning_cards_as_more_promo(self):
        raw_card = {
            "href": "https://rewards.bing.com/earn",
            "text": "Turn referrals into Rewards\nEarn points when friends search.\n+100",
            "aria": "",
            "title": "",
            "index": 0,
            "sectionHeading": "Keep earning",
        }
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda script: [raw_card] if "querySelectorAll" in script else None)
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            tasks = await scan_dashboard_dom(page)

        self.assertEqual(tasks[0]["category"], "more_promo")

    async def test_dashboard_scraper_classifies_referral_url_as_more_promo_without_heading(self):
        raw_card = {
            "href": "https://rewards.bing.com/referandearn/?form=ML2XHD&rnoreward=1",
            "text": "Turn referrals into Rewards\nEarn points when friends search.\n+100",
            "aria": "",
            "title": "",
            "index": 0,
            "sectionHeading": "",
        }
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda script: [raw_card] if "querySelectorAll" in script else None)
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            tasks = await scan_dashboard_dom(page)

        self.assertEqual(tasks[0]["category"], "more_promo")

    async def test_dashboard_scraper_prioritizes_referral_url_over_stale_daily_set_heading(self):
        raw_card = {
            "href": "https://rewards.bing.com/referandearn/?form=ML2XHD&rnoreward=1",
            "text": "Turn referrals into Rewards\nEarn points when friends search.\n+100",
            "aria": "",
            "title": "",
            "index": 0,
            "sectionHeading": "Daily set",
        }
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda script: [raw_card] if "querySelectorAll" in script else None)
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            tasks = await scan_dashboard_dom(page)

        self.assertEqual(tasks[0]["category"], "more_promo")

    async def test_dashboard_scraper_classifies_earn_more_heading_as_more_promo(self):
        raw_card = {
            "href": "https://www.bing.com/search?q=Travel+to+Galway&FORM=tgrew4&filters=sid:%22abc%22&rnoreward=1",
            "text": "Galway's Winter Festival Happiness\nExciting entertainment and mild weather\n+10",
            "aria": "",
            "title": "",
            "index": 0,
            "sectionHeading": "Earn more",
        }
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda script: [raw_card] if "querySelectorAll" in script else None)
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            tasks = await scan_dashboard_dom(page)

        self.assertEqual(tasks[0]["category"], "more_promo")

    async def test_dashboard_scraper_skips_non_actionable_streak_cards(self):
        raw_cards = [
            {
                "href": "",
                "text": "Bing Search Streak\nSearch: 0/3\n+9",
                "aria": "",
                "title": "",
                "index": 0,
                "sectionHeading": "",
            },
            {
                "href": "https://www.bing.com/search?q=Quote%20of%20the%20day&form=ML2BFU",
                "text": "Have you heard this quote?\nQuote of the day\n+5",
                "aria": "",
                "title": "",
                "index": 1,
                "sectionHeading": "Earn more",
            },
        ]
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda script: raw_cards if "querySelectorAll" in script else None)
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            tasks = await scan_dashboard_dom(page)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Have you heard this quote?")
        self.assertIn("bing search streak", NON_ACTIONABLE_CARD_TITLES)

    async def test_fetch_all_tasks_merges_dashboard_and_earn_surfaces(self):
        scanner = UniversalTaskScanner(Humanizer())

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/dashboard"

            async def goto(self, url, **_kwargs):
                self.url = url

        page = FakePage()

        async def fake_scan_dashboard_dom(current_page):
            if current_page.url.endswith("/dashboard"):
                return []
            if current_page.url.endswith("/earn"):
                return [
                    {
                        "title": "Have you heard this quote?",
                        "description": "Quote of the day",
                        "points": 5,
                        "url": "https://www.bing.com/search?q=Quote%20of%20the%20day&form=ML2BFU",
                        "element_index": 27,
                        "category": "more_promo",
                        "is_quiz": False,
                    }
                ]
            return []

        with patch("src.dashboard_scraper.scan_dashboard_dom", new=fake_scan_dashboard_dom), \
             patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            tasks = await scanner._fetch_all_tasks(page)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Have you heard this quote?")
        self.assertEqual(tasks[0].category, "more_promo")
        self.assertEqual(tasks[0].raw_data["scan_url"], "https://rewards.bing.com/earn")

    async def test_daily_set_executor_runs_before_generic_click_path(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._click_task_on_current_page = AsyncMock(return_value=True)

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"
                self.context = SimpleNamespace(pages=[self])

            async def goto(self, url, **_kwargs):
                self.url = url

        task = RewardsTask(id="daily-1", title="Parrot intelligence", category="daily_set", task_type="visit")

        with patch(
            "src.daily_set.DailySetCompleter.complete_daily_set",
            new=AsyncMock(
                return_value={
                    "state": "target_proven",
                    "target_proven": True,
                    "category_proven": False,
                    "proof_titles": ["Parrot intelligence"],
                    "progress_completed": 1,
                    "progress_total": 3,
                }
            ),
        ) as daily_set_executor:
            self.assertTrue(await scanner._execute_task(FakePage(), task))

        daily_set_executor.assert_awaited_once()
        scanner._click_task_on_current_page.assert_not_awaited()

    async def test_daily_set_execution_proof_is_written_for_task_title_and_category_keys(self):
        scanner = UniversalTaskScanner(Humanizer())
        task = RewardsTask(id="daily-1", title="Gaudi's Masterpiece?", category="daily_set", task_type="visit")

        proof = scanner._record_daily_set_execution_proof(
            task,
            {
                "state": "target_proven",
                "target_proven": True,
                "proof_titles": ["Gaudi's Masterpiece?"],
                "progress_completed": 1,
                "progress_total": 3,
            },
        )

        self.assertEqual(proof["state"], "target_proven")
        self.assertEqual(scanner.daily_set_execution_proofs["daily-1"]["state"], "target_proven")
        self.assertEqual(
            scanner.daily_set_execution_proofs["gaudi s masterpiece"]["proof_titles"],
            ["Gaudi's Masterpiece?"],
        )
        self.assertEqual(scanner.daily_set_execution_proofs["daily_set"]["progress_total"], 3)

    async def test_daily_set_verification_checks_execution_proof_before_api_polling(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._dom_check_task_done_across_rewards_pages = AsyncMock(return_value=False)
        scanner.daily_set_execution_proofs["daily-1"] = {
            "state": "target_proven",
            "proof_titles": ["Parrot intelligence"],
            "progress_completed": 1,
            "progress_total": 3,
            "source": "test",
        }
        scanner._fetch_all_tasks = AsyncMock(side_effect=AssertionError("API polling should not run"))
        task = RewardsTask(
            id="daily-1",
            title="Parrot intelligence",
            category="daily_set",
            task_type="visit",
        )

        verified = await scanner._verify_task_completion(None, task)

        self.assertTrue(verified)
        scanner._fetch_all_tasks.assert_not_awaited()

    async def test_daily_set_verification_accepts_disappearance_after_attempted_execution(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._dom_check_task_done_across_rewards_pages = AsyncMock(return_value=False)
        scanner.daily_set_execution_proofs["daily-1"] = {
            "state": "panel_control_failed",
            "proof_titles": [],
            "progress_completed": 0,
            "progress_total": 3,
            "source": "test",
        }
        scanner._fetch_all_tasks = AsyncMock(return_value=[
            RewardsTask(id="daily-2", title="Other daily item", category="daily_set", task_type="visit"),
        ])
        task = RewardsTask(
            id="daily-1",
            title="Parrot intelligence",
            category="daily_set",
            task_type="visit",
        )

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            verified = await scanner._verify_task_completion(None, task)

        self.assertTrue(verified)
        self.assertEqual(scanner.daily_set_execution_proofs["daily-1"]["state"], "target_proven")
        self.assertEqual(
            scanner.daily_set_execution_proofs["daily-1"]["source"],
            "daily_set_inventory_disappearance",
        )

    async def test_daily_set_disappearance_does_not_pass_when_same_title_still_present(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._dom_check_task_done_across_rewards_pages = AsyncMock(return_value=False)
        scanner.daily_set_execution_proofs["daily-1"] = {
            "state": "attempted_only",
            "proof_titles": [],
            "progress_completed": 0,
            "progress_total": 3,
            "source": "test",
        }
        scanner._fetch_all_tasks = AsyncMock(return_value=[
            RewardsTask(id="other-id", title="Parrot intelligence", category="daily_set", task_type="visit"),
        ])
        task = RewardsTask(
            id="daily-1",
            title="Parrot intelligence",
            category="daily_set",
            task_type="visit",
        )

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            verified = await scanner._verify_task_completion(None, task)

        self.assertFalse(verified)
        self.assertEqual(scanner.daily_set_execution_proofs["daily-1"]["state"], "attempted_only")

    async def test_daily_set_generic_fallback_only_runs_for_attempted_only_and_panel_failures(self):
        task = RewardsTask(id="daily-1", title="Parrot intelligence", category="daily_set", task_type="visit")

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"
                self.context = SimpleNamespace(pages=[self])

            async def goto(self, url, **_kwargs):
                self.url = url

        attempted_scanner = UniversalTaskScanner(Humanizer())
        attempted_scanner._click_task_on_current_page = AsyncMock(return_value=False)
        with patch(
            "src.daily_set.DailySetCompleter.complete_daily_set",
            new=AsyncMock(
                return_value={
                    "state": "attempted_only",
                    "attempted_only": True,
                    "target_proven": False,
                    "category_proven": False,
                    "progress_completed": 0,
                    "progress_total": 3,
                }
            ),
        ):
            self.assertFalse(await attempted_scanner._execute_task(FakePage(), task))
        self.assertGreater(attempted_scanner._click_task_on_current_page.await_count, 0)

        proven_scanner = UniversalTaskScanner(Humanizer())
        proven_scanner._click_task_on_current_page = AsyncMock(return_value=False)
        with patch(
            "src.daily_set.DailySetCompleter.complete_daily_set",
            new=AsyncMock(
                return_value={
                    "state": "target_proven",
                    "attempted_only": False,
                    "target_proven": True,
                    "category_proven": False,
                    "progress_completed": 1,
                    "progress_total": 3,
                }
            ),
        ):
            self.assertTrue(await proven_scanner._execute_task(FakePage(), task))
        proven_scanner._click_task_on_current_page.assert_not_awaited()

    async def test_daily_set_click_ignores_visual_index_fast_path(self):
        scanner = UniversalTaskScanner(Humanizer())

        class FakeLocator:
            async def count(self):
                return 0

            async def is_visible(self, timeout=None):
                return False

            async def scroll_into_view_if_needed(self, timeout=None):
                return None

            async def click(self, timeout=None):
                return None

            @property
            def first(self):
                return self

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"

            def locator(self, *args, **kwargs):
                return FakeLocator()

        task = RewardsTask(
            id="daily-1",
            title="Rose-Red City?",
            category="daily_set",
            task_type="quiz",
            raw_data={"element_index": 27},
        )

        with patch("src.dashboard_scraper.click_task_by_index", new=AsyncMock(return_value=True)) as click_by_index, \
             patch.object(scanner, "_mark_dom_text_candidate", new=AsyncMock(return_value=None)):
            clicked = await scanner._click_task_on_current_page(FakePage(), task)

        self.assertFalse(clicked)
        click_by_index.assert_not_awaited()

    async def test_daily_set_bulk_fallback_retries_for_each_unproven_title(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._click_task_on_current_page = AsyncMock(return_value=False)

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/earn"
                self.context = SimpleNamespace(pages=[self])

            async def goto(self, url, **_kwargs):
                self.url = url

        page = FakePage()
        first = RewardsTask(id="daily-1", title="Jupiter's moons", category="daily_set", task_type="visit")
        second = RewardsTask(id="daily-2", title="Renaissance Genius?", category="daily_set", task_type="visit")

        fallback = AsyncMock(side_effect=[
            {
                "completed": 1,
                "total": 1,
                "tasks": [{"status": "completed"}],
                "attempted": True,
                "target_status": "proven",
                "target_proven": True,
                "category_proven": False,
                "attempted_only": False,
                "proof_titles": ["Jupiter's moons"],
                "progress_completed": 1,
                "progress_total": 3,
            },
            {
                "completed": 1,
                "total": 1,
                "tasks": [{"status": "completed"}],
                "attempted": True,
                "target_status": "proven",
                "target_proven": True,
                "category_proven": False,
                "attempted_only": False,
                "proof_titles": ["Renaissance Genius?"],
                "progress_completed": 2,
                "progress_total": 3,
            },
        ])

        with patch("src.daily_set.DailySetCompleter.complete_daily_set", new=fallback):
            self.assertTrue(await scanner._execute_task(page, first))
            self.assertTrue(await scanner._execute_task(page, second))

        self.assertEqual(fallback.await_count, 2)
        self.assertEqual(fallback.await_args_list[0].kwargs["expected_title"], "Jupiter's moons")
        self.assertEqual(fallback.await_args_list[1].kwargs["expected_title"], "Renaissance Genius?")

    async def test_cross_surface_dom_verification_checks_candidate_rewards_pages(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._candidate_rewards_pages = lambda _task: [
            "https://rewards.bing.com/earn",
            "https://rewards.bing.com/dashboard",
        ]
        scanner._dom_check_single_task_done = AsyncMock(side_effect=[False, True])
        scanner._open_daily_set_panel = AsyncMock()

        class FakePage:
            def __init__(self):
                self.url = "https://rewards.bing.com/"

            async def goto(self, url, **_kwargs):
                self.url = url

        page = FakePage()
        task = RewardsTask(
            id="promo-1",
            title="Perks of standing",
            description="Explore the benefits of standing desks",
            category="more_promo",
        )

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            done = await scanner._dom_check_task_done_across_rewards_pages(page, task)

        self.assertTrue(done)
        self.assertEqual(scanner._dom_check_single_task_done.await_count, 2)
        self.assertEqual(page.url, "https://rewards.bing.com/dashboard")

    async def test_search_status_merges_counters_across_rewards_surfaces(self):
        searcher = Searcher(Humanizer(), SimpleNamespace(), {})

        def payload(pc=None, mobile=None):
            counters = {}
            if pc is not None:
                counters["pcSearch"] = [{
                    "pointProgress": pc[0],
                    "pointProgressMax": pc[1],
                }]
            if mobile is not None:
                counters["mobileSearch"] = [{
                    "pointProgress": mobile[0],
                    "pointProgressMax": mobile[1],
                }]
            return {
                "dashboard": {
                    "userStatus": {
                        "availablePoints": 1171,
                        "counters": counters,
                    }
                }
            }

        class FakePage:
            def __init__(self, payload_by_url):
                self.url = "about:blank"
                self._payload_by_url = payload_by_url

            async def goto(self, url, **_kwargs):
                self.url = url

            async def evaluate(self, _script):
                return self._payload_by_url.get(self.url)

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

        page = FakePage({
            "https://rewards.bing.com/dashboard": payload(pc=(81, 90)),
            "https://rewards.bing.com/earn": payload(mobile=(60, 60)),
            "https://rewards.bing.com/": payload(),
            "https://rewards.bing.com/about": payload(),
        })

        with patch("src.searcher.asyncio.sleep", new=AsyncMock()):
            status = await searcher.get_search_points_status(page)

        self.assertEqual(status["pc_current"], 81)
        self.assertEqual(status["pc_max"], 90)
        self.assertEqual(status["mobile_current"], 60)
        self.assertEqual(status["mobile_max"], 60)

    async def test_scan_short_circuits_daily_set_when_live_overview_marks_category_complete(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._fetch_all_tasks = AsyncMock(return_value=[
            RewardsTask(id="daily-1", title="Parrot intelligence", category="daily_set", task_type="visit"),
            RewardsTask(id="daily-2", title="Gaudi's Masterpiece?", category="daily_set", task_type="visit"),
        ])
        scanner._dom_verify_task_status = AsyncMock(return_value=set())
        scanner._execute_task = AsyncMock(return_value=True)
        scanner._verify_task_completion = AsyncMock(return_value=True)

        with patch("src.streaks.TaskDetector.get_all_tasks", new=AsyncMock(return_value={
            "daily_set": {"completed": 2, "total": 2},
        })), \
             patch("src.universal_task._load_state", return_value={}), \
             patch("src.universal_task._save_state"):
            result = await scanner.scan_and_complete(object(), account_email="test@example.com")

        self.assertEqual(result["skipped_done"], 2)
        self.assertIn("daily_set", scanner._session_completed_categories)
        self.assertTrue(result["session_proofs"]["daily_set_complete"])
        scanner._execute_task.assert_not_awaited()

    async def test_mobile_credit_recheck_returns_resolved_status(self):
        settings = {
            "mobile_searches": 60,
            "mobile_credit_recheck_attempts": 2,
            "mobile_credit_recheck_delay_seconds": 1,
        }
        ambiguous = {"mobile_current": 0, "mobile_max": 0}
        resolved = {"mobile_current": 27, "mobile_max": 60}
        searcher = AsyncMock()
        searcher.get_search_points_status = AsyncMock(side_effect=[ambiguous, resolved])

        with patch("main.asyncio.sleep", new=AsyncMock()):
            status = await _read_search_status_with_mobile_recheck(searcher, object(), settings)

        self.assertEqual(status, resolved)
        self.assertEqual(searcher.get_search_points_status.await_count, 2)

    async def test_mobile_run_searches_prefers_search_box_path(self):
        trends = SimpleNamespace(
            fetch_trending=AsyncMock(),
            get_batch_queries=lambda count: ["Quote of the day"] * count,
        )
        searcher = Searcher(Humanizer(), trends, {})
        searcher._search_via_box = AsyncMock(return_value=True)
        searcher._search_via_url = AsyncMock(return_value=True)
        searcher._session_break_interval = lambda: 99
        searcher._search_delay_bounds = lambda: (0, 0)

        with patch("src.searcher.asyncio.sleep", new=AsyncMock()), \
             patch.object(searcher.humanizer, "random_delay", new=AsyncMock()), \
             patch.object(searcher.humanizer, "simulate_tab_switch", new=AsyncMock()):
            result = await searcher.run_searches(SimpleNamespace(), 1, "mobile")

        self.assertEqual(result["completed"], 1)
        searcher._search_via_box.assert_awaited_once()
        searcher._search_via_url.assert_not_awaited()

    async def test_mobile_requirement_resolution_uses_mobile_probe(self):
        settings = {"mobile_searches": 60}
        baseline = {"mobile_current": 0, "mobile_max": 0}
        probed = {"mobile_current": 27, "mobile_max": 60, "total_points": 9036}

        with patch("main._probe_search_status_in_mode", new=AsyncMock(return_value=probed)):
            resolved = await _resolve_mobile_search_requirement(
                settings,
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                "state.json",
                baseline,
            )

        self.assertEqual(resolved["mobile_current"], 27)
        self.assertEqual(resolved["mobile_max"], 60)
        self.assertEqual(resolved["total_points"], 9036)

    async def test_mobile_search_pass_enables_emulation_and_records_credit_proof(self):
        settings = {"mobile_searches": 60}
        account = {"email": "test@example.com", "password": "pw"}
        ctx = object()
        page = SimpleNamespace(context=ctx, goto=AsyncMock())
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            start=AsyncMock(),
            create_context=AsyncMock(return_value=ctx),
            new_page=AsyncMock(return_value=page),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        login_mgr = AsyncMock()
        login_mgr.is_logged_in = AsyncMock(return_value=True)
        searcher = AsyncMock()
        searcher.run_searches = AsyncMock(return_value={"completed": 11, "failed": 0})
        before = {"mobile_current": 27, "mobile_max": 60}
        after = {"mobile_current": 60, "mobile_max": 60}

        class DummyProgress:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def add_task(self, *_args, **_kwargs):
                return 1
            def update(self, *_args, **_kwargs):
                return None

        with patch("main.BrowserManager", return_value=browser_mgr), \
             patch("main._persist_storage_state", new=AsyncMock()), \
             patch("main._read_search_status_with_mobile_recheck", new=AsyncMock(return_value=before)), \
             patch("main._wait_for_mobile_credit_update", new=AsyncMock(return_value=after)), \
             patch("main._raise_if_search_stopped"), \
             patch("main.console.print"), \
             patch("main.Progress", DummyProgress), \
             patch("main.asyncio.sleep", new=AsyncMock()):
            result = await _run_mobile_search_pass(
                settings,
                account,
                None,
                login_mgr,
                searcher,
                Path("state.json"),
                count=11,
            )

        self.assertEqual(browser_mgr.toggle_mobile_emulation.await_count, 2)
        self.assertTrue(result["credit_proven"])
        self.assertEqual(result["status_after"]["mobile_current"], 60)

    async def test_mobile_search_pass_prefers_patchright_mobile_when_available(self):
        settings = {"mobile_searches": 60}
        account = {"email": "test@example.com", "password": "pw"}
        ctx = object()
        page = SimpleNamespace(context=ctx, goto=AsyncMock())
        patchright_pw = SimpleNamespace(stop=AsyncMock())
        patchright_browser = SimpleNamespace(close=AsyncMock())
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            create_mobile_patchright=AsyncMock(return_value=(patchright_pw, patchright_browser, ctx, page)),
            start=AsyncMock(),
            create_context=AsyncMock(),
            new_page=AsyncMock(),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        login_mgr = AsyncMock()
        login_mgr.is_logged_in = AsyncMock(return_value=True)
        searcher = AsyncMock()
        searcher.run_searches = AsyncMock(return_value={"completed": 11, "failed": 0})
        before = {"mobile_current": 27, "mobile_max": 60}
        after = {"mobile_current": 60, "mobile_max": 60}

        class DummyProgress:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def add_task(self, *_args, **_kwargs):
                return 1
            def update(self, *_args, **_kwargs):
                return None

        with patch("main.BrowserManager", return_value=browser_mgr), \
             patch("main._persist_storage_state", new=AsyncMock()), \
             patch("main._read_search_status_with_mobile_recheck", new=AsyncMock(return_value=before)), \
             patch("main._wait_for_mobile_credit_update", new=AsyncMock(return_value=after)), \
             patch("main._raise_if_search_stopped"), \
             patch("main.console.print"), \
             patch("main.Progress", DummyProgress), \
             patch("main.asyncio.sleep", new=AsyncMock()):
            result = await _run_mobile_search_pass(
                settings,
                account,
                None,
                login_mgr,
                searcher,
                Path("state.json"),
                count=11,
            )

        browser_mgr.create_mobile_patchright.assert_awaited_once()
        browser_mgr.start.assert_not_awaited()
        self.assertEqual(browser_mgr.toggle_mobile_emulation.await_count, 1)
        patchright_browser.close.assert_awaited_once()
        patchright_pw.stop.assert_awaited_once()
        self.assertEqual(result["runtime_family"], "patchright_mobile")
        self.assertTrue(result["credit_proven"])

    async def test_mobile_search_pass_prefers_gpm_mobile_when_available(self):
        settings = {
            "mobile_searches": 60,
            "gpm_integration_enabled": True,
            "gpm_api_url": "http://127.0.0.1:9495",
        }
        account = {
            "email": "test@example.com",
            "password": "pw",
            "gpm_mobile_profile_id": "gpm-mobile-123",
        }
        ctx = object()
        page = SimpleNamespace(context=ctx, goto=AsyncMock())
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            start_connected_edge=AsyncMock(),
            create_context=AsyncMock(return_value=ctx),
            new_page=AsyncMock(return_value=page),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        login_mgr = AsyncMock()
        login_mgr.is_logged_in = AsyncMock(return_value=True)
        searcher = AsyncMock()
        searcher.run_searches = AsyncMock(return_value={"completed": 11, "failed": 0})
        before = {"mobile_current": 27, "mobile_max": 60}
        after = {"mobile_current": 60, "mobile_max": 60}

        class DummyProgress:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def add_task(self, *_args, **_kwargs):
                return 1
            def update(self, *_args, **_kwargs):
                return None

        with patch("main.BrowserManager", return_value=browser_mgr), \
             patch("main._persist_storage_state", new=AsyncMock()), \
             patch("main._read_search_status_with_mobile_recheck", new=AsyncMock(return_value=before)), \
             patch("main._wait_for_mobile_credit_update", new=AsyncMock(return_value=after)), \
             patch("main._raise_if_search_stopped"), \
             patch("main.console.print"), \
             patch("main.Progress", DummyProgress), \
             patch("main._start_gpm_profile", new=AsyncMock(return_value="http://127.0.0.1:45678")), \
             patch("main._stop_gpm_profile") as stop_gpm_profile, \
             patch("main.asyncio.sleep", new=AsyncMock()):
            result = await _run_mobile_search_pass(
                settings,
                account,
                None,
                login_mgr,
                searcher,
                Path("state.json"),
                count=11,
            )

        browser_mgr.start_connected_edge.assert_awaited_once_with("http://127.0.0.1:45678")
        browser_mgr.create_context.assert_awaited_once()
        self.assertEqual(browser_mgr.toggle_mobile_emulation.await_count, 1)
        stop_gpm_profile.assert_called_once_with("gpm-mobile-123", "http://127.0.0.1:9495")
        self.assertEqual(result["runtime_family"], "gpm_mobile")
        self.assertTrue(result["credit_proven"])

    async def test_strict_verification_fails_when_task_disappears(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._fetch_all_tasks = AsyncMock(return_value=[])
        task = RewardsTask(
            id="daily-1",
            title="Gaudi's Masterpiece?",
            category="daily_set",
            task_type="visit",
        )

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            verified = await scanner._verify_task_completion(None, task)

        self.assertFalse(verified)

    async def test_more_promo_strict_verification_fails_when_task_disappears(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._fetch_all_tasks = AsyncMock(return_value=[])
        task = RewardsTask(
            id="promo-1",
            title="Turn referrals into Rewards",
            category="more_promo",
            task_type="visit",
        )

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            verified = await scanner._verify_task_completion(None, task)

        self.assertFalse(verified)

    async def test_unknown_referral_offer_defers_via_semantic_fallback(self):
        task = RewardsTask(
            id="promo-referral",
            title="Turn referrals into Rewards",
            description="Earn points when friends search on Bing.",
            category="unknown",
            task_type="unknown",
            destination_url="https://rewards.bing.com/referandearn/?form=ML2XHD&rnoreward=1",
        )

        self.assertEqual(get_deferred_offer_reason(task), "external_referral")

    async def test_unknown_multi_day_search_bar_offer_defers_via_semantic_fallback(self):
        task = RewardsTask(
            id="promo-searchbar",
            title="Nhận 100 điểm với thanh tìm kiếm",
            description="Bật thanh tìm kiếm và tìm kiếm bằng thanh này trong 3 ngày để nhận 100 điểm",
            category="unknown",
            task_type="unknown",
            destination_url="microsoft-edge://?ux=searchbar&pc=esb004",
        )

        self.assertEqual(get_deferred_offer_reason(task), "multi_day_search_bar")

    async def test_nonmatched_unknown_offer_does_not_gain_defer_fallback(self):
        task = RewardsTask(
            id="promo-galway",
            title="Galway's Winter Festival Happiness",
            description="Exciting entertainment and mild weather",
            category="unknown",
            task_type="unknown",
            destination_url="https://www.bing.com/search?q=Travel+to+Galway&FORM=tgrew4&filters=sid:%22abc%22&rnoreward=1",
        )

        self.assertIsNone(get_deferred_offer_reason(task))

    async def test_keep_earning_quote_offer_uses_quote_handler(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._complete_quote_of_day_offer = AsyncMock(return_value=True)
        task = RewardsTask(
            id="promo-quote",
            title="Have you heard this quote?",
            description="Quote of the day",
            category="more_promo",
            task_type="unknown",
            destination_url="https://www.bing.com/search?q=Quote%20of%20the%20day&form=ML2BFU",
        )

        handled = await scanner._complete_known_more_promo(SimpleNamespace(), task)

        self.assertTrue(handled)
        scanner._complete_quote_of_day_offer.assert_awaited_once()

    async def test_non_strict_verification_tolerates_missing_task_after_action(self):
        scanner = UniversalTaskScanner(Humanizer())
        scanner._log = lambda *_args, **_kwargs: None
        scanner._dom_check_single_task_done = AsyncMock(return_value=False)
        scanner._fetch_all_tasks = AsyncMock(return_value=[])
        task = RewardsTask(
            id="promo-1",
            title="Turn referrals into Rewards",
            category="punch_card",
            task_type="quiz",
        )

        with patch("src.universal_task.asyncio.sleep", new=AsyncMock()):
            verified = await scanner._verify_task_completion(None, task)

        self.assertTrue(verified)

    async def test_daily_set_ai_success_with_expected_title_stays_unproven(self):
        ai_agent = AsyncMock()
        ai_agent.enabled = True
        ai_agent.complete_daily_set = AsyncMock(return_value={"success": True, "steps": 1})
        completer = DailySetCompleter(Humanizer(), ai_agent=ai_agent)

        stats = await completer.complete_daily_set(object(), expected_title="Parrot intelligence")

        self.assertTrue(stats["attempted"])
        self.assertEqual(stats["target_status"], "not_proven")
        self.assertEqual(stats["proof_titles"], [])

    async def test_daily_set_ai_success_can_return_category_proof(self):
        ai_agent = AsyncMock()
        ai_agent.enabled = True
        ai_agent.complete_daily_set = AsyncMock(return_value={"success": True, "steps": 1})
        completer = DailySetCompleter(Humanizer(), ai_agent=ai_agent)

        with patch.object(
            completer,
            "_read_daily_set_progress",
            new=AsyncMock(return_value={"completed": 3, "total": 3, "category_proven": True}),
        ):
            stats = await completer.complete_daily_set(object(), expected_title="Parrot intelligence")

        self.assertTrue(stats["category_proven"])
        self.assertEqual(stats["progress_completed"], 3)
        self.assertEqual(stats["progress_total"], 3)


if __name__ == "__main__":
    unittest.main()
