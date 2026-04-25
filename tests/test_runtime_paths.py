import os
import subprocess
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
