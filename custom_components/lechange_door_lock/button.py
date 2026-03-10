import asyncio
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    _LOGGER.debug("Setting up button platform for entry %s", entry.entry_id)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data["device_id"]

    entities = [
        LeChangeOpenDoorButton(coordinator, device_id),
        LeChangeWakeUpButton(coordinator, device_id),
    ]
    _LOGGER.debug("Adding %d button entities", len(entities))
    async_add_entities(entities, True)


class LeChangeOpenDoorButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, device_id):
        super().__init__(coordinator)

        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_open_door"

        self._attr_translation_key = "open_door"
        self._attr_has_entity_name = True

        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}

    async def async_press(self) -> None:
        _LOGGER.debug("Remote open button pressed for device %s", self._device_id)
        await self.coordinator.api.async_open_door_remote()


class LeChangeWakeUpButton(CoordinatorEntity, ButtonEntity):

    def __init__(self, coordinator, device_id):
        super().__init__(coordinator)

        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_wake_up"

        self._attr_translation_key = "wake_up"
        self._attr_has_entity_name = True

        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}

    async def async_press(self) -> None:
        _LOGGER.debug("Wake up button pressed for device %s", self._device_id)

        result = await self.coordinator.api.async_wake_up_device()

        if result is not None:

            async def delayed_refresh():
                await asyncio.sleep(10)
                await self.coordinator.async_request_refresh()

            self.hass.async_create_task(delayed_refresh())
