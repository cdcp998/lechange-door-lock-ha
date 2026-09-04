"""Text setting for temporary-password generation (名称)."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import LeChangeEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up the temporary-password name text entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities(
        [LeChangeSnapkeyNameText(coordinator, device_id)],
    )


class LeChangeSnapkeyNameText(LeChangeEntity, TextEntity):
    """临时密码名称."""

    def __init__(self, coordinator, device_id):
        super().__init__(coordinator, device_id, "snapkey_name")
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_value = coordinator.snapkey_config.get("name", "Home Assistant")
        self._attr_native_max = 64

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.coordinator.update_snapkey_config(name=value)
        self.async_write_ha_state()
