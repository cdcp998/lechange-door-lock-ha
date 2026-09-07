"""开门设置(OpenDoorSettingDetailPage)属性面: 能力门控 + 安全写入.

来源: API/report/开门设置API提取与测试.md(热更包逆向 + SKG8J5RP 实测):
- 「开门设置」页无专用 REST API, 读写全部走 iot.control.GetProperties /
  SetProperties, 参数即门锁物模型属性(ref 编号)的批量读写;
- ★ 写值铁律: SetProperties 值必须是 **int** —— 字符串会被设备拒
  (19999 device error code:-1), 详见报告 §3.2;
- 设备休眠时: 读 → 仍 10000(云端缓存值); 改值写 → 19999; 唤醒后写即
  成功(things.media.GetRealTransferStreamUrl 请求自带唤醒语义)。

能力门控(避免污染):
  App 侧同一物模型(SKG8J5RP 99 属性)在真机上仅返回其中一部分
  (报告 §3.1: 20 请求 → 15 返回, openDoorByTouch/openDoorMsg 等 5 属性
  被云端静默丢弃)。因此实体创建必须双重确认:
  ① 型号声明: iot.manager.QueryModelInfo 含该属性(不同机型物模型不同);
  ② 实测返回: GetProperties/云端缓存 props 中出现过该标识符;
  ③ 持久化: 首次确认写入 entry.options, 重启不因休眠窗口错过而丢实体。
  三者交集之外的属性绝不建实体 —— 不产生永不更新的"僵尸实体"。
"""

from __future__ import annotations

import logging
from typing import Iterable

from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_DOOR_SETTINGS_SUPPORTED,
    DOOR_SETTING_IDENTIFIERS,
)
from .imou_client import ImouAPIError

_LOGGER = logging.getLogger(__name__)

# 写入被设备拒 → 多为休眠(报告 §3.3: 休眠时改值写 19999; 10003=离线/休眠)
_DEVICE_REJECT_CODES = {10003, 19999}


def persisted_identifiers(coordinator) -> set[str]:
    """entry.options 中持久化的已确认能力清单."""
    saved = (coordinator.entry.options or {}).get(CONF_DOOR_SETTINGS_SUPPORTED)
    return {str(i) for i in saved} if isinstance(saved, (list, tuple, set)) else set()


async def resolve_door_setting_capabilities(coordinator) -> frozenset[str]:
    """解析本机支持的开门设置标识符全集(型号声明 ∩ 实测确认 ∩ 持久化).

    结果在 coordinator 实例上缓存(每条目 setup 只解析一次/持久化一次);
    条目重载(reload)后随新 coordinator 重新解析。
    """
    cached = getattr(coordinator, "_door_setting_caps", None)
    if cached is not None:
        return cached

    props = (coordinator.data or {}).get("props") or {}
    seen = {i for i in DOOR_SETTING_IDENTIFIERS if i in props}
    confirmed = persisted_identifiers(coordinator) | seen

    supported: set[str] = set()
    model = None
    try:
        model = await coordinator.api.async_get_model(
            coordinator.device_id, coordinator.product_id
        )
    except Exception as err:  # noqa: BLE001 — 能力查询失败不阻断平台 setup
        _LOGGER.debug("Door-settings model query failed: %s", err)

    if model is not None:
        for ident in confirmed:
            if not model.has_property(ident):
                continue
            supported.add(ident)
        # 新实测确认的标识符持久化(重启后休眠窗口不再影响)
        newly = sorted(supported - persisted_identifiers(coordinator))
        if newly:
            coordinator.persist_door_settings(
                sorted(supported | persisted_identifiers(coordinator))
            )
            _LOGGER.info(
                "Door-setting capabilities confirmed: %s", newly
            )
    else:
        # 型号不可得: 只信实测返回(证据不足的属性宁可先不建实体)
        supported = seen

    caps = frozenset(supported)
    # 空结果不缓存: 同一条目 setup 周期内后续平台可重试(无 API 开销,
    # 仅读 coordinator.data/客户端模型缓存); 非空结果才固化本次解析。
    if caps:
        coordinator._door_setting_caps = caps  # noqa: SLF001 — 模块内缓存约定
    return caps


async def filter_writable(coordinator, identifiers: Iterable[str]) -> list[str]:
    """能力交集里再过滤出型号声明可写(accessMode 含 'w')的属性."""
    try:
        model = await coordinator.api.async_get_model(
            coordinator.device_id, coordinator.product_id
        )
    except Exception:  # noqa: BLE001
        return []
    caps = await resolve_door_setting_capabilities(coordinator)
    return [
        i for i in identifiers
        if i in caps and "w" in model.property_access_mode(i).lower()
    ]


async def async_write_property(coordinator, prop: str, value: int) -> None:
    """写单个开门设置属性: int 值铁律 + 休眠唤醒后重试一次.

    - 值必须 int(报告 §3.2: 字符串 "0" → 19999 拒收, int 0 → 10000);
    - 设备休眠时改值写被拒(19999)/离线(10003) → ensure_awake(取流请求
      自带唤醒语义, ~5s 上线)后重试一次; 仍失败则抛错。
    """
    value = int(value)
    try:
        await coordinator.api.async_set_properties(
            coordinator.device_id, coordinator.product_id, {prop: value}
        )
        return
    except ImouAPIError as err:
        if err.code not in _DEVICE_REJECT_CODES:
            raise
        _LOGGER.info(
            "Property %s write rejected (%s) — device likely asleep, "
            "waking up then retrying once",
            prop, err,
        )
    if not await coordinator.ensure_awake(max_wait=15.0):
        raise HomeAssistantError(
            f"设备休眠且唤醒失败, 属性 {prop} 未写入; 设备唤醒后请重试"
        )
    await coordinator.api.async_set_properties(
        coordinator.device_id, coordinator.product_id, {prop: value}
    )


def apply_optimistic(coordinator, prop: str, value: int) -> None:
    """写入成功后的乐观同步: props 即时更新 + 广播(与童锁开关同链路).

    设备应答延迟/下次唤醒生效由轮询纠正; 广播让实体立即重算状态,
    避免"点击后 30s 才看到状态变化"。
    """
    coordinator.data.setdefault("props", {})[prop] = value
    try:
        coordinator.async_set_updated_data(coordinator.data)
    except Exception as err:  # noqa: BLE001 — 广播失败不影响写入结果
        _LOGGER.debug("Coordinator broadcast after %s write failed: %s", prop, err)
