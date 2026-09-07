"""本地选择接口: 工作模式 + 开门设置(自动上锁时间/音量等) + 截图选择."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_SNAPSHOT_CHANNELS,
    CONF_SNAPSHOT_LAYOUT,
    DEFAULT_SNAPSHOT_CHANNELS,
    DEFAULT_SNAPSHOT_LAYOUT,
    DOOR_SETTING_SELECTS,
    DOMAIN,
    EVENT_PREFIX,
    PROP_POWER_MODE,
    SNAPSHOT_CHANNEL_OPTIONS,
    SNAPSHOT_LAYOUT_OPTIONS,
    WORK_MODE_OPTIONS,
)
from .door_settings import (
    apply_optimistic,
    async_write_property,
    filter_writable,
)
from .entity import LeChangeEntity
from .state_utils import _int_or_none, work_mode_option, work_mode_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up work-mode select + doorfront snapshot channel/layout selects."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    entities = [
        LeChangeWorkModeSelect(coordinator, device_id),
        LeChangeSnapshotChannelSelect(coordinator, device_id),
        LeChangeSnapshotLayoutSelect(coordinator, device_id),
    ]
    # ★ 能力门控: 仅型号声明 ∩ 实测返回 ∩ 可写 的开门设置枚举才建实体
    writable = await filter_writable(coordinator, DOOR_SETTING_SELECTS)
    for prop in writable:
        key, value_to_option = DOOR_SETTING_SELECTS[prop]
        entities.append(
            LeChangeDoorSettingSelect(coordinator, device_id, prop, key, value_to_option)
        )
    async_add_entities(entities)


class LeChangeWorkModeSelect(LeChangeEntity, SelectEntity):
    """工作模式: 自动 / 正常 / 省电 / 超级省电 (powerMode).

    读: coordinator 轮询的 props.powerMode(GetProperties 显式列表已含);
    写: iot.control.SetProperties(设备休眠时必然失败, 由 ensure_awake 语义
    由调用方重试/报错 —— 与童锁开关同一行为)。
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(WORK_MODE_OPTIONS)
    _attr_icon = "mdi:power-settings"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "work_mode")
        self._prop = PROP_POWER_MODE

    @property
    def current_option(self) -> str | None:
        props = (self.coordinator.data or {}).get("props") or {}
        return work_mode_option(props.get(self._prop))

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        value = work_mode_value(option)
        if value is None:
            return
        coordinator = self.coordinator
        try:
            await coordinator.api.async_set_work_mode(
                coordinator.device_id, coordinator.product_id, value
            )
        except Exception as err:
            _LOGGER.warning("工作模式设置失败 (%s): %s", option, err)
            raise HomeAssistantError(f"设置工作模式 {option} 失败: {err}") from err
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "set_property", "device_id": self._device_id,
             "property": self._prop, "value": option},
        )
        await coordinator.async_request_refresh()


class _LeChangeSnapshotSelect(LeChangeEntity, SelectEntity):
    """门外截图选择基类: 持久化到 entry.options, 供 MediaManager 实时读取。

    current_option 动态读 entry.options(不缓存) —— 集成配置页与 select
    实体写的是同一个键, 双入口编辑永远显示同一当前值(改完无需重载)。
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device_id, key, options, config_key, default):
        super().__init__(coordinator, device_id, f"snapshot_{config_key}")
        self._attr_options = list(options)
        self._config_key = config_key
        self._default = default

    @property
    def current_option(self) -> str:
        stored = str((self.coordinator.entry.options or {}).get(self._config_key, self._default))
        return stored if stored in self._attr_options else self._default

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        self.coordinator.update_media_options(**{self._config_key: option})
        self.async_write_ha_state()


class LeChangeSnapshotChannelSelect(_LeChangeSnapshotSelect):
    """门外截图通道: 双摄 0+1 / 单摄 0 猫眼 / 单摄 1 辅摄."""

    def __init__(self, coordinator, device_id):
        super().__init__(
            coordinator,
            device_id,
            "snapshot_channels",
            SNAPSHOT_CHANNEL_OPTIONS,
            CONF_SNAPSHOT_CHANNELS,
            DEFAULT_SNAPSHOT_CHANNELS,
        )
        self._attr_translation_key = "snapshot_channels"


class LeChangeSnapshotLayoutSelect(_LeChangeSnapshotSelect):
    """门外截图布局: 左右组合 / 上下组合 / 单摄单图."""

    def __init__(self, coordinator, device_id):
        super().__init__(
            coordinator,
            device_id,
            "snapshot_layout",
            SNAPSHOT_LAYOUT_OPTIONS,
            CONF_SNAPSHOT_LAYOUT,
            DEFAULT_SNAPSHOT_LAYOUT,
        )
        self._attr_translation_key = "snapshot_layout"


class LeChangeDoorSettingSelect(LeChangeEntity, SelectEntity):
    """开门设置多值选择(自动上锁时间/门未关提醒时间/音量/室内开门模式).

    能力门控创建(filter_writable: 型号声明 ∩ 实测返回 ∩ 可写), 选项表
    与 lock_model_SKG8J5RP.json 枚举 specs 一致; 选项键经 translations
    显示(如 15s → 「15 秒」/ mute → 「静音」)。

    写链路: SetProperties {ref: int} —— ★ int 值铁律(字符串被设备拒
    19999); 休眠时唤醒后重试一次; 成功后乐观同步 + 广播。
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator,
        device_id: str,
        prop: str,
        translation_key: str,
        value_to_option: dict,
    ) -> None:
        super().__init__(coordinator, device_id, translation_key)
        self._prop = prop
        self._value_to_option = {int(k): str(v) for k, v in value_to_option.items()}
        self._option_to_value = {v: k for k, v in self._value_to_option.items()}
        # 注册表声明序即展示序(低→高)
        self._attr_options = list(self._value_to_option.values())

    @property
    def current_option(self) -> str | None:
        v = (self.coordinator.data or {}).get("props", {}).get(self._prop)
        return self._value_to_option.get(_int_or_none(v))

    async def async_select_option(self, option: str) -> None:
        if option not in self._option_to_value:
            return
        value = self._option_to_value[option]
        coordinator = self.coordinator
        try:
            await async_write_property(coordinator, self._prop, value)
        except Exception as err:
            _LOGGER.warning("属性 %s 设置 %s 失败: %s", self._prop, option, err)
            raise HomeAssistantError(f"设置 {self._prop}={option} 失败: {err}") from err
        apply_optimistic(coordinator, self._prop, value)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "set_property", "device_id": self._device_id,
             "property": self._prop, "value": value},
        )
