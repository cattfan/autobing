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
    _resolve_desktop_search_requirement,
    _select_mobile_runtime_strategy,
    _wait_for_mobile_credit_update,
)
from src.runtime_identity import (
    build_runtime_descriptor,
    choose_search_verification_source,
    describe_search_remaining_items,
    invalidate_runtime_attachment,
)


class DashboardStatusTests(unittest.TestCase):
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
        self.assertEqual(_account_timeout_seconds(0, 2), 4500.0)
        self.assertEqual(_account_timeout_seconds(1, 2), 4500.0)
        self.assertEqual(_account_timeout_seconds(2, 2), 9000.0)

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

    def test_dashboard_remaining_items_can_ignore_mobile_app_and_edge_streak(self):
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

    def test_dashboard_reconcile_applies_reporting_overrides_without_daily_set_proof(self):
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
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be reacquired from http://127.0.0.1:9555"):
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
