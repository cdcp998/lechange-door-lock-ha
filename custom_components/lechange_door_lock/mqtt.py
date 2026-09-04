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

RECONNECT_DELAYS = (5, 10, 30, 60)  # 指数退避
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
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("MQTT loop error: %s; retry in %ss", err,
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
            await mqtt.async_connect()
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
