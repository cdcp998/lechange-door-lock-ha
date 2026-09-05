"""MQTT realtime channel facade for the LeChange integration.

- 连接管理: 后台任务持有 MqttClient, 断线自动重连(指数退避)
- 事件处理: iot_response/iot_request/android_iot_property 推送 → coordinator 回调
- 控制: MQTT iot_request 优先 → 失败/未连接 → 云 API 兜底(iot.control.SetService/
  SetProperties —— 由调用方注入 cloud 方法)
- 云 API 兜底原则: MQTT 只是实时加速通道; 断连时功能不降级(轮询/云控制照常)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .imou_client import ImouAPIError, ImouClient
from .mqtt_client import MqttClient, _build_head

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAYS = (5, 10, 30, 60, 120, 300, 600)  # 指数退避(封顶 10 分钟)
REFRESH_INTERVAL = 60 * 60          # 每小时后重取凭据(token 过期)


class MqttManager:
    """Per-device MQTT realtime channel (coordinator 持有).

    cloud_ctrl: callable(api_name, params) -> dict 云 API 兜底控制(未连时用)
    on_event:  callable(event_type, data) -> None MQTT 推送事件回调
    """

    def __init__(
        self,
        api: ImouClient,
        device_id: str,
        product_id: str,
        cloud_ctrl: Callable[[str, dict], Awaitable[dict]],
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        certs_dir: str = "",
    ) -> None:
        self.api = api
        self.device_id = device_id
        self.product_id = product_id
        self.cloud_ctrl = cloud_ctrl
        self.on_event = on_event
        self.certs_dir = certs_dir

        self._mqtt: MqttClient | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_creds: dict = {}
        self._creds_at: float = 0.0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and bool(self._mqtt and self._mqtt.connected)

    # ------------------------------------------------------------------ life
    async def async_start(self) -> None:
        """启动后台连接循环(幂等)."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def async_stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._mqtt:
            await self._mqtt.async_close()
            self._mqtt = None
        self._connected = False

    async def _run_loop(self) -> None:
        delay_idx = 0
        while True:
            try:
                await self.async_ensure_connected()
                # 连接成功: 保持(等待凭据过期; 底层断线由 MqttClient 内部标记)
                while self._connected:
                    await asyncio.sleep(5)
                    # 定期刷新凭据(token 可能过期)
                    if time.monotonic() - self._creds_at > REFRESH_INTERVAL:
                        await self.async_reconnect()
                    # 底层连接已断(manager 标志未感知) → 主动重连
                    elif self._mqtt and not self._mqtt.connected:
                        _LOGGER.warning("MQTT connection lost; reconnecting")
                        await self.async_reconnect()
                delay_idx = 0
            except asyncio.CancelledError:
                raise
            except ImouAPIError as err:
                # 登录会话失效类(12001/12112): 重连无意义(云 API 主通道照常),
                # 降噪为 info 避免刷屏; 12112 已由 client 触发 reauth 引导
                _LOGGER.info(
                    "MQTT channel unavailable (session error %s) — "
                    "cloud API remains primary; retry in %ss",
                    err.code, RECONNECT_DELAYS[delay_idx],
                )
                await asyncio.sleep(RECONNECT_DELAYS[delay_idx])
                delay_idx = min(delay_idx + 1, len(RECONNECT_DELAYS) - 1)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("MQTT loop error: %s; retry in %ss", err,
                                RECONNECT_DELAYS[delay_idx])
                await asyncio.sleep(RECONNECT_DELAYS[delay_idx])
                delay_idx = min(delay_idx + 1, len(RECONNECT_DELAYS) - 1)

    async def async_ensure_connected(self) -> None:
        """获取凭据并连接(失败抛异常, 供循环重试)."""
        async with self._lock:
            creds = await self._get_creds()
            if self._mqtt and self._mqtt.connected:
                return
            if self._mqtt:
                await self._mqtt.async_close()
            host = ((creds.get("mqttServer") or {}).get("sslAddr") or
                    "iotmqtt-app-hz.imou.com:8883")
            hostname, _, port = host.partition(":")
            mqtt = MqttClient(
                creds.get("clientId", ""),
                "Authorization: x-pcs-signature",
                self._build_password(creds),
                hostname,
                int(port or 8883),
                certs_dir=self.certs_dir,
                on_message=self._on_message,
            )
            try:
                await mqtt.async_connect()
            except Exception:
                # ★ 连接失败 → 凭据缓存立即作废:
                # 服务端拒连(TLS 后断开/认证拒绝)多因凭据过期/被吊销,
                # 若继续沿用 1h 缓存, 循环会拿同一坏凭据重试到天荒地老。
                # 作废后下次重试强制向云 API 取新凭据(带 EVERGREEN 续期)。
                self._last_creds = {}
                self._creds_at = 0.0
                # ★ 半开客户端必须关闭: connect 抛异常时 TLS socket 可能已
                # 建立(CONNACK 前 EOF/认证拒绝) —— 不 close 则 socket 泄漏,
                # 服务端滞留半开会话, 加剧同 clientId 后续连接被拒。
                await mqtt.async_close()
                raise
            self._mqtt = mqtt
            self._connected = True
            _LOGGER.info("MQTT channel ready (cid=%s)", creds.get("clientId", "")[:20])

    async def async_reconnect(self) -> None:
        async with self._lock:
            if self._mqtt:
                await self._mqtt.async_close()
            self._connected = False
        await self.async_ensure_connected()

    # ------------------------------------------------------------------ creds
    async def _get_creds(self) -> dict:
        now = time.monotonic()
        if self._last_creds and (now - self._creds_at) < REFRESH_INTERVAL:
            return self._last_creds
        creds = await self.api.async_get_mqtt_credentials()
        self._last_creds = creds
        self._creds_at = now
        return creds

    def _build_password(self, creds: dict) -> str:
        uid = creds.get("uid") or creds.get("username") or ""
        token = creds.get("token") or self.api.token or ""
        return _build_head("uuid\\" + uid, token, uid, self.api.terminal_id)

    # ---------------------------------------------------------------- control
    async def async_request(self, api: str, params: dict, timeout: float = 10.0) -> dict:
        """控制: MQTT 优先; 未连接/超时 → 云 API 兜底(抛 ImouAPIError 由调用方处理).

        返回业务响应 dict; 云兜底时会标记 {"via": "cloud"}。
        """
        if self.connected and self._mqtt:
            try:
                resp = await self._mqtt.async_request(api, params, timeout=timeout)
                if resp.get("statusCode") == 200:
                    inner = resp.get("params") or {}
                    if inner.get("code") == 10000:
                        return inner | {"via": "mqtt"}
                    raise ImouAPIError(
                        inner.get("code", -1), str(inner.get("desc") or "mqtt failed")
                    )
                raise ImouAPIError(int(resp.get("statusCode", -1)), "mqtt http status")
            except (ConnectionError, asyncio.TimeoutError) as err:
                _LOGGER.debug("MQTT control failed (%s); falling back to cloud", err)
        # 云 API 兜底
        data = await self.cloud_ctrl(api, params)
        if isinstance(data, dict):
            data = dict(data)
            data["via"] = "cloud"
        return data

    async def _on_message(self, topic: str, msg: dict) -> None:
        """MQTT 推送 → coordinator 事件回调."""
        if not self.on_event:
            return
        try:
            await self.on_event({"topic": topic, "msg": msg})
        except Exception:  # noqa: BLE001
            _LOGGER.exception("MQTT event handler failed")


