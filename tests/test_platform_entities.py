"""平台实体构造冒烟测试: 任何平台 setup 列表中的实体都必须能被构造.

回归背景: sensor 平台 6 个实体类缺 __init__(未传 translation_key),
async_setup_entry 直接 TypeError → 整个 sensor 平台搭建失败,
所有传感器(电量/门状态/电量模式/WiFi/开门记录/临时密码数量)从 HA 消失。
本测试实例化每个平台 setup 列表中的全部实体, 构造签名漂移立即红。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# 沿用本文件族的最小 HA 桩(conftest 只注册了包路径, 不含 HA 模块)
from test_config_flow_device_step import _install_ha_stubs  # noqa: E402

_install_ha_stubs()

# 实体平台桩(sensor/switch/binary_sensor/button/camera 均 import)
helpers = sys.modules["homeassistant.helpers"]
if not hasattr(helpers, "entity_platform"):
    ep = types.ModuleType("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = object
    helpers.entity_platform = ep
    sys.modules["homeassistant.helpers.entity_platform"] = ep
const = types.ModuleType("homeassistant.const")
if not hasattr(const, "PERCENTAGE"):
    const.PERCENTAGE = "%"
    sys.modules["homeassistant.const"] = const
    setattr(sys.modules["homeassistant"], "const", const)

# CoordinatorEntity 桩(entity.py 基类依赖)
if "homeassistant.helpers.update_coordinator" not in sys.modules:
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")


    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success


    uc.CoordinatorEntity = _CoordinatorEntity
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc

import pytest  # noqa: E402

# ---------------------------------------------------------------- HA 组件桩
_already = "homeassistant.components.sensor" in sys.modules
if not _already:
    _sensor_mod = types.ModuleType("homeassistant.components.sensor")


    class _SensorDeviceClass:
        BATTERY = "battery"
        ENUM = "enum"


    class _SensorEntity:
        pass


    _sensor_mod.SensorDeviceClass = _SensorDeviceClass
    _sensor_mod.SensorEntity = _SensorEntity
    sys.modules["homeassistant.components.sensor"] = _sensor_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.sensor = _sensor_mod

if "homeassistant.components.binary_sensor" not in sys.modules:
    _bs_mod = types.ModuleType("homeassistant.components.binary_sensor")


    class _BSDeviceClass:
        CONNECTIVITY = "connectivity"
        TAMPER = "tamper"
        LOCK = "lock"
        BATTERY = "battery"
        BATTERY_CHARGING = "battery_charging"
        DOOR = "door"
        WINDOW = "window"
        OPENING = "opening"
        MOTION = "motion"


    class _BinarySensorEntity:
        pass


    _bs_mod.BinarySensorDeviceClass = _BSDeviceClass
    _bs_mod.BinarySensorEntity = _BinarySensorEntity
    sys.modules["homeassistant.components.binary_sensor"] = _bs_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.binary_sensor = _bs_mod

if "homeassistant.components.switch" not in sys.modules:
    _sw_mod = types.ModuleType("homeassistant.components.switch")


    class _SwitchEntity:
        pass


    _sw_mod.SwitchEntity = _SwitchEntity
    sys.modules["homeassistant.components.switch"] = _sw_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.switch = _sw_mod

if "homeassistant.components.button" not in sys.modules:
    _btn_mod = types.ModuleType("homeassistant.components.button")


    class _ButtonEntity:
        pass


    _btn_mod.ButtonEntity = _ButtonEntity
    sys.modules["homeassistant.components.button"] = _btn_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.button = _btn_mod

if "homeassistant.components.camera" not in sys.modules:
    _cam_mod = types.ModuleType("homeassistant.components.camera")


    class _CameraEntityFeature(int):
        STREAM = 1


    class _Camera:
        def __init__(self):
            pass


    _cam_mod.CameraEntityFeature = _CameraEntityFeature
    _cam_mod.Camera = _Camera
    sys.modules["homeassistant.components.camera"] = _cam_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.camera = _cam_mod

from lechange_door_lock import (  # noqa: E402
    binary_sensor,
    button,
    camera,
    sensor,
    switch,
)


def _fake_coordinator() -> MagicMock:
    c = MagicMock()
    c.data = {
        "channels": [{"channelId": "0", "channelName": "猫眼"}],
        "props": {},
    }
    c.entry.data = {"device_id": "DEV1"}
    return c


def test_sensor_platform_entities_constructible():
    """sensor 平台 setup 列表中的每个实体都必须能构造(translation_key 完整)."""
    coord = _fake_coordinator()
    entities = [
        sensor.LeChangeBatterySensor(coord, "DEV1", "battery_lock"),
        sensor.LeChangeBatterySensor(coord, "DEV1", "battery_camera"),
        sensor.LeChangeDoorStateSensor(coord, "DEV1"),
        sensor.LeChangePowerModeSensor(coord, "DEV1"),
        sensor.LeChangeWifiSignalSensor(coord, "DEV1"),
        sensor.LeChangeLastOpenSensor(coord, "DEV1"),
        sensor.LeChangeLatestOpenDoorTimeSensor(coord, "DEV1"),
        sensor.LeChangeLatestOpenDoorMethodSensor(coord, "DEV1"),
        sensor.LeChangeSnapkeyCountSensor(coord, "DEV1"),
        sensor.LeChangeLatestAlarmSensor(coord, "DEV1"),
    ]
    for e in entities:
        assert getattr(e, "_attr_translation_key", None), type(e).__name__
        assert e._attr_unique_id == f"DEV1_{e._attr_translation_key}"


def test_other_platform_entities_constructible():
    """binary_sensor/switch/button/camera 平台实体构造冒烟."""
    coord = _fake_coordinator()
    # binary_sensor(含通道实体)
    bs = [
        binary_sensor.LeChangeOnlineSensor(coord, "DEV1"),
        binary_sensor.LeChangeSleepingSensor(coord, "DEV1"),
        binary_sensor.LeChangeTamperSensor(coord, "DEV1"),
        binary_sensor.LeChangeChildLockSensor(coord, "DEV1"),
        binary_sensor.LeChangeChannelOnlineSensor(coord, "DEV1", "0"),
    ]
    # switch/button
    sw = [switch.LeChangeChildLockSwitch(coord, "DEV1")]
    btn = [
        button.LeChangeOpenDoorButton(coord, "DEV1"),
        button.LeChangeSnapshotDoorButton(coord, "DEV1"),
        button.LeChangeGenerateSnapkeyButton(coord, "DEV1"),
        button.LeChangeRefreshSnapkeyListButton(coord, "DEV1"),
    ]
    # camera
    cam = camera.LeChangeCameraEntity(coord, "DEV1", "0")
    for e in [*bs, *sw, *btn]:
        assert e._attr_unique_id and e._attr_translation_key, type(e).__name__
    assert cam._attr_unique_id == "DEV1_camera_0"


@pytest.mark.asyncio
async def test_sensor_setup_entry_end_to_end():
    """整平台 setup 冒烟: async_setup_entry 不再 TypeError."""
    import lechange_door_lock.sensor as sensor_mod

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {"device_id": "DEV1"}
    hass.data = {"lechange_door_lock": {"E1": _fake_coordinator()}}
    added = []
    await sensor_mod.async_setup_entry(hass, entry, added.extend)
    keys = [e._attr_translation_key for e in added]
    # 关键实体一个不能少
    for expect in (
        "battery_lock", "door_state", "power_mode", "wifi_signal",
        "last_open", "latest_open_door_time", "latest_open_door_method",
        "snapkey_count", "latest_alarm",
    ):
        assert expect in keys, f"missing sensor entity: {expect}"
