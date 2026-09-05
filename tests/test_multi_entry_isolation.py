"""多设备(多条目)数据隔离回归测试.

隔离模型: 一个设备一个集成条目一个集成配置 —— 设备层严格隔离;
账号层共享一条云会话与一条 MQTT 连接(对齐真机 App 形态)。

历史干扰形态(本文件锁死的行为边界):
  ① 账号级 MQTT 主题是全账号广播, 每条目各持连接会把其它设备的属性
     推送合并进自己的 data(props) → A 锁实体显示 B 锁电量/状态;
  ② iot_response 中未按 seq 匹配上的响应(异条目请求的响应/超时残响)
     被当推送合并 → 陈旧/他设备数据污染;
  ③ 同账号多条目各自登录/建连 → 服务端单活跃 token(10001)互踢 +
     MQTT clientId(terminal 派生)相同 → 接管踢线风暴;
  ④ www 文件名跨条目互相覆盖。

本文件自带最小 hass 桩(config_entries 注册表), 与 conftest 的
无 HA 加载策略一致。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import lechange_door_lock.mqtt as mqtt_mod
import lechange_door_lock.account_runtime as ar
from lechange_door_lock.const import DOMAIN


# --------------------------------------------------------------------- stubs
class _StubEntry:
    """hass.config_entries 注册表里的条目桩(data 可变, 记录更新次数)."""

    def __init__(self, entry_id: str, data: dict, options: dict | None = None):
        self.entry_id = entry_id
        self.data = dict(data)
        self.options = dict(options or {})
        self.updates: list[dict] = []


class _StubConfigEntries:
    def __init__(self, entries: list[_StubEntry]):
        self._entries = entries

    def async_entries(self, domain=None):
        # 对齐真实 HA: 可选 domain 过滤(本 domain 全数返回)
        return list(self._entries)

    def async_update_entry(self, entry, data=None, options=None):
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)
        entry.updates.append({"data": data, "options": options})


def _make_hass(entries: list[_StubEntry]) -> SimpleNamespace:
    hass = SimpleNamespace()
    hass.data = {}
    hass.config_entries = _StubConfigEntries(entries)
    return hass


def _entry_data(username: str, device_id: str, terminal_id: str = "T1",
                session_id: str = "", token: str = "") -> dict:
    return {
        "username": username,
        "device_id": device_id,
        "terminal_id": terminal_id,
        "session_id": session_id,
        "token": token,
        "internal_username": "uuid-internal",
        "api_host": "https://app-gz-hw.imou.com",
    }


# ------------------------------------------------------------ extract_device_id
def test_extract_device_id_top_level():
    assert mqtt_mod.extract_device_id({"deviceId": "SN1", "data": {}}) == "SN1"


def test_extract_device_id_nested_data_and_params():
    assert mqtt_mod.extract_device_id({"data": {"deviceId": "SN2"}}) == "SN2"
    assert mqtt_mod.extract_device_id({"params": {"data": {"deviceId": "SN3"}}}) == "SN3"
    assert mqtt_mod.extract_device_id({"content": {"deviceId": "SN4"}}) == "SN4"


def test_extract_device_id_missing_or_junk():
    assert mqtt_mod.extract_device_id({"properties": {"powerState": 1}}) == ""
    assert mqtt_mod.extract_device_id("not-a-dict") == ""
    assert mqtt_mod.extract_device_id({"deviceId": "   "}) == ""


# ------------------------------------------------------------------ hub 路由
class _StubManager:
    """替代 MqttManager: 只暴露 hub 用到的面, 不真正建连."""

    def __init__(self):
        self.api = SimpleNamespace(
            async_post=None,  # cloud_ctrl 不会被这些测试触发
        )
        self.started = 0
        self.stopped = 0
        self.requests: list[tuple] = []

    async def async_start(self):
        self.started += 1

    async def async_stop(self):
        self.stopped += 1

    @property
    def connected(self):
        return True

    async def async_request(self, api, params, timeout=10.0):
        self.requests.append((api, params, timeout))
        return {"via": "mqtt"}


def _make_hub() -> tuple[mqtt_mod.AccountMqttHub, _StubManager]:
    mgr = _StubManager()
    hub = mqtt_mod.AccountMqttHub(api=None, certs_dir="", manager=mgr)
    return hub, mgr


@pytest.mark.asyncio
async def test_hub_routes_push_to_owning_device_only():
    """同账号两台设备: 推送只进归属条目, 他设备推送不串扰."""
    hub, _mgr = _make_hub()
    got_a: list[dict] = []
    got_b: list[dict] = []

    async def handler_a(data):
        got_a.append(data)

    async def handler_b(data):
        got_b.append(data)

    await hub.start_for("SN_A", handler_a)
    await hub.start_for("SN_B", handler_b)

    push_a = {"topic": "android_iot_property", "msg": {"deviceId": "SN_A", "data": {"powerState": 1}}}
    push_b = {"topic": "android_iot_property", "msg": {"deviceId": "SN_B", "data": {"powerState": 0}}}
    await hub._on_event(push_a)
    await hub._on_event(push_b)

    assert [d["msg"]["deviceId"] for d in got_a] == ["SN_A"]
    assert [d["msg"]["deviceId"] for d in got_b] == ["SN_B"]


@pytest.mark.asyncio
async def test_hub_drops_unattributed_and_foreign_pushes():
    """无 deviceId 归属 / 无人认领的推送一律丢弃(宁可轮询补齐)."""
    hub, _mgr = _make_hub()
    got: list[dict] = []

    async def handler(data):
        got.append(data)

    await hub.start_for("SN_A", handler)

    # 无归属 → 丢弃
    await hub._on_event({"topic": "android_iot_property", "msg": {"data": {"x": 1}}})
    # 他设备(未注册条目, 如账号里的摄像头) → 丢弃
    await hub._on_event({"topic": "android_iot_property", "msg": {"deviceId": "SN_OTHER", "data": {}}})
    # iot_response(非 seq 匹配残响) → 丢弃
    await hub._on_event({"topic": "iot_response", "msg": {"params": {"data": {"deviceId": "SN_A", "properties": {}}}}})

    assert got == []


@pytest.mark.asyncio
async def test_hub_stop_when_empty_closes_connection():
    """最后一个条目注销后才关闭共享连接; 仍有条目时连接保持."""
    hub, mgr = _make_hub()

    async def handler(data):
        pass

    await hub.start_for("SN_A", handler)
    await hub.start_for("SN_B", handler)
    await hub.stop_for("SN_A")
    assert mgr.stopped == 0
    await hub.stop_for("SN_B")
    assert mgr.stopped == 1


@pytest.mark.asyncio
async def test_hub_request_delegates_to_manager():
    hub, mgr = _make_hub()

    async def handler(data):
        pass

    await hub.start_for("SN_A", handler)
    result = await hub.request("iot.control.SetService", {"deviceId": "SN_A"})
    assert result == {"via": "mqtt"}
    assert mgr.requests == [("iot.control.SetService", {"deviceId": "SN_A"}, 10.0)]


@pytest.mark.asyncio
async def test_hub_handler_exception_does_not_break_dispatch():
    """一个条目的 handler 抛异常不影响通道(下条推送照常分发)."""
    hub, _mgr = _make_hub()
    got: list[dict] = []

    async def bad_handler(data):
        raise RuntimeError("boom")

    async def good_handler(data):
        got.append(data)

    await hub.start_for("SN_A", bad_handler)
    await hub.start_for("SN_B", good_handler)
    await hub._on_event({"topic": "android_iot_property", "msg": {"deviceId": "SN_A"}})
    await hub._on_event({"topic": "android_iot_property", "msg": {"deviceId": "SN_B"}})
    assert len(got) == 1 and got[0]["msg"]["deviceId"] == "SN_B"


# --------------------------------------------------------------- 账号运行时
def test_runtime_shares_client_for_same_account_and_terminal():
    """同账号同终端的两个条目 → 共享同一个 ImouClient(不互踢)."""
    hass = _make_hass([])
    runtime, shared = ar.resolve_account_runtime(hass, "user@mail.com", "T1")
    assert shared is True

    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    entry2 = _StubEntry("e2", _entry_data("user@mail.com", "SN2", "T1", session_id="sid-x"))
    client1 = runtime.ensure_client(None, entry1, "T1")
    client2 = runtime.ensure_client(None, entry2, "T1")
    assert client1 is client2
    # 已登录的共享会话不被后来条目的旧快照覆盖
    runtime.client.session_id = "sid-live"
    runtime.client.token = "tok-live"
    client3 = runtime.ensure_client(None, entry2, "T1")
    assert client3.session_id == "sid-live"
    assert client3.token == "tok-live"

    # 未登录时 → 用条目引导快照补会话
    runtime.client.session_id = ""
    runtime.client.token = ""
    runtime.ensure_client(None, entry2, "T1")
    assert runtime.client.session_id == "sid-x"


def test_runtime_not_shared_across_different_terminals():
    """terminal 不同的 legacy 条目 → 不可共享(回退独立会话/连接)."""
    hass = _make_hass([])
    runtime, shared_first = ar.resolve_account_runtime(hass, "user@mail.com", "T1")
    assert shared_first is True
    _same, shared_second = ar.resolve_account_runtime(hass, "user@mail.com", "T2")
    assert shared_second is False


def test_runtime_registry_is_per_account():
    hass = _make_hass([])
    rt_a, _ = ar.resolve_account_runtime(hass, "user@mail.com", "T1")
    rt_b, _ = ar.resolve_account_runtime(hass, "other@mail.com", "T1")
    assert rt_a is not rt_b
    assert ar.get_account_runtime(hass, "user@mail.com") is rt_a


def test_persist_session_fan_out_isolation():
    """会话扇出: 同账号同终端条目全量同步; 异账号/异终端不动.

    共享 client 由任一条目触发重登, 新会话是账号级资产 → 全部条目的
    引导快照(重启/重载引导)都要拿到, 否则各自旧会话互踢式重登。
    本集成未注册条目更新监听器 → 写 loaded 条目 data 不会引发重载。
    """
    e_loaded_self = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    e_loaded_sibling = _StubEntry("e2", _entry_data("user@mail.com", "SN2", "T1"))
    e_unloaded = _StubEntry("e3", _entry_data("user@mail.com", "SN3", "T1"))
    e_other_account = _StubEntry("e4", _entry_data("other@mail.com", "SN4", "T1"))
    e_other_terminal = _StubEntry("e5", _entry_data("user@mail.com", "SN5", "T9"))
    hass = _make_hass([e_loaded_self, e_loaded_sibling, e_unloaded, e_other_account, e_other_terminal])

    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, e_loaded_self, "T1")
    runtime.mark_entry_active("e1")
    runtime.mark_entry_active("e2")

    session = {"session_id": "sid-new", "token": "tok-new",
               "internal_username": "uuid-internal", "api_host": "https://app-gz-hw.imou.com"}
    runtime.persist_session(session)

    # 同账号同终端: 全部同步(加载与否不影响)
    for e in (e_loaded_self, e_loaded_sibling, e_unloaded):
        assert e.data["session_id"] == "sid-new"
        assert e.data["token"] == "tok-new"
    # 异账号 / 异终端: 不动
    assert e_other_account.data.get("session_id", "") == ""
    assert e_other_terminal.data.get("session_id", "") == ""


def test_block_listener_fans_out_to_all_active_coordinators():
    """12112 终端拦截: 所有活动条目都收到广播(各自 reauth 引导).

    共享 client 只有一组回调槽位 —— 后 setup 条目覆盖前条目回调会让
    前条目永远看不到拦截信号(修过回归, 此处锁死)。
    """
    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    hass = _make_hass([entry1])
    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    client = runtime.ensure_client(None, entry1, "T1")
    runtime.bind_client_callbacks(client)

    hits: list[tuple[str, int]] = []

    def blocker_a(code):
        hits.append(("a", code))

    def blocker_b(code):
        hits.append(("b", code))

    runtime.add_block_listener(blocker_a)
    runtime.add_block_listener(blocker_b)
    # 触发: 模拟 client 内部调用回调(两个监听器都收到)
    client._on_login_blocked(12112)
    assert hits == [("a", 12112), ("b", 12112)]

    # 卸载 a 后只剩 b
    runtime.remove_block_listener(blocker_a)
    client._on_login_blocked(12001)
    assert hits[-1] == ("b", 12001)

    # 会话持久化也走账号级扇出
    client._on_session_update({"session_id": "sid-via-callback"})
    assert entry1.data["session_id"] == "sid-via-callback"


def test_apply_fresh_login_updates_client_and_fans_out():
    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    e_unloaded = _StubEntry("e2", _entry_data("user@mail.com", "SN2", "T1"))
    hass = _make_hass([entry1, e_unloaded])

    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, entry1, "T1")
    runtime.mark_entry_active("e1")
    # 模块级 apply_fresh_login 经 hass.data 注册表查找 → 先注册
    hass.data.setdefault(DOMAIN, {}).setdefault("account_runtimes", {})["user@mail.com"] = runtime

    ar.apply_fresh_login(hass, "user@mail.com", {
        "session_id": "sid-fresh", "token": "tok-fresh",
        "username": "uuid-fresh", "host": "https://app2.imou.com",
    })

    assert runtime.client.session_id == "sid-fresh"
    assert runtime.client.token == "tok-fresh"
    assert runtime.client.internal_username == "uuid-fresh"
    assert runtime.client.api_host == "https://app2.imou.com"
    # 源条目 + 未加载兄弟拿到新会话
    assert entry1.data["session_id"] == "sid-fresh"
    assert e_unloaded.data["session_id"] == "sid-fresh"


def test_apply_fresh_login_noop_without_runtime_or_session():
    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    hass = _make_hass([entry1])
    # 无运行时 → 不抛错不建运行时
    ar.apply_fresh_login(hass, "user@mail.com", {"session_id": "sid"})
    assert ar.get_account_runtime(hass, "user@mail.com") is None
    # 有运行时但登录结果无 session → 不动
    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, entry1, "T1")
    hass.data.setdefault(DOMAIN, {}).setdefault("account_runtimes", {})["user@mail.com"] = runtime
    ar.apply_fresh_login(hass, "user@mail.com", {"token": "tok-only"})
    assert runtime.client.session_id == ""


def test_idle_runtime_dropped_and_active_kept():
    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    hass = _make_hass([entry1])
    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, entry1, "T1")
    hass.data.setdefault(DOMAIN, {}).setdefault("account_runtimes", {})["user@mail.com"] = runtime

    runtime.mark_entry_active("e1")
    ar.drop_account_runtime_if_idle(hass, "user@mail.com")
    assert ar.get_account_runtime(hass, "user@mail.com") is runtime

    runtime.unmark_entry_active("e1")
    ar.drop_account_runtime_if_idle(hass, "user@mail.com")
    assert ar.get_account_runtime(hass, "user@mail.com") is None


@pytest.mark.asyncio
async def test_facade_lifecycle_and_stop_when_idle():
    """条目 facade: start 注册归属, stop 注销; 连接随最后一个条目关闭."""
    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    hass = _make_hass([entry1])
    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, entry1, "T1")

    mgr = _StubManager()
    hub = mqtt_mod.AccountMqttHub(api=None, certs_dir="", manager=mgr)
    runtime.hub = hub  # 预置(模拟 ensure_hub 已建)

    facade1 = runtime.facade("SN1")
    got: list[dict] = []

    async def handler(data):
        got.append(data)

    facade1.bind(handler)
    await facade1.async_start()
    assert mgr.started == 1
    await hub._on_event({"topic": "android_iot_property", "msg": {"deviceId": "SN1"}})
    assert len(got) == 1

    facade2 = runtime.facade("SN2")
    facade2.bind(handler)
    await facade2.async_start()
    await facade1.async_stop()
    assert mgr.stopped == 0    # SN2 还在
    await facade2.async_stop()
    assert mgr.stopped == 1    # 最后一个 → 关闭连接

    # 未 bind 就 start → 显式报错(防静默丢推送)
    facade3 = runtime.facade("SN3")
    with pytest.raises(RuntimeError):
        await facade3.async_start()


def test_persist_session_merges_not_replaces_entry_data():
    """扇出是合并而非整体替换: 设备字段/凭据不被会话键冲掉."""
    entry = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1", session_id="old"))
    hass = _make_hass([entry])
    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, entry, "T1")
    runtime.persist_session({"session_id": "new"})
    assert entry.data["device_id"] == "SN1"
    assert entry.data["terminal_id"] == "T1"
    assert entry.data["session_id"] == "new"


@pytest.mark.asyncio
async def test_facade_bound_by_coordinator_construction_contract():
    """coordinator 共享路径必须 bind handler 才能启动(防止静默丢推送).

    这里直接验证 facade 契约: 未 bind → async_start 抛 RuntimeError;
    bind 后可重复 start(幂等由 hub manager 保证)。
    """
    entry1 = _StubEntry("e1", _entry_data("user@mail.com", "SN1", "T1"))
    hass = _make_hass([entry1])
    runtime = ar.AccountRuntime(hass, "user@mail.com", "T1")
    runtime.ensure_client(None, entry1, "T1")
    mgr = _StubManager()
    runtime.hub = mqtt_mod.AccountMqttHub(api=None, certs_dir="", manager=mgr)

    facade = runtime.facade("SN1")
    with pytest.raises(RuntimeError):
        await facade.async_start()

    async def handler(data):
        pass

    facade.bind(handler)
    await facade.async_start()
    assert mgr.started == 1
    # 重复 start: 真实 MqttManager.async_start 幂等(任务在跑直接返回);
    # 桩只计数, 故预期 2
    await facade.async_start()
    assert mgr.started == 2

    runtime.unmark_entry_active("e1")
    await facade.async_stop()
