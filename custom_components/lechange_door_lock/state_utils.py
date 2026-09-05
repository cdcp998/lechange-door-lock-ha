"""Pure state-derivation helpers (no Home Assistant imports)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .const import (
    KEY_TYPE_NAMES,
    VALUE_TO_WORK_MODE,
    WORK_MODE_TO_VALUE,
)
from .i18n import tr as i18n_tr
from .i18n import DEFAULT_LANG as I18N_DEFAULT_LANG

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
def build_usage_period(effect_days: int, now: Optional[datetime] = None) -> str:
    """构建 SmartLockSecretAdd.usagePeriod,如 '127-20260903T0000Z-20260904T2359Z'.

    ★ 生效星期由系统自动分配: 掩码固定 127(每天) —— 生效窗口由起止日期
      (创建日 + effective_day)约束, 日期区间本身就是生效范围, 星期维度
      不再需要用户配置(原 snapkey_weekdays 选择项已移除)。
    结束日期 = 开始日期 + 有效天数(effectTimes=1 → 当日00:00 至次日23:59)。
    """
    if now is None:
        now = datetime.now()
    begin = now.date()
    end = begin + timedelta(days=max(int(effect_days), 1))
    return (
        f"127-{begin.strftime('%Y%m%d')}T0000Z-{end.strftime('%Y%m%d')}T2359Z"
    )


# ------------------------------------------------------------- 临时密码展示
# 服务端 state 枚举 → i18n 词条(snapkey.state.<n>); 0=未使用 1=已使用
# 2=已过期 3=已删除/失效(实测样例 0/1, 其余按业务语义)
_SNAPKEY_PERIOD_RE = re.compile(r"^\d+-(\d{8}T\d{4})Z-(\d{8}T\d{4})Z$")


def _local_tz() -> timezone:
    """设备时区(全库统一 UTC+8): 时间戳换算与"已过期"判断的基准."""
    return timezone(timedelta(hours=8))


def _parse_period_end_utc(usage_period: Any) -> Optional[datetime]:
    """usagePeriod '127-20260905T0000Z-20260906T2359Z' → 结束时刻(UTC aware).

    前缀数字是星期位掩码(见 build_usage_period 生成端), 解析按段数切分。
    """
    if not isinstance(usage_period, str):
        return None
    m = _SNAPKEY_PERIOD_RE.match(usage_period.strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), "%Y%m%dT%H%M").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _parse_snapkey_ts(value: Any) -> Optional[datetime]:
    """Unix 秒级时间戳(str/int) → UTC+8 aware datetime; 无效返回 None."""
    try:
        text = str(value).strip()
        if not text.isdigit():
            return None
        return datetime.fromtimestamp(int(text), _local_tz())
    except (OSError, OverflowError, ValueError):
        return None


def _snapkey_expiry(secret: dict, lang: str = I18N_DEFAULT_LANG) -> str:
    """过期时间文本: expiredTime 优先, 回退 usagePeriod 结束时刻(+ "(UTC)").

    两者皆缺/皆无效 → 兜底词(未知)。
    """
    dt = _parse_snapkey_ts(secret.get("expiredTime"))
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    end_utc = _parse_period_end_utc(secret.get("usagePeriod"))
    if end_utc is not None:
        return end_utc.strftime("%Y-%m-%d %H:%M:%S") + " (UTC)"
    return i18n_tr(lang, "snapkey.fallback.unknown")


def _snapkey_effect_times(value: Any, lang: str = I18N_DEFAULT_LANG) -> str:
    """使用次数(number 字段)→ 文本: '-1'/'0'→无限次, '1'→'1 次', 'N'→'N 次'."""
    text = str(value if value is not None else "").strip()
    if text in ("-1", "0"):
        return i18n_tr(lang, "snapkey.times.unlimited")
    if text.isdigit():
        return i18n_tr(lang, "snapkey.times.count").replace(
            "{n}", str(int(text))
        )
    return i18n_tr(lang, "snapkey.fallback.unknown")


def _snapkey_state_label(
    secret: dict,
    now: Optional[datetime] = None,
    lang: str = I18N_DEFAULT_LANG,
) -> str:
    """state 枚举 → 文本; state=0 但已过期 → '未使用(已过期)'.

    过期判定: expiredTime(Unix 秒)或 usagePeriod 结束时刻(Z=UTC)——
    aware datetime 直接跨时区比较(统一为绝对时刻, 不做挂钟重解释)。
    未收录 state 值 → 兜底词(未知)。
    """
    state = str(secret.get("state", "")).strip()
    label = i18n_tr(lang, f"snapkey.state.{state}") if state else None
    if label is None:
        label = i18n_tr(lang, "snapkey.fallback.unknown")
    if state == "0":
        expired = _parse_snapkey_ts(secret.get("expiredTime"))
        if expired is None:
            expired = _parse_period_end_utc(secret.get("usagePeriod"))
        if expired is not None:
            if now is None:
                now = datetime.now(_local_tz())
            elif now.tzinfo is None:
                now = now.replace(tzinfo=_local_tz())
            if now > expired:
                label = i18n_tr(lang, "snapkey.state.0_expired")
    return label


def format_snapkey_fields(
    secret: Any,
    now: Optional[datetime] = None,
    lang: str = I18N_DEFAULT_LANG,
) -> dict:
    """单条临时密码 → 结构化字段 dict(字段键与枚举值随实例语言).

    ★ 结构化而非拼串: HA more-info 对 dict 列表逐块渲染(每条一张小表),
      字符串列表会被逗号拼接成一段(不换行、超宽截断, 界面实测)。
    字段缺失/解析失败一律兜底词(未知/未命名/无), 绝不抛异常。
    """
    if not isinstance(secret, dict):
        return {
            i18n_tr(lang, "snapkey.field.name"): i18n_tr(
                lang, "snapkey.fallback.unknown"
            ),
            i18n_tr(lang, "snapkey.field.password"): i18n_tr(
                lang, "snapkey.fallback.none"
            ),
            i18n_tr(lang, "snapkey.field.expiry"): i18n_tr(
                lang, "snapkey.fallback.unknown"
            ),
            i18n_tr(lang, "snapkey.field.times"): i18n_tr(
                lang, "snapkey.fallback.unknown"
            ),
            i18n_tr(lang, "snapkey.field.state"): i18n_tr(
                lang, "snapkey.fallback.unknown"
            ),
        }
    return {
        i18n_tr(lang, "snapkey.field.name"): (
            str(secret.get("name") or "").strip()
            or i18n_tr(lang, "snapkey.fallback.unnamed")
        ),
        i18n_tr(lang, "snapkey.field.password"): (
            str(secret.get("tempKey") or "").strip()
            or i18n_tr(lang, "snapkey.fallback.none")
        ),
        i18n_tr(lang, "snapkey.field.expiry"): _snapkey_expiry(secret, lang),
        # ★ 次数 = number 字段(使用次数, -1=不限)—— 真机抓包实证:
        #   SecretAdd/ListV2 中 number=-1(不限次数)、effectTimes=有效天数
        #   (天,[1,90],见 API/report/02-云API参考.md §6.1)。此前误读
        #   effectTimes(天数 1)为次数 → "1 次"(实际不限次数)。
        i18n_tr(lang, "snapkey.field.times"): _snapkey_effect_times(
            secret.get("number"), lang
        ),
        i18n_tr(lang, "snapkey.field.state"): _snapkey_state_label(
            secret, now, lang
        ),
    }


def format_snapkey_line(
    secret: dict, now: Optional[datetime] = None, lang: str = I18N_DEFAULT_LANG
) -> str:
    """单条临时密码 → '名称 | 密码 | 过期时间 | 次数 | 状态' 显示行.

    (单行场景用; 属性展示请用 format_snapkey_fields 结构化)
    """
    f = format_snapkey_fields(secret, now, lang)
    return " | ".join(f.values())


def format_snapkeys_display(
    secrets: Any,
    now: Optional[datetime] = None,
    lang: str = I18N_DEFAULT_LANG,
) -> list[dict]:
    """Snapkeys 列表 → 结构化条目列表(按 createTime 从新到旧排序).

    ★ dict 列表: more-info 逐块渲染, 永不截断(字符串列表会被拼接)。
    坏元素(非 dict)跳过; 输入非列表/为空返回 []。
    """
    if not isinstance(secrets, list):
        return []
    items = [s for s in secrets if isinstance(s, dict) and s]
    items.sort(
        key=lambda s: _parse_snapkey_ts(s.get("createTime")) or
        datetime.min.replace(tzinfo=_local_tz()),
        reverse=True,
    )
    return [format_snapkey_fields(s, now, lang) for s in items]


def format_snapkey_lines(
    secrets: Any,
    now: Optional[datetime] = None,
    lang: str = I18N_DEFAULT_LANG,
    limit: int = 20,
) -> list[str]:
    """Snapkeys → 单行显示串列表('名称 | 密码 | 过期 | 次数 | 状态', 新→旧).

    编号属性(snapkey_1..N)场景用: 每个属性键一个短字符串, more-info
    逐键成行。limit 截断(与实体展示上限一致)。
    """
    entries = format_snapkeys_display(secrets, now, lang)[:limit] if limit \
        else format_snapkeys_display(secrets, now, lang)
    return [" | ".join(e.values()) for e in entries]


# ------------------------------------------------------------- 开门记录展示
def format_open_door_time(value: Any) -> str:
    """格式化开门记录时间字段。

    支持形态: `20260625T003156`(设备上报)、`2026-06-25 00:31:56`(已格式化)、
    纯数字 Unix 时间戳(pTimestamp, 云侧告警; 秒 10 位/毫秒 13 位, 按设备
    时区 UTC+8 转换, 与 imou_client 告警查询窗口的时区语义一致)。
    无法识别时原样返回(绝不用错误时间冒充)。
    """
    text = str(value).strip()
    if text.isdigit() and len(text) in (10, 13):
        ts = int(text)
        if len(text) == 13:
            ts //= 1000
        try:
            dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return text
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
    "pTimestamp",  # 云侧 Unix 时间戳, 仅作最低优先级回退
)
OPEN_DOOR_METHOD_KEYS = (
    "keyType", "type", "openType", "open_type",
    "openDoorType", "unlockType", "method", "openMethod",
)
OPEN_DOOR_USER_KEYS = ("name", "nickName", "userName", "user", "keyName")

# 云侧告警(GeDeviceAlarmMixMessage)无 keyType/name 结构化字段, 方式与用户
# 只在 title/message 文本里: "伟使用指纹开门了" / "有人出门了"。
ALARM_TEXT_KEYS = ("title", "message", "label", "content", "msg")
ALARM_METHOD_KEYWORDS = (
    # 顺序即优先级: 长词在前("临时密码"先于"密码")
    ("指纹", "指纹"),
    ("人脸", "人脸"),
    ("卡片", "卡片"),
    ("刷卡", "卡片"),
    ("门卡", "卡片"),
    ("临时", "临时密码"),
    ("密码", "密码"),
    ("远程", "远程开门"),
    ("机械", "机械钥匙"),
    ("钥匙", "钥匙"),
    ("出门", "出门按钮/室内开门"),  # "有人出门了": 室内开门(出门按钮), 服务端不给方式
)
_ALARM_METHOD_RE = re.compile(r"使用(.{1,16}?)开门")
_ALARM_USER_RE = re.compile(r"^(.{1,20}?)使用")
# 泛指主语不算用户
_ANONYMOUS_USERS = {"有人", "某人", "用户", "未知", ""}


def parse_alarm_title(text: str) -> tuple[str, str]:
    """从告警文本提取 (user, method): '伟使用指纹开门了' → ('伟', '指纹').

    '有人出门了' 无 "使用…开门" 结构 → 走关键词表 → ('', '出门按钮/室内开门')。
    解析不出时返回 ('', '')(调用方给默认值, 不中断)。
    """
    text = str(text or "").strip()
    if not text:
        return "", ""
    m = _ALARM_METHOD_RE.search(text)
    if m:
        method = m.group(1).strip()
        um = _ALARM_USER_RE.match(text)
        user = um.group(1).strip() if um else ""
        if user in _ANONYMOUS_USERS:
            user = ""
        return user, method
    for kw, label in ALARM_METHOD_KEYWORDS:
        if kw in text:
            return "", label
    return "", ""


def _fields_from_text(rec: dict) -> tuple[str, str]:
    """遍历告警文本字段提取 (user, method), 双双解析不出则继续."""
    for key in ALARM_TEXT_KEYS:
        text = rec.get(key)
        if not isinstance(text, str) or not text:
            continue
        user, method = parse_alarm_title(text)
        if user or method:
            return user, method
    return "", ""


def _record_time_text(rec: dict) -> str:
    """按优先级取记录的时间值并归一化(无有效时间返回空串)."""
    for key in OPEN_DOOR_TIME_KEYS:
        value = rec.get(key)
        if value:
            return format_open_door_time(value)
    return ""


def _time_digits(text: Any) -> int:
    """'2026-09-05 08:23:36' → 20260905082336(可比较); 不足 14 位 → 0(沉底)."""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())[:14]
    return int(digits) if len(digits) >= 14 else 0


def sort_records_by_time(records: list) -> list:
    """记录(告警/开门记录)按时间升序稳定排序.

    不依赖云 API 的返回顺序(实测顺序未文档化, 不能盲取末条当最新);
    缺时间/时间解析失败的记录沉底, 绝不丢弃。
    """
    return sorted(
        records,
        key=lambda r: _time_digits(_record_time_text(r)) if isinstance(r, dict) else 0,
    )


# ------------------------------------------------------------- 告警展示
def alarm_label_text(label_type: Any, lang: str = I18N_DEFAULT_LANG) -> str:
    """labelType 码 → 行为文本: 先查 i18n 映射, 未收录时仅当形如可读文本
    (含非数字字符)才原样透传, 数字码返回空串."""
    code = str(label_type or "").strip()
    if not code:
        return ""
    text = i18n_tr(lang, f"alarm.behavior.{code}")
    if text is not None:
        return text
    return "" if code.isdigit() else code


def format_alarm_fields(alarm: Any, lang: str = I18N_DEFAULT_LANG) -> dict:
    """单条云侧告警 → 结构化字段 dict(字段键与枚举值随实例语言).

    ★ 结构化而非拼串: HA more-info 对 dict 列表逐块渲染(每条一张小表),
      字符串列表会被逗号拼接成一段(不换行、超宽截断, 界面实测)。
    - 行为: labelType 经 i18n 映射(accessAlarm→开门/出门事件), 未收录
      可读文本透传, 数字枚举码/缺失 → 兜底词
    - 标题: title 原文(云端数据文本, 不翻译); 缺失 → 兜底词
    - 时间: time 优先, 回退 pTimestamp(Unix 秒 → UTC+8); 均缺 → 兜底词
    非 dict 输入返回 {}(调用方跳过)。
    """
    if not isinstance(alarm, dict):
        return {}
    return {
        i18n_tr(lang, "alarm.field.behavior"): (
            alarm_label_text(alarm.get("labelType"), lang)
            or i18n_tr(lang, "alarm.fallback.behavior")
        ),
        i18n_tr(lang, "alarm.field.title"): (
            str(alarm.get("title") or "").strip()
            or i18n_tr(lang, "alarm.fallback.title")
        ),
        i18n_tr(lang, "alarm.field.time"): (
            _record_time_text(alarm) or i18n_tr(lang, "alarm.fallback.time")
        ),
    }


def format_alarm_line(alarm: Any, lang: str = I18N_DEFAULT_LANG) -> str:
    """单条云侧告警 → 可读显示行: '行为 · 标题'(不含时间).

    状态栏不重复时间 —— 时间在 alarms 属性的结构化条目(时间键)里。
    """
    fields = format_alarm_fields(alarm, lang)
    if not fields:
        return ""
    behavior = i18n_tr(lang, "alarm.field.behavior")
    title = i18n_tr(lang, "alarm.field.title")
    parts = [
        v for k, v in fields.items()
        if k in (behavior, title)
    ]
    return " · ".join(parts)


def format_alarm_entries(
    alarms: Any, newest_first: bool = True, lang: str = I18N_DEFAULT_LANG
) -> list[dict]:
    """告警列表 → 结构化条目列表(默认最新在最上, 词映射随实例语言).

    ★ dict 列表: more-info 逐块渲染, 永不截断(字符串列表会被拼接)。
    坏元素(非 dict)跳过; 输入非列表返回空列表。
    """
    if not isinstance(alarms, list):
        return []
    entries = [
        format_alarm_fields(a, lang) for a in alarms if isinstance(a, dict)
    ]
    entries = [e for e in entries if e]
    if newest_first:
        entries.reverse()
    return entries


def _looks_like_open_record(rec: dict) -> bool:
    """判定告警记录是否为开门/出门类(区别于移动侦测、门铃等).

    有结构化方式字段(keyType 等)直接算; 否则看文本含 开门/出门。
    """
    if any(rec.get(k) is not None for k in OPEN_DOOR_METHOD_KEYS):
        return True
    text = " ".join(
        str(rec.get(k)) for k in ALARM_TEXT_KEYS if rec.get(k)
    )
    return ("开门" in text) or ("出门" in text)


def extract_latest_open_record(
    props_record: Any, alarm: Any
) -> Optional[dict]:
    """开门记录双通道合并: lockNoteReport 属性(设备上报) vs 云侧告警消息.

    电池锁大部分时间休眠, lockNoteReport 仅在设备主动上报时更新;
    云侧告警(GetDeviceAlarmMixMessage)休眠也可拉到 — 取两边更新的记录。
    alarm 支持单条(dict)或整批(list): 整批时先过滤开门类记录再按时间
    取最新 — 不依赖云 API 的返回顺序, 移动侦测等非开门告警不会混入。
    返回归一化: {time, method, user, raw}; 缺失字段给默认值, 不中断。
    """
    candidates: list[tuple[Any, dict]] = []

    def _norm(rec: Any) -> Optional[dict]:
        if not isinstance(rec, dict) or not rec:
            return None
        t = _record_time_text(rec)
        m = next((rec[k] for k in OPEN_DOOR_METHOD_KEYS if rec.get(k) is not None), "")
        u = next((rec[k] for k in OPEN_DOOR_USER_KEYS if rec.get(k)), "")
        # 结构化字段缺失 → 从告警文本(title/message)提取方式与用户。
        # ★ 注意 keyType=0(密码)是合法值但 falsy — 必须用 m == "" 判缺失
        if m == "" or not u:
            tu, tm = _fields_from_text(rec)
            if m == "":
                m = tm
            if not u:
                u = tu
        if not t and not m and not u:
            return None
        return {
            "time": t,
            "method": open_method_label(m) if m != "" else "",
            "user": str(u or ""),
            "raw": rec,
        }

    if isinstance(props_record, dict) and props_record:
        n = _norm(props_record)
        if n:
            candidates.append((n["time"], n))
    # 云侧告警: 单条或整批(整批先过滤开门类, 再按时间取最新)
    alarm_items = []
    if isinstance(alarm, list):
        alarm_items = [
            a for a in alarm
            if isinstance(a, dict) and a and _looks_like_open_record(a)
        ]
    elif isinstance(alarm, dict) and alarm:
        alarm_items = [alarm]
    for item in alarm_items:
        n = _norm(item)
        if n:
            candidates.append((n["time"], n))
    if not candidates:
        return None
    # 时间取新者(两侧均已归一化为 "YYYY-MM-DD HH:MM:SS" → 去符号即数字可比)
    best = max(candidates, key=lambda c: _time_digits(c[0]))[1]
    return best


def open_method_label(key_type: Any) -> str:
    """keyType(枚举 0-23)→ 中文方式;未知时原样返回."""
    return KEY_TYPE_NAMES.get(str(key_type), str(key_type))


# ------------------------------------------------------------- 工作模式
def work_mode_option(value: Any) -> Optional[str]:
    """powerMode 原始值(0-3)→ 选项键("auto"/"normal"/"power_saving"/
    "super_power_saving"); 不支持/未知 → None(实体显示 unavailable)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return VALUE_TO_WORK_MODE.get(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return VALUE_TO_WORK_MODE.get(int(value.strip()))
    return None


def work_mode_value(option: Any) -> Optional[int]:
    """选项键 → powerMode 写入值(0-3); 非法 → None."""
    if not isinstance(option, str):
        return None
    return WORK_MODE_TO_VALUE.get(option.strip())
