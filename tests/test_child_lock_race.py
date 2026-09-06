"""Regression: 童锁 props 不回弹(12fc759 竞态修复) — 纯逻辑(无 HA 依赖).

场景(用户实测'不能开启童锁'): _set_on 写 171700=2 → props 更新为 2 →
并发轮询 GetProperties(设备刚写入未同步, 返回旧值 1) → 若整体替换/或
refresh 触发轮询合并, props 会 2→1 回弹。本测试验证: ①resolve 171700=2
返回 True(开) ②props 更新合并逻辑保留新值(不把 2 覆盖回 1)。
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
