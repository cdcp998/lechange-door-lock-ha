"""账号级运行时: 同账号设备条目共享一条云会话与一条 MQTT 连接.

隔离模型(一个设备一个集成条目一个集成配置, 避免数据干扰):

- **设备层(严格隔离)**: 每个设备一个集成条目、一份独立配置(entry.data/options)、
  一个独立 coordinator、一套实体 —— 任何设备数据/选项/实体互不串扰。
- **传输层(按账号共享)**: 云会话(ImouClient)与 MQTT 长连接按账号共用一条 ——
  对齐真机 App"一个账号一个会话、一条 MQTT 连接"的形态。多条目各自登录/建连
  是数据干扰的根源:
  · 各自 GetToken → 服务端单活跃 token → 10001 token-alive 互踢, 条目轮询失败
  · 各自 MQTT 建连 → clientId 由 terminal 派生(相同) → 连接互相接管, 踢线风暴
  · 共享后推送按 deviceId 归属分发(见 mqtt.AccountMqttHub), 不再串扰

终端一致性: 只有 terminal_id 相同的条目才共享运行时(legacy 混合终端安装
回退旧行为, 各自独立会话/连接)。

本模块不导入 homeassistant(duck-typing hass), 保持与测试桩兼容。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from .const import (
    CONF_API_HOST,
    CONF_INTERNAL_USERNAME,
    CONF_PASSWORD,
    CONF_SESSION_ID,
    CONF_TOKEN,
    CONF_USERNAME,
    DOMAIN,
)
from .imou_client import ImouClient
from .mqtt import AccountMqttHub

_LOGGER = logging.getLogger(__name__)


def _account_key(username: str) -> str:
    return str(username or "").strip()


def _entry_account(entry: Any) -> str:
    return _account_key((entry.data or {}).get(CONF_USERNAME, ""))


def _entry_terminal_id(entry: Any) -> str:
    data = entry.data or {}
    options = entry.options or {}
    return str(data.get("terminal_id") or options.get("terminal_id") or "")


def _certs_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")


class _EntryMqttFacade:
    """条目视角的 MQTT 通道: 接口与 MqttManager 一致, 背后是账号级 hub.

    coordinator 不感知共享 —— async_start/async_stop/connected/async_request
    语义与原 per-entry MqttManager 完全一致, 卸载最后一个条目时连接才关闭。
    """

    def __init__(self, runtime: "AccountRuntime", device_id: str) -> None:
        self._runtime = runtime
        self.device_id = device_id
        self._handler: Callable[[dict], Awaitable[None]] | None = None

    def bind(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        """绑定本条目的推送处理回调(coordinator 构造完成后调用)."""
        self._handler = handler

    async def async_start(self) -> None:
        if self._handler is None:
            raise RuntimeError("MQTT facade used before bind(handler)")
        hub = await self._runtime.ensure_hub()
        await hub.start_for(self.device_id, self._handler)

    async def async_stop(self) -> None:
        hub = self._runtime.hub
        if hub is not None:
            await hub.stop_for(self.device_id)

    @property
    def connected(self) -> bool:
        hub = self._runtime.hub
        return bool(hub and hub.connected)

    async def async_request(self, api: str, params: dict, timeout: float = 10.0) -> dict:
        hub = await self._runtime.ensure_hub()
        return await hub.request(api, params, timeout=timeout)


class AccountRuntime:
    """一个账号(同 terminal)的共享会话 + 共享 MQTT 枢纽."""

    def __init__(self, hass: Any, username: str, terminal_id: str) -> None:
        self.hass = hass
        self.username = _account_key(username)
        self.terminal_id = str(terminal_id or "")
        self.client: ImouClient | None = None
        self.hub: AccountMqttHub | None = None
        self._hub_lock = asyncio.Lock()
        self._active_entry_ids: set[str] = set()
        self._block_listeners: list[Callable[[int], None]] = []

    # ------------------------------------------------------------ 会话
    def bind_client_callbacks(self, client: ImouClient) -> None:
        """把共享 client 的会话持久化/终端拦截回调改接账号级扇出.

        共享 client 只有一组回调槽位 —— 不能让后 setup 的条目覆盖前条目
        的回调(否则会话只扇出到最后条目, 12112 时只有最后条目能进 reauth)。
        """
        client._on_session_update = self._on_client_session_update
        client._on_login_blocked = self._on_client_login_blocked

    def _on_client_session_update(self, session: dict) -> None:
        self.persist_session(session)

    def _on_client_login_blocked(self, code: int) -> None:
        for listener in list(self._block_listeners):
            try:
                listener(code)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Login-blocked listener failed")

    def add_block_listener(self, listener: Callable[[int], None]) -> None:
        if listener not in self._block_listeners:
            self._block_listeners.append(listener)

    def remove_block_listener(self, listener: Callable[[int], None]) -> None:
        try:
            self._block_listeners.remove(listener)
        except ValueError:
            pass

    def ensure_client(self, client_session: Any, entry: Any, terminal_id: str) -> ImouClient:
        """返回共享 ImouClient(首用从引导条目构建; 后续条目复用)."""
        data = entry.data or {}
        if self.client is None:
            self.client = ImouClient(
                client_session,
                username=data.get(CONF_USERNAME, ""),
                password=data.get(CONF_PASSWORD, ""),
                session_id=data.get(CONF_SESSION_ID, ""),
                token=data.get(CONF_TOKEN, ""),
                internal_username=data.get(CONF_INTERNAL_USERNAME, ""),
                api_host=data.get(CONF_API_HOST, ""),
                terminal_id=terminal_id or self.terminal_id,
            )
            return self.client
        # 账号身份恒定同步(改密/终端迁移 → 运行时跟随最新已授信值)
        if data.get(CONF_USERNAME):
            self.client.username = data[CONF_USERNAME]
        if data.get(CONF_PASSWORD):
            self.client.password = data[CONF_PASSWORD]
        if terminal_id:
            self.client.terminal_id = terminal_id
        if not self.client.logged_in:
            # 运行时无可用会话(重启后首次/会话清空) → 用本条目引导快照
            self.client.session_id = data.get(CONF_SESSION_ID, "") or self.client.session_id
            self.client.token = data.get(CONF_TOKEN, "") or self.client.token
            self.client.internal_username = (
                data.get(CONF_INTERNAL_USERNAME, "") or self.client.internal_username
            )
            self.client.api_host = data.get(CONF_API_HOST, "") or self.client.api_host
        return self.client

    def apply_fresh_login(self, login: dict) -> None:
        """把服务端刚验证成功的登录结果写入共享会话并扇出持久化.

        config_flow 登录/重新授权成功后调用 —— 该会话是服务端证明有效的
        最新状态; 共享 client 是同一对象, 所有同账号条目即刻生效。
        """
        if self.client is None or not isinstance(login, dict):
            return
        session_id = str(login.get("session_id") or "")
        if not session_id:
            return
        self.client.session_id = session_id
        if login.get("token"):
            self.client.token = login["token"]
        if login.get("username"):
            self.client.internal_username = login["username"]
        if login.get("host"):
            self.client.api_host = login["host"]
        self.persist_session(
            {
                CONF_SESSION_ID: session_id,
                CONF_TOKEN: self.client.token,
                CONF_INTERNAL_USERNAME: self.client.internal_username,
                CONF_API_HOST: self.client.api_host,
            }
        )

    def persist_session(self, session: dict) -> None:
        """会话扇出: 同账号同终端的**全部**条目引导快照同步.

        共享 client 由任一条目的轮询触发重登 → 新会话是账号级资产:
        所有条目(无论加载与否)的 entry.data 同步, 重启/重载后任何条目
        都能直接续用最新会话, 不再各自拿旧会话互踢式重登(干扰源②)。
        本集成未注册条目更新监听器 → 写 loaded 条目 data 不会引发重载。
        终端不同的条目不写(会话与终端绑定, 跨终端快照无意义)。
        """
        if not isinstance(session, dict) or not session:
            return
        try:
            entries = list(self.hass.config_entries.async_entries(DOMAIN))
        except Exception:  # noqa: BLE001
            return
        for entry in entries:
            try:
                if _entry_account(entry) != self.username:
                    continue
                tid = _entry_terminal_id(entry)
                if self.terminal_id and tid and tid != self.terminal_id:
                    continue
                merged = {**(entry.data or {}), **session}
                self.hass.config_entries.async_update_entry(entry, data=merged)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Session fan-out to an entry failed", exc_info=True)

    # -------------------------------------------------------------- MQTT
    async def ensure_hub(self) -> AccountMqttHub:
        async with self._hub_lock:
            if self.hub is None:
                if self.client is None:
                    raise RuntimeError("AccountRuntime hub requires a client (ensure_client first)")
                self.hub = AccountMqttHub(self.client, certs_dir=_certs_dir())
            return self.hub

    def facade(self, device_id: str) -> _EntryMqttFacade:
        return _EntryMqttFacade(self, device_id)

    # ----------------------------------------------------------- 生命周期
    def mark_entry_active(self, entry_id: str) -> None:
        self._active_entry_ids.add(entry_id)

    def unmark_entry_active(self, entry_id: str) -> None:
        self._active_entry_ids.discard(entry_id)

    @property
    def idle(self) -> bool:
        return not self._active_entry_ids and not (self.hub and self.hub.has_handlers)


# ------------------------------------------------------------------ 注册表
def _registry(hass: Any) -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault("account_runtimes", {})


def resolve_account_runtime(
    hass: Any, username: str, terminal_id: str
) -> tuple["AccountRuntime", bool]:
    """解析(或创建)账号运行时; 返回 (runtime, 可共享).

    已存在 runtime 但 terminal_id 不同 → (existing, False): legacy 混合
    终端安装保持旧行为(条目各自独立会话/连接, 互不影响)。
    """
    key = _account_key(username)
    registry = _registry(hass)
    runtime = registry.get(key)
    if runtime is None:
        runtime = AccountRuntime(hass, username, terminal_id)
        registry[key] = runtime
        return runtime, True
    if terminal_id and runtime.terminal_id and runtime.terminal_id != terminal_id:
        return runtime, False
    return runtime, True


def get_account_runtime(hass: Any, username: str) -> "AccountRuntime | None":
    return _registry(hass).get(_account_key(username))


def apply_fresh_login(hass: Any, username: str, login: dict) -> None:
    """config_flow 登录成功 → 把新会话推给同账号运行时(无运行时则忽略)."""
    runtime = get_account_runtime(hass, username)
    if runtime is not None:
        runtime.apply_fresh_login(login)


def drop_account_runtime_if_idle(hass: Any, username: str) -> None:
    """账号下已无活动条目 → 移除运行时(会话凭据不残留内存)."""
    key = _account_key(username)
    registry = _registry(hass)
    runtime = registry.get(key)
    if runtime is not None and runtime.idle:
        registry.pop(key, None)
        _LOGGER.debug("Account runtime dropped for %s (idle)", key[:4] + "***")
