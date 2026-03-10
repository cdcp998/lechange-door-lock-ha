import logging

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    _LOGGER.debug("Setting up sensor platform for entry %s", entry.entry_id)

    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data["device_id"]

    entities = [LeChangeBatterySensor(coordinator, device_id)]

    _LOGGER.debug("Adding %d sensor entities", len(entities))
    async_add_entities(entities,True)


class LeChangeBatterySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id):
        super().__init__(coordinator)

        self._device_id = device_id

        self._attr_unique_id = f"{device_id}_battery"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_device_class = SensorDeviceClass.BATTERY

        self._attr_translation_key = "battery"
        self._attr_has_entity_name = True

        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)}
        }

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None

        return self.coordinator.data.get("battery_level")
