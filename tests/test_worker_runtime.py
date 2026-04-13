import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.worker_runtime import _resolve_password


class WorkerRuntimeTests(unittest.TestCase):
    def test_resolve_password_prefers_direct_environment_variable(self):
        with patch.dict("os.environ", {"REWARDS_BOT_PASSWORD": "from-env"}, clear=False):
            password, error = _resolve_password(None)

        self.assertEqual(password, "from-env")
        self.assertIsNone(error)

    def test_resolve_password_supports_env_secret_ref(self):
        with patch.dict("os.environ", {"WORKER_SECRET": "from-ref"}, clear=True):
            password, error = _resolve_password("env:WORKER_SECRET")

        self.assertEqual(password, "from-ref")
        self.assertIsNone(error)

    def test_resolve_password_supports_file_secret_ref(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as handle:
            handle.write("from-file\n")
            file_path = handle.name

        try:
            password, error = _resolve_password(f"file:{file_path}")
        finally:
            Path(file_path).unlink(missing_ok=True)

        self.assertEqual(password, "from-file")
        self.assertIsNone(error)
        self.assertFalse(Path(file_path).exists())

    def test_resolve_password_reports_missing_secret_ref(self):
        with patch.dict("os.environ", {}, clear=True):
            password, error = _resolve_password(None)

        self.assertEqual(password, "")
        self.assertIn("REWARDS_BOT_PASSWORD", error or "")
