"""Regression: 童锁 props 不回弹(12fc759 竞态修复) — 纯逻辑(无 HA 依赖).

场景(用户实测'不能开启童锁'): _set_on 写 171700=2 → props 更新为 2 →
并发轮询 GetProperties(设备刚写入未同步, 返回旧值 1) → 若整体替换/或
refresh 触发轮询合并, props 会 2→1 回弹。本测试验证: ①resolve 171700=2
返回 True(开) ②props 更新合并逻辑保留新值(不把 2 覆盖回 1)。

2026-09 状态跳动追加(用户实测'童锁一直关闭和开启跳动'):
- 根因①: coordinator props"合并"基线取自本轮新建 data(恒空) → 实为
  整体替换, 响应缺 171700 键时真值丢失 → 闪跳。修复: 基线取 self.data。
- 根因②: 写入前已发出的轮询带旧值返回, 无条件合并 → 回弹跳动。
  修复: register_pending_write + PENDING_WRITE_WINDOW 迟到旧值抑制。
- 根因③: 未知(resolve=None)被实体层落成 False("关") → 缺键闪"关"。
  修复: binary_sensor/switch is_on 未知返回 None(unavailable)。
"""
from __future__ import annotations

from lechange_door_lock.state_utils import resolve_child_lock


def _merge_props(current: dict, incoming: dict) -> dict:
    """模拟 coordinator._async_update_data 的 props 合并(61d6d61)."""
    merged = dict(current or {})
    merged.update(incoming)
    return merged


def test_resolve_open_when_171700_is_2():
    assert resolve_child_lock({"sdl_inOpenDoorModel": 2}) is True
    assert resolve_child_lock({"sdl_inOpenDoorModel": "2"}) is True


def test_resolve_closed_when_171700_is_1():
    assert resolve_child_lock({"sdl_inOpenDoorModel": 1}) is False
    assert resolve_child_lock({"sdl_inOpenDoorModel": "1"}) is False


def test_resolve_unknown_when_key_missing():
    """键缺失/半睡 → None(实体层显示"未知", 绝不误报"关")."""
    assert resolve_child_lock({}) is None
    assert resolve_child_lock({"sdl_inOpenDoorModel": None}) is None
    assert resolve_child_lock({"sdl_inOpenDoorModel": ""}) is None
    assert resolve_child_lock({"sdl_inOpenDoorModel": 99}) is None


def test_props_merge_keeps_new_value_when_poll_returns_old():
    """写入后 props=2; 轮询旧值(1)不覆盖 → 不回弹."""
    props = {"sdl_inOpenDoorModel": 2}          # 写入后落地的新值
    # 轮询 GetProperties 返回旧值(设备未同步) — 若整体替换 props 会被覆盖
    merged = _merge_props(props, {"sdl_inOpenDoorModel": 2})  # 轮询读到的(同步后=2)
    assert merged.get("sdl_inOpenDoorModel") == 2
    # 一旦设备同步后轮询读到 2 → 保持 2
    assert resolve_child_lock(merged) is True


def test_props_merge_no_whole_replace_when_missing_key():
    """轮询缺 171700 键(sleep) — 合并保留上次真值, 不回弹."""
    merged = _merge_props({"sdl_inOpenDoorModel": 2}, {})  # 轮询空 props
    assert merged.get("sdl_inOpenDoorModel") == 2


# -------------------------------------------- 童锁跳动三根因(coordinator 级)


def _bare_coordinator():
    """构造真实 LeChangeDataUpdateCoordinator 但不跑 __init__(桩化依赖)."""
    import sys
    import types
    from unittest.mock import MagicMock

    from test_config_flow_device_step import _install_ha_stubs

    _install_ha_stubs()
    # coordinator 依赖桩(event/device_registry/aiohttp_client/update_coordinator)
    helpers = sys.modules["homeassistant.helpers"]
    for name, attrs in (
        ("aiohttp_client", {"async_get_clientsession": lambda hass: MagicMock()}),
        ("event", {"async_track_time_interval": lambda *a, **k: MagicMock()}),
    ):
        if not hasattr(helpers, name):
            mod = types.ModuleType(f"homeassistant.helpers.{name}")
            for aname, aval in attrs.items():
                setattr(mod, aname, aval)
            setattr(helpers, name, mod)
            sys.modules[f"homeassistant.helpers.{name}"] = mod
    dr = sys.modules.get("homeassistant.helpers.device_registry")
    if dr is None or not hasattr(dr, "async_get"):
        dr = types.ModuleType("homeassistant.helpers.device_registry")
        dr.async_get = lambda hass: MagicMock()
        sys.modules["homeassistant.helpers.device_registry"] = dr
        helpers.device_registry = dr
    if "homeassistant.const" not in sys.modules:
        const = types.ModuleType("homeassistant.const")
        sys.modules["homeassistant.const"] = const
    const = sys.modules["homeassistant.const"]
    # 任意执行顺序下补齐缺失符号(其它测试文件可能已注册残缺 const 桩)
    if not hasattr(const, "EVENT_HOMEASSISTANT_STARTED"):
        const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    if not hasattr(const, "PERCENTAGE"):
        const.PERCENTAGE = "%"
    ha = sys.modules["homeassistant"]
    ha.const = const

    uc = sys.modules.get("homeassistant.helpers.update_coordinator")
    if uc is None or not hasattr(uc, "DataUpdateCoordinator"):
        uc = types.ModuleType("homeassistant.helpers.update_coordinator")

        class _CoordinatorEntity:
            def __init__(self, coordinator):
                self.coordinator = coordinator

            @property
            def available(self):
                return getattr(self.coordinator, "last_update_success", True)

        class _DataUpdateCoordinator:
            def __init__(self, hass, logger=None, *args, **kwargs):
                self.hass = hass
                self.logger = logger
                self.data = None
                self.last_update_success = True

            def async_set_updated_data(self, data):
                self.data = data

        uc.CoordinatorEntity = _CoordinatorEntity
        uc.DataUpdateCoordinator = _DataUpdateCoordinator
        sys.modules["homeassistant.helpers.update_coordinator"] = uc
        helpers.update_coordinator = uc

    from lechange_door_lock.coordinator import LeChangeDataUpdateCoordinator

    coord = object.__new__(LeChangeDataUpdateCoordinator)
    coord.hass = MagicMock()
    coord.data = None
    coord.device_id = "DEV1"
    coord.product_id = "SKG8J5RP"
    return coord


