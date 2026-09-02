"""Pure state-derivation helpers (no Home Assistant imports)."""

from __future__ import annotations

from typing import Any, Optional

LOCK_STATE_CLOSED = "beClosed"
LOCK_STATE_OPENED_KEYS = {"beOpened", "beAjar"}


def derive_lock_state(
    door_lock_status: Optional[int],
    door_lock_state: Any,
    lock_state: str,
) -> Optional[bool]:
    """HA lock state (True=locked) with a fallback chain.

    Priority: doorLockStatus (0 locked / 1 unlocked / 2 unknown)
              -> doorLockState (0 closed / 1 open)
              -> device list lockState (beClosed / beOpened / beAjar)
    """
    if door_lock_status == 0:
        return True
    if door_lock_status == 1:
        return False
    if isinstance(door_lock_state, int):
        return door_lock_state == 0
    if lock_state == LOCK_STATE_CLOSED:
        return True
    if lock_state in LOCK_STATE_OPENED_KEYS:
        return False
    return None


def derive_door_state(door_lock_state: Any, lock_state: str) -> str:
    """Text door state: closed / open / unknown."""
    if door_lock_state == 0:
        return "closed"
    if door_lock_state == 1:
        return "open"
    if lock_state:
        return "open" if lock_state != LOCK_STATE_CLOSED else "closed"
    return "unknown"


def extract_batteries(props: dict) -> tuple[Optional[int], Optional[int]]:
    """Split devicePowerLock into (lock battery %, camera battery %)."""
    lock_batt = None
    cam_batt = None
    for batt in props.get("devicePowerLock") or []:
        if not isinstance(batt, dict):
            continue
        pct = batt.get("elecPercent")
        if not isinstance(pct, int) and isinstance(pct, str) and pct.isdigit():
            pct = int(pct)
        if batt.get("type") == 1:
            lock_batt = pct if isinstance(pct, int) else None
        elif batt.get("type") == 0:
            cam_batt = pct if isinstance(pct, int) else None
    return lock_batt, cam_batt


def normalize_wifi(props: dict) -> Optional[dict]:
    """Extract the wifiDoorLock struct into a flat dict."""
    wifi = props.get("wifiDoorLock")
    if not isinstance(wifi, dict):
        return None
    return {
        "ssid": wifi.get("SSID", ""),
        "status": _int_or_none(wifi.get("status")),
        "intensity": _int_or_none(wifi.get("intensity")),
        "auth": wifi.get("auth", ""),
    }


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
