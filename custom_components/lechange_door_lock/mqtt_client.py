"""LeChange (Imou) IoT MQTT channel — asyncio client with cloud-API fallback.

协议要点(实测全通):
  ① 凭据: POST /pcs/v1/client_v2/auth/get (apiver 6550, identifier="lcbaseapp"+android_id)
     → {clientId, mqttServer{sslAddr:8883}, username:"Authorization: x-pcs-signature"}
  ② CONNECT(3.1.1): clientId=响应.clientId; username=常量; password=头部 JSON
     x-pcs-*(client-ua/date/nonce/username/signature HMAC-SHA256(canonical, 登录token))
  ③ 订阅 iot_response / iot_request / android_iot_property
  ④ 请求: publish "iot_request" {"api":"iot.control.GetProperties","params":{...},"seq":N}
  ⑤ 响应: "iot_response" {"id":N,"seq":N,"statusCode":200,"params":{...}}
     业务体在 params 内(statusCode=HTTP 语义)

★ 云 API 兜底: MQTT 断开/未连接/超时 → 控制走云 API(iot.control.SetService /
  SetProperties),事件读不到时由 coordinator 轮询补齐。MQTT 只是实时加速通道。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import socket
import ssl
import string
import struct
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

_LOGGER = logging.getLogger(__name__)

# 订阅主题(App 同款)
TOPIC_RESPONSE = "iot_response"
TOPIC_REQUEST = "iot_request"
TOPIC_PROPERTY = "android_iot_property"

MQTT_TIMEOUT = 10.0          # 连接/读写超时
MQTT_KEEPALIVE = 60          # CONNECT keepalive

# MQTT 3.1.1 包类型
PKT_CONNECT = 0x10
PKT_CONNACK = 0x20
PKT_PUBLISH = 0x30
PKT_PUBACK = 0x40
PKT_SUBSCRIBE = 0x82
PKT_SUBACK = 0x90
PKT_PINGREQ = 0xC0
PKT_PINGRESP = 0xD0
PKT_DISCONNECT = 0xE0


# ------------------------------------------------------------------ helpers
def _enc_str(s: str) -> bytes:
    return struct.pack(">H", len(s.encode())) + s.encode()


def _remaining_len(n: int) -> bytes:
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out.append(d)
        if n == 0:
            break
    return bytes(out)


def _mqtt_packet(ptype: int, payload: bytes) -> bytes:
    return bytes([ptype]) + _remaining_len(len(payload)) + payload


def _pkt_connect(client_id: str, username: str, password: str) -> bytes:
    """CONNECT(3.1.1): clean session + username + password flags."""
    vh = b"\x00\x04MQTT\x04\xc2\x00\x3c"  # proto "MQTT" level 4, flags, keepalive 60
    payload = _enc_str(client_id) + _enc_str(username) + _enc_str(password)
    return _mqtt_packet(PKT_CONNECT, vh + payload)


def _pkt_subscribe(topics: list[str], pid: int) -> bytes:
    body = struct.pack(">H", pid)
    for t in topics:
        body += _enc_str(t) + b"\x01"  # QoS 1
    return _mqtt_packet(PKT_SUBSCRIBE, body)


def _pkt_publish(topic: str, payload: bytes, pid: int) -> bytes:
    body = _enc_str(topic) + struct.pack(">H", pid) + payload
    return _mqtt_packet(PKT_PUBLISH | 0x02, body)  # QoS 1


def _pkt_ping() -> bytes:
    return bytes([PKT_PINGREQ, 0x00])


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one MQTT packet: (type_flags, payload)."""
    head = await reader.readexactly(1)
    ptype = head[0]
    mult, rem = 1, 0
    while True:
        b = (await reader.readexactly(1))[0]
        rem += (b & 0x7F) * mult
        mult *= 128
        if not (b & 0x80):
            break
    payload = await reader.readexactly(rem) if rem else b""
    return ptype, payload


