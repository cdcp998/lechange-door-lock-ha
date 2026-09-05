"""Tests for MQTT client / manager (packet build, fallback, event decode)."""

import asyncio
import struct

import pytest

import lechange_door_lock.mqtt_client as mc
import lechange_door_lock.mqtt as mqtt


# ---------------------------------------------------------------- packets
def test_connect_packet_shape():
    pkt = mc._pkt_connect("lcbaseappabc", "Authorization: x-pcs-signature", "{}")
    assert pkt[0] == 0x10
    assert pkt[1] < 128  # 1-byte remaining len
    # 协议头 "MQTT" + level 4(在第 7 字节)
    body = pkt[2:]
    assert body[:6] == b"\x00\x04MQTT"
    assert body[6] == 0x04
    # flags: clean + password + username
    assert body[7] == 0xC2
    # clientId("lcbaseappabc"=12B) + username(30B) + password 顺序
    assert b"\x00\x0clcbaseappabc" in body
    assert b"\x00\x1eAuthorization: x-pcs-signature" in body
    assert body.endswith(b"\x00\x02{}")


def test_subscribe_packet():
    pkt = mc._pkt_subscribe(["iot_response", "iot_request"], pid=7)
    assert pkt[0] == 0x82
    body = pkt[2:]
    assert struct.unpack(">H", body[:2])[0] == 7
    # 每个 topic 前有长度+名字, 后跟 QoS 字节
    assert b"\x00\x0ciot_response\x01" in body
    assert b"\x00\x0biot_request\x01" in body


def test_publish_packet_qos1():
    pkt = mc._pkt_publish("iot_request", b"{}", pid=5)
    assert pkt[0] == (0x30 | 0x02)  # PUBLISH QoS1
    body = pkt[2:]
    # topic len + topic + pid + payload
    topic_len = struct.unpack(">H", body[:2])[0]
    assert body[2:2 + topic_len] == b"iot_request"
    assert struct.unpack(">H", body[2 + topic_len:4 + topic_len])[0] == 5
    assert body[4 + topic_len:] == b"{}"


def test_remaining_len_encoding():
    assert mc._remaining_len(0) == b"\x00"
    assert mc._remaining_len(127) == b"\x7f"
    assert mc._remaining_len(128) == b"\x80\x01"
    assert mc._remaining_len(300) == b"\xac\x02"


def test_build_head_signed():
    """password 头部含 HMAC-SHA256 签名 + 关键字段。"""
    import base64
    import hmac
    import hashlib
    import json

    head = mc._build_head("uuid\\u1", "secret-token", "1205043699", "TERM123")
    h = json.loads(head)
    assert "x-pcs-signature" in h
    assert h["x-pcs-username"] == "uuid\\u1"
    assert h["x-pcs-conn-type"] == "main"
    # 验证签名(与脚本一致)
    canon = (f"x-pcs-client-ua:{h['x-pcs-client-ua']}\n"
             f"x-pcs-date:{h['x-pcs-date']}\n"
             f"x-pcs-nonce:{h['x-pcs-nonce']}\n"
             f"x-pcs-username:uuid\\u1\n")
    expect = base64.b64encode(
        hmac.new(b"secret-token", canon.encode(), hashlib.sha256).digest()
    ).decode()
    assert h["x-pcs-signature"] == expect


# ---------------------------------------------------------------- manager
class _FakeApi:
    """Stub ImouClient: credentials + terminal_id + async_post (cloud fallback)."""

    def __init__(self, creds=None, post_result=None):
        self.terminal_id = "TERM-1234-ABCD"
        self.token = "tok123"
        self._creds = creds or {
            "clientId": "lcbaseapp001",
            "mqttServer": {"sslAddr": "iotmqtt-app-hz.imou.com:8883"},
            "uid": "lc1user123",
        }
        self._post_result = post_result or {"code": 10000, "via_fake": True}
        self.posted = []

    async def async_get_mqtt_credentials(self):
        return self._creds

    async def async_post(self, api, params, **kw):
        self.posted.append((api, params))
        return dict(self._post_result)


async def _noop_event(data):
    pass


@pytest.mark.asyncio
async def test_manager_cloud_fallback_when_disconnected():
    """MQTT 未连接 → async_request 走云兜底(cloud_ctrl), 标记 via=cloud."""
    api = _FakeApi()
    calls = []

    async def cloud_ctrl(api_name, params):
        calls.append(api_name)
        return {"code": 10000}

    mgr = mqtt.MqttManager(
        api, "DEV", "PROD", cloud_ctrl,
        on_event=_noop_event, certs_dir="",
    )
    resp = await mgr.async_request("iot.control.GetProperties", {"x": 1})
    assert resp.get("via") == "cloud"
    assert calls == ["iot.control.GetProperties"]


@pytest.mark.asyncio
async def test_manager_connected_property_true():
    """connected 属性: 未连接时 False; 不应抛异常(惰性)."""
    api = _FakeApi()
    mgr = mqtt.MqttManager(api, "DEV", "PROD", lambda a, p: None,
                           on_event=_noop_event, certs_dir="")
    assert mgr.connected is False


