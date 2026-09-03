"""Binary sensor platform for the LeChange door lock."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data["device_id"]
    entities = [
        LeChangeOnlineSensor(coordinator, device_id),
        LeChangeSleepingSensor(coordinator, device_id),
        LeChangeTamperSensor(coordinator, device_id),
        LeChangeChildLockSensor(coordinator, device_id),
    ]
    # 通道在线状态(主/辅摄像头通道)
    for ch in (coordinator.data or {}).get("channels", []):
        entities.append(
            LeChangeChannelOnlineSensor(coordinator, device_id, str(ch.get("channelId", "0")))
        )
    async_add_entities(entities, True)


class _BaseLeChangeBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, device_id: str, translation_key: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{device_id}_{translation_key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}


class LeChangeOnlineSensor(_BaseLeChangeBinarySensor):
    """Device online (not sleeping)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "online")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        # MQTT 通道在线(实时推送)优先; 否则轮询 online/sleeping
        if data.get("mqtt_online") is True:
            return True
        if data.get("sleeping") is True:
            return False
        return bool(data.get("online"))


class LeChangeSleepingSensor(_BaseLeChangeBinarySensor):
    """Device is in the low-power sleep state."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "sleeping")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        if data.get("sleeping") is True:
            return True
        props = data.get("props") or {}
        sleep_status = props.get("sleepStatus")
        if isinstance(sleep_status, bool):
            return sleep_status
        return False


class LeChangeTamperSensor(_BaseLeChangeBinarySensor):
    """Anti-tamper alarm (防拆)."""

    _attr_device_class = BinarySensorDeviceClass.TAMPER

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "tamper")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("props", {}).get("tamper"))


class LeChangeChildLockSensor(_BaseLeChangeBinarySensor):
    """Child lock / 童锁 engaged."""

    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "child_lock")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("props", {}).get("child_lock"))


class LeChangeChannelOnlineSensor(_BaseLeChangeBinarySensor):
    """通道在线状态 (channelList[].status)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, device_id: str, channel_id: str) -> None:
        # unique_id 必须带通道号,避免多通道实体 id 冲突
        super().__init__(coordinator, device_id, f"channel_{channel_id}_online")
        self._channel_id = channel_id
        self._attr_translation_key = "channel_online"
        self._attr_translation_placeholders = {"channel_id": channel_id}

    @property
    def is_on(self) -> bool:
        for ch in (self.coordinator.data or {}).get("channels", []):
            if str(ch.get("channelId")) == str(self._channel_id):
                return ch.get("status") == "online"
        return False
