"""Sensor platform for the LeChange door lock."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN, PROP_LOCK_STATE
from .entity import LeChangeEntity
from .state_utils import derive_door_state, format_open_door_time, open_method_label

DOOR_STATE_OPTIONS = ["closed", "open", "unknown"]
POWER_MODE_OPTIONS = ["0", "1", "2", "unknown"]
WIFI_SIGNAL_OPTIONS = ["0", "1", "2", "3", "4"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up sensor entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    entities = [
        LeChangeBatterySensor(coordinator, device_id, "battery_lock"),
        LeChangeBatterySensor(coordinator, device_id, "battery_camera"),
        LeChangeDoorStateSensor(coordinator, device_id),
        LeChangePowerModeSensor(coordinator, device_id),
        LeChangeWifiSignalSensor(coordinator, device_id),
        LeChangeLastOpenSensor(coordinator, device_id),
        LeChangeLatestOpenDoorTimeSensor(coordinator, device_id),
        LeChangeLatestOpenDoorMethodSensor(coordinator, device_id),
        LeChangeSnapkeyCountSensor(coordinator, device_id),
        LeChangeLatestAlarmSensor(coordinator, device_id),
    ]
    async_add_entities(entities)


class _BaseLeChangeSensor(LeChangeEntity, SensorEntity):
    def __init__(self, coordinator, device_id: str, translation_key: str) -> None:
        super().__init__(coordinator, device_id, translation_key)


class LeChangeBatterySensor(_BaseLeChangeSensor):
    """Battery percentage of the 门锁电池 / 摄像机电池."""

    def __init__(self, coordinator, device_id: str, kind: str) -> None:
        super().__init__(coordinator, device_id, kind)
        self._kind = kind
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if not data:
            return None
        if self._kind == "battery_lock":
            return data.get("battery_lock")
        return data.get("battery_camera")

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class LeChangeDoorStateSensor(_BaseLeChangeSensor):
    """Door physical state: 关/开/未知."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DOOR_STATE_OPTIONS

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        state = (data.get("props") or {}).get(PROP_LOCK_STATE)
        return derive_door_state(state, data.get("lock_state", ""))


class LeChangePowerModeSensor(_BaseLeChangeSensor):
    """Power mode: 正常/省电/超级省电."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = POWER_MODE_OPTIONS

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        mode = (data.get("props") or {}).get("powerState")
        if isinstance(mode, int) and 0 <= mode <= 2:
            return str(mode)
        return "unknown"


class LeChangeWifiSignalSensor(_BaseLeChangeSensor):
    """WiFi signal intensity: 0 异常 .. 4 较好."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = WIFI_SIGNAL_OPTIONS

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        wifi = data.get("wifi") if data else None
        if not wifi:
            return None
        intensity = wifi.get("intensity")
        if isinstance(intensity, int) and 0 <= intensity <= 4:
            return str(intensity)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        wifi = data.get("wifi") if data else None
        if not wifi:
            return {}
        return {"ssid": wifi.get("ssid"), "wifi_status": wifi.get("status")}


class LeChangeLastOpenSensor(_BaseLeChangeSensor):
    """Latest door open record (user + method), from lockNoteReport."""

    _attr_icon = "mdi:door-open"

    @property
    def native_value(self) -> str | None:
        record = self._latest_record()
        if not record:
            return None
        name = record.get("name") or ""
        method = open_method_label(record.get("keyType"))
        parts = [p for p in (name, method) if p]
        return " · ".join(parts) if parts else None

    def _latest_record(self) -> dict | None:
        data = self.coordinator.data
        if not data:
            return None
        record = data.get("latest_open_door_record")
        return record if isinstance(record, dict) and record else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if not data:
            return {}
        notes = data.get("lock_notes") or []
        return {
            "open_record_count": len(notes),
            "records": notes[-10:],
        }


class LeChangeLatestOpenDoorTimeSensor(_BaseLeChangeSensor):
    """最近一次开门时间(格式化 lockNoteReport.localTime)."""

    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "latest_open_door_time")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        record = data.get("latest_open_door_record")
        if not isinstance(record, dict):
            return None
        for key in ("localTime", "time", "openTime", "occurTime", "recordTime"):
            value = record.get(key)
            if value:
                return format_open_door_time(value)
        return None


class LeChangeLatestOpenDoorMethodSensor(_BaseLeChangeSensor):
    """最近一次开门方式(密码/卡片/指纹/远程...)."""

    _attr_icon = "mdi:key-variant"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "latest_open_door_method")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        record = data.get("latest_open_door_record")
        if not isinstance(record, dict):
            return None
        key_type = record.get("keyType")
        if key_type is None:
            return None
        return open_method_label(key_type)


class LeChangeSnapkeyCountSensor(_BaseLeChangeSensor):
    """当前账户下已生成的临时密码分组数量(按钮刷新后更新)."""

    _attr_icon = "mdi:key-chain-variant"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "snapkey_count")

    @property
    def native_value(self) -> int | None:
        return len(self.coordinator.snapkey_list)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "snapkeys": self.coordinator.snapkey_list[-20:],
            "last_generated": self.coordinator.last_snapkey_result,
        }


class LeChangeLatestAlarmSensor(_BaseLeChangeSensor):
    """最新云侧告警(标签/时间/消息),设备休眠时云消息仍可用."""

    _attr_icon = "mdi:bell-alert-outline"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "latest_alarm")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        alarm = data.get("latest_alarm")
        if not isinstance(alarm, dict):
            return None
        label = alarm.get("labelType") or ""
        timer = format_open_door_time(alarm.get("time")) if alarm.get("time") else ""
        parts = [p for p in (label, timer) if p]
        return " · ".join(parts) if parts else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if not data:
            return {}
        alarms = data.get("alarms") or []
        return {
            "alarm_count": len(alarms),
            "alarms": alarms[-10:],
        }
