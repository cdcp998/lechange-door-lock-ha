"""本地选择接口: 临时密码星期 + 门外截图 通道/布局选择。"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_SNAPSHOT_CHANNELS,
    CONF_SNAPSHOT_LAYOUT,
    DEFAULT_SNAPSHOT_CHANNELS,
    DEFAULT_SNAPSHOT_LAYOUT,
    DOMAIN,
    SNAPSHOT_CHANNEL_OPTIONS,
    SNAPSHOT_LAYOUT_OPTIONS,
    SNAPKEY_WEEKDAY_OPTIONS,
)
from .entity import LeChangeEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up weekday select + doorfront snapshot channel/layout selects."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities(
        [
            LeChangeSnapkeyWeekdaySelect(coordinator, device_id),
            LeChangeSnapshotChannelSelect(coordinator, device_id),
            LeChangeSnapshotLayoutSelect(coordinator, device_id),
        ],
    )


class LeChangeSnapkeyWeekdaySelect(LeChangeEntity, SelectEntity):
    """临时密码生效星期:每天/工作日/周末/单日."""

    def __init__(self, coordinator, device_id):
        super().__init__(coordinator, device_id, "snapkey_weekday_mode")
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = list(SNAPKEY_WEEKDAY_OPTIONS)

    @property
    def current_option(self) -> str:
        value = self.coordinator.snapkey_config.get("weekday_mode", "Every day")
        return value if value in self._attr_options else "Every day"

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        self.coordinator.update_snapkey_config(weekday_mode=option)
        self.async_write_ha_state()


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
