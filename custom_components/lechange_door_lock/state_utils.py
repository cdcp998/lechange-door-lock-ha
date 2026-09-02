"""Pure state-derivation helpers (no Home Assistant imports)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .const import KEY_TYPE_NAMES, WEEKDAYS, WEEKDAY_TO_PERIOD

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


# ------------------------------------------------------------- 临时密码配置
def weekday_mode_to_periods(mode: str) -> list[int]:
    """select 的 weekday_mode → 设备模型 period 枚举列表(0=周日..6=周六)."""
    if mode == "Every day":
        return [0, 1, 2, 3, 4, 5, 6]
    if mode == "Weekdays":
        return [1, 2, 3, 4, 5]
    if mode == "Weekend":
        return [0, 6]
    if mode in WEEKDAY_TO_PERIOD:
        return [WEEKDAY_TO_PERIOD[mode]]
    return [0, 1, 2, 3, 4, 5, 6]


def build_snapkey_periods(
    begin_time: str, end_time: str, weekday_mode: str
) -> list[dict]:
    """构建 CreateDeviceSnapkey 的 effectPeriod(每条一个周期日)."""
    return [
        {"period": p, "beginTime": begin_time, "endTime": end_time}
        for p in weekday_mode_to_periods(weekday_mode)
    ]


def weekday_bitmask(mode: str) -> int:
    """星期位掩码(smartLockSecretAdd 的 usagePeriod 前缀)。

    位定义与设备模型 period 枚举一致:bit0=周日 .. bit6=周六;127=每天。
    依据抓包:`"usagePeriod":"127-20260903T0000Z-20260904T2359Z"`。
    """
    return sum(1 << p for p in weekday_mode_to_periods(mode))


def build_usage_period(
    weekday_mode: str, effect_days: int, now: Optional[datetime] = None
) -> str:
    """构建 SmartLockSecretAdd.usagePeriod,如 '127-20260903T0000Z-20260904T2359Z'.

    结束日期 = 开始日期 + 有效天数(App 抓包:effectTimes=1 → 当日00:00 至次日23:59)。
    """
    if now is None:
        now = datetime.now()
    begin = now.date()
    end = begin + timedelta(days=max(int(effect_days), 1))
    mask = weekday_bitmask(weekday_mode)
    return (
        f"{mask}-{begin.strftime('%Y%m%d')}T0000Z-{end.strftime('%Y%m%d')}T2359Z"
    )


# ------------------------------------------------------------- 开门记录展示
def format_open_door_time(value: Any) -> str:
    """格式化 lockNoteReport 时间字段(如 20260625T003156)."""
    text = str(value)
    for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return text


def open_method_label(key_type: Any) -> str:
    """keyType(枚举 0-23)→ 中文方式;未知时原样返回."""
    return KEY_TYPE_NAMES.get(str(key_type), str(key_type))
