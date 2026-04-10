from __future__ import annotations

from typing import Optional


def build_runtime_descriptor(
    family: str,
    source_id: str,
    mode: str,
    *,
    account_proven: bool = True,
    cdp_url: str = "",
    runtime_key: str = "",
    live_for_account_run: bool = False,
) -> dict:
    """Describe the runtime family that executed a Rewards phase."""
    normalized_family = str(family or "").strip()
    normalized_source = str(source_id or "").strip()
    normalized_cdp = str(cdp_url or "").strip()
    if not runtime_key:
        if normalized_family in {"gpm_desktop", "gpm_mobile"}:
            runtime_key = f"gpm:{normalized_source}"
        elif normalized_family == "native_edge":
            runtime_key = f"native:{normalized_cdp or normalized_source}"
        elif normalized_family == "managed_edge":
            runtime_key = f"managed:{normalized_source}"
        else:
            runtime_key = f"{normalized_family}:{normalized_source}"
    return {
        "family": normalized_family,
        "source_id": normalized_source,
        "mode": mode,
        "account_proven": bool(account_proven),
        "cdp_url": normalized_cdp,
        "runtime_key": str(runtime_key),
        "live_for_account_run": bool(live_for_account_run),
    }


def invalidate_runtime_attachment(
    attach_runtime: bool,
    cdp_url: str,
    runtime_descriptor: Optional[dict] = None,
    *,
    reason: str = "",
) -> tuple[bool, str, Optional[dict]]:
    """Clear ephemeral CDP attachment data once the underlying runtime is gone."""
    invalidated = dict(runtime_descriptor) if runtime_descriptor else None
    if invalidated is not None and reason:
        invalidated["invalidated"] = True
        invalidated["invalid_reason"] = reason
        invalidated["cdp_url"] = ""
        invalidated["live_for_account_run"] = False
    return False, "", invalidated


def choose_search_verification_source(
    mode: str,
    *,
    desktop_runtime: Optional[dict],
    mobile_runtime: Optional[dict],
) -> Optional[dict]:
    """Pick the runtime family that is allowed to verify a search track."""
    normalized_mode = (mode or "").strip().lower()
    if normalized_mode == "mobile":
        return mobile_runtime
    if normalized_mode in {"desktop", "edge"}:
        return desktop_runtime
    return None


def build_search_verification(
    mode: str,
    runtime_descriptor: Optional[dict],
    *,
    verified: bool,
    reason: str = "",
) -> dict:
    """Normalize search verification metadata for summaries and diagnostics."""
    descriptor = runtime_descriptor or {}
    return {
        "mode": mode,
        "verified": bool(verified),
        "family": descriptor.get("family", "unknown"),
        "source_id": descriptor.get("source_id", ""),
        "account_proven": bool(descriptor.get("account_proven", False)),
        "reason": reason,
    }


def merge_search_status(
    *,
    base_status: Optional[dict] = None,
    desktop_status: Optional[dict] = None,
    mobile_status: Optional[dict] = None,
) -> dict:
    """Combine desktop/mobile reads into one Rewards search status payload."""
    merged = {
        "pc_current": 0,
        "pc_max": 0,
        "mobile_current": 0,
        "mobile_max": 0,
        "edge_current": 0,
        "edge_max": 0,
        "total_points": 0,
    }

    if base_status:
        merged.update(base_status)

    if desktop_status:
        for key in ("pc_current", "pc_max", "edge_current", "edge_max"):
            merged[key] = int(desktop_status.get(key, merged.get(key, 0)) or 0)
        merged["total_points"] = max(
            int(merged.get("total_points", 0) or 0),
            int(desktop_status.get("total_points", 0) or 0),
        )

    if mobile_status:
        for key in ("mobile_current", "mobile_max"):
            merged[key] = int(mobile_status.get(key, merged.get(key, 0)) or 0)
        merged["total_points"] = max(
            int(merged.get("total_points", 0) or 0),
            int(mobile_status.get("total_points", 0) or 0),
        )

    return merged


def describe_search_remaining_items(snapshot: dict) -> list[str]:
    """Describe verified deficits and unverified search tracks separately."""
    search_status = snapshot.get("search_status", {})
    verification = snapshot.get("search_verification", {})
    items: list[str] = []

    track_specs = (
        ("desktop", "Desktop", "pc_current", "pc_max"),
        ("mobile", "Mobile", "mobile_current", "mobile_max"),
        ("edge", "Edge Search", "edge_current", "edge_max"),
    )
    for mode, label, current_key, max_key in track_specs:
        meta = verification.get(mode)
        current_value = int(search_status.get(current_key, 0) or 0)
        max_value = int(search_status.get(max_key, 0) or 0)

        # Prefer concrete counters only when we have observed non-zero progress.
        # This preserves the explicit "unverified" message for 0/N cases while
        # preventing regressions like 24/60 being reported as unverified.
        if max_value > 0 and current_value > 0:
            if current_value < max_value:
                items.append(f"{label} {current_value}/{max_value}")
            continue

        if meta and not meta.get("verified", False):
            items.append(f"{label} unverified from original runtime")
            continue

        if max_value > 0 and current_value < max_value:
            items.append(f"{label} {current_value}/{max_value}")
            continue

    return items