@pytest.mark.asyncio
async def test_manager_reconnect_retries():
    """断线重连逻辑: ensure_connected 失败抛异常, 由循环重试(测试直接调用)."""
    api = _FakeApi()

    async def cloud_ctrl(a, p):
        return {"code": 10000}

    mgr = mqtt.MqttManager(api, "DEV", "PROD", cloud_ctrl,
                           on_event=_noop_event, certs_dir="")
    # 无网络时 ensure_connected 应抛 ConnectionError(非静默)
    with pytest.raises(Exception):
        await mgr.async_ensure_connected()


@pytest.mark.asyncio
async def test_on_event_property_push(monkeypatch):
    """android_iot_property 推送 → on_event 回调收到(解码在 coordinator 层)."""
    api = _FakeApi()
    received = []

    async def on_event(data):
        received.append(data)

    async def cloud_ctrl(a, p):
        return {"code": 10000}

    mgr = mqtt.MqttManager(api, "DEV", "PROD", cloud_ctrl,
                           on_event=on_event, certs_dir="")
    await mgr._on_message("iot_response", {"params": {"data": {"properties": {}}}})
    assert received and received[0]["topic"] == "iot_response"


# ------------------------------------------------- graceful close (DISCONNECT)
class _FakeWriter:
    """记录 write 字节的最小 writer 桩(async_close 语义所需)."""

    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def _client_with_writer(session_up: bool) -> mc.MqttClient:
    client = mc.MqttClient("cid", "u", "p", "host", 8883)
    client._session_up = session_up
    client._writer = _FakeWriter()
    return client


@pytest.mark.asyncio
async def test_close_sends_disconnect_after_connack():
    """会话已建立 → close 先发 DISCONNECT 再关 TCP.

    否则服务端视为异常掉线, 会话滞留至 keepalive 宽限超时, 期间同
    clientId 重连被拒 → "0 bytes read" 退避刷屏(线上 v1.6.2 观测)。
    """
    client = _client_with_writer(session_up=True)
    writer = client._writer
    await client.async_close()
    assert bytes(writer.written) == b"\xe0\x00"  # DISCONNECT 先于 TCP 关闭
    assert writer.closed
    assert client._writer is None and client._session_up is False  # 状态清理


@pytest.mark.asyncio
async def test_close_sends_disconnect_packet_bytes():
    """DISCONNECT 包字节确实写入: 固定 0xE0 0x00(2 字节)."""
    client = _client_with_writer(session_up=True)
    writer = client._writer
    await client.async_close()
    assert bytes(writer.written) == b"\xe0\x00"


@pytest.mark.asyncio
async def test_close_without_session_sends_no_disconnect():
    """连接从未成功(CONNACK 前被服务端断开) → 无会话可释放, 不发 DISCONNECT."""
    client = _client_with_writer(session_up=False)
    writer = client._writer
    await client.async_close()
    assert writer.written == bytearray()  # 只关 TCP, 不写 DISCONNECT


@pytest.mark.asyncio
async def test_read_loop_server_disconnect_clears_session(monkeypatch):
    """服务端主动 DISCONNECT → 会话状态清除, close 不再补发 DISCONNECT."""
    client = _client_with_writer(session_up=True)
    writer = client._writer

    async def fake_read_packet(_reader):
        return (mc.PKT_DISCONNECT, b"")

    monkeypatch.setattr(mc, "_read_packet", fake_read_packet)
    client._connected = True
    client._reader = object()  # 非 None 即可进入循环
    await asyncio.wait_for(client._read_loop(), timeout=2)
    assert client._session_up is False
    assert client.connected is False
    await client.async_close()
    assert writer.written == bytearray()  # 服务端已回收, 无需补发


# ------------------------------------------------- failed connect: no leak
class _LeakProbeClient:
    """模拟 CONNACK 前 EOF: connect 抛异常, TLS 已建立(半开)."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.close_calls = 0
        _LeakProbeClient.instances.append(self)

    async def async_connect(self):
        raise ConnectionError("MQTT CONNACK failed rc=-1")  # 0 bytes read 场景

    async def async_close(self):
        self.close_calls += 1
        self.closed = True


@pytest.mark.asyncio
async def test_ensure_connected_failure_closes_half_open_client(monkeypatch):
    """连接失败路径必须 close 半开客户端, 否则 socket/服务端会话泄漏."""
    monkeypatch.setattr(mqtt, "MqttClient", _LeakProbeClient)
    api = _FakeApi()

    async def cloud_ctrl(a, p):
        return {"code": 10000}

    mgr = mqtt.MqttManager(api, "DEV", "PROD", cloud_ctrl,
                           on_event=_noop_event, certs_dir="")
    with pytest.raises(ConnectionError):
        await mgr.async_ensure_connected()
    assert len(_LeakProbeClient.instances) == 1
    assert _LeakProbeClient.instances[0].close_calls == 1  # ★ 不泄漏
    # 凭据缓存已作废(原有行为保持)
    assert mgr._last_creds == {} and mgr._creds_at == 0.0
    # 失败的客户端不会被记为当前连接
    assert mgr._mqtt is None and mgr.connected is False
