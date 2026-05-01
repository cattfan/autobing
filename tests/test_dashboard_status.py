import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.dashboard import (
    _account_display_label,
    _account_state_key,
    _account_timeout_seconds,
    _build_profile_summary,
    _build_profile_views,
    _collect_final_verification,
    _ensure_dashboard_bind_is_safe,
    _describe_remaining_items,
    _ensure_usable_desktop_search_page,
    _reconcile_verification_with_session_proof,
    _effective_max_threads,
    _profile_lock_keys_for_account,
    _read_search_status_with_mobile_recheck,
    _read_search_status_for_runtime_descriptor,
    _resolve_account_current_points,
    _calculate_account_earned_today,
    _merge_search_status_preserving_progress,
    _resolve_desktop_search_requirement,
    _select_mobile_runtime_strategy,
    _upsert_account_daily_snapshot,
    _wait_for_mobile_credit_update,
)
from src.runtime_identity import (
    build_runtime_descriptor,
    choose_search_verification_source,
    describe_search_remaining_items,
    invalidate_runtime_attachment,
)
from src.universal_task import RewardsTask


class DashboardStatusTests(unittest.TestCase):
    def test_account_current_points_ignore_global_dashboard_total(self):
        self.assertEqual(
            _resolve_account_current_points(500, {"total_points": 520}),
            520,
        )
        self.assertEqual(
            _resolve_account_current_points(500, {}),
            500,
        )

    def test_account_earned_today_uses_account_baseline(self):
        self.assertEqual(_calculate_account_earned_today(520, 500), 20)
        self.assertEqual(_calculate_account_earned_today(490, 500), 0)

    def test_search_status_merge_preserves_verified_counters_when_page_read_is_ambiguous(self):
        merged = _merge_search_status_preserving_progress(
            {"pc_current": 0, "pc_max": 0, "mobile_current": 0, "mobile_max": 0, "total_points": 0},
            {"pc_current": 90, "pc_max": 90, "mobile_current": 60, "mobile_max": 60, "total_points": 14428},
        )
        self.assertEqual(merged["pc_current"], 90)
        self.assertEqual(merged["pc_max"], 90)
        self.assertEqual(merged["mobile_current"], 60)
        self.assertEqual(merged["mobile_max"], 60)
        self.assertEqual(merged["total_points"], 14428)

    def test_search_status_merge_keeps_current_nonzero_track(self):
        merged = _merge_search_status_preserving_progress(
            {"pc_current": 24, "pc_max": 90, "total_points": 100},
            {"pc_current": 90, "pc_max": 90, "total_points": 200},
        )
        self.assertEqual(merged["pc_current"], 24)
        self.assertEqual(merged["pc_max"], 90)
        self.assertEqual(merged["total_points"], 100)

    def test_account_daily_snapshot_upsert_uses_file_lock(self):
        entered = []

        class FakeLock:
            def __enter__(self):
                entered.append(True)

            def __exit__(self, exc_type, exc, tb):
                entered.append(False)

        records = []
        with patch("src.dashboard._snapshot_file_lock", return_value=FakeLock()), \
             patch("src.dashboard._read_account_daily_snapshots_unlocked", return_value=records), \
             patch("src.dashboard._write_account_daily_snapshots_unlocked") as write_snapshots:
            _upsert_account_daily_snapshot({"account_key": "acct:a", "date": "2026-04-24", "points_now": 100})

        self.assertEqual(entered, [True, False])
        written = write_snapshots.call_args.args[0]
        self.assertEqual(written[0]["account_key"], "acct:a")
        self.assertEqual(written[0]["points_now"], 100)

    def test_mobile_runtime_strategy_marks_missing_gpm_profile_as_native_fallback(self):
        fallback, reason = _select_mobile_runtime_strategy(True, "")

        self.assertTrue(fallback)
        self.assertEqual(reason, "missing_gpm_mobile_profile_id")

    def test_mobile_runtime_strategy_prefers_gpm_when_profile_is_present(self):
        fallback, reason = _select_mobile_runtime_strategy(True, "mob-1")

        self.assertFalse(fallback)
        self.assertEqual(reason, "gpm_mobile_profile")

    def test_account_state_key_is_stable_and_unique_per_email(self):
        a = _account_state_key("yunataauto4@outlook.com.vn")
        b = _account_state_key("yunataauto5@outlook.com.vn")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("acct:"))

    def test_account_display_label_keeps_accounts_distinguishable(self):
        a = _account_display_label("yunataauto4@outlook.com.vn")
        b = _account_display_label("yunataauto5@outlook.com.vn")
        self.assertNotEqual(a, b)
        self.assertIn("@outlook.com.vn", a)

    def test_profile_lock_keys_include_desktop_and_mobile_gpm_ids(self):
        keys = _profile_lock_keys_for_account(
            {"gpm_integration_enabled": True},
            {
                "email": "user@example.com",
                "gpm_profile_id": "desk-1",
                "gpm_mobile_profile_id": "mob-1",
            },
        )

        self.assertEqual(keys, ["gpm:desk-1", "gpm:mob-1"])

    def test_profile_lock_keys_fall_back_to_native_email_when_no_gpm_ids(self):
        keys = _profile_lock_keys_for_account(
            {"gpm_integration_enabled": True},
            {"email": "user@example.com"},
        )

        self.assertEqual(keys, ["native:user@example.com"])

    def test_profile_lock_keys_deduplicate_same_profile_id_across_desktop_and_mobile(self):
        keys = _profile_lock_keys_for_account(
            {"gpm_integration_enabled": True},
            {
                "email": "user@example.com",
                "gpm_profile_id": "shared-1",
                "gpm_mobile_profile_id": "shared-1",
            },
        )

        self.assertEqual(keys, ["gpm:shared-1"])

    def test_effective_max_threads_stays_configured_for_safe_headless_non_edge_mode(self):
        effective, reason = _effective_max_threads({
            "max_threads": 4,
            "gpm_integration_enabled": False,
            "native_edge_runtime_enabled": False,
            "headless": True,
        })

        self.assertEqual(effective, 4)
        self.assertEqual(reason, "")

    def test_effective_max_threads_keeps_configured_parallelism_for_live_edge_and_gpm(self):
        effective, reason = _effective_max_threads({
            "max_threads": 10,
            "gpm_integration_enabled": True,
            "native_edge_runtime_enabled": True,
            "headless": False,
        })

        self.assertEqual(effective, 2)
        self.assertIn("capped to 2", reason)

    def test_effective_max_threads_keeps_smaller_gpm_parallelism(self):
        effective, reason = _effective_max_threads({
            "max_threads": 2,
            "gpm_integration_enabled": True,
            "native_edge_runtime_enabled": True,
            "headless": False,
        })

        self.assertEqual(effective, 2)
        self.assertEqual(reason, "")

    def test_account_timeout_seconds_adds_budget_for_later_batches(self):
        self.assertEqual(_account_timeout_seconds(0, 2), 7200.0)
        self.assertEqual(_account_timeout_seconds(1, 2), 7200.0)
        self.assertEqual(_account_timeout_seconds(2, 2), 14400.0)

    def test_dashboard_bind_stays_local_when_no_dashboard_password_is_configured(self):
        with self.assertRaisesRegex(RuntimeError, "Refusing to bind"):
            _ensure_dashboard_bind_is_safe("0.0.0.0", {"master_password_hash": ""})

    def test_dashboard_bind_allows_non_loopback_when_dashboard_password_is_configured(self):
        _ensure_dashboard_bind_is_safe("0.0.0.0", {"master_password_hash": "configured"})

    def test_profile_views_include_recent_log_context(self):
        accounts_snapshot = {
            "yunat***": {
                "id": "yunat@example.com",
                "email": "yunat@example.com",
                "display_name": "yunat***",
                "status": "running",
                "task": "Desktop Searches",
                "progress": 12,
                "progress_total": 30,
                "points": 775,
            }
        }
        account_logs_snapshot = {
            "yunat***": [
                {"time": "16:40:08", "level": "info", "message": "Stopped GPM profile"}
            ]
        }

        profiles = _build_profile_views(accounts_snapshot, account_logs_snapshot)

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["id"], "yunat@example.com")
        self.assertEqual(profiles[0]["status"], "running")
        self.assertEqual(profiles[0]["progress_percent"], 40)
        self.assertEqual(profiles[0]["last_message"], "Stopped GPM profile")
        self.assertTrue(profiles[0]["has_logs"])

    def test_profile_summary_counts_statuses(self):
        summary = _build_profile_summary([
            {"status": "running", "points": 100, "has_logs": True},
            {"status": "done", "points": 200, "has_logs": False},
            {"status": "error", "points": 0, "has_logs": True},
            {"status": "idle", "points": 50, "has_logs": False},
        ])

        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["running"], 1)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["idle"], 1)
        self.assertEqual(summary["profiles_with_logs"], 2)
        self.assertEqual(summary["total_points"], 350)

    def test_mobile_verification_prefers_mobile_runtime_family(self):
        desktop_runtime = build_runtime_descriptor("gpm_desktop", "desk-1", "desktop")
        mobile_runtime = build_runtime_descriptor("gpm_mobile", "mob-1", "mobile")

        selected = choose_search_verification_source(
            "mobile",
            desktop_runtime=desktop_runtime,
            mobile_runtime=mobile_runtime,
        )

        self.assertEqual(selected["family"], "gpm_mobile")
        self.assertEqual(selected["source_id"], "mob-1")

    def test_desktop_verification_stays_on_desktop_runtime_family(self):
        desktop_runtime = build_runtime_descriptor("native_edge", "http://127.0.0.1:9330", "desktop")
        mobile_runtime = build_runtime_descriptor("gpm_mobile", "mob-1", "mobile")

        selected = choose_search_verification_source(
            "desktop",
            desktop_runtime=desktop_runtime,
            mobile_runtime=mobile_runtime,
        )

        self.assertEqual(selected["family"], "native_edge")

    def test_mobile_verification_does_not_fallback_to_desktop_runtime_family(self):
        desktop_runtime = build_runtime_descriptor("gpm_desktop", "desk-1", "desktop")

        selected = choose_search_verification_source(
            "mobile",
            desktop_runtime=desktop_runtime,
            mobile_runtime=None,
        )

        self.assertIsNone(selected)

    def test_desktop_verification_does_not_fallback_to_mobile_runtime_family(self):
        mobile_runtime = build_runtime_descriptor("gpm_mobile", "mob-1", "mobile")

        selected = choose_search_verification_source(
            "desktop",
            desktop_runtime=None,
            mobile_runtime=mobile_runtime,
        )

        self.assertIsNone(selected)

    def test_invalidate_runtime_attachment_clears_stale_cdp_url(self):
        runtime = build_runtime_descriptor("gpm_desktop", "profile-1", "desktop")

        attach_runtime, cdp_url, invalidated = invalidate_runtime_attachment(
            True,
            "http://127.0.0.1:9555",
            runtime,
            reason="desktop_gpm_profile_stopped_before_mobile_pass",
        )

        self.assertFalse(attach_runtime)
        self.assertEqual(cdp_url, "")
        self.assertTrue(invalidated["invalidated"])
        self.assertEqual(
            invalidated["invalid_reason"],
            "desktop_gpm_profile_stopped_before_mobile_pass",
        )

    def test_unverified_search_tracks_are_reported_separately_from_deficits(self):
        snapshot = {
            "search_status": {
                "pc_current": 39,
                "pc_max": 90,
                "mobile_current": 0,
                "mobile_max": 60,
            },
            "search_verification": {
                "desktop": {"verified": True},
                "mobile": {"verified": False, "reason": "runtime_unavailable"},
            },
        }

        self.assertEqual(
            describe_search_remaining_items(snapshot),
            ["Desktop 39/90", "Mobile unverified from original runtime"],
        )

    def test_dashboard_remaining_items_can_ignore_mobile_app_but_not_edge_streak(self):
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

        self.assertEqual(_describe_remaining_items(snapshot), ["Edge Minutes 0/30"])

    def test_dashboard_remaining_items_reports_missing_daily_set_when_no_actionable_daily_task_is_scanned(self):
        snapshot = {
            "search_status": {},
            "task_overview": {"daily_set": {"completed": 2, "total": 3}},
            "category_status": {"more_promo": {"completed": 0, "total": 1}},
            "pending_by_category": {"more_promo": ["Montreal's Winter Glow"]},
            "pending_tasks": ["Montreal's Winter Glow"],
        }

        self.assertEqual(_describe_remaining_items(snapshot), ["Daily Set 2/3", "Task: Montreal's Winter Glow"])

    def test_dashboard_remaining_items_reports_daily_set_when_actionable_daily_task_is_scanned(self):
        snapshot = {
            "search_status": {},
            "task_overview": {"daily_set": {"completed": 2, "total": 3}},
            "category_status": {"daily_set": {"completed": 0, "total": 1}},
            "pending_by_category": {"daily_set": ["King of Rock?"]},
            "pending_tasks": ["King of Rock?"],
        }

        self.assertEqual(
            _describe_remaining_items(snapshot),
            ["Daily Set 2/3", "Task: King of Rock?"],
        )

    def test_dashboard_remaining_items_ignores_implausible_daily_set_counts(self):
        snapshot = {
            "search_status": {},
            "task_overview": {"daily_set": {"completed": 24, "total": 999}},
            "category_status": {"daily_set": {"completed": 0, "total": 1}},
            "pending_by_category": {"daily_set": ["Bogus"]},
            "pending_tasks": ["Bogus"],
        }

        self.assertEqual(_describe_remaining_items(snapshot), ["Task: Bogus"])

    def test_dashboard_reconcile_applies_bing_app_override_without_daily_set_proof(self):
        snapshot = {"task_overview": {}, "pending_tasks": []}
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "ignore_bing_app_checkin": True,
                "ignore_edge_streak": True,
            },
        )

        self.assertTrue(reconciled["reporting_overrides"]["ignore_bing_app_checkin"])
        self.assertNotIn("ignore_edge_streak", reconciled["reporting_overrides"])

    def test_dashboard_remaining_items_reports_visible_edge_gap_even_with_ignore_proof(self):
        snapshot = {
            "search_status": {},
            "task_overview": {
                "daily_set": {"completed": 3, "total": 3},
                "streaks": {
                    "edge": {"exists": True, "done": False, "minutes": 0, "target": 30},
                },
            },
            "reporting_overrides": {"ignore_edge_streak": True},
            "pending_tasks": [],
        }

        self.assertEqual(_describe_remaining_items(snapshot), ["Edge Minutes 0/30"])

    def test_dashboard_reconcile_does_not_hide_visible_daily_or_edge_gaps(self):
        snapshot = {
            "task_overview": {
                "daily_set": {"completed": 2, "total": 3},
                "streaks": {
                    "edge": {"exists": True, "done": False, "minutes": 0, "target": 30},
                },
            },
            "category_status": {"daily_set": {"completed": 2, "total": 3}},
            "pending_tasks": [],
            "pending_by_category": {"daily_set": []},
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "daily_set_complete": True,
                "daily_set_progress_completed": 3,
                "daily_set_progress_total": 3,
                "edge_streak_verified_exists": True,
                "edge_streak_verified_minutes": 35,
                "edge_streak_verified_target": 30,
                "edge_streak_verified_done": True,
            },
        )

        self.assertEqual(reconciled["task_overview"]["daily_set"], {"completed": 2, "total": 3})
        self.assertFalse(reconciled["task_overview"]["streaks"]["edge"].get("done", False))
        self.assertEqual(
            _describe_remaining_items(reconciled),
            ["Daily Set 2/3", "Edge Minutes 0/30"],
        )

    def test_dashboard_reconcile_applies_partial_daily_set_progress(self):
        snapshot = {
            "task_overview": {"daily_set": {"completed": 1, "total": 3}},
            "category_status": {"daily_set": {"completed": 1, "total": 3}},
            "pending_tasks": ["Task A", "Task B", "Task C"],
            "pending_by_category": {"daily_set": ["Task A", "Task B", "Task C"]},
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "daily_set_progress_completed": 2,
                "daily_set_progress_total": 3,
                "daily_set_titles": ["Task A"],
            },
        )

        self.assertEqual(reconciled["task_overview"]["daily_set"]["completed"], 2)
        self.assertEqual(reconciled["category_status"]["daily_set"]["completed"], 2)
        self.assertEqual(reconciled["pending_tasks"], ["Task B", "Task C"])
        self.assertEqual(reconciled["pending_by_category"]["daily_set"], ["Task B", "Task C"])

    def test_dashboard_reconcile_full_daily_set_progress_clears_category(self):
        snapshot = {
            "task_overview": {"daily_set": {"completed": 1, "total": 3}},
            "category_status": {"daily_set": {"completed": 1, "total": 3}},
            "pending_tasks": ["Task A", "Task B"],
            "pending_by_category": {"daily_set": ["Task A", "Task B"]},
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "daily_set_complete": True,
                "daily_set_progress_completed": 3,
                "daily_set_progress_total": 3,
                "daily_set_titles": ["Task A", "Task B"],
            },
        )

        self.assertEqual(reconciled["task_overview"]["daily_set"]["completed"], 3)
        self.assertEqual(reconciled["category_status"]["daily_set"]["completed"], 3)
        self.assertEqual(reconciled["pending_tasks"], [])
        self.assertEqual(reconciled["pending_by_category"]["daily_set"], [])

    def test_dashboard_reconcile_can_upgrade_counts_from_progress_only(self):
        snapshot = {
            "task_overview": {"daily_set": {"completed": 1, "total": 3}},
            "category_status": {"daily_set": {"completed": 1, "total": 3}},
            "pending_tasks": [],
            "pending_by_category": {"daily_set": []},
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "daily_set_progress_completed": 3,
                "daily_set_progress_total": 3,
                "daily_set_titles": [],
            },
        )

        self.assertEqual(reconciled["task_overview"]["daily_set"]["completed"], 3)
        self.assertEqual(reconciled["category_status"]["daily_set"]["completed"], 3)

    def test_dashboard_reconcile_merges_stronger_edge_streak_proof(self):
        snapshot = {
            "task_overview": {"streaks": {"edge": {"exists": True, "minutes": 10, "target": 30, "done": False}}},
            "pending_tasks": [],
            "pending_by_category": {},
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "edge_streak_verified_exists": True,
                "edge_streak_verified_minutes": 24,
                "edge_streak_verified_target": 30,
                "edge_streak_verified_done": False,
            },
        )

        self.assertEqual(reconciled["task_overview"]["streaks"]["edge"]["minutes"], 24)
        self.assertEqual(reconciled["task_overview"]["streaks"]["edge"]["target"], 30)
        self.assertFalse(reconciled["task_overview"]["streaks"]["edge"]["done"])

    def test_dashboard_reconcile_marks_edge_done_from_verified_proof(self):
        snapshot = {
            "task_overview": {"streaks": {"edge": {"exists": True, "minutes": 15, "target": 30, "done": False}}},
            "pending_tasks": [],
            "pending_by_category": {},
        }
        reconciled = _reconcile_verification_with_session_proof(
            snapshot,
            {
                "edge_streak_verified_exists": True,
                "edge_streak_verified_minutes": 30,
                "edge_streak_verified_target": 30,
                "edge_streak_verified_done": True,
            },
        )

        self.assertEqual(reconciled["task_overview"]["streaks"]["edge"]["minutes"], 30)
        self.assertTrue(reconciled["task_overview"]["streaks"]["edge"]["done"])


class DashboardStatusAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_search_status_uses_task_detector_fallback_before_retry(self):
        settings = {
            "mobile_searches": 60,
            "mobile_credit_recheck_attempts": 2,
            "mobile_credit_recheck_delay_seconds": 1,
        }
        ambiguous = {
            "pc_current": 0,
            "pc_max": 0,
            "mobile_current": 0,
            "mobile_max": 0,
            "edge_current": 0,
            "edge_max": 0,
            "total_points": 3454,
        }
        page = SimpleNamespace(url="https://rewards.bing.com/about")
        searcher = AsyncMock()
        searcher.get_search_points_status = AsyncMock(return_value=ambiguous)

        with patch(
            "src.dashboard.TaskDetector.get_all_tasks",
            new=AsyncMock(return_value={
                "searches": {
                    "pc_current": 66,
                    "pc_max": 90,
                    "mobile_current": 57,
                    "mobile_max": 60,
                },
                "total_points": 3454,
            }),
        ), patch("src.dashboard.asyncio.sleep", new=AsyncMock()):
            status = await _read_search_status_with_mobile_recheck(
                searcher,
                page,
                settings,
            )

        self.assertEqual(status["pc_current"], 66)
        self.assertEqual(status["pc_max"], 90)
        self.assertEqual(status["mobile_current"], 57)
        self.assertEqual(status["mobile_max"], 60)
        searcher.get_search_points_status.assert_awaited_once()

    async def test_mobile_credit_postcheck_stops_after_total_points_advance(self):
        baseline = {
            "pc_current": 0,
            "pc_max": 0,
            "mobile_current": 0,
            "mobile_max": 0,
            "edge_current": 0,
            "edge_max": 0,
            "total_points": 3397,
        }
        after = {
            "pc_current": 0,
            "pc_max": 0,
            "mobile_current": 0,
            "mobile_max": 0,
            "edge_current": 0,
            "edge_max": 0,
            "total_points": 3454,
        }

        with patch(
            "src.dashboard._read_search_status_with_mobile_recheck",
            new=AsyncMock(return_value=after),
        ) as read_status, patch("src.dashboard.asyncio.sleep", new=AsyncMock()):
            status = await _wait_for_mobile_credit_update(
                AsyncMock(),
                object(),
                {
                    "mobile_credit_postcheck_attempts": 3,
                    "mobile_credit_postcheck_delay_seconds": 6,
                },
                baseline_status=baseline,
            )

        self.assertEqual(status["total_points"], 3454)
        read_status.assert_awaited_once()

    async def test_live_gpm_desktop_requirement_resolution_falls_back_to_original_probe_when_dedicated_probe_stays_ambiguous(self):
        settings = {"desktop_searches": 30}
        baseline = {"pc_current": 0, "pc_max": 0}
        desktop_runtime = build_runtime_descriptor(
            "gpm_desktop",
            "desk-1",
            "desktop",
            cdp_url="http://127.0.0.1:9555",
            live_for_account_run=True,
        )
        original_probe = AsyncMock(return_value=(
            {"pc_current": 90, "pc_max": 90, "total_points": 9021},
            {"verified": True},
        ))
        dedicated_probe = AsyncMock(return_value={"pc_current": 0, "pc_max": 0})

        with patch("src.dashboard._probe_search_status_in_mode", new=dedicated_probe), \
             patch("src.dashboard._read_search_status_for_runtime_descriptor", new=original_probe):
            resolved = await _resolve_desktop_search_requirement(
                settings,
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                baseline,
                desktop_runtime,
            )

        self.assertEqual(resolved["pc_current"], 90)
        self.assertEqual(resolved["pc_max"], 90)
        dedicated_probe.assert_awaited_once()
        original_probe.assert_awaited_once()

    async def test_live_gpm_desktop_requirement_uses_dedicated_probe_when_available(self):
        settings = {"desktop_searches": 30}
        baseline = {"pc_current": 0, "pc_max": 0}
        desktop_runtime = build_runtime_descriptor(
            "gpm_desktop",
            "desk-1",
            "desktop",
            cdp_url="http://127.0.0.1:9555",
            live_for_account_run=True,
        )
        dedicated_probe = AsyncMock(return_value={
            "pc_current": 84,
            "pc_max": 90,
            "edge_current": 0,
            "edge_max": 0,
            "total_points": 9021,
        })

        with patch("src.dashboard._probe_search_status_in_mode", new=dedicated_probe):
            resolved = await _resolve_desktop_search_requirement(
                settings,
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                baseline,
                desktop_runtime,
            )

        self.assertEqual(resolved["pc_current"], 84)
        self.assertEqual(resolved["pc_max"], 90)
        dedicated_probe.assert_awaited_once()

    async def test_desktop_requirement_resolution_skips_when_probe_finds_full_credits(self):
        settings = {"desktop_searches": 30}
        baseline = {"pc_current": 0, "pc_max": 0}
        desktop_runtime = build_runtime_descriptor("gpm_desktop", "desk-1", "desktop")

        with patch(
            "src.dashboard._read_search_status_for_runtime_descriptor",
            new=AsyncMock(return_value=(
                {"pc_current": 90, "pc_max": 90, "total_points": 9021},
                {"verified": True},
            )),
        ):
            resolved = await _resolve_desktop_search_requirement(
                settings,
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                baseline,
                desktop_runtime,
            )

        remaining_points = max(0, resolved["pc_max"] - resolved["pc_current"])
        planned_searches = (remaining_points + 2) // 3 if remaining_points > 0 else 0
        self.assertEqual(planned_searches, 0)

    async def test_desktop_requirement_resolution_runs_only_missing_searches(self):
        settings = {"desktop_searches": 30}
        baseline = {"pc_current": 0, "pc_max": 0}
        desktop_runtime = build_runtime_descriptor("native_edge", "http://127.0.0.1:9330", "desktop")

        with patch(
            "src.dashboard._read_search_status_for_runtime_descriptor",
            new=AsyncMock(return_value=(
                {"pc_current": 39, "pc_max": 90, "total_points": 9036},
                {"verified": True},
            )),
        ):
            resolved = await _resolve_desktop_search_requirement(
                settings,
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                baseline,
                desktop_runtime,
            )

        remaining_points = max(0, resolved["pc_max"] - resolved["pc_current"])
        planned_searches = (remaining_points + 2) // 3 if remaining_points > 0 else 0
        self.assertEqual(planned_searches, 17)

    async def test_runtime_descriptor_reader_uses_browser_manager_without_name_error(self):
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            start=AsyncMock(),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        ctx = object()
        page = object()

        with patch("src.browser.BrowserManager", return_value=browser_mgr), \
             patch("src.dashboard._open_account_context", new=AsyncMock(return_value=(ctx, page))), \
             patch("src.dashboard._read_search_status_with_mobile_recheck", new=AsyncMock(return_value={
                 "pc_current": 39,
                 "pc_max": 90,
                 "mobile_current": 0,
                 "mobile_max": 0,
                 "edge_current": 0,
                 "edge_max": 0,
                 "total_points": 9036,
             })), \
             patch("src.dashboard._persist_storage_state", new=AsyncMock()):
            status, meta = await _read_search_status_for_runtime_descriptor(
                {"use_stealth": False},
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                build_runtime_descriptor("managed_edge", "test@example.com", "desktop"),
            )

        self.assertEqual(status["pc_current"], 39)
        self.assertTrue(meta["verified"])
        browser_mgr.close.assert_awaited_once()

    async def test_runtime_descriptor_reader_skips_mobile_recheck_for_desktop_track(self):
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            start=AsyncMock(),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        ctx = object()
        page = object()
        read_status = AsyncMock(return_value={
            "pc_current": 66,
            "pc_max": 90,
            "mobile_current": 0,
            "mobile_max": 0,
            "edge_current": 0,
            "edge_max": 0,
            "total_points": 3454,
        })

        with patch("src.browser.BrowserManager", return_value=browser_mgr), \
             patch("src.dashboard._open_account_context", new=AsyncMock(return_value=(ctx, page))), \
             patch("src.dashboard._read_search_status_with_mobile_recheck", new=read_status), \
             patch("src.dashboard._persist_storage_state", new=AsyncMock()):
            _status, meta = await _read_search_status_for_runtime_descriptor(
                {"use_stealth": False},
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                build_runtime_descriptor("managed_edge", "test@example.com", "desktop"),
            )

        self.assertTrue(meta["verified"])
        self.assertFalse(read_status.await_args.kwargs["recheck_mobile"])

    async def test_runtime_descriptor_reader_marks_ambiguous_zero_zero_as_unverified(self):
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            start=AsyncMock(),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        ctx = object()
        page = object()

        with patch("src.browser.BrowserManager", return_value=browser_mgr), \
             patch("src.dashboard._open_account_context", new=AsyncMock(return_value=(ctx, page))), \
             patch("src.dashboard._read_search_status_with_mobile_recheck", new=AsyncMock(return_value={
                 "pc_current": 0,
                 "pc_max": 0,
                 "mobile_current": 0,
                 "mobile_max": 0,
                 "edge_current": 0,
                 "edge_max": 0,
                 "total_points": 3454,
             })), \
             patch("src.dashboard._persist_storage_state", new=AsyncMock()):
            status, meta = await _read_search_status_for_runtime_descriptor(
                {"use_stealth": False},
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                build_runtime_descriptor("managed_edge", "test@example.com", "desktop"),
            )

        self.assertEqual(status["pc_current"], 0)
        self.assertFalse(meta["verified"])
        self.assertIn("ambiguous", meta["reason"])

    async def test_patchright_mobile_runtime_reader_reuses_mobile_family_without_emulation(self):
        ctx = object()
        page = SimpleNamespace(context=ctx)
        patchright_pw = SimpleNamespace(stop=AsyncMock())
        patchright_browser = SimpleNamespace(close=AsyncMock())
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            create_mobile_patchright=AsyncMock(return_value=(patchright_pw, patchright_browser, ctx, page)),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        login_mgr = AsyncMock()
        login_mgr.is_logged_in = AsyncMock(return_value=True)

        with patch("src.browser.BrowserManager", return_value=browser_mgr), \
             patch("src.dashboard._read_search_status_with_mobile_recheck", new=AsyncMock(return_value={
                 "pc_current": 0,
                 "pc_max": 0,
                 "mobile_current": 60,
                 "mobile_max": 60,
                 "edge_current": 0,
                 "edge_max": 0,
                 "total_points": 9036,
             })), \
             patch("src.dashboard._persist_storage_state", new=AsyncMock()):
            status, meta = await _read_search_status_for_runtime_descriptor(
                {"use_stealth": False},
                {"email": "test@example.com", "password": "pw"},
                None,
                login_mgr,
                AsyncMock(),
                Path("state.json"),
                build_runtime_descriptor("patchright_mobile", "test@example.com", "mobile"),
            )

        self.assertEqual(status["mobile_current"], 60)
        self.assertTrue(meta["verified"])
        browser_mgr.create_mobile_patchright.assert_awaited_once()
        browser_mgr.toggle_mobile_emulation.assert_awaited_once()
        patchright_browser.close.assert_awaited_once()
        patchright_pw.stop.assert_awaited_once()
        browser_mgr.close.assert_awaited_once()

    async def test_live_gpm_runtime_reader_reuses_existing_cdp_without_restart(self):
        browser_mgr = SimpleNamespace(
            set_account=lambda _email: None,
            start_connected_edge=AsyncMock(),
            toggle_mobile_emulation=AsyncMock(),
            close=AsyncMock(),
        )
        ctx = object()
        page = object()
        start_gpm = AsyncMock()

        with patch("src.browser.BrowserManager", return_value=browser_mgr), \
             patch("src.dashboard._start_gpm_profile", new=start_gpm), \
             patch("src.dashboard._open_account_context", new=AsyncMock(return_value=(ctx, page))), \
             patch("src.dashboard._read_search_status_with_mobile_recheck", new=AsyncMock(return_value={
                 "pc_current": 90,
                 "pc_max": 90,
                 "mobile_current": 0,
                 "mobile_max": 0,
                 "edge_current": 0,
                 "edge_max": 0,
                 "total_points": 9021,
             })), \
             patch("src.dashboard._persist_storage_state", new=AsyncMock()):
            status, meta = await _read_search_status_for_runtime_descriptor(
                {"use_stealth": False},
                {"email": "test@example.com", "password": "pw"},
                None,
                AsyncMock(),
                AsyncMock(),
                Path("state.json"),
                build_runtime_descriptor(
                    "gpm_desktop",
                    "profile-1",
                    "desktop",
                    cdp_url="http://127.0.0.1:9555",
                    live_for_account_run=True,
                ),
            )

        self.assertEqual(status["pc_current"], 90)
        self.assertTrue(meta["verified"])
        browser_mgr.start_connected_edge.assert_awaited_once_with("http://127.0.0.1:9555")
        start_gpm.assert_not_awaited()

    async def test_collect_final_verification_merges_task_overview_search_counters(self):
        page = object()
        scanner = SimpleNamespace(_fetch_all_tasks=AsyncMock(return_value=[]))

        with patch(
            "src.dashboard.TaskDetector.get_all_tasks",
            new=AsyncMock(return_value={
                "searches": {
                    "pc_current": 66,
                    "pc_max": 90,
                    "mobile_current": 57,
                    "mobile_max": 60,
                },
                "total_points": 3454,
                "daily_set": {"completed": 3, "total": 3},
                "streaks": {},
            }),
        ), patch("src.dashboard.UniversalTaskScanner", return_value=scanner):
            snapshot = await _collect_final_verification(
                page,
                AsyncMock(),
                SimpleNamespace(),
                {},
                search_status_override={
                    "pc_current": 0,
                    "pc_max": 0,
                    "mobile_current": 0,
                    "mobile_max": 0,
                    "edge_current": 0,
                    "edge_max": 0,
                    "total_points": 3454,
                },
            )

        self.assertEqual(snapshot["search_status"]["pc_current"], 66)
        self.assertEqual(snapshot["search_status"]["pc_max"], 90)
        self.assertEqual(snapshot["search_status"]["mobile_current"], 57)
        self.assertEqual(snapshot["search_status"]["mobile_max"], 60)
        evidence = snapshot["verification_evidence"]
        self.assertEqual(evidence["search_status"]["source"], "override")
        self.assertEqual(evidence["task_overview"]["source"], "rewards_api")
        self.assertEqual(evidence["dom_scan"]["source"], "dashboard_dom")
        self.assertEqual(evidence["dom_scan"]["selector_health"]["task_count"], 0)

    async def test_collect_final_verification_keeps_stronger_override_counters(self):
        page = object()
        scanner = SimpleNamespace(_fetch_all_tasks=AsyncMock(return_value=[]))

        with patch(
            "src.dashboard.TaskDetector.get_all_tasks",
            new=AsyncMock(return_value={
                "searches": {
                    "pc_current": 87,
                    "pc_max": 90,
                    "mobile_current": 60,
                    "mobile_max": 60,
                },
                "total_points": 14593,
                "daily_set": {"completed": 3, "total": 3},
                "streaks": {},
            }),
        ), patch("src.dashboard.UniversalTaskScanner", return_value=scanner):
            snapshot = await _collect_final_verification(
                page,
                AsyncMock(),
                SimpleNamespace(),
                {},
                search_status_override={
                    "pc_current": 90,
                    "pc_max": 90,
                    "mobile_current": 60,
                    "mobile_max": 60,
                    "edge_current": 0,
                    "edge_max": 0,
                    "total_points": 14603,
                },
            )

        self.assertEqual(snapshot["search_status"]["pc_current"], 90)
        self.assertEqual(snapshot["search_status"]["pc_max"], 90)
        self.assertEqual(snapshot["search_status"]["total_points"], 14603)

    def test_dashboard_remaining_items_does_not_ignore_non_actionable_daily_set_gap(self):
        snapshot = {
            "search_status": {},
            "task_overview": {"daily_set": {"completed": 2, "total": 3}},
            "category_status": {"daily_set": {"completed": 2, "total": 3}},
            "pending_by_category": {"daily_set": []},
            "pending_tasks": [],
            "reporting_overrides": {"ignore_daily_set_gap": True},
        }

        self.assertEqual(_describe_remaining_items(snapshot), ["Daily Set 2/3"])


    def test_daily_set_recovery_candidates_uses_incomplete_inventory_urls(self):
        from src.dashboard import _daily_set_recovery_candidates

        snapshot = {
            "verification_tasks": [
                {
                    "title": "Done Daily",
                    "category": "daily_set",
                    "is_complete": True,
                    "is_locked": False,
                    "destination_url": "https://example.test/done",
                },
                {
                    "title": "Starry Visionary?",
                    "category": "daily_set",
                    "is_complete": False,
                    "is_locked": False,
                    "destination_url": "https://example.test/daily",
                },
                {
                    "title": "Promo",
                    "category": "more_promo",
                    "is_complete": False,
                    "is_locked": False,
                    "destination_url": "https://example.test/promo",
                },
            ],
            "pending_by_category": {"daily_set": ["Fallback Daily"]},
        }

        self.assertEqual(
            _daily_set_recovery_candidates(snapshot),
            [
                {"title": "Starry Visionary?", "destination_url": "https://example.test/daily"},
                {"title": "Fallback Daily", "destination_url": ""},
            ],
        )

    async def test_final_daily_set_repair_updates_session_proof(self):
        from src.dashboard import _repair_daily_set_gap_from_final_verification

        page = object()
        snapshot = {"task_overview": {"daily_set": {"completed": 2, "total": 3}}}
        session_proofs = {}
        completer = SimpleNamespace(
            complete_daily_set=AsyncMock(return_value={
                "progress_completed": 3,
                "progress_total": 3,
                "category_proven": True,
            }),
            _read_daily_set_progress=AsyncMock(return_value={"completed": 3, "total": 3, "category_proven": True}),
        )

        with patch("src.daily_set.DailySetCompleter", return_value=completer):
            repaired = await _repair_daily_set_gap_from_final_verification(
                page,
                SimpleNamespace(),
                {},
                snapshot,
                session_proofs,
            )

        self.assertTrue(repaired)
        self.assertTrue(session_proofs["daily_set_complete"])
        self.assertEqual(session_proofs["daily_set_progress_completed"], 3)
        self.assertEqual(session_proofs["daily_set_progress_total"], 3)

    async def test_final_daily_set_repair_keeps_no_target_gap_incomplete(self):
        from src.dashboard import _repair_daily_set_gap_from_final_verification

        page = object()
        snapshot = {"task_overview": {"daily_set": {"completed": 2, "total": 3}}}
        session_proofs = {}
        completer = SimpleNamespace(
            complete_daily_set=AsyncMock(return_value={
                "completed": 0,
                "total": 0,
                "progress_completed": 2,
                "progress_total": 3,
                "category_proven": False,
                "panel_control_failed": True,
            }),
            _read_daily_set_progress=AsyncMock(return_value={"completed": 2, "total": 3, "category_proven": False}),
            try_direct_daily_set_url=AsyncMock(),
        )

        with patch("src.daily_set.DailySetCompleter", return_value=completer):
            repaired = await _repair_daily_set_gap_from_final_verification(
                page,
                SimpleNamespace(),
                {},
                snapshot,
                session_proofs,
            )

        self.assertTrue(repaired)
        self.assertNotIn("ignore_daily_set_gap", session_proofs)
        self.assertFalse(session_proofs.get("daily_set_complete", False))
        self.assertEqual(session_proofs["daily_set_progress_completed"], 2)
        self.assertEqual(session_proofs["daily_set_progress_total"], 3)
        completer.try_direct_daily_set_url.assert_not_called()

    async def test_final_daily_set_repair_tries_direct_inventory_url(self):
        from src.dashboard import _repair_daily_set_gap_from_final_verification

        page = object()
        snapshot = {
            "task_overview": {"daily_set": {"completed": 2, "total": 3}},
            "verification_tasks": [{
                "title": "Starry Visionary?",
                "category": "daily_set",
                "is_complete": False,
                "is_locked": False,
                "destination_url": "https://example.test/daily",
            }],
        }
        session_proofs = {}
        completer = SimpleNamespace(
            complete_daily_set=AsyncMock(return_value={
                "completed": 0,
                "total": 0,
                "progress_completed": 2,
                "progress_total": 3,
                "category_proven": False,
                "panel_control_failed": True,
            }),
            _read_daily_set_progress=AsyncMock(side_effect=[
                {"completed": 2, "total": 3, "category_proven": False},
                {"completed": 3, "total": 3, "category_proven": True},
            ]),
            try_direct_daily_set_url=AsyncMock(return_value={
                "attempted": True,
                "progress_completed": 3,
                "progress_total": 3,
                "category_proven": True,
            }),
            extract_hidden_daily_set_urls=AsyncMock(return_value=[]),
        )

        with patch("src.daily_set.DailySetCompleter", return_value=completer):
            repaired = await _repair_daily_set_gap_from_final_verification(
                page,
                SimpleNamespace(),
                {},
                snapshot,
                session_proofs,
            )

        self.assertTrue(repaired)
        completer.try_direct_daily_set_url.assert_awaited_once_with(page, "https://example.test/daily", "Starry Visionary?")
        self.assertTrue(session_proofs["daily_set_complete"])
        self.assertEqual(session_proofs["daily_set_progress_completed"], 3)
        self.assertEqual(session_proofs["daily_set_progress_total"], 3)


    async def test_final_daily_set_repair_tries_hidden_extracted_url(self):
        from src.dashboard import _repair_daily_set_gap_from_final_verification

        page = object()
        snapshot = {"task_overview": {"daily_set": {"completed": 2, "total": 3}}}
        session_proofs = {}
        completer = SimpleNamespace(
            complete_daily_set=AsyncMock(return_value={
                "completed": 0,
                "total": 0,
                "progress_completed": 2,
                "progress_total": 3,
                "category_proven": False,
                "panel_control_failed": True,
            }),
            _read_daily_set_progress=AsyncMock(side_effect=[
                {"completed": 2, "total": 3, "category_proven": False},
                {"completed": 3, "total": 3, "category_proven": True},
            ]),
            extract_hidden_daily_set_urls=AsyncMock(return_value=[{
                "title": "Hidden Daily",
                "destination_url": "https://example.test/hidden-daily",
                "source": "script",
            }]),
            try_direct_daily_set_url=AsyncMock(return_value={
                "attempted": True,
                "progress_completed": 3,
                "progress_total": 3,
                "category_proven": True,
            }),
        )

        with patch("src.daily_set.DailySetCompleter", return_value=completer):
            repaired = await _repair_daily_set_gap_from_final_verification(
                page,
                SimpleNamespace(),
                {},
                snapshot,
                session_proofs,
            )

        self.assertTrue(repaired)
        completer.extract_hidden_daily_set_urls.assert_awaited_once_with(page)
        completer.try_direct_daily_set_url.assert_awaited_once_with(page, "https://example.test/hidden-daily", "Hidden Daily")
        self.assertTrue(session_proofs["daily_set_complete"])

    async def test_collect_final_verification_honors_recent_verified_task_cache(self):
        page = object()
        task = RewardsTask(
            id="winter-dublin",
            title="Winter in Dublin",
            category="more_promo",
            task_type="search",
            is_complete=False,
        )
        scanner = SimpleNamespace(_fetch_all_tasks=AsyncMock(return_value=[task]))

        with patch(
            "src.dashboard.TaskDetector.get_all_tasks",
            new=AsyncMock(return_value={"searches": {}, "total_points": 100, "streaks": {}}),
        ), patch("src.dashboard.UniversalTaskScanner", return_value=scanner), patch(
            "src.dashboard._load_task_state",
            return_value={"test@example.com": {"visited_tasks": {"winter-dublin": "2026-04-25T12:00:00"}}},
        ), patch("src.dashboard.datetime") as fake_datetime:
            fake_datetime.now.return_value = fake_datetime.fromisoformat("2026-04-25T12:30:00")
            fake_datetime.fromisoformat.side_effect = __import__("datetime").datetime.fromisoformat
            snapshot = await _collect_final_verification(
                page,
                AsyncMock(),
                SimpleNamespace(),
                {},
                account_email="test@example.com",
                search_status_override={"total_points": 100},
            )

        self.assertEqual(snapshot["pending_tasks"], ["Winter in Dublin"])
        self.assertEqual(snapshot["category_status"]["more_promo"], {"completed": 0, "total": 1})

    async def test_dead_desktop_page_reacquires_from_live_cdp(self):
        dead_page = SimpleNamespace(
            is_closed=lambda: True,
            context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False)),
        )
        live_page = SimpleNamespace(
            is_closed=lambda: False,
            context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True)),
            evaluate=AsyncMock(return_value=1),
        )
        reopen = AsyncMock(return_value=("ctx-live", live_page))

        with patch("src.dashboard._open_account_context", new=reopen):
            ctx, page = await _ensure_usable_desktop_search_page(
                {"diagnostic_logging": True},
                object(),
                AsyncMock(),
                {"email": "test@example.com", "password": "pw"},
                None,
                Path("state.json"),
                build_runtime_descriptor(
                    "gpm_desktop",
                    "profile-1",
                    "desktop",
                    cdp_url="http://127.0.0.1:9555",
                    live_for_account_run=True,
                ),
                "ctx-dead",
                dead_page,
            )

        self.assertEqual(ctx, "ctx-live")
        self.assertIs(page, live_page)
        reopen.assert_awaited_once()

    async def test_dead_desktop_page_raises_clear_error_when_reacquire_fails(self):
        dead_page = SimpleNamespace(
            is_closed=lambda: True,
            context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False)),
        )

        with patch(
            "src.dashboard._open_account_context",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ), patch(
            "src.dashboard._start_gpm_profile_serialized",
            new=AsyncMock(side_effect=RuntimeError("restart boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be recovered"):
                await _ensure_usable_desktop_search_page(
                    {"diagnostic_logging": True},
                    object(),
                    AsyncMock(),
                    {"email": "test@example.com", "password": "pw"},
                    None,
                    Path("state.json"),
                    build_runtime_descriptor(
                        "gpm_desktop",
                        "profile-1",
                        "desktop",
                        cdp_url="http://127.0.0.1:9555",
                        live_for_account_run=True,
                    ),
                    "ctx-dead",
                    dead_page,
                )


if __name__ == "__main__":
    unittest.main()
