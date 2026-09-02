"""Time settings for temporary-password generation (生效时间段)."""

from __future__ import annotations

import logging
from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import LeChangeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up temporary-password time entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities(
        [
            LeChangeSnapkeyBeginTime(coordinator, device_id),
            LeChangeSnapkeyEndTime(coordinator, device_id),
        ],
        True,
    )


def _parse_time(value: str, default: dt_time) -> dt_time:
    try:
        hour, minute, second = (int(part) for part in str(value).split(":"))
        return dt_time(hour, minute, second)
    except (TypeError, ValueError):
        return default


class _SnapkeyTime(LeChangeEntity, TimeEntity):
    """Base for CONFIG-category snapkey time fields."""

    def __init__(self, coordinator, device_id, key, config_key, default):
        super().__init__(coordinator, device_id, key)
        self._attr_entity_category = EntityCategory.CONFIG
        self._config_key = config_key
        saved = coordinator.snapkey_config.get(config_key)
        self._attr_native_value = _parse_time(saved, _parse_time(default, dt_time(0, 0)))

    async def async_set_value(self, value: dt_time) -> None:
        self._attr_native_value = value
        self.coordinator.update_snapkey_config(
            **{self._config_key: value.strftime("%H:%M:%S")}
        )
        self.async_write_ha_state()


class LeChangeSnapkeyBeginTime(_SnapkeyTime):
    """临时密码起始时间."""

    def __init__(self, coordinator, device_id):
        super().__init__(
            coordinator, device_id, "snapkey_begin_time_picker", "begin_time", "00:00:00"
        )


class LeChangeSnapkeyEndTime(_SnapkeyTime):
    """临时密码结束时间."""

    def __init__(self, coordinator, device_id):
        super().__init__(
            coordinator, device_id, "snapkey_end_time_picker", "end_time", "23:59:59"
        )
