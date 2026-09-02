"""Switch platform: writable lock properties (童锁/呼叫转接/触屏开门)."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    EVENT_PREFIX,
    PROP_CALL_TRANSFER,
    PROP_CHILD_LOCK,
    PROP_OPEN_DOOR_BY_TOUCH,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up switch entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data["device_id"]
    entities = [
        LeChangeChildLockSwitch(coordinator, device_id),
        LeChangeCallTransferSwitch(coordinator, device_id),
        LeChangeOpenDoorByTouchSwitch(coordinator, device_id),
    ]
    async_add_entities(entities, True)


class _BaseLeChangeSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator, device_id: str, translation_key: str, prop: str, is_enum: bool = False
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._prop = prop
        self._is_enum = is_enum
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{device_id}_{translation_key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}

    def _value(self):
        return (self.coordinator.data or {}).get("props", {}).get(self._prop)

    @property
    def is_on(self) -> bool:
        v = self._value()
        if isinstance(v, bool):
            return v
        return v in (1, "1", "true", "True")

    async def _set_on(self, on: bool) -> None:
        coordinator = self.coordinator
        if self._is_enum:
            # enum 属性与设备返回形态一致:字符串 "1"/"0"
            value = "1" if on else "0"
        else:
            value = on
        await coordinator.api.async_set_properties(
            coordinator.device_id, coordinator.product_id, {self._prop: value}
        )
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "set_property", "device_id": self._device_id,
             "property": self._prop, "value": on},
        )
        await coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        await self._set_on(True)

    async def async_turn_off(self) -> None:
        await self._set_on(False)


class LeChangeChildLockSwitch(_BaseLeChangeSwitch):
    """童锁开关 (child_lock)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "child_lock", PROP_CHILD_LOCK)


class LeChangeCallTransferSwitch(_BaseLeChangeSwitch):
    """门铃呼叫转接开关 (sdl_callTransferSwitch, 枚举 0/1)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(
            coordinator, device_id, "call_transfer", PROP_CALL_TRANSFER, is_enum=True
        )


class LeChangeOpenDoorByTouchSwitch(_BaseLeChangeSwitch):
    """触屏开门开关 (openDoorByTouch)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "open_door_by_touch", PROP_OPEN_DOOR_BY_TOUCH)