def _build_ua(user_id: str, terminal_id: str) -> str:
    """客户端 UA(与 imou_client 池对齐; 固定 terminal_id 保证稳定)."""
    data = {
        "clientType": "android", "clientVersion": "10.2.2.0831", "clientOV": "15",
        "clientOS": "android", "terminalModel": "HA-Integration-Box",
        "terminalId": terminal_id, "appid": "lcbaseapp", "project": "Base",
        "language": "zh-CN", "clientProtocolVersion": "V9.7.6", "ttid": "",
        "userId": str(user_id), "timezoneOffset": "480", "terminalBrand": "Generic",
        "country": "CN", "darkMode": "0",
    }
    return base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def _build_head(user: str, secret: str, user_id: str, terminal_id: str,
                conn_type: str = "main") -> str:
    """MQTT password = 头部 JSON(含 HMAC-SHA256 签名)."""
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ua = _build_ua(user_id, terminal_id)
    h = {"x-pcs-client-ua": ua, "x-pcs-date": date, "x-pcs-nonce": nonce,
         "x-pcs-username": user, "x-pcs-conn-type": conn_type}
    canon = f"x-pcs-client-ua:{ua}\nx-pcs-date:{date}\nx-pcs-nonce:{nonce}\nx-pcs-username:{user}\n"
    sig = base64.b64encode(
        hmac.new(secret.encode(), canon.encode(), hashlib.sha256).digest()
    ).decode()
    h["x-pcs-signature"] = sig
    return json.dumps(h, separators=(",", ":"))


def _tls_context(certs_dir: str = "") -> ssl.SSLContext:
    """TLS context: 加载插件内置 CA(dh_sub_ca/ims_root_ca/dahua-root, DER→PEM).

    MQTT 服务器(iotmqtt-app-hz.imou.com:8883)使用自签证书链,
    必须加载乐橙客户端分发的内置 CA 才能验证(系统信任库不含)。
    """
    ctx = ssl.create_default_context()
    if certs_dir:
        for fname in ("dh_sub_ca.crt", "ims_root_ca.crt", "dahua-root.pem"):
            path = os.path.join(certs_dir, fname)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                if b"-----BEGIN" not in raw:
                    # DER → PEM
                    raw = (b"-----BEGIN CERTIFICATE-----\n"
                           + base64.encodebytes(raw)
                           + b"-----END CERTIFICATE-----\n")
                ctx.load_verify_locations(cadata=raw.decode())
            except (OSError, ssl.SSLError) as err:
                _LOGGER.debug("CA load failed %s: %s", fname, err)
    return ctx


