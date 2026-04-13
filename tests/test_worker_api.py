import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.control_plane import BotRunRequest
from src.job_protocol import (
    JobSpec,
    StartJobCommand,
    WorkerCapabilities,
    WorkerHealth,
    parse_job_spec,
    start_job_to_run_request,
)
from src.worker_api import cli


class JobProtocolTests(unittest.TestCase):
    def test_parse_job_spec_normalizes_task_and_targets(self):
        spec = parse_job_spec(
            {
                "job_id": "job-1",
                "task": "unknown",
                "target_emails": ["user@example.com", " ", None],
                "secret_ref": "vault:main",
                "correlation_id": "corr-1",
            }
        )

        self.assertEqual(
            spec,
            JobSpec(
                job_id="job-1",
                task="all",
                target_emails=("user@example.com",),
                secret_ref="vault:main",
                correlation_id="corr-1",
            ),
        )

    def test_start_job_to_run_request_keeps_secret_outside_public_run_request(self):
        command = StartJobCommand(
            job=JobSpec(
                job_id="job-2",
                task="searches",
                target_emails=("user@example.com",),
                secret_ref="vault:main",
            )
        )

        request = start_job_to_run_request(command, "master-password")

        self.assertEqual(
            request,
            BotRunRequest(
                task="searches",
                password="master-password",
                target_emails=("user@example.com",),
            ),
        )

    def test_worker_capabilities_stay_run_scoped(self):
        capabilities = WorkerCapabilities().to_dict()

        self.assertEqual(
            capabilities["commands"],
            [
                "start_job",
                "cancel_job",
                "query_job",
                "subscribe_events",
                "health",
                "capabilities",
            ],
        )
        self.assertIn("gpm", capabilities["retained_sidecar_domains"])


class WorkerApiCliTests(unittest.TestCase):
    def test_health_command_prints_json(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("sys.stdout", stream):
                exit_code = cli(["health"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload, WorkerHealth().to_dict())

    def test_capabilities_command_prints_json(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("sys.stdout", stream):
                exit_code = cli(["capabilities"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["worker_kind"], "python-sidecar")

    def test_normalize_start_job_supports_file_input(self):
        raw = {
            "job_id": "job-3",
            "task": "promos",
            "target_emails": ["user@example.com"],
        }
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as handle:
            json.dump(raw, handle)
            file_path = handle.name

        try:
            with tempfile.TemporaryFile(mode="w+") as stream:
                with patch("sys.stdout", stream):
                    exit_code = cli(["normalize-start-job", "--file", file_path])
                    stream.seek(0)
                    payload = json.loads(stream.read())
        finally:
            Path(file_path).unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["command"], "start_job")
        self.assertEqual(payload["job"]["task"], "promos")

    def test_normalize_start_job_supports_direct_flags(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("sys.stdout", stream):
                exit_code = cli(
                    [
                        "normalize-start-job",
                        "--job-id",
                        "job-flags",
                        "--task",
                        "daily",
                        "--target-email",
                        "one@example.com",
                        "--target-email",
                        "two@example.com",
                        "--secret-ref",
                        "env:AUTOBING_SECRET",
                        "--correlation-id",
                        "corr-1",
                    ]
                )
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["job"]["job_id"], "job-flags")
        self.assertEqual(payload["job"]["task"], "daily")
        self.assertEqual(payload["job"]["target_emails"], ["one@example.com", "two@example.com"])
        self.assertEqual(payload["job"]["secret_ref"], "env:AUTOBING_SECRET")

    def test_start_job_uses_worker_store_and_returns_public_request(self):
        raw = {
            "job_id": "job-9",
            "task": "searches",
            "target_emails": ["user@example.com"],
        }

        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch(
                "src.worker_api.start_job_process",
                return_value={"job_id": "job-9", "status": "accepted", "pid": 4242},
            ) as start_job_process, patch("sys.stdout", stream):
                exit_code = cli(["start-job", "--json", json.dumps(raw)])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["job_id"], "job-9")
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["public_request"]["command"], "start_job")
        start_job_process.assert_called_once()

    def test_start_job_supports_direct_flags(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch(
                "src.worker_api.start_job_process",
                return_value={"job_id": "job-direct", "status": "accepted", "pid": 111},
            ) as start_job_process, patch("sys.stdout", stream):
                exit_code = cli(
                    [
                        "start-job",
                        "--job-id",
                        "job-direct",
                        "--task",
                        "all",
                        "--target-email",
                        "user@example.com",
                        "--secret-ref",
                        "env:AUTOBING_SECRET",
                    ]
                )
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["public_request"]["job"]["job_id"], "job-direct")
        self.assertEqual(payload["public_request"]["job"]["secret_ref"], "env:AUTOBING_SECRET")
        start_job_process.assert_called_once()

    def test_query_job_can_return_state_or_events(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("src.worker_api.read_state", return_value={"job_id": "job-1", "status": "running"}), \
                 patch("sys.stdout", stream):
                exit_code = cli(["query-job", "--job-id", "job-1"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "running")

        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch("src.worker_api.read_events", return_value=[{"job_id": "job-1", "event_type": "job_running"}]), \
                 patch("sys.stdout", stream):
                exit_code = cli(["query-job", "--job-id", "job-1", "--events"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload[0]["event_type"], "job_running")

    def test_subscribe_events_returns_existing_event_stream(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch(
                "src.worker_api.read_events",
                return_value=[{"job_id": "job-1", "event_type": "job_completed"}],
            ), patch("sys.stdout", stream):
                exit_code = cli(["subscribe-events", "--job-id", "job-1"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload[0]["event_type"], "job_completed")

    def test_cancel_job_returns_updated_state(self):
        with tempfile.TemporaryFile(mode="w+") as stream:
            with patch(
                "src.worker_api.cancel_job",
                return_value={"job_id": "job-1", "status": "cancelled"},
            ) as cancel_job_mock, patch("sys.stdout", stream):
                exit_code = cli(["cancel-job", "--job-id", "job-1"])
                stream.seek(0)
                payload = json.loads(stream.read())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "cancelled")
        cancel_job_mock.assert_called_once_with("job-1")
