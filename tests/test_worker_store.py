import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.job_protocol import JobSpec
from src.worker_store import cancel_job, job_paths, read_events, read_state, start_job_process


class WorkerStoreTests(unittest.TestCase):
    def test_read_state_returns_unknown_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = read_state("job-missing", tmp)

        self.assertEqual(state["job_id"], "job-missing")
        self.assertEqual(state["status"], "unknown")

    def test_start_job_process_writes_initial_state_and_spec(self):
        spec = JobSpec(job_id="job-1", task="all", target_emails=("user@example.com",))

        with tempfile.TemporaryDirectory() as tmp, patch(
            "src.worker_store.subprocess.Popen",
            return_value=SimpleNamespace(pid=4242),
        ) as popen_mock:
            state = start_job_process(spec, tmp)
            paths = job_paths("job-1", tmp)

            self.assertEqual(state["status"], "accepted")
            self.assertEqual(state["pid"], 4242)
            self.assertTrue(paths["spec"].exists())
            self.assertTrue(paths["state"].exists())
            spec_payload = json.loads(paths["spec"].read_text(encoding="utf-8"))
            self.assertEqual(spec_payload["task"], "all")
            popen_mock.assert_called_once()

    def test_cancel_job_marks_state_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = job_paths("job-2", tmp)
            paths["state"].write_text(
                json.dumps({"job_id": "job-2", "status": "running", "pid": 777}),
                encoding="utf-8",
            )
            with patch("src.worker_store.subprocess.run") as run_mock:
                state = cancel_job("job-2", tmp)
                self.assertEqual(state["status"], "cancelled")
                self.assertTrue(paths["cancel"].exists())
                if os.name == "nt":
                    run_mock.assert_called()

    def test_read_events_reads_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = job_paths("job-3", tmp)
            paths["events"].write_text(
                '\n'.join(
                    [
                        json.dumps({"job_id": "job-3", "event_type": "job_running"}),
                        json.dumps({"job_id": "job-3", "event_type": "job_completed"}),
                    ]
                ),
                encoding="utf-8",
            )
            events = read_events("job-3", tmp)

        self.assertEqual([event["event_type"] for event in events], ["job_running", "job_completed"])
