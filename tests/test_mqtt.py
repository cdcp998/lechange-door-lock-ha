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
