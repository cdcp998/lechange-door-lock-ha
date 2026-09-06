"""属性展示文本的 i18n 目录(纯模块, 无 HA 依赖).

HA 前端不翻译属性(attribute)内容 —— more-info 显示的属性值由集成按
实例语言(hass.config.language)自行产出。本目录覆盖展示层的"词映射"
(字段名/枚举映射/兜底词); 云端下发的数据文本(告警标题原文等)不属于
UI 文案, 不翻译。

新增语言: 复制一份目录并翻译; 键保持稳定, 缺词条自动回退 zh-Hans。
"""

from __future__ import annotations

DEFAULT_LANG = "zh-Hans"

_CATALOGS: dict[str, dict[str, str]] = {
    "zh-Hans": {
        # 告警字段名
        "alarm.field.behavior": "行为",
        "alarm.field.title": "标题",
        "alarm.field.time": "时间",
        # labelType 行为映射(const.ALARM_LABEL_TYPE_NAMES 的各语言对应)
        "alarm.behavior.accessAlarm": "开门/出门事件",
        "alarm.behavior.wander": "徘徊",
        # 兜底词
        "alarm.fallback.behavior": "未知行为",
        "alarm.fallback.title": "未知标题",
        "alarm.fallback.time": "未知时间",
        # 临时密码字段名
        "snapkey.field.name": "名称",
        "snapkey.field.password": "密码",
        "snapkey.field.expiry": "过期时间",
        "snapkey.field.times": "次数",
        "snapkey.field.state": "状态",
        # 临时密码枚举/模板
        "snapkey.state.0": "未使用",
        "snapkey.state.1": "已使用",
        "snapkey.state.2": "已过期",
        "snapkey.state.3": "已删除/失效",
        "snapkey.state.0_expired": "未使用(已过期)",
        "snapkey.times.unlimited": "无限次",
        "snapkey.times.count": "{n} 次",
        # 通用兜底词
        "snapkey.fallback.unknown": "未知",
        "snapkey.fallback.unnamed": "未命名",
        "snapkey.fallback.none": "无",
    },
    "en": {
        "alarm.field.behavior": "Behavior",
        "alarm.field.title": "Title",
        "alarm.field.time": "Time",
        "alarm.behavior.accessAlarm": "Open/exit event",
        "alarm.behavior.wander": "Wandering",
        "alarm.fallback.behavior": "Unknown behavior",
        "alarm.fallback.title": "Unknown title",
        "alarm.fallback.time": "Unknown time",
        "snapkey.field.name": "Name",
        "snapkey.field.password": "Password",
        "snapkey.field.expiry": "Expires",
        "snapkey.field.times": "Uses",
        "snapkey.field.state": "Status",
        "snapkey.state.0": "Unused",
        "snapkey.state.1": "Used",
        "snapkey.state.2": "Expired",
        "snapkey.state.3": "Deleted/Invalid",
        "snapkey.state.0_expired": "Unused (expired)",
        "snapkey.times.unlimited": "Unlimited",
        "snapkey.times.count": "{n} uses",
        "snapkey.fallback.unknown": "Unknown",
        "snapkey.fallback.unnamed": "Unnamed",
        "snapkey.fallback.none": "None",
    },
}


def normalize_lang(language: object) -> str:
    """语言码归一化: 'zh-CN'/'zh' → zh-Hans(前缀匹配); 未收录回退默认.

    集成中文优先: 未知语言(如 'de')回落 zh-Hans, 与历史行为一致。
    """
    lang = str(language or "").strip()
    if not lang:
        return DEFAULT_LANG
    if lang in _CATALOGS:
        return lang
    base = lang.split("-")[0].lower()
    for key in _CATALOGS:
        if key.lower().startswith(base):
            return key
    return DEFAULT_LANG


def tr(language: object, key: str) -> str | None:
    """词条翻译: 当前语言缺词条 → 回退 zh-Hans; 仍缺 → None(调用方兜底)."""
    lang = normalize_lang(language)
    return (
        _CATALOGS.get(lang, {}).get(key)
        or _CATALOGS[DEFAULT_LANG].get(key)
    )