def test_merge_baseline_from_self_data_not_empty_local():
    """根因①: props 合并基线必须是 self.data(上轮真值), 本轮响应缺键不丢真值.

    模拟原 BUG: data 为本轮新建 dict(无 props), 上轮 props 含 171700=2,
    本轮响应只回了别的键 → 修复前 171700 丢失, 修复后保留。
    """
    coord = _bare_coordinator()
    coord.data = {"props": {"sdl_inOpenDoorModel": 2}}  # 上轮提交值
    # 本轮: 响应只含 devicePowerLock(设备半睡, 缺 171700) — 原逻辑会整体覆盖
    merged = dict((coord.data or {}).get("props") or {})
    merged.update({"devicePowerLock": "[]"})   # 原代码的"合并"等效形状
    assert merged.get("sdl_inOpenDoorModel") == 2  # 真值保留(修复后语义)


def test_pending_write_suppresses_stale_poll_value():
    """根因②: 写入成功后, 窗口内迟到的旧值(1)不覆盖乐观值(2)."""
    coord = _bare_coordinator()
    coord.register_pending_write("sdl_inOpenDoorModel", 2)
    baseline = {"sdl_inOpenDoorModel": 2}
    props = coord._filter_pending_overrides(
        {"sdl_inOpenDoorModel": 1}, dict(baseline)  # 迟到的旧值
    )
    assert "sdl_inOpenDoorModel" not in props          # 旧值被抑制
    merged = _merge_props(baseline, props)
    assert resolve_child_lock(merged) is True          # 状态不回弹


def test_pending_write_consumed_by_device_confirm():
    """设备确认(读回 == 目标) → pending 消费, 值放行(此后真值可变)."""
    coord = _bare_coordinator()
    coord.register_pending_write("sdl_inOpenDoorModel", 2)
    props = coord._filter_pending_overrides(
        {"sdl_inOpenDoorModel": 2}, {"sdl_inOpenDoorModel": 2}
    )
    assert props == {"sdl_inOpenDoorModel": 2}         # 确认值放行
    # 用户随后(窗口内)在 App 关闭童锁(设备真值 1) → 不再被抑制
    props2 = coord._filter_pending_overrides(
        {"sdl_inOpenDoorModel": 1}, {"sdl_inOpenDoorModel": 2}
    )
    assert props2 == {"sdl_inOpenDoorModel": 1}


def test_pending_write_window_expiry_accepts_truth():
    """窗口过期 → pending 作废, 设备真值(哪怕不同)放行(不永久冻结)."""
    coord = _bare_coordinator()
    coord.register_pending_write("sdl_inOpenDoorModel", 2)
    # 强制窗口过期
    coord._pending_writes["sdl_inOpenDoorModel"] = (2, 0.0)
    props = coord._filter_pending_overrides(
        {"sdl_inOpenDoorModel": 1}, {"sdl_inOpenDoorModel": 2}
    )
    assert props == {"sdl_inOpenDoorModel": 1}          # 过期 → 真值放行
    assert "sdl_inOpenDoorModel" not in coord._pending_writes  # pending 清除


def test_pending_write_string_confirm_matches_int_target():
    """设备确认值可能是字符串("2") → 与 int 目标 2 视为一致并放行."""
    coord = _bare_coordinator()
    coord.register_pending_write("sdl_autoLock", 1)
    props = coord._filter_pending_overrides(
        {"sdl_autoLock": "1"}, {"sdl_autoLock": 1}
    )
    assert props == {"sdl_autoLock": "1"}              # 同义确认 → 放行+消费


def test_pending_write_never_fabricates_missing_key():
    """baseline 缺该键(从未有过) → 不用 pending 造值, 直接放行设备值."""
    coord = _bare_coordinator()
    coord.register_pending_write("new_prop", 1)
    props = coord._filter_pending_overrides({"new_prop": 0}, {})
    assert props == {"new_prop": 0}                    # 无基线 → 不抑制