# ==================================================================== 账号级枢纽
def extract_device_id(msg: dict) -> str:
    """从推送消息中尽力提取 deviceId(探测多种已知/疑似嵌套形态).

    账号级 MQTT 主题(iot_response/iot_request/android_iot_property)是
    全账号广播: 一个连接会收到该账号下所有设备的推送。没有 deviceId
    归属就无法判断消息属于哪个条目 —— 多设备场景必须先归属再分发,
    否则 A 条目会把 B 设备的属性合并进自己的实体(数据干扰)。
    """
    if not isinstance(msg, dict):
        return ""
    params = msg.get("params")
    data = msg.get("data")
    containers = (
        msg,
        data if isinstance(data, dict) else None,
        params if isinstance(params, dict) else None,
        msg.get("content") if isinstance(msg.get("content"), dict) else None,
        (params.get("data") if isinstance(params, dict) else None)
        if isinstance(params, dict)
        else None,
        (data.get("params") if isinstance(data, dict) else None)
        if isinstance(data, dict)
        else None,
    )
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("deviceId", "deviceID", "devId"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class AccountMqttHub:
    """账号级 MQTT 通道枢纽: 同账号所有设备条目共用一条 MQTT 连接.

    对齐真机 App 形态 —— 一个账号一条长连接, 全部设备的推送都在这条
    连接上到达。多条目各自建连时 clientId 由 terminal 派生(相同)会
    互相接管踢线;单连接 + 按 deviceId 分发从根上消除该干扰。

    - start_for/stop_for: 各条目 coordinator 注册/注销自己的归属
    - 推送路由: extract_device_id 归属 → 分发给注册的 coordinator;
      无法归属/无人认领的推送一律丢弃(宁可轮询补齐, 不做错误合并)
    - iot_response: 自己请求的响应由 MqttClient 按 seq 匹配消费;
      到达 on_event 的都是异条目请求的响应/超时残响 → 一律丢弃
    """

    def __init__(self, api: ImouClient, certs_dir: str = "", manager: "MqttManager | None" = None) -> None:
        self._manager = manager or MqttManager(
            api,
            "",             # hub 级连接不属于单一设备; 凭据是账号级的
            "",
            cloud_ctrl=self._cloud_ctrl,
            on_event=self._on_event,
            certs_dir=certs_dir,
        )
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}

    # ----------------------------------------------------------- 注册/注销
    def register(self, device_id: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        """注册设备归属(coordinator 重载会覆盖旧 handler)."""
        if device_id:
            self._handlers[device_id] = handler

    def unregister(self, device_id: str) -> None:
        self._handlers.pop(device_id, None)

    @property
    def has_handlers(self) -> bool:
        return bool(self._handlers)

    @property
    def connected(self) -> bool:
        return self._manager.connected

    # ----------------------------------------------------------- 生命周期
    async def start_for(self, device_id: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        self.register(device_id, handler)
        await self._manager.async_start()

    async def stop_for(self, device_id: str) -> None:
        self.unregister(device_id)
        if not self._handlers:
            await self._manager.async_stop()

    async def request(self, api: str, params: dict, timeout: float = 10.0) -> dict:
        """控制请求(与其它条目共用一条连接; seq 匹配各自响应)."""
        return await self._manager.async_request(api, params, timeout=timeout)

    # ------------------------------------------------------------- 分发
    async def _cloud_ctrl(self, api: str, params: dict) -> dict:
        return await self._manager.api.async_post(api, params)

    async def _on_event(self, data: dict) -> None:
        topic = data.get("topic") or ""
        msg = data.get("msg") or {}
        if topic == "iot_response":
            # 未按 seq 匹配上的响应 = 异条目请求的响应/超时迟到残响。
            # 合并它会把他条目(或手机 App)设备的属性写进本条目 → 串扰。
            _LOGGER.debug("MQTT iot_response dropped (no pending seq match)")
            return
        device_id = extract_device_id(msg)
        if not device_id:
            _LOGGER.debug(
                "MQTT push without deviceId dropped: topic=%s keys=%s",
                topic, list(msg)[:6] if isinstance(msg, dict) else type(msg).__name__,
            )
            return
        handler = self._handlers.get(device_id)
        if handler is None:
            # 同账号其它设备的推送(包括未接入 HA 的设备) → 无人认领, 丢弃
            _LOGGER.debug(
                "MQTT push for %s… has no registered entry (topic=%s)",
                device_id[:6], topic,
            )
            return
        try:
            await handler({"topic": topic, "msg": msg})
        except Exception:  # noqa: BLE001
            _LOGGER.exception("MQTT event dispatch failed for %s…", device_id[:6])
