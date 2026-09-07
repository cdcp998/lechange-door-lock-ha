"""平台实体构造冒烟测试: 任何平台 setup 列表中的实体都必须能被构造.

回归背景: sensor 平台 6 个实体类缺 __init__(未传 translation_key),
async_setup_entry 直接 TypeError → 整个 sensor 平台搭建失败,
所有传感器(电量/门状态/电量模式/WiFi/开门记录/临时密码列表)从 HA 消失。
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

if "homeassistant.components.select" not in sys.modules:
    _sel_mod = types.ModuleType("homeassistant.components.select")


    class _SelectEntity:
        pass


    _sel_mod.SelectEntity = _SelectEntity
    sys.modules["homeassistant.components.select"] = _sel_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.select = _sel_mod

if "homeassistant.components.text" not in sys.modules:
    _txt_mod = types.ModuleType("homeassistant.components.text")


    class _TextEntity:
        @property
        def native_value(self):
            # 真实 HA TextEntity.native_value 即返回 _attr_native_value
            return getattr(self, "_attr_native_value", None)


    _txt_mod.TextEntity = _TextEntity
    sys.modules["homeassistant.components.text"] = _txt_mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    ha.components.text = _txt_mod

_const_mod = sys.modules.get("homeassistant.const")
if _const_mod is not None and not hasattr(_const_mod, "EntityCategory"):
    _const_mod.EntityCategory = types.SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic")

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
    select as select_mod,
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
    c.language = ""  # 实例语言(空 → i18n 回退 zh-Hans)
    return c


def test_sensor_platform_entities_constructible():
    """sensor 平台 setup 列表中的每个实体都必须能构造(translation_key 完整)."""
    coord = _fake_coordinator()
    entities = [
        sensor.LeChangeBatterySensor(coord, "DEV1", "battery_lock"),
        sensor.LeChangeBatterySensor(coord, "DEV1", "battery_camera"),
        sensor.LeChangeDoorStateSensor(coord, "DEV1"),
        sensor.LeChangePowerModeSensor(coord, "DEV1"),
        sensor.LeChangeWorkModeSensor(coord, "DEV1"),
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
        button.LeChangeWakeUpButton(coord, "DEV1"),
    ]
    # select(工作模式 + 截图通道/布局)
    sel = [
        select_mod.LeChangeWorkModeSelect(coord, "DEV1"),
        select_mod.LeChangeSnapshotChannelSelect(coord, "DEV1"),
        select_mod.LeChangeSnapshotLayoutSelect(coord, "DEV1"),
    ]
    # camera
    cam = camera.LeChangeCameraEntity(coord, "DEV1", "0")
    for e in [*bs, *sw, *btn, *sel]:
        assert e._attr_unique_id and e._attr_translation_key, type(e).__name__
    assert cam._attr_unique_id == "DEV1_camera_0"


def _ensure_entity_category_stub():
    """运行期兜底: 其他文件的桩可能覆盖 homeassistant.const(无 EntityCategory).

    text.py 引用 EntityCategory.CONFIG/DIAGNOSTIC —— 桩缺符号时补齐,
    保证 text 平台模块在任意测试执行顺序下均可导入。
    """
    const_mod = sys.modules.get("homeassistant.const")
    if const_mod is not None and not hasattr(const_mod, "EntityCategory"):
        const_mod.EntityCategory = types.SimpleNamespace(
            CONFIG="config", DIAGNOSTIC="diagnostic"
        )


def test_text_platform_entities_constructible():
    """text 平台(临时密码名称配置实体)构造冒烟."""
    _ensure_entity_category_stub()
    from lechange_door_lock import text as text_mod

    coord = _fake_coordinator()
    name_e = text_mod.LeChangeSnapkeyNameText(coord, "DEV1")
    assert name_e._attr_translation_key == "snapkey_name"
    assert name_e._attr_unique_id == "DEV1_snapkey_name"


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
        "battery_lock", "door_state", "power_mode", "work_mode", "wifi_signal",
        "last_open", "latest_open_door_time", "latest_open_door_method",
        "snapkey_count", "latest_alarm",
    ):
        assert expect in keys, f"missing sensor entity: {expect}"


@pytest.mark.asyncio
async def test_work_mode_sensor_and_select():
    """工作模式: sensor 读 props.powerMode → 选项键; select 写 → 接口调用."""
    from unittest.mock import AsyncMock

    coord = _fake_coordinator()
    coord.device_id = "DEV1"
    coord.product_id = "PROD1"
    coord.data["props"]["powerMode"] = 2

    sensor_entity = sensor.LeChangeWorkModeSensor(coord, "DEV1")
    assert sensor_entity.native_value == "power_saving"
    assert list(sensor_entity._attr_options) == [
        "auto", "normal", "power_saving", "super_power_saving",
    ]

    sel = select_mod.LeChangeWorkModeSelect(coord, "DEV1")
    assert sel.current_option == "power_saving"
    assert list(sel._attr_options) == [
        "auto", "normal", "power_saving", "super_power_saving",
    ]

    coord.api.async_set_work_mode = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    await sel.async_select_option("super_power_saving")
    coord.api.async_set_work_mode.assert_awaited_once_with("DEV1", "PROD1", 3)
    coord.async_request_refresh.assert_awaited_once()

    # 非法选项 → 拒绝且不触发接口
    coord.api.async_set_work_mode.reset_mock()
    await sel.async_select_option("turbo")
    coord.api.async_set_work_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_mode_unsupported_shows_unavailable():
    """设备模型无 powerMode → sensor/select 均无值(unavailable 语义)."""
    coord = _fake_coordinator()
    coord.data["props"] = {"child_lock": True}  # 无 powerMode
    sensor_entity = sensor.LeChangeWorkModeSensor(coord, "DEV1")
    assert sensor_entity.native_value is None
    sel = select_mod.LeChangeWorkModeSelect(coord, "DEV1")
    assert sel.current_option is None


def test_latest_alarm_sensor_readable_lines():
    """最新告警: alarms 属性输出结构化条目(最新在最上), 不外露原始 dict."""
    coord = _fake_coordinator()
    coord.data["alarms"] = [
        {"refId": 328800, "labelType": "accessAlarm", "pTimestamp": "1788566400",
         "time": "20260905T080000", "title": "有人出门了"},
        {"refId": 339600, "labelType": "accessAlarm", "pTimestamp": "1788567816",
         "time": "20260905T082336", "title": "伟使用指纹开门了"},
    ]
    coord.data["latest_alarm"] = coord.data["alarms"][-1]

    entity = sensor.LeChangeLatestAlarmSensor(coord, "DEV1")
    # native_value: 最新一条(行为 · 标题, 不含时间)
    assert entity.native_value == "开门/出门事件 · 伟使用指纹开门了"
    attrs = entity.extra_state_attributes
    assert attrs["alarm_count"] == 2
    entries = attrs["alarms"]
    # 结构化条目(dict 列表): more-info 逐块渲染, 永不截断
    assert entries[0] == {
        "行为": "开门/出门事件", "标题": "伟使用指纹开门了",
        "时间": "2026-09-05 08:23:36",
    }
    assert entries[1] == {
        "行为": "开门/出门事件", "标题": "有人出门了",
        "时间": "2026-09-05 08:00:00",
    }
    # 原始字段(refId/pTimestamp/alarmId)不再外露
    joined = str(entries)
    assert "refId" not in joined and "pTimestamp" not in joined


def test_snapkey_sensor_dict_list_attributes():
    """临时密码: 单属性 snapkeys = 字典列表(与 alarms 同形).

    more-info 对 dict 列表逐块渲染小表, 每条独立、永不截断; 字符串
    列表/嵌套 dict 会被拼成单行一段(不分行、截断, 界面实测)。
    """
    coord = _fake_coordinator()
    coord.snapkey_list = [
        {"name": "Home Assistant", "tempKey": "89561560",
         "createTime": "1788556812", "state": 0, "number": -1,
         "effectTimes": "1",
         "expiredTime": "1893456000"},   # 2030-01-01 08:00 UTC+8(远期, 不受时钟影响)
        {"name": "Home Assistant", "tempKey": "46753218",
         "createTime": "1788550539", "state": 1, "number": 3,
         "effectTimes": "1",
         "expiredTime": "1893456000"},
    ]
    coord.last_snapkey_result = None
    entity = sensor.LeChangeSnapkeyCountSensor(coord, "DEV1")
    assert entity.native_value == 2
    attrs = entity.extra_state_attributes
    # 单属性字典列表(createTime 新→旧), 每条一张字段小表
    # ★ 次数读 number(-1=不限), 非 effectTimes(有效天数)
    assert attrs["snapkeys"] == [
        {
            "名称": "Home Assistant", "密码": "89561560",
            "过期时间": "2030-01-01 08:00:00",
            "次数": "无限次", "状态": "未使用",
        },
        {
            "名称": "Home Assistant", "密码": "46753218",
            "过期时间": "2030-01-01 08:00:00",
            "次数": "3 次", "状态": "已使用",
        },
    ]
    assert len(attrs) == 1  # 无多余键
    # 未生成过临时密码 → last_generated 不输出(避免 more-info 显示"未知")


def test_snapkey_sensor_last_generated_only_when_present():
    coord = _fake_coordinator()
    coord.snapkey_list = []
    coord.last_snapkey_result = {"name": "P", "tempKey": "12345678"}
    entity = sensor.LeChangeSnapkeyCountSensor(coord, "DEV1")
    attrs = entity.extra_state_attributes
    assert attrs == {"last_generated": {"name": "P", "tempKey": "12345678"}}


def test_door_setting_entities_constructible():
    """开门设置实体(开关/选择/只读状态)构造冒烟(能力门控前的可构造性)."""
    coord = _fake_coordinator()
    switches = [
        switch.LeChangeDoorSettingSwitch(coord, "DEV1", "sdl_autoLock", "auto_lock"),
        switch.LeChangeDoorSettingSwitch(
            coord, "DEV1", "sdl_doorOpenReSwitch", "door_open_reminder"
        ),
    ]
    selects = [
        select_mod.LeChangeDoorSettingSelect(
            coord, "DEV1", "sdl_autoLockTime", "auto_lock_time",
            {15: "15s", 30: "30s", 45: "45s", 60: "60s", 180: "180s"},
        ),
        select_mod.LeChangeDoorSettingSelect(
            coord, "DEV1", "deviceLockVol", "lock_volume",
            {0: "mute", 1: "low", 2: "lower", 3: "normal", 4: "higher", 5: "high"},
        ),
    ]
    state_sensors = [
        sensor.LeChangeDoorSettingStateSensor(
            coord, "DEV1", "sdl_forceLock", "force_lock_state"
        ),
    ]
    for e in [*switches, *selects, *state_sensors]:
        assert e._attr_unique_id and e._attr_translation_key, type(e).__name__
        assert e._attr_unique_id == f"DEV1_{e._attr_translation_key}"
