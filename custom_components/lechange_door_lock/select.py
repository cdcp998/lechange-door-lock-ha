"""Weekday selection for temporary-password generation."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from .const import CONF_DEVICE_ID, DOMAIN, SNAPKEY_WEEKDAY_OPTIONS
from .entity import LeChangeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the temporary-password weekday select."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LeChangeSnapkeyWeekdaySelect(coordinator, entry.data[CONF_DEVICE_ID])],
        True,
    )


class LeChangeSnapkeyWeekdaySelect(LeChangeEntity, SelectEntity):
    """临时密码生效星期:每天/工作日/周末/单日."""

    def __init__(self, coordinator, device_id):
        super().__init__(coordinator, device_id, "snapkey_weekday_mode")
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = list(SNAPKEY_WEEKDAY_OPTIONS)
        value = coordinator.snapkey_config.get("weekday_mode", "Every day")
        self._attr_current_option = value if value in self._attr_options else "Every day"

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        self._attr_current_option = option
        self.coordinator.update_snapkey_config(weekday_mode=option)
        self.async_write_ha_state()
