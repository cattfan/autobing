import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimePathTests(unittest.TestCase):
    def test_utils_runtime_paths_honor_autobing_home(self):
        root = Path(os.getcwd())
        env = os.environ.copy()
        env["AUTOBING_HOME"] = str(root / ".tmp-autobing-home")
        script = "from src import utils; print('PATH::' + str(utils.CONFIG_DIR)); print('PATH::' + str(utils.DATA_DIR)); print('PATH::' + str(utils.PROFILES_DIR))"

        result = subprocess.run(
            ["python", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

        lines = [Path(line.strip().removeprefix("PATH::")) for line in result.stdout.splitlines() if line.strip().startswith("PATH::")]
        self.assertEqual(lines[0], root / ".tmp-autobing-home" / "config")
        self.assertEqual(lines[1], root / ".tmp-autobing-home" / "data")
        self.assertEqual(lines[2], root / ".tmp-autobing-home" / "data" / "profiles")

    def test_settings_template_contains_no_live_secrets(self):
        content = Path("config/settings.example.json").read_text(encoding="utf-8")

        self.assertNotIn("sk-or-", content)
        self.assertNotIn("script.google.com/macros", content)
        self.assertNotIn("password\": \"", content)

    def test_local_settings_and_accounts_are_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "config/settings.json", "config/accounts.json", "config/accounts.json.enc"],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(result.stdout.strip(), "")

    def test_frontend_public_contains_no_personal_mock_data(self):
        public_dir = Path("autobing-app/public")
        forbidden_names = {"mock-dashboard-state.json", "mock-settings.json"}

        for name in forbidden_names:
            self.assertFalse((public_dir / name).exists(), f"{name} must not be bundled into the app")

        for path in public_dir.rglob("*.json") if public_dir.exists() else []:
            content = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("@gmail", content.lower())
            self.assertNotIn("@outlook", content.lower())
            self.assertNotIn("password", content.lower())

    def test_defaults_use_edge_chromium_without_gpm(self):
        from src.utils import get_default_settings

        settings = get_default_settings()

        self.assertEqual(settings["browser_type"], "chromium")
        self.assertTrue(settings["native_edge_runtime_enabled"])

    def test_browser_scanner_honors_autobing_config_dir(self):
        from src.browser_scanner import scan_profiles

        requested_urls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"data": [{"user_id": "profile-1", "name": "Profile One"}]}).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            requested_urls.append(request.full_url)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(
                json.dumps({"browser_type": "adspower", "browser_api_url": "http://127.0.0.1:5555/"}),
                encoding="utf-8",
            )

            output = io.StringIO()
            with patch.dict(os.environ, {"AUTOBING_CONFIG_DIR": tmp}, clear=False), patch(
                "src.browser_scanner.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as cm:
                    scan_profiles()

        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(requested_urls, ["http://127.0.0.1:5555/api/v1/user/list"])
        self.assertEqual(json.loads(output.getvalue()), [{"id": "profile-1", "name": "Profile One"}])


if __name__ == "__main__":
    unittest.main()
