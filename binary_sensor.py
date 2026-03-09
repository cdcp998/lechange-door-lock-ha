"""Binary sensor platform for LeChange Door Lock."""
import logging

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
async def async_setup_entry(hass, entry, async_add_entities):
    """Set up LeChange binary sensor based on a config entry."""
    _LOGGER.debug("Setting up binary_sensor platform for entry %s", entry.entry_id)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data["device_id"]

    entities = [
        LeChangeOnlineBinarySensor(coordinator, device_id),
    ]
    _LOGGER.debug("Added main online sensor")

    # 添加通道传感器
    channels = coordinator.data.get("channels", [])
    _LOGGER.debug("Found %d channels in data", len(channels))
    for ch in channels:
        _LOGGER.debug("Adding channel %s online sensor", ch.get("channelId"))
        entities.append(LeChangeChannelBinarySensor(coordinator, device_id, ch["channelId"]))

    _LOGGER.debug("Adding %d binary_sensor entities", len(entities))
    async_add_entities(entities, True)
    _LOGGER.debug("Binary sensor platform setup completed")


class LeChangeOnlineBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of LeChange device online status."""

    def __init__(self, coordinator, device_id):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_online"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_translation_key = "online"
        self._attr_has_entity_name = True

    @property
    def is_on(self):
        """Return true if device is online."""
        online = self.coordinator.data.get("online")
        return online == "1"

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._device_id)}}


class LeChangeChannelBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of LeChange channel online status."""

    def __init__(self, coordinator, device_id, channel_id):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._channel_id = channel_id
        self._attr_unique_id = f"{device_id}_ch{channel_id}_online"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_translation_key = "channel_online"  # 添加此行
        self._attr_has_entity_name = True
        self._attr_translation_placeholders = {"channel_id": str(channel_id)}  # 添加此行

    @property
    def is_on(self):
        """Return true if channel is online."""
        for ch in self.coordinator.data.get("channels", []):
            if str(ch.get("channelId")) == str(self._channel_id):
                online = self.coordinator.data.get("online")
                return str(online) == "1"
        return False

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._device_id)}}