"""Sensor platform for the LeChange door lock."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    DOOR_SETTING_SENSORS,
    DOMAIN,
    PROP_LOCK_STATE,
    PROP_POWER_MODE,
    WORK_MODE_OPTIONS,
)
from .door_settings import resolve_door_setting_capabilities
from .entity import LeChangeEntity
from .state_utils import (
    _int_or_none,
    derive_door_state,
    derive_lock_state,
    format_alarm_entries,
    format_alarm_line,
    format_open_door_time,
    format_snapkeys_display,
    open_method_label,
    work_mode_option,
)

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
        LeChangeWorkModeSensor(coordinator, device_id),
        LeChangeWifiSignalSensor(coordinator, device_id),
        LeChangeLastOpenSensor(coordinator, device_id),
        LeChangeLatestOpenDoorTimeSensor(coordinator, device_id),
        LeChangeLatestOpenDoorMethodSensor(coordinator, device_id),
        LeChangeSnapkeyCountSensor(coordinator, device_id),
        LeChangeLatestAlarmSensor(coordinator, device_id),
    ]
    # ★ 开门设置高危属性只读传感器: 能力门控创建(型号声明 ∩ 实测返回);
    #   ⚠️ 强制锁定/电子反锁/组合开门 只读展示, 不提供任何写控制(WI-004)。
    caps = await resolve_door_setting_capabilities(coordinator)
    for prop, key in DOOR_SETTING_SENSORS.items():
        if prop in caps:
            entities.append(
                LeChangeDoorSettingStateSensor(coordinator, device_id, prop, key)
            )
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

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "door_state")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        # ★ 门状态 = 门锁状态(与原 lock 实体同一条判定链):
        # doorLockStatus(0 锁定/1 未锁/2 未知) → lockState(beClosed/beOpened/beAjar)
        # → doorLockState(0 关/1 开)。doorLockStatus 表达"是否锁定",
        # lockState 表达"锁舌状态", 取先可得者。
        locked = derive_lock_state(
            data.get("door_lock_status"),
            (data.get("props") or {}).get("doorLockState"),
            data.get("lock_state", ""),
        )
        if locked is not None:
            return "closed" if locked else "open"
        # 全链不可得 → 回退旧逻辑(doorLockState/lockState 文本)
        state = (data.get("props") or {}).get(PROP_LOCK_STATE)
        return derive_door_state(state, data.get("lock_state", ""))


class LeChangePowerModeSensor(_BaseLeChangeSensor):
    """Power mode: 正常/省电/超级省电."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = POWER_MODE_OPTIONS

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "power_mode")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        mode = (data.get("props") or {}).get("powerState")
        if isinstance(mode, int) and 0 <= mode <= 2:
            return str(mode)
        return "unknown"


class LeChangeWorkModeSensor(_BaseLeChangeSensor):
    """工作模式: 自动/正常/省电/超级省电 (powerMode)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(WORK_MODE_OPTIONS)
    _attr_icon = "mdi:power-settings"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "work_mode")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        mode = (data.get("props") or {}).get(PROP_POWER_MODE)
        return work_mode_option(mode)  # 未知/非法 → None(unavailable)


class LeChangeWifiSignalSensor(_BaseLeChangeSensor):
    """WiFi signal intensity: 0 异常 .. 4 较好."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = WIFI_SIGNAL_OPTIONS

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "wifi_signal")

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

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "last_open")

    @property
    def native_value(self) -> str | None:
        record = self._latest_record()
        if not record:
            return None
        # 归一化形状(user/method) + 原始形状(name/keyType) 双兼容
        name = record.get("user") or record.get("name") or ""
        method = record.get("method") or open_method_label(
            record.get("keyType")
        )
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
        for key in ("time", "localTime", "openTime", "open_time",
                    "recordTime", "unlockTime", "occurTime"):
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
        method = record.get("method")
        if method:
            return str(method)
        key_type = record.get("keyType")
        if key_type is None:
            return None
        return open_method_label(key_type)


class LeChangeSnapkeyCountSensor(_BaseLeChangeSensor):
    """临时密码列表(状态值=数量; 属性 snapkeys=字典列表明细, 按钮刷新后更新)."""

    _attr_icon = "mdi:key-chain-variant"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "snapkey_count")
    @property
    def native_value(self) -> int | None:
        return len(self.coordinator.snapkey_list)

    @property
    def extra_state_attributes(self) -> dict:
        # ★ 字典列表属性(与 latest_alarm 的 alarms 同形): more-info 对
        #   dict 列表逐块渲染小表, 每条独立、永不截断; 词映射随实例语言
        #   (i18n.py, HA 不翻译属性内容由集成产出)。最新在前, 上限 20。
        attrs: dict = {}
        entries = format_snapkeys_display(
            self.coordinator.snapkey_list, lang=self.coordinator.language
        )[:20]
        if entries:
            attrs["snapkeys"] = entries
        # 未生成过临时密码时不输出该键(more-info 显示"未知"很刺眼)
        if self.coordinator.last_snapkey_result:
            attrs["last_generated"] = self.coordinator.last_snapkey_result
        return attrs


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
        return format_alarm_line(alarm, self.coordinator.language) or None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if not data:
            return {}
        alarms = data.get("alarms") or []
        return {
            "alarm_count": len(alarms),
            # 结构化条目列表({行为/标题/时间} 小 dict): more-info 逐块渲染,
            # 每条独立成表 —— 字符串列表会被逗号拼接(不换行、截断, 界面实测)。
            # 顺序: 行为 · 标题 · 时间, 最新在最上; 词映射随实例语言。
            # 原始数据仍在 coordinator.data.alarms(事件负载不受影响)
            "alarms": format_alarm_entries(
                alarms, lang=self.coordinator.language
            ) or None,
        }


DOOR_SETTING_STATE_OPTIONS = ["off", "on"]


class LeChangeDoorSettingStateSensor(_BaseLeChangeSensor):
    """开门设置高危属性只读状态(强制锁定/电子反锁/组合开门).

    ⚠️ 只读展示, 刻意不提供 HA 写控制 —— 开启后仅管理员可开/普通密钥
    失效/改动开锁方式(开门设置API提取与测试.md 高危约束, 与 WI-004
    一致); 状态变化(用户在 App 操作)经轮询/MQTT 推送反映。
    能力门控: 型号声明 ∩ 实测返回才创建。
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = DOOR_SETTING_STATE_OPTIONS

    def __init__(self, coordinator, device_id: str, prop: str, translation_key: str) -> None:
        super().__init__(coordinator, device_id, translation_key)
        self._prop = prop

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        v = _int_or_none((data.get("props") or {}).get(self._prop))
        if v is None:
            return None
        return "on" if v == 1 else "off"
