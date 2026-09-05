"""本地选择接口: 工作模式 + 门外截图 通道/布局选择."""

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
    DOMAIN,
    EVENT_PREFIX,
    PROP_POWER_MODE,
    SNAPSHOT_CHANNEL_OPTIONS,
    SNAPSHOT_LAYOUT_OPTIONS,
    WORK_MODE_OPTIONS,
)
from .entity import LeChangeEntity
from .state_utils import work_mode_option, work_mode_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up work-mode select + doorfront snapshot channel/layout selects."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities(
        [
            LeChangeWorkModeSelect(coordinator, device_id),
            LeChangeSnapshotChannelSelect(coordinator, device_id),
            LeChangeSnapshotLayoutSelect(coordinator, device_id),
        ],
    )


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
