"""Binary sensor platform for the LeChange door lock."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .entity import LeChangeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
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
    async_add_entities(entities)


class _BaseLeChangeBinarySensor(LeChangeEntity, BinarySensorEntity):
    def __init__(self, coordinator, device_id: str, translation_key: str) -> None:
        super().__init__(coordinator, device_id, translation_key)


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
    """Child lock / 童锁 engaged.

    ⚠️ 不用 BinarySensorDeviceClass.LOCK(其"已锁定/已解锁"文本与童锁
    语义易混/与开关显示相反) — 显示 on/off 与开关"开/关"一致。
    """

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "child_lock")

    @property
    def is_on(self) -> bool | None:
        # 单一真源(child_lock 已过期不参与): resolve_child_lock(171700)
        from .state_utils import resolve_child_lock

        resolved = resolve_child_lock((self.coordinator.data or {}).get("props", {}))
        # ★ 未知(键缺失/半睡应答不完整) → None → HA 显示"未知",
        #   绝不回落 False("关") —— 否则 props 短暂缺键时实体就在
        #   开/关 之间跳动(状态不稳定根因之三)。
        return resolved


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
