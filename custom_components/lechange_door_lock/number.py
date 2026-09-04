"""Number settings for temporary-password generation (用次数/有效天数)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import LeChangeEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up temporary-password number entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities(
        [
            LeChangeSnapkeyEffectiveNum(coordinator, device_id),
            LeChangeSnapkeyEffectiveDay(coordinator, device_id),
        ],
    )


class _SnapkeyNumber(LeChangeEntity, NumberEntity):
    """Base for CONFIG-category snapkey numeric fields."""

    def __init__(self, coordinator, device_id, key, config_key, default, minimum, maximum):
        super().__init__(coordinator, device_id, key)
        self._attr_entity_category = EntityCategory.CONFIG
        self._config_key = config_key
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = 1
        self._attr_native_value = coordinator.snapkey_config.get(config_key, default)

    async def async_set_native_value(self, value: float) -> None:
        value = int(value)
        self._attr_native_value = value
        self.coordinator.update_snapkey_config(**{self._config_key: value})
        self.async_write_ha_state()


class LeChangeSnapkeyEffectiveNum(_SnapkeyNumber):
    """临时密码使用次数(-1 不限)."""

    def __init__(self, coordinator, device_id):
        super().__init__(
            coordinator, device_id, "snapkey_effective_num", "effective_num", -1, -1, 100
        )


class LeChangeSnapkeyEffectiveDay(_SnapkeyNumber):
    """临时密码有效天数."""

    def __init__(self, coordinator, device_id):
        super().__init__(
            coordinator, device_id, "snapkey_effective_day", "effective_day", 1, 1, 90
        )
