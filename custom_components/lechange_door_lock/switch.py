"""Switch platform: writable lock properties (童锁/呼叫转接)."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    DOMAIN,
    EVENT_PREFIX,
    PROP_CALL_TRANSFER,
    PROP_CHILD_LOCK,
)
from .entity import LeChangeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up switch entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    entities = [
        LeChangeChildLockSwitch(coordinator, device_id),
        LeChangeCallTransferSwitch(coordinator, device_id),
    ]
    async_add_entities(entities)


class _BaseLeChangeSwitch(LeChangeEntity, SwitchEntity):
    def __init__(
        self, coordinator, device_id: str, translation_key: str, prop: str
    ) -> None:
        super().__init__(coordinator, device_id, translation_key)
        self._prop = prop

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
        # bool/enum 属性统一发 "1"/"0"(设备模型 _encode_value 对 bool 亦转 1/0,
        # 但部分设备把属性声明为 int: 发 "1"/"0" 最稳)
        value = "1" if on else "0"
        try:
            await coordinator.api.async_set_properties(
                coordinator.device_id, coordinator.product_id, {self._prop: value}
            )
        except Exception as err:
            _LOGGER.warning("属性 %s 设置失败: %s", self._prop, err)
            raise HomeAssistantError(f"设置 {self._prop} 失败: {err}") from err
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
            coordinator, device_id, "call_transfer", PROP_CALL_TRANSFER
        )


