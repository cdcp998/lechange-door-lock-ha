"""Lock entity for the LeChange door lock (remote open via remoteOpenDoor)."""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_PREFIX, KEY_TYPE_NAMES, PROP_LOCK_STATE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the lock entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LeChangeDoorLock(coordinator, entry.data["device_id"])], True)


class LeChangeDoorLock(CoordinatorEntity, LockEntity):
    """Representation of the smart door lock."""

    _attr_has_entity_name = True
    _attr_translation_key = "door_lock"
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_lock"
        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}

    @property
    def is_locked(self) -> bool | None:
        """True when the door is locked."""
        return self.coordinator.is_locked

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        props = data.get("props", {})
        attrs: dict = {
            "battery_lock": data.get("battery_lock"),
            "battery_camera": data.get("battery_camera"),
            "door_open": props.get(PROP_LOCK_STATE),
            "power_mode": props.get("powerState"),
            "sleeping": data.get("sleeping"),
            "status": data.get("status"),
        }
        wifi = data.get("wifi")
        if wifi:
            attrs["wifi_ssid"] = wifi.get("ssid")
            attrs["wifi_signal"] = wifi.get("intensity")
            attrs["wifi_state"] = wifi.get("status")
        notes = data.get("lock_notes") or []
        if notes:
            last = notes[-1]
            if isinstance(last, dict):
                attrs["last_open_user"] = last.get("name", "")
                attrs["last_open_method"] = KEY_TYPE_NAMES.get(
                    str(last.get("keyType")), str(last.get("keyType", ""))
                )
            attrs["open_record_count"] = len(notes)
        return attrs

    async def async_unlock(self) -> None:
        """Unlock the door remotely (iot.control.SetService remoteOpenDoor)."""
        coordinator = self.coordinator
        result = await coordinator.api.async_set_service(
            coordinator.device_id,
            coordinator.product_id,
            "remoteOpenDoor",
            {},
        )
        _LOGGER.debug("remoteOpenDoor result: %s", result)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX, {"type": "open_door", "device_id": self._device_id, "result": result}
        )
        await coordinator.async_request_refresh()
