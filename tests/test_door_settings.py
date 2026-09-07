"""开门设置(能力门控 + int 写入 + 唤醒重试)单元测试.

回归背景(2026-09-06, 开门设置API提取与测试.md):
- 写值必须是 int: 字符串 "0"/"1" 被设备拒(19999 device error code:-1);
- 设备休眠时改值写被拒(19999)/离线(10003), 唤醒后重试即成功;
- 云端按设备能力过滤属性: 型号声明但本机不支持的属性(本机 20 请求 →
  15 返回)被静默丢弃 —— 实体必须双重门控(型号声明 ∩ 实测返回), 否则
  产生永远 unavailable 的"僵尸实体"。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

# 沿用本文件族的最小 HA 桩(conftest 只注册包路径)
from test_config_flow_device_step import _install_ha_stubs  # noqa: E402

_install_ha_stubs()

# 实体平台 + sensor/binary_sensor/switch/button/select/camera 桩
# (与 test_platform_entities 同形; 幂等, 已注册则跳过)
helpers = sys.modules["homeassistant.helpers"]
if not hasattr(helpers, "entity_platform"):
    ep = types.ModuleType("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = object
    helpers.entity_platform = ep
    sys.modules["homeassistant.helpers.entity_platform"] = ep

const_mod = sys.modules.get("homeassistant.const")
if const_mod is None:
    const_mod = types.ModuleType("homeassistant.const")
    const_mod.PERCENTAGE = "%"
    sys.modules["homeassistant.const"] = const_mod
    setattr(sys.modules["homeassistant"], "const", const_mod)
if not hasattr(const_mod, "PERCENTAGE"):
    const_mod.PERCENTAGE = "%"
if not hasattr(const_mod, "EntityCategory"):
    const_mod.EntityCategory = types.SimpleNamespace(
        CONFIG="config", DIAGNOSTIC="diagnostic"
    )

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

_platform_stubs = {
    "sensor": ("SensorDeviceClass", "SensorEntity"),
    "binary_sensor": ("BinarySensorDeviceClass", "BinarySensorEntity"),
    "switch": (None, "SwitchEntity"),
    "button": (None, "ButtonEntity"),
    "select": (None, "SelectEntity"),
    "camera": (None, "Camera"),
}


def _ensure_platform_stub(name: str) -> None:
    key = f"homeassistant.components.{name}"
    if key in sys.modules:
        return
    mod = types.ModuleType(key)
    dev_cls, ent_cls = _platform_stubs[name]
    if dev_cls == "SensorDeviceClass":
        mod.SensorDeviceClass = type(
            "SensorDeviceClass", (), {"BATTERY": "battery", "ENUM": "enum"}
        )
    elif dev_cls == "BinarySensorDeviceClass":
        mod.BinarySensorDeviceClass = type(
            "BinarySensorDeviceClass",
            (), {k: k.lower() for k in
                 ("CONNECTIVITY", "TAMPER", "LOCK", "DOOR", "OPENING", "MOTION")},
        )
    mod.__dict__[ent_cls] = type(ent_cls, (), {})
    if name == "camera":
        mod.CameraEntityFeature = type("CameraEntityFeature", (int,), {"STREAM": 1})
    sys.modules[key] = mod
    ha = sys.modules["homeassistant"]
    ha.components = getattr(ha, "components", types.SimpleNamespace())
    setattr(ha.components, name, mod)


for _name in _platform_stubs:
    _ensure_platform_stub(_name)

from lechange_door_lock import (  # noqa: E402
    const as const_py,
    door_settings,
    select as select_mod,
    sensor,
    switch,
)
from lechange_door_lock.imou_client import ImouAPIError  # noqa: E402


def _coordinator(props: dict | None = None, persisted=None) -> MagicMock:
    """Coordinator 桩: props + entry.options(已确认清单) + api 模型桩."""
    c = MagicMock()
    c.device_id = "DEV1"
    c.product_id = "SKG8J5RP"
    c.data = {"props": dict(props or {})}
    c.entry.options = {} if persisted is None else {
        const_py.CONF_DOOR_SETTINGS_SUPPORTED: list(persisted)
    }
    c._door_setting_caps = None
    c._pending_writes = {}
    # 持久化/乐观登记真实现(与 coordinator 同形, 落实例状态)
    def _persist(identifiers):
        c.entry.options[const_py.CONF_DOOR_SETTINGS_SUPPORTED] = list(identifiers)
    c.persist_door_settings = MagicMock(side_effect=_persist)

    def _register_pending(prop, value):
        c._pending_writes[prop] = (value, float("inf"))
    c.register_pending_write = MagicMock(side_effect=_register_pending)
    # 型号桩: 本机声明面(模拟 QueryModelInfo 子集, 非全量 99 属性)
    model = MagicMock()
    declared = {
        "sdl_autoLock": "rw", "sdl_autoLockTime": "rw",
        "sdl_doorOpenReSwitch": "rw", "sdl_doorOpenReTime": "rw",
        "sdl_faceAutoOpenDoor": "rw", "sdl_antiMmaloperationTime": "rw",
        "sdl_welcomeHomeLightSwitch": "rw", "sdl_hJReOpDoorSwitch": "rw",
        "deviceLockVol": "rw", "sdl_indoorOpenMode": "r",  # 只读!
        "openDoorByTouch": "rw",   # 声明但实测不返回 → 门控排除
        "openDoorMsg": "rw",       # 同上
        "sdl_forceLock": "rw", "electroniclock": "rw", "openDoorCombined": "rw",
    }
    model.has_property = lambda ident: ident in declared
    model.property_access_mode = lambda ident: declared.get(ident, "")
    c.api.async_get_model = AsyncMock(return_value=model)
    c.async_set_updated_data = MagicMock()
    return c


# ------------------------------------------------------- 能力门控(避免污染)


async def test_capabilities_intersect_model_and_observed():
    """能力 = 型号声明 ∩ 实测返回: openDoorByTouch 声明但未返回 → 排除."""
    coord = _coordinator(props={
        "sdl_autoLock": 1, "sdl_doorOpenReSwitch": 1, "deviceLockVol": 5,
    })
    caps = await door_settings.resolve_door_setting_capabilities(coord)
    assert "sdl_autoLock" in caps
    assert "sdl_doorOpenReSwitch" in caps
    assert "deviceLockVol" in caps
    assert "openDoorByTouch" not in caps   # 未实测返回 → 不建实体
    assert "openDoorMsg" not in caps
    assert "sdl_doorOpenReTime" not in caps  # 型号声明但本机未返回
    # 首次确认即持久化(entry.options)
    saved = coord.entry.options[const_py.CONF_DOOR_SETTINGS_SUPPORTED]
    assert set(saved) == caps


async def test_persisted_capabilities_survive_sleep_window():
    """休眠窗口(实测键暂缺)时持久化清单兜底 — 实体不闪失."""
    coord = _coordinator(
        props={"sdl_autoLock": 1},            # 本轮只拿到 1 个键(休眠)
        persisted=["sdl_autoLock", "deviceLockVol"],
    )
    caps = await door_settings.resolve_door_setting_capabilities(coord)
    assert "deviceLockVol" in caps  # 来自持久化, 本轮未返回也保留
    # 无新增确认 → persist_door_settings 不再被调用
    coord.persist_door_settings.assert_not_called()


async def test_unknown_identifier_never_becomes_entity():
    """实测返回但型号未声明(脏键) → 不建实体(污染防线)."""
    coord = _coordinator(props={"some_dirty_key": 1, "sdl_autoLock": 0})
    caps = await door_settings.resolve_door_setting_capabilities(coord)
    assert "some_dirty_key" not in caps
    assert "sdl_autoLock" in caps


async def test_switch_setup_gated_by_capabilities():
    """switch 平台 setup: 只有能力内的可写属性才建开关."""
    coord = _coordinator(props={
        "sdl_autoLock": 1, "sdl_doorOpenReSwitch": 0,
        "sdl_indoorOpenMode": 0,      # 只读(r) → 不建开关
        "openDoorByTouch": 1,         # 实测返回 + 声明可写 → 门控放行(数据驱动)
    })
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {"device_id": "DEV1"}
    hass.data = {"lechange_door_lock": {"E1": coord}}
    added = []
    await switch.async_setup_entry(hass, entry, added.extend)
    props = {e._prop for e in added if isinstance(e, switch.LeChangeDoorSettingSwitch)}
    assert "sdl_autoLock" in props
    assert "sdl_doorOpenReSwitch" in props
    assert "openDoorByTouch" in props   # 三道门都过 → 建(真机未返回则不会出现)
    assert "sdl_indoorOpenMode" not in props   # accessMode=r → 只读不建开关
    assert "sdl_forceLock" not in props        # 高危属性未实测返回(也不该建)


async def test_select_setup_gated_by_capabilities():
    """select 平台 setup: 能力内的枚举建选择, 选项与物模型枚举一致."""
    coord = _coordinator(props={
        "sdl_autoLockTime": 15, "deviceLockVol": 5, "sdl_indoorOpenMode": 0,
    })
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {"device_id": "DEV1"}
    hass.data = {"lechange_door_lock": {"E1": coord}}
    added = []
    await select_mod.async_setup_entry(hass, entry, added.extend)
    sel = {e._prop: e for e in added if isinstance(e, select_mod.LeChangeDoorSettingSelect)}
    assert set(sel) == {"sdl_autoLockTime", "deviceLockVol"}  # indoor r → 排除
    assert sel["sdl_autoLockTime"].current_option == "15s"
    assert sel["sdl_autoLockTime"]._attr_options == ["15s", "30s", "45s", "60s", "180s"]
    assert sel["deviceLockVol"]._attr_options == [
        "mute", "low", "lower", "normal", "higher", "high"
    ]


async def test_sensor_setup_readonly_state_entities():
    """sensor 平台: 高危属性实测返回才建只读状态传感器."""
    coord = _coordinator(props={"sdl_forceLock": 0, "electroniclock": 0})
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {"device_id": "DEV1"}
    hass.data = {"lechange_door_lock": {"E1": coord}}
    added = []
    await sensor.async_setup_entry(hass, entry, added.extend)
    keys = [e._attr_translation_key for e in added]
    assert "force_lock_state" in keys
    assert "electronic_lock_state" in keys
    assert "open_door_combined_state" not in keys  # 本机未实测返回
    state_ent = next(e for e in added if e._attr_translation_key == "force_lock_state")
    assert state_ent.native_value == "off"
    coord.data["props"]["sdl_forceLock"] = 1
    assert state_ent.native_value == "on"


# ------------------------------------------------------- 写入(int 铁律/唤醒)


async def test_switch_write_sends_int_value():
    """开关写链路: SetProperties 收到 int 1/0(字符串会被设备拒 19999)."""
    coord = _coordinator(props={"sdl_autoLock": 1})
    coord.api.async_set_properties = AsyncMock(return_value={"code": 10000})
    sw = switch.LeChangeDoorSettingSwitch(coord, "DEV1", "sdl_autoLock", "auto_lock")
    await sw.async_turn_off()
    args = coord.api.async_set_properties.await_args
    assert args.args[2] == {"sdl_autoLock": 0}          # int, 非 "0"
    assert sw.is_on is False                             # 乐观同步生效
    await sw.async_turn_on()
    assert coord.api.async_set_properties.await_args.args[2] == {"sdl_autoLock": 1}
    assert sw.is_on is True
    coord.async_set_updated_data.assert_called()


async def test_select_write_sends_int_value():
    """选择写链路: 选项键 → 物模型枚举 int 值."""
    coord = _coordinator(props={"deviceLockVol": 5})
    coord.api.async_set_properties = AsyncMock(return_value={"code": 10000})
    sel = select_mod.LeChangeDoorSettingSelect(
        coord, "DEV1", "deviceLockVol", "lock_volume",
        {0: "mute", 1: "low", 2: "lower", 3: "normal", 4: "higher", 5: "high"},
    )
    await sel.async_select_option("mute")
    assert coord.api.async_set_properties.await_args.args[2] == {"deviceLockVol": 0}
    assert sel.current_option == "mute"
    # 非法选项拒绝且不触发接口
    coord.api.async_set_properties.reset_mock()
    await sel.async_select_option("loud")
    coord.api.async_set_properties.assert_not_awaited()


async def test_write_wakes_sleeping_device_then_retries():
    """休眠拒写(19999) → ensure_awake → 重试一次 → 成功."""
    coord = _coordinator(props={"sdl_autoLock": 1})
    coord.api.async_set_properties = AsyncMock(
        side_effect=[ImouAPIError(19999, "device error code:-1"), {"code": 10000}]
    )
    coord.ensure_awake = AsyncMock(return_value=True)
    await door_settings.async_write_property(coord, "sdl_autoLock", 0)
    assert coord.ensure_awake.await_count == 1
    assert coord.api.async_set_properties.await_count == 2
    # 重试仍带 int 值
    assert coord.api.async_set_properties.await_args.args[2] == {"sdl_autoLock": 0}


async def test_write_offline_code_10003_also_wakes():
    """10003(设备休眠/离线)同样走唤醒重试路径."""
    coord = _coordinator(props={"sdl_autoLock": 0})
    coord.api.async_set_properties = AsyncMock(
        side_effect=[ImouAPIError(10003, "device offline"), {"code": 10000}]
    )
    coord.ensure_awake = AsyncMock(return_value=True)
    await door_settings.async_write_property(coord, "sdl_autoLock", 1)
    coord.ensure_awake.assert_awaited_once()


async def test_write_fails_when_wake_unsuccessful():
    """唤醒失败 → 明确报错(设备休眠未写入), 不静默吞掉."""
    coord = _coordinator(props={"sdl_autoLock": 1})
    coord.api.async_set_properties = AsyncMock(
        side_effect=ImouAPIError(19999, "device error code:-1")
    )
    coord.ensure_awake = AsyncMock(return_value=False)
    try:
        await door_settings.async_write_property(coord, "sdl_autoLock", 0)
        raise AssertionError("should raise HomeAssistantError")
    except Exception as err:  # noqa: BLE001
        from homeassistant.exceptions import HomeAssistantError as HAE

        assert isinstance(err, HAE)
    assert coord.api.async_set_properties.await_count == 1  # 不无限重试


async def test_write_non_reject_error_propagates_immediately():
    """非设备拒收错误(如网络 -2/认证)不唤醒重试, 直接抛出."""
    coord = _coordinator(props={"sdl_autoLock": 1})
    coord.api.async_set_properties = AsyncMock(
        side_effect=ImouAPIError(-2, "network: timeout")
    )
    coord.ensure_awake = AsyncMock()
    try:
        await door_settings.async_write_property(coord, "sdl_autoLock", 0)
        raise AssertionError("should raise")
    except ImouAPIError:
        pass
    coord.ensure_awake.assert_not_awaited()


async def test_wake_up_button_calls_ensure_awake():
    """唤醒按钮: ensure_awake + 事件广播."""
    from lechange_door_lock import button

    coord = _coordinator()
    coord.ensure_awake = AsyncMock(return_value=True)
    btn = button.LeChangeWakeUpButton(coord, "DEV1")
    assert btn._attr_translation_key == "wake_up"
    await btn.async_press()
    coord.ensure_awake.assert_awaited_once()
    fired = coord.hass.bus.async_fire.call_args
    assert fired.args[0] == const_py.EVENT_PREFIX
    assert fired.args[1]["type"] == "wake_up"
    assert fired.args[1]["awake"] is True


# ------------------------------------------------- 童锁未知态(防跳动的显示语义)


def test_child_lock_sensor_unknown_shows_unknown_not_off():
    """props 缺 171700(半睡/缺键) → 传感器未知(None), 绝不误报"关".

    状态跳动根因之三: 未知落 False → 缺键闪"关"。修复后 None → unknown。
    """
    from lechange_door_lock import binary_sensor

    coord = _coordinator(props={})          # 无 171700
    entity = binary_sensor.LeChangeChildLockSensor(coord, "DEV1")
    assert entity.is_on is None
    coord.data["props"]["sdl_inOpenDoorModel"] = 2
    assert entity.is_on is True
    coord.data["props"]["sdl_inOpenDoorModel"] = 1
    assert entity.is_on is False


def test_child_lock_switch_unknown_shows_unknown_not_off():
    """童锁开关同语义: 未知 → None(unavailable), 与传感器永不相反."""
    coord = _coordinator(props={})
    sw = switch.LeChangeChildLockSwitch(coord, "DEV1")
    assert sw.is_on is None
    coord.data["props"]["sdl_inOpenDoorModel"] = 2
    assert sw.is_on is True


async def test_door_switch_write_registers_pending():
    """开门设置开关写入 → register_pending_write 登记(防迟到旧值回打)."""
    coord = _coordinator(props={"sdl_autoLock": 1})
    coord.api.async_set_properties = AsyncMock(return_value={"code": 10000})
    sw = switch.LeChangeDoorSettingSwitch(coord, "DEV1", "sdl_autoLock", "auto_lock")
    await sw.async_turn_off()
    # 乐观值 + pending 双登记(真实现由 _coordinator 桩的真实 side_effect 承载)
    assert coord.data["props"]["sdl_autoLock"] == 0
    assert coord._pending_writes["sdl_autoLock"][0] == 0
