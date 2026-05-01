import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.job_protocol import JobSpec
from src.worker_store import (
    acquire_runtime_lock,
    cancel_job,
    job_paths,
    read_events,
    read_state,
    release_runtime_lock,
    reserve_native_edge_port,
    release_native_edge_port,
    runtime_lock_path,
    runtime_state_root,
    start_job_process,
)


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

    def test_start_job_process_uses_self_internal_runtime_when_frozen(self):
        spec = JobSpec(job_id="job-frozen", task="all", target_emails=("user@example.com",))

        with tempfile.TemporaryDirectory() as tmp, patch(
            "src.worker_store.subprocess.Popen",
            return_value=SimpleNamespace(pid=4243),
        ) as popen_mock, patch("src.worker_store.sys.executable", "C:\\AutoBing\\worker_api.exe"), patch(
            "src.worker_store.sys.frozen",
            True,
            create=True,
        ):
            start_job_process(spec, tmp)

        command = popen_mock.call_args.args[0]
        self.assertEqual(command[0], "C:\\AutoBing\\worker_api.exe")
        self.assertEqual(command[1], "internal-runtime")
        self.assertIn("--job-file", command)
        self.assertIn("--state-file", command)
        self.assertIn("--events-file", command)

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

    def test_acquire_runtime_lock_clears_stale_empty_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = runtime_lock_path("native:user@example.com", tmp)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text("", encoding="utf-8")
            old_timestamp = time.time() - 120
            os.utime(lock_path, (old_timestamp, old_timestamp))

            from src import worker_store

            real_try_lock = worker_store._try_lock_handle
            attempts = {"count": 0}

            def flaky_try_lock(path):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("simulated stale lock")
                return real_try_lock(path)

            with patch("src.worker_store._try_lock_handle", side_effect=flaky_try_lock):
                lock_info = acquire_runtime_lock(
                    "native:user@example.com",
                    job_id="job-new",
                    account_email="user@example.com",
                    root=tmp,
                )

            try:
                self.assertEqual(lock_info["owner"]["job_id"], "job-new")
                self.assertEqual(attempts["count"], 2)
            finally:
                release_runtime_lock(lock_info)

    def test_read_state_marks_running_job_failed_when_pid_exited(self):
        with tempfile.TemporaryDirectory() as tmp, patch("src.worker_store._pid_alive", return_value=False):
            paths = job_paths("job-stale", tmp)
            paths["state"].write_text(
                json.dumps({"job_id": "job-stale", "status": "running", "pid": 12345}),
                encoding="utf-8",
            )

            state = read_state("job-stale", tmp)
            events = read_events("job-stale", tmp)

        self.assertEqual(state["status"], "failed")
        self.assertIn("exited before writing final state", state["error"])
        self.assertEqual(events[-1]["event_type"], "job_failed")

    def test_reserve_native_edge_port_handles_bom_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            reservation_file = runtime_state_root(tmp) / "native_edge_ports.json"
            reservation_file.write_text("{}", encoding="utf-8-sig")

            reservation = reserve_native_edge_port(
                "user@example.com",
                base_port=9300,
                job_id="job-native",
                root=tmp,
            )

            try:
                self.assertIsInstance(reservation["port"], int)
                payload = json.loads(reservation_file.read_text(encoding="utf-8"))
                self.assertEqual(payload[str(reservation["port"])]["job_id"], "job-native")
            finally:
                release_native_edge_port(reservation, tmp)
