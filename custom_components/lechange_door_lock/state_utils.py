"""Pure state-derivation helpers (no Home Assistant imports)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .const import KEY_TYPE_NAMES, WEEKDAY_TO_PERIOD

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
    """Split devicePowerLock into (lock battery %, camera battery %).

    兼容多种上报形态: list[struct] / 单 struct / elecPercent 为
    int/float/str("-1..100"); type 以字符串或 int 出现均可。
    """
    lock_batt = None
    cam_batt = None
    raw = props.get("devicePowerLock")
    items: list = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = [raw]
    for batt in items:
        if not isinstance(batt, dict):
            continue
        pct = batt.get("elecPercent")
        if isinstance(pct, str) and pct.strip().lstrip("-").isdigit():
            pct = int(pct)
        elif isinstance(pct, float):
            pct = int(round(pct))
        if not isinstance(pct, int) or not (0 <= pct <= 100):
            continue
        batt_type = str(batt.get("type", ""))
        if batt_type == "1":
            lock_batt = pct
        elif batt_type == "0":
            cam_batt = pct
    return lock_batt, cam_batt


def decode_bool_prop(value: Any) -> Optional[bool]:
    """严格布尔判定(bool/int(0,1)/str("0","1","true","false")) → None=未知.

    设备休眠时 GetProperties 失败、快照缺该属性、或服务端返回 "" —
    一律视为未知(None), 由实体层决定显示 unavailable 而非错误的关状态。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "on"):
            return True
        if v in ("0", "false", "off"):
            return False
    return None


def resolve_child_lock(props: dict) -> Optional[bool]:
    """童锁双源判定 → None=未知.

    ① child_lock(120000, bool) — 标准 IoT 属性, 部分固件实时上报;
    ② sdl_inOpenDoorModel(171700, enum 1=普通/2=童锁) — R10-Max 实测
      的童锁真实载体: App"童锁开关"即切室内开门模式, **云端缓存含此
      属性(抓包 20260905: 值=2)**, 休眠期也可读。child_lock 缺失时
      由此回退。
    """
    strict = decode_bool_prop(props.get("child_lock"))
    if strict is not None:
        return strict
    mode = _int_or_none(props.get("sdl_inOpenDoorModel"))
    if mode == 2:
        return True
    if mode == 1:
        return False
    return None


# 休眠期可继承的展示字段(本次刷新拿不到时保留上次值, 绝不清零)
INHERITABLE_KEYS = (
    "props", "lock_notes", "latest_open_door_record", "alarms", "latest_alarm",
    "channel_names", "wifi", "battery_lock", "battery_camera",
    "door_lock_status", "lock_state",
)


def inherit_previous(prev: Optional[dict], data: dict) -> dict:
    """把上次成功数据中的展示字段继承进本次 data(电池锁休眠韧性).

    设备休眠时 GetProperties 10003 / propertiesMap 快照为空 → 本次刷新
    天然缺字段;若直接返回残缺 data, 所有传感器/开关状态会被清空
    (MQTT 刚推来的实时值也一并丢失)。此处仅补齐本次未产出的键,
    本次成功获取的字段一律以新值为准。
    """
    if not isinstance(prev, dict):
        return data
    for key in INHERITABLE_KEYS:
        if key not in data and key in prev:
            data[key] = prev[key]
    return data


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
    格式示例:`"usagePeriod":"127-20260903T0000Z-20260904T2359Z"`。
    """
    return sum(1 << p for p in weekday_mode_to_periods(mode))


def build_usage_period(
    weekday_mode: str, effect_days: int, now: Optional[datetime] = None
) -> str:
    """构建 SmartLockSecretAdd.usagePeriod,如 '127-20260903T0000Z-20260904T2359Z'.

    结束日期 = 开始日期 + 有效天数(effectTimes=1 → 当日00:00 至次日23:59)。
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


# 开门记录的多键兼容(参考 OpenAPI 实现 + 本地协议字段)
OPEN_DOOR_TIME_KEYS = (
    "localTime", "time", "openTime", "open_time",
    "recordTime", "unlockTime", "occurTime", "createTime",
)
OPEN_DOOR_METHOD_KEYS = (
    "keyType", "type", "openType", "open_type",
    "openDoorType", "unlockType", "method", "openMethod",
)
OPEN_DOOR_USER_KEYS = ("name", "nickName", "userName", "user", "keyName")


def extract_latest_open_record(
    props_record: Any, alarm: Any
) -> Optional[dict]:
    """开门记录双通道合并: lockNoteReport 属性(设备上报) vs 云侧告警消息.

    电池锁大部分时间休眠, lockNoteReport 仅在设备主动上报时更新;
    云侧告警(GetDeviceAlarmMixMessage)休眠也可拉到 — 取两边更新的记录。
    返回归一化: {time, method, user, raw}。
    """
    candidates: list[tuple[Any, dict]] = []

    def _norm(rec: Any) -> Optional[dict]:
        if not isinstance(rec, dict) or not rec:
            return None
        t = next((rec[k] for k in OPEN_DOOR_TIME_KEYS if rec.get(k)), "")
        m = next((rec[k] for k in OPEN_DOOR_METHOD_KEYS if rec.get(k) is not None), "")
        u = next((rec[k] for k in OPEN_DOOR_USER_KEYS if rec.get(k)), "")
        if not t and not m and not u:
            return None
        return {
            "time": format_open_door_time(t) if t else "",
            "method": open_method_label(m) if m != "" else "",
            "user": str(u or ""),
            "raw": rec,
        }

    if isinstance(props_record, dict) and props_record:
        n = _norm(props_record)
        if n:
            candidates.append((n["time"], n))
    # 云侧告警: 取最近一条开门类(labelType/alarms 含"开门"/"open"语义不判,
    # 有 time/method 即可 — 由调用方保证 alarm 是最新一条)
    if isinstance(alarm, dict) and alarm:
        n = _norm(alarm)
        if n:
            candidates.append((n["time"], n))
    if not candidates:
        return None
    # 时间取新者(两侧均已归一化为 "YYYY-MM-DD HH:MM:SS" → 去符号即数字可比)
    def _sort_key(c):
        digits = "".join(ch for ch in str(c[0] or "") if ch.isdigit())[:14]
        return int(digits) if len(digits) >= 14 else 0

    best = max(candidates, key=_sort_key)[1]
    return best


def open_method_label(key_type: Any) -> str:
    """keyType(枚举 0-23)→ 中文方式;未知时原样返回."""
    return KEY_TYPE_NAMES.get(str(key_type), str(key_type))