# ------------------------------------------------------------------ client
class MqttClient:
    """Async MQTT 3.1.1 client for LeChange IoT channel.

    - async_connect(): 建立 TLS 连接 + CONNECT/CONNACK + SUBSCRIBE
    - async_publish(topic, payload): QoS1 publish(等 PUBACK)
    - async_request(api, params): publish iot_request + 等 iot_response(seq 匹配)
    - on_message: 回调(解析推送, 转 coordinator)
    - 云 API 兜底由调用方(coordinator/services)保证: 连接失败/请求超时 → 云 API。
    """

    def __init__(
        self,
        client_id: str,
        username: str,
        password: str,
        host: str,
        port: int = 8883,
        certs_dir: str = "",
        on_message: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    ) -> None:
        self.client_id = client_id
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.certs_dir = certs_dir
        self.on_message = on_message
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._seq = random.randint(1000, 9999)
        self._pending: dict[int, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._read_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def async_connect(self) -> None:
        """连接并完成 CONNECT/CONNACK/SUBSCRIBE(失败抛异常)."""
        self._loop = asyncio.get_running_loop()
        # ssl.create_default_context()/证书文件读取是阻塞调用 —
        # HA 2026 起在事件循环内执行会被判违规, 必须移入 executor
        ctx = await self._loop.run_in_executor(
            None, _tls_context, self.certs_dir
        )
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host, self.port, ssl=ctx, server_hostname=self.host
                ),
                timeout=MQTT_TIMEOUT,
            )
        except (socket.gaierror, OSError, asyncio.TimeoutError) as err:
            raise ConnectionError(f"MQTT connect {self.host}:{self.port}: {err}") from err

        self._writer.write(_pkt_connect(self.client_id, self.username, self.password))
        await self._writer.drain()
        ptype, payload = await asyncio.wait_for(
            _read_packet(self._reader), timeout=MQTT_TIMEOUT
        )
        if ptype != PKT_CONNACK or (payload and payload[0] != 0):
            rc = payload[0] if payload else -1
            raise ConnectionError(f"MQTT CONNACK failed rc={rc}")
        # 订阅
        self._writer.write(_pkt_subscribe(
            [TOPIC_RESPONSE, TOPIC_REQUEST, TOPIC_PROPERTY], pid=1
        ))
        await self._writer.drain()
        # 读 SUBACK
        ptype, _payload = await asyncio.wait_for(
            _read_packet(self._reader), timeout=MQTT_TIMEOUT
        )
        if ptype != PKT_SUBACK:
            raise ConnectionError(f"MQTT SUBACK failed type={hex(ptype)}")
        self._connected = True
        self._read_task = asyncio.create_task(self._read_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())
        _LOGGER.info("MQTT connected: %s cid=%s", self.host, self.client_id)

    async def async_close(self) -> None:
        self._connected = False
        for task in (self._read_task, self._ping_task):
            if task:
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
        self._reader = self._writer = None

    async def async_publish(self, topic: str, payload: bytes, pid: int) -> None:
        """QoS1 publish(等服务端 PUBACK)."""
        if not self._connected or not self._writer:
            raise ConnectionError("MQTT not connected")
        self._writer.write(_pkt_publish(topic, payload, pid))
        await self._writer.drain()
        # 读 PUBACK(可容忍若干读取在 read_loop 中)
        # read_loop 会把 PUBACK 丢弃; 这里不等 PUBACK(保持简单)

    async def async_request(self, api: str, params: dict, timeout: float = 12.0) -> dict:
        """publish iot_request 并等 iot_response(seq 匹配). 云 API 兜底由调用方。"""
        if not self._connected:
            raise ConnectionError("MQTT not connected")
        self._seq += 1
        seq = self._seq
        fut: asyncio.Future = self._loop.create_future()
        # 先注册再发布: 防止响应在注册前到达被 _read_loop 丢弃(竞态)
        self._pending[seq] = fut
        req = {"api": api, "params": params, "seq": seq}
        try:
            await self.async_publish(TOPIC_REQUEST, json.dumps(
                req, separators=(",", ":")
            ).encode(), pid=seq % 65535)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq, None)

    async def _read_loop(self) -> None:
        try:
            while self._connected and self._reader:
                ptype, payload = await _read_packet(self._reader)
                if ptype == PKT_PINGRESP:
                    continue
                if ptype == PKT_PUBLISH:
                    await self._on_publish(payload)
                elif ptype == PKT_PUBACK:
                    continue  # 广播确认(每包 pid 单发, 无队列)
                elif ptype == PKT_DISCONNECT:
                    _LOGGER.warning("MQTT server disconnect")
                    self._connected = False
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as err:
            if self._connected:
                _LOGGER.warning("MQTT read loop ended: %s", err)
            self._connected = False
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("MQTT connection lost"))

    async def _on_publish(self, payload: bytes) -> None:
        topic_len = struct.unpack(">H", payload[:2])[0]
        topic = payload[2:2 + topic_len].decode()
        body = payload[2 + topic_len:]
        # 业务 JSON
        try:
            msg = json.loads(body.decode(errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        # seq 匹配请求响应
        seq = msg.get("seq") or msg.get("id")
        if isinstance(seq, int) and seq in self._pending:
            fut = self._pending.get(seq)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        # 推送(事件/属性更新)
        if self.on_message:
            try:
                await self.on_message(topic, msg)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("MQTT on_message handler failed")

    async def _ping_loop(self) -> None:
        while self._connected:
            await asyncio.sleep(MQTT_KEEPALIVE / 2)
            try:
                if self._writer:
                    self._writer.write(_pkt_ping())
                    await self._writer.drain()
            except (OSError, ConnectionError):
                self._connected = False
                break
