"""
Filesystem-backed worker job store.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

import time
import hashlib
import re
import shutil
from tempfile import mkstemp

from src.job_protocol import JobSpec


def _default_jobs_root() -> Path:
    if explicit := os.getenv("AUTOBING_WORKER_JOBS_DIR", "").strip():
        return Path(explicit).expanduser()
    if home := os.getenv("AUTOBING_HOME", "").strip():
        return Path(home).expanduser() / ".omx" / "worker-jobs"
    if data_dir := os.getenv("AUTOBING_DATA_DIR", "").strip():
        return Path(data_dir).expanduser() / ".omx" / "worker-jobs"
    return Path(".omx") / "worker-jobs"


DEFAULT_JOBS_ROOT = _default_jobs_root()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json_file(path: Path, default: Any | None = None, *, allow_invalid: bool = False) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip() and default is not None:
            return default
        return json.loads(text)
    except json.JSONDecodeError:
        if allow_invalid and default is not None:
            return default
        raise


def _write_json_file(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def jobs_root(path: str | None = None) -> Path:
    root = Path(path) if path else DEFAULT_JOBS_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_directory(job_id: str, root: str | None = None) -> Path:
    directory = jobs_root(root) / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def job_paths(job_id: str, root: str | None = None) -> dict[str, Path]:
    directory = job_directory(job_id, root)
    return {
        "dir": directory,
        "spec": directory / "job.json",
        "state": directory / "state.json",
        "events": directory / "events.jsonl",
        "stdout": directory / "stdout.log",
        "stderr": directory / "stderr.log",
        "cancel": directory / "cancel.requested",
    }


def runtime_locks_root(root: str | None = None) -> Path:
    path = jobs_root(root) / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_state_root(root: str | None = None) -> Path:
    path = jobs_root(root) / "runtime-state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_runtime_key(runtime_key: str) -> str:
    text = str(runtime_key or "").strip() or "runtime"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:80]}-{digest}"


def runtime_lock_path(runtime_key: str, root: str | None = None) -> Path:
    return runtime_locks_root(root) / f"{_sanitize_runtime_key(runtime_key)}.lock"


def account_runtime_state_dir(job_id: str, account_key: str, root: str | None = None) -> Path:
    path = runtime_state_root(root) / job_id / _sanitize_runtime_key(account_key)
    path.mkdir(parents=True, exist_ok=True)
    return path


def account_storage_state_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_runtime_state_dir(job_id, account_key, root) / "storage_state.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return str(pid) in output
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _try_lock_handle(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise RuntimeError("No file locking backend available")
        handle.seek(0)
        return handle
    except Exception:
        handle.close()
        raise


def _runtime_lock_owner_has_identity(owner: dict[str, Any]) -> bool:
    return bool(owner.get("pid") or owner.get("job_id") or owner.get("account_email"))


def _unknown_runtime_lock_is_stale(lock_path: Path, *, min_age_seconds: float = 30.0) -> bool:
    try:
        return (time.time() - lock_path.stat().st_mtime) >= min_age_seconds
    except OSError:
        return False


def acquire_runtime_lock(runtime_key: str, *, job_id: str, account_email: str, root: str | None = None) -> dict[str, Any]:
    lock_path = runtime_lock_path(runtime_key, root)
    owner_payload = {
        "runtime_key": runtime_key,
        "job_id": job_id,
        "account_email": account_email,
        "pid": os.getpid(),
        "created_at": _utcnow(),
    }

    for attempt in range(2):
        try:
            handle = _try_lock_handle(lock_path)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(owner_payload, ensure_ascii=False, indent=2))
            handle.flush()
            return {
                "runtime_key": runtime_key,
                "path": lock_path,
                "handle": handle,
                "owner": owner_payload,
            }
        except Exception:
            owner = read_runtime_lock(runtime_key, root=root)
            pid = int(owner.get("pid", 0) or 0)
            if attempt == 0 and pid and not _pid_alive(pid):
                with suppress(Exception):
                    lock_path.unlink()
                continue
            if (
                attempt == 0
                and not _runtime_lock_owner_has_identity(owner)
                and _unknown_runtime_lock_is_stale(lock_path)
            ):
                with suppress(Exception):
                    lock_path.unlink()
                if not lock_path.exists():
                    continue
            holder = owner.get("job_id") or owner.get("account_email") or "unknown"
            raise RuntimeError(f"runtime lock busy for {runtime_key} ({holder})")

    raise RuntimeError(f"runtime lock busy for {runtime_key}")


def release_runtime_lock(lock_info: dict[str, Any] | None) -> None:
    if not lock_info:
        return
    handle = lock_info.get("handle")
    lock_path = lock_info.get("path")
    if handle is None:
        return
    try:
        handle.seek(0)
        handle.truncate()
        handle.flush()
    except Exception:
        pass
    try:
        try:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    finally:
        with suppress(Exception):
            handle.close()
    if lock_path:
        with suppress(Exception):
            Path(lock_path).unlink()


def read_runtime_lock(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    lock_path = runtime_lock_path(runtime_key, root)
    if not lock_path.exists():
        return {}
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
        payload["path"] = str(lock_path)
        return payload
    except Exception:
        return {"runtime_key": runtime_key, "path": str(lock_path)}


def promote_account_storage_state(temp_path: Path, canonical_path: Path) -> None:
    if not temp_path.exists():
        return
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = mkstemp(prefix="storage-state-", suffix=".json", dir=str(canonical_path.parent))
    os.close(fd)
    tmp_target = Path(tmp_name)
    shutil.copyfile(temp_path, tmp_target)
    os.replace(tmp_target, canonical_path)
    try:
        temp_path.unlink(missing_ok=True)
    except Exception:
        pass


def reserve_native_edge_port(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    state_root = runtime_state_root(root)
    reservation_file = state_root / "native_edge_ports.json"
    reservation_file.parent.mkdir(parents=True, exist_ok=True)
    account_text = str(account_email or "default").strip() or "default"
    anchor = int(hashlib.md5(account_text.encode("utf-8")).hexdigest()[:8], 16)
    span = max(50, int(span or 400))

    for _ in range(span):
        port = base_port + (anchor % span)
        anchor += 1
        runtime_key = f"native-port:{port}"
        try:
            lock_info = acquire_runtime_lock(runtime_key, job_id=job_id, account_email=account_text, root=root)
            try:
                reservations = {}
                if reservation_file.exists():
                    reservations = _read_json_file(reservation_file, {}, allow_invalid=True)
                reservations[str(port)] = {
                    "account_email": account_text,
                    "job_id": job_id,
                    "updated_at": _utcnow(),
                }
                _write_json_file(reservation_file, reservations)
            except Exception:
                release_runtime_lock(lock_info)
                raise
            return {"port": port, "lock": lock_info}
        except Exception:
            continue
    raise RuntimeError(f"no native Edge port available for {account_text}")


def release_native_edge_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    if not reservation:
        return
    port = reservation.get("port")
    lock_info = reservation.get("lock")
    reservation_file = runtime_state_root(root) / "native_edge_ports.json"
    if port is not None and reservation_file.exists():
        try:
            reservations = _read_json_file(reservation_file, {}, allow_invalid=True)
            reservations.pop(str(port), None)
            _write_json_file(reservation_file, reservations)
        except Exception:
            pass
    release_runtime_lock(lock_info)


def active_native_edge_port_for_account(account_email: str, root: str | None = None) -> int | None:
    reservation_file = runtime_state_root(root) / "native_edge_ports.json"
    if not reservation_file.exists():
        return None
    try:
        reservations = _read_json_file(reservation_file, {}, allow_invalid=True)
    except Exception:
        return None
    for port_text, payload in reservations.items():
        if str(payload.get("account_email", "")).strip().lower() == str(account_email or "").strip().lower():
            try:
                return int(port_text)
            except Exception:
                return None
    return None


def cleanup_job_runtime_state(job_id: str, root: str | None = None) -> None:
    path = runtime_state_root(root) / job_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def canonical_storage_state_path(account_email: str) -> Path:
    safe_email = account_email.replace("@", "_at_").replace(".", "_")
    from src.utils import PROFILES_DIR
    return PROFILES_DIR / f"{safe_email}_state.json"


def finalize_account_storage_state(job_id: str, account_key: str, account_email: str, *, root: str | None = None) -> None:
    temp_path = account_storage_state_path(job_id, account_key, root)
    promote_account_storage_state(temp_path, canonical_storage_state_path(account_email))


def cleanup_account_runtime_state(job_id: str, account_key: str, root: str | None = None) -> None:
    path = account_runtime_state_dir(job_id, account_key, root)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def current_job_id_from_state(job_id: str, root: str | None = None) -> str:
    state = read_state(job_id, root)
    return str(state.get("job_id", job_id) or job_id)


def wait_briefly_for_runtime_release(seconds: float = 0.2) -> None:
    time.sleep(max(0.0, seconds))


def job_runtime_storage_paths(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return {
        "temp": account_storage_state_path(job_id, account_key, root),
        "canonical": canonical_storage_state_path(account_email),
    }


def runtime_owner_summary(runtime_key: str, root: str | None = None) -> str:
    owner = read_runtime_lock(runtime_key, root=root)
    if not owner:
        return ""
    job_id = str(owner.get("job_id", "") or "").strip()
    account_email = str(owner.get("account_email", "") or "").strip()
    return ", ".join(part for part in [job_id, account_email] if part)


def release_all_runtime_locks(lock_infos: list[dict[str, Any]]) -> None:
    for lock_info in reversed(lock_infos or []):
        release_runtime_lock(lock_info)


def copy_canonical_storage_state_to_job(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    paths = job_runtime_storage_paths(job_id, account_key, account_email, root)
    temp_path = paths["temp"]
    canonical_path = paths["canonical"]
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    if canonical_path.exists():
        shutil.copyfile(canonical_path, temp_path)
    return temp_path


def complete_job_storage_state(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    if verified:
        finalize_account_storage_state(job_id, account_key, account_email, root=root)
    else:
        cleanup_account_runtime_state(job_id, account_key, root=root)


def update_job_state(job_id: str, payload: dict[str, Any], root: str | None = None) -> None:
    paths = job_paths(job_id, root)
    paths["state"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_job_spec(job_id: str, root: str | None = None) -> dict[str, Any]:
    paths = job_paths(job_id, root)
    if not paths["spec"].exists():
        return {}
    return json.loads(paths["spec"].read_text(encoding="utf-8"))


def root_for_job(job_id: str, root: str | None = None) -> Path:
    return job_directory(job_id, root)


def canonical_job_storage_dir(job_id: str, root: str | None = None) -> Path:
    path = runtime_state_root(root) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_state_exists_for_job(job_id: str, account_key: str, root: str | None = None) -> bool:
    return account_storage_state_path(job_id, account_key, root).exists()


def ensure_runtime_state_root(root: str | None = None) -> Path:
    return runtime_state_root(root)


def ensure_runtime_locks_root(root: str | None = None) -> Path:
    return runtime_locks_root(root)


def job_storage_state_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def cleanup_storage_state_file(path: Path) -> None:
    with suppress(Exception):
        path.unlink(missing_ok=True)


def clone_canonical_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def persist_verified_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def discard_job_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def lock_owner_for_runtime(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def reserve_port_for_runtime(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def release_reserved_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def port_for_account(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def finish_job_runtime_state(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_job_state_path(job_id: str, root: str | None = None) -> Path:
    return runtime_state_root(root) / job_id


def runtime_lock_file(runtime_key: str, root: str | None = None) -> Path:
    return runtime_lock_path(runtime_key, root)


def describe_runtime_owner(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def sleep_for_runtime_release(seconds: float = 0.2) -> None:
    wait_briefly_for_runtime_release(seconds)


def storage_paths_for_account(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return job_runtime_storage_paths(job_id, account_key, account_email, root)


def release_runtime_lock_set(lock_infos: list[dict[str, Any]]) -> None:
    release_all_runtime_locks(lock_infos)


def write_runtime_state_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_runtime_state_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def temporary_storage_state_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def finalize_storage_state_if_verified(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def current_lock_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def remove_runtime_state_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def release_lock_if_any(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def acquire_lock_for_runtime(runtime_key: str, *, job_id: str, account_email: str, root: str | None = None) -> dict[str, Any]:
    return acquire_runtime_lock(runtime_key, job_id=job_id, account_email=account_email, root=root)


def state_root_for_runtime(root: str | None = None) -> Path:
    return runtime_state_root(root)


def lock_root_for_runtime(root: str | None = None) -> Path:
    return runtime_locks_root(root)


def canonical_storage_path_for_account(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def temp_storage_path_for_job(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def ensure_job_runtime_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def complete_account_runtime_state(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def release_reserved_native_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def reserved_native_port_for_account(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def remove_job_runtime_state(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def read_runtime_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def release_runtime_locks(lock_infos: list[dict[str, Any]]) -> None:
    release_all_runtime_locks(lock_infos)


def create_job_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def commit_job_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def drop_job_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_lock_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def runtime_job_storage_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def clear_runtime_lock(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def reserve_runtime_port(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def clear_reserved_runtime_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def existing_runtime_port(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def prepare_job_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def finalize_job_storage_state(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def cleanup_runtime_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def remove_job_runtime_tree(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_lock_debug_owner(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def canonical_account_storage_state(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def temp_account_storage_state(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def update_runtime_reservation_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_runtime_reservation_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def sleep_for_runtime_retry(seconds: float = 0.2) -> None:
    wait_briefly_for_runtime_release(seconds)


def runtime_port_reservation_path(root: str | None = None) -> Path:
    return runtime_state_root(root) / "native_edge_ports.json"


def release_runtime_port_lock(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def account_runtime_lock_path(runtime_key: str, root: str | None = None) -> Path:
    return runtime_lock_path(runtime_key, root)


def account_runtime_lock_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def make_job_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def complete_verified_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def clear_unverified_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def owned_runtime_port(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def release_owned_runtime_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_storage_dir(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_runtime_state_dir(job_id, account_key, root)


def remove_runtime_storage_dir(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_storage_file(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_lock_acquire(runtime_key: str, *, job_id: str, account_email: str, root: str | None = None) -> dict[str, Any]:
    return acquire_runtime_lock(runtime_key, job_id=job_id, account_email=account_email, root=root)


def runtime_lock_release(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def runtime_lock_read(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_storage_prepare(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def runtime_storage_finalize(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def runtime_storage_cleanup(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_job_cleanup(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def port_reservation_for_account(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def port_reservation_release(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def port_reservation_acquire(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def runtime_lock_owner(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def account_job_storage_paths(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return job_runtime_storage_paths(job_id, account_key, account_email, root)


def temp_storage_for_job(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def storage_for_account(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def storage_finalize_verified(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def storage_drop_unverified(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_storage_root(root: str | None = None) -> Path:
    return runtime_state_root(root)


def runtime_lock_root(root: str | None = None) -> Path:
    return runtime_locks_root(root)


def runtime_port_file(root: str | None = None) -> Path:
    return runtime_state_root(root) / "native_edge_ports.json"


def runtime_job_root(job_id: str, root: str | None = None) -> Path:
    return runtime_state_root(root) / job_id


def storage_state_promote(temp_path: Path, canonical_path: Path) -> None:
    promote_account_storage_state(temp_path, canonical_path)


def storage_state_commit(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def storage_state_remove(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def reserved_port_owner(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def release_port_owner(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_storage_clone(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def runtime_storage_commit(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def runtime_storage_discard(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_lock_info(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_reserve_port(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def runtime_release_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_port_for_account(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def cleanup_job_runtime(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def cleanup_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def clone_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def commit_storage_state(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def lock_busy_owner(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def canonical_account_state(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def temp_account_state(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_account_dir(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_runtime_state_dir(job_id, account_key, root)


def remove_runtime_account_dir(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def path_for_runtime_lock(runtime_key: str, root: str | None = None) -> Path:
    return runtime_lock_path(runtime_key, root)


def path_for_runtime_storage(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def path_for_runtime_state_root(root: str | None = None) -> Path:
    return runtime_state_root(root)


def path_for_runtime_locks_root(root: str | None = None) -> Path:
    return runtime_locks_root(root)


def release_runtime_resources(lock_infos: list[dict[str, Any]], reservation: dict[str, Any] | None = None, root: str | None = None) -> None:
    if reservation:
        release_native_edge_port(reservation, root=root)
    release_all_runtime_locks(lock_infos)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def best_effort_cleanup_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with suppress(Exception):
            path.unlink(missing_ok=True)


def runtime_lock_handle(runtime_key: str, *, job_id: str, account_email: str, root: str | None = None) -> dict[str, Any]:
    return acquire_runtime_lock(runtime_key, job_id=job_id, account_email=account_email, root=root)


def unlock_runtime_lock(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def get_runtime_lock(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def reserve_account_port(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def release_account_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def active_account_port(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def cleanup_runtime_job(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def prepare_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def apply_verified_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def discard_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_owner_label(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def remove_runtime_job_state(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def get_account_storage_paths(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return job_runtime_storage_paths(job_id, account_key, account_email, root)


def promote_verified_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def clear_account_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_resources_root(root: str | None = None) -> Path:
    return runtime_state_root(root)


def runtime_lock_resources_root(root: str | None = None) -> Path:
    return runtime_locks_root(root)


def native_edge_port_owner(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def native_edge_port_release(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def native_edge_port_reserve(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def runtime_storage_paths(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return job_runtime_storage_paths(job_id, account_key, account_email, root)


def cleanup_runtime_paths(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def release_reserved_native_edge_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def read_lock_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def release_lock_owner(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def acquire_lock_owner(runtime_key: str, *, job_id: str, account_email: str, root: str | None = None) -> dict[str, Any]:
    return acquire_runtime_lock(runtime_key, job_id=job_id, account_email=account_email, root=root)


def runtime_port_lookup(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def runtime_port_unreserve(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_port_reserve_for_account(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def runtime_state_tree(job_id: str, root: str | None = None) -> Path:
    return runtime_state_root(root) / job_id


def delete_runtime_state_tree(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_lock_summary(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def account_storage_state(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def account_storage_canonical(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def runtime_storage_commit_if_verified(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def runtime_storage_temp(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_storage_canonical(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def cleanup_runtime_storage_tree(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def cleanup_runtime_storage_account(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_state_lock_path(runtime_key: str, root: str | None = None) -> Path:
    return runtime_lock_path(runtime_key, root)


def runtime_state_storage_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_state_cleanup(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_port_file_path(root: str | None = None) -> Path:
    return runtime_state_root(root) / "native_edge_ports.json"


def sync_storage_state_to_canonical(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def teardown_runtime_state(job_id: str, account_key: str | None = None, root: str | None = None) -> None:
    if account_key is None:
        cleanup_job_runtime_state(job_id, root=root)
        return
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_owner_name(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def temporary_storage_state(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def canonical_storage_state(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def account_runtime_state(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_runtime_state_dir(job_id, account_key, root)


def release_reserved_native_edge(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def reserve_native_edge(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def current_reserved_native_edge(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def runtime_state_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_state_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def cleanup_runtime_job_tree(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_state_account_dir(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_runtime_state_dir(job_id, account_key, root)


def runtime_state_account_file(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_state_account_paths(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return job_runtime_storage_paths(job_id, account_key, account_email, root)


def release_runtime_port_if_any(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def remove_runtime_lock_file(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def account_runtime_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def account_runtime_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def ensure_job_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def finalize_job_storage_if_verified(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def cleanup_job_storage(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def native_port_for_account(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def native_port_release(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def native_port_reserve(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def runtime_account_storage(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_account_storage_canonical(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def cleanup_runtime_account_storage(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_lock_status(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_lock_status_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def runtime_prepare_storage(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def runtime_finish_storage(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def runtime_drop_storage(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_cleanup_all(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_get_lock_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_get_lock_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def account_state_temp_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def account_state_canonical_path(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def cleanup_runtime_account_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def cleanup_runtime_job_state_tree(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_reservation_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_reservation_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def create_temp_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def promote_temp_storage_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> None:
    finalize_account_storage_state(job_id, account_key, account_email, root=root)


def drop_temp_storage_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_port_current_owner(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def runtime_port_clear_owner(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_port_claim(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def cleanup_runtime_resources_for_job(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def lock_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def lock_owner_data(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def storage_state_paths(job_id: str, account_key: str, account_email: str, root: str | None = None) -> dict[str, Path]:
    return job_runtime_storage_paths(job_id, account_key, account_email, root)


def canonical_state_path(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def temp_state_path(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def complete_storage_state(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def reserve_edge_port(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def release_edge_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def edge_port_for_account(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def cleanup_all_runtime_state(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_storage(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_storage_owner(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def runtime_storage_owner_text(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def job_lock_summary(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def release_runtime_lock_info(lock_info: dict[str, Any] | None) -> None:
    release_runtime_lock(lock_info)


def acquire_runtime_lock_info(runtime_key: str, *, job_id: str, account_email: str, root: str | None = None) -> dict[str, Any]:
    return acquire_runtime_lock(runtime_key, job_id=job_id, account_email=account_email, root=root)


def reserve_runtime_native_port(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def release_runtime_native_port(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_native_port(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def cleanup_job_runtime_resources(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_lock_owner_compact(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def clone_account_state(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def commit_account_state(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def discard_account_state(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_owner_brief(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def job_storage_clone(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def job_storage_commit(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def job_storage_discard(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def runtime_port_claimed_by(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def runtime_port_release_claim(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_port_claim_for_account(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def cleanup_runtime_state_for_job(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_temp_storage(job_id: str, account_key: str, root: str | None = None) -> Path:
    return account_storage_state_path(job_id, account_key, root)


def runtime_canonical_storage(account_email: str) -> Path:
    return canonical_storage_state_path(account_email)


def runtime_sync_storage(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def release_runtime_lock_and_port(lock_info: dict[str, Any] | None, reservation: dict[str, Any] | None = None, root: str | None = None) -> None:
    if reservation:
        release_native_edge_port(reservation, root=root)
    if lock_info:
        release_runtime_lock(lock_info)


def runtime_reserved_port(account_email: str, root: str | None = None) -> int | None:
    return active_native_edge_port_for_account(account_email, root=root)


def runtime_reserved_port_release(reservation: dict[str, Any] | None, root: str | None = None) -> None:
    release_native_edge_port(reservation, root=root)


def runtime_reserved_port_acquire(account_email: str, *, base_port: int, job_id: str, root: str | None = None, span: int = 400) -> dict[str, Any]:
    return reserve_native_edge_port(account_email, base_port=base_port, job_id=job_id, root=root, span=span)


def cleanup_runtime_root(job_id: str, root: str | None = None) -> None:
    cleanup_job_runtime_state(job_id, root=root)


def runtime_storage_promote(temp_path: Path, canonical_path: Path) -> None:
    promote_account_storage_state(temp_path, canonical_path)


def runtime_storage_prepare_for_account(job_id: str, account_key: str, account_email: str, root: str | None = None) -> Path:
    return copy_canonical_storage_state_to_job(job_id, account_key, account_email, root)


def runtime_storage_finalize_for_account(job_id: str, account_key: str, account_email: str, *, verified: bool, root: str | None = None) -> None:
    complete_job_storage_state(job_id, account_key, account_email, verified=verified, root=root)


def runtime_storage_drop_for_account(job_id: str, account_key: str, root: str | None = None) -> None:
    cleanup_account_runtime_state(job_id, account_key, root=root)


def account_lock_summary(runtime_key: str, root: str | None = None) -> str:
    return runtime_owner_summary(runtime_key, root=root)


def account_lock_data(runtime_key: str, root: str | None = None) -> dict[str, Any]:
    return read_runtime_lock(runtime_key, root=root)


def cleanup_runtime_file(path: Path) -> None:
    if path.exists():
        best_effort_cleanup_path(path)


def write_job_spec(job: JobSpec, root: str | None = None) -> dict[str, Path]:
    paths = job_paths(job.job_id, root)
    paths["spec"].write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return paths




def _append_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

def read_state(job_id: str, root: str | None = None) -> dict[str, Any]:
    paths = job_paths(job_id, root)
    if not paths["state"].exists():
        return {
            "protocol_version": "0.1",
            "worker_kind": "python-sidecar",
            "job_id": job_id,
            "status": "unknown",
            "updated_at": _utcnow(),
        }

    state = _read_json_file(paths["state"])
    if state.get("status") == "running":
        pid = int(state.get("pid", 0) or 0)
        if pid and not _pid_alive(pid):
            state = dict(state)
            state.update({
                "status": "failed",
                "error": "worker process exited before writing final state",
                "completed_at": _utcnow(),
                "updated_at": _utcnow(),
            })
            paths["state"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            _append_event(paths["events"], {
                "protocol_version": "0.1",
                "job_id": job_id,
                "event_type": "job_failed",
                "timestamp": _utcnow(),
                "error": state["error"],
                "pid": pid,
            })
    return state


def read_events(job_id: str, root: str | None = None) -> list[dict[str, Any]]:
    paths = job_paths(job_id, root)
    if not paths["events"].exists():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in paths["events"].read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        events.append(json.loads(raw_line))
    return events


def start_job_process(job: JobSpec, root: str | None = None) -> dict[str, Any]:
    paths = write_job_spec(job, root)
    stdout = paths["stdout"].open("a", encoding="utf-8")
    stderr = paths["stderr"].open("a", encoding="utf-8")

    if getattr(sys, "frozen", False):
        command = [
            sys.executable,
            "internal-runtime",
            "--job-file",
            str(paths["spec"]),
            "--state-file",
            str(paths["state"]),
            "--events-file",
            str(paths["events"]),
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "src.worker_runtime",
            "--job-file",
            str(paths["spec"]),
            "--state-file",
            str(paths["state"]),
            "--events-file",
            str(paths["events"]),
        ]

    popen_kwargs: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "cwd": str(Path.cwd()),
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    process = subprocess.Popen(command, **popen_kwargs)
    stdout.close()
    stderr.close()
    initial_state = {
        "protocol_version": "0.1",
        "worker_kind": "python-sidecar",
        "job_id": job.job_id,
        "status": "accepted",
        "task": job.task,
        "target_emails": list(job.target_emails),
        "secret_ref": job.secret_ref,
        "correlation_id": job.correlation_id,
        "pid": process.pid,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    paths["state"].write_text(json.dumps(initial_state, ensure_ascii=False, indent=2), encoding="utf-8")
    return initial_state


def cancel_job(job_id: str, root: str | None = None) -> dict[str, Any]:
    paths = job_paths(job_id, root)
    paths["cancel"].parent.mkdir(parents=True, exist_ok=True)
    paths["cancel"].write_text("cancelled\n", encoding="utf-8")
    state = read_state(job_id, root)
    pid = state.get("pid")
    if isinstance(pid, int):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    state["status"] = "cancelled"
    state["updated_at"] = _utcnow()
    paths["state"].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
